# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""A minimal, complete seller integration — the shape of a real one, in one file.

Read it top to bottom: a catalog, a backend, the wiring, and what to do with the
webhook afterwards. Nothing here is scaffolding for the example's sake.

    python examples/seller_integration.py
    python examples/seller_integration.py --create
    python examples/seller_integration.py --create --show-sensitive-output

``--create`` needs ``MERCADOPAGO_ACCESS_TOKEN`` and calls the live API, so point it at a
test seller. It creates one order for the cart below but redacts its checkout URL and
identifiers by default. The explicit output flag works only in an interactive terminal.
The order expires after 24 hours and nothing is charged until someone pays it.

The cart and session types are defined here on purpose. In a real deployment they are
commerce-agents' own ``Cart`` and ``ShoppingSessionContext``, and this package never
imports them — it reads the same attributes off whatever the host passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import mercadopago

from mercadopago_commerce_agents import (
    CheckoutHandoff,
    MercadoPagoCheckout,
)

_ATTEMPT_RETENTION = timedelta(hours=25)
"""Outlive the Order's P1D expiry by a safety margin before rotating its key."""

# --- what the seller already has -------------------------------------------------

@dataclass(frozen=True)
class Product:
    """A catalog record. The adapter reads exactly these four attributes."""

    title: str
    price: str
    currency: str
    in_stock: bool


class SellerCatalog:  # pylint: disable=too-few-public-methods
    """The seller's own catalog. This one method is all the adapter requires.

    Return a record for an id you sell, or None. This is what makes the charge
    trustworthy: the cart was assembled by a model's tool calls, so its prices are a
    claim, and every line is re-read from here before an order is created.
    """

    _ROWS = {
        "tshirt-m": Product("ACME T-shirt, M", "49.90", "BRL", True),
        "mug": Product("ACME mug", "29.90", "BRL", True),
        "poster": Product("ACME poster", "19.90", "BRL", False),  # out of stock
    }

    async def get_product_details(self, session: object, product_id: str) -> Product | None:
        """Look one product up. Real implementations hit a database or a service."""
        del session  # a real catalog scopes by the shopper; this one does not
        return self._ROWS.get(product_id)


# --- what commerce-agents would hand you ------------------------------------------

@dataclass
class CartLine:
    """One line the shopping agent added."""

    product_id: str
    price: str
    quantity: int


@dataclass
class Cart:
    """The confirmed cart. `currency` must match the catalog records."""

    items: list[CartLine] = field(default_factory=list)
    currency: str = "BRL"


@dataclass(frozen=True)
class Session:
    """The shopper's session. The adapter never reads its id into the payment."""

    session_id: str


@dataclass
class _CheckoutAttempt:
    """One active purchase, retained until the host observes a terminal Order state."""

    key: str
    external_reference: str
    expires_at: datetime
    handoffs: tuple[CheckoutHandoff, ...] | None = None


# --- the integration itself: two lines ---------------------------------------------

class SellerBackend:
    """In a real deployment this subclasses commerce-agents' ``StorefrontBackend`` and
    already implements ``get_product_details``; wiring the checkout adds one method.

    ``checkout_handoff`` takes exactly the two arguments commerce-agents calls it with —
    `enrichment.py` does ``await backend.checkout_handoff(context.session, cart)`` — so
    the idempotency key has to be obtained *here*, not passed in from outside. Holding it
    yourself is also what makes reconciliation possible: a key the adapter generates
    internally is never handed back.
    """

    def __init__(self, sdk: mercadopago.SDK) -> None:
        self.catalog = SellerCatalog()
        self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self.catalog)
        # A dict is enough to read. A real deployment needs a durable checkout_attempt
        # table keyed by seller/session plus snapshot fingerprint, because a retry may
        # cross a process restart and A -> B -> A must still find A's original attempt.
        self._attempts: dict[tuple[str, str], _CheckoutAttempt] = {}
        # Webhooks carry the Mercado Pago Order ID and external_reference, not the
        # shopper's current cart. Keep the reverse lookup needed to close the exact
        # attempt even after the shopper navigates A -> B.
        self._attempt_ids_by_reference: dict[str, tuple[str, str]] = {}

    async def get_product_details(self, session: object, product_id: str) -> Product | None:
        """Delegate to the catalog, as a StorefrontBackend already does."""
        return await self.catalog.get_product_details(session, product_id)

    async def checkout_handoff(self, session: Session, cart: Cart):
        """Exactly the signature commerce-agents calls."""
        attempt_id, attempt = self._attempt(session, cart)
        if attempt.handoffs is not None:
            # A host retry after the adapter returned must not rebuild a body from a
            # catalog that may have changed. Return the already-issued checkout instead.
            return list(attempt.handoffs)

        handoffs = await self.mercadopago.checkout_handoff(
            session,
            cart,
            idempotency_key=attempt.key,
            external_reference=attempt.external_reference,
        )
        if handoffs:
            attempt.handoffs = tuple(handoffs)
        else:
            # [] is now definitive: no Order exists, or cleanup was confirmed. The next
            # explicit checkout can start a new operation instead of inheriting this key.
            self._discard_attempt(attempt_id)
        return handoffs

    async def checkout_key(self, session: Session, cart: Cart) -> str:
        """Return the key for this cart's active checkout attempt.

        The record remains active across A -> B -> A and process restarts. A terminal
        webhook must call ``finish_checkout_attempt``; only then may an intentional new
        purchase of the same cart receive a new key.
        """
        return self._attempt(session, cart)[1].key

    async def checkout_reference(self, session: Session, cart: Cart) -> str:
        """Return the seller Order reference stored with this active attempt."""
        return self._attempt(session, cart)[1].external_reference

    def _attempt(
        self, session: Session, cart: Cart
    ) -> tuple[tuple[str, str], _CheckoutAttempt]:
        """Get or durably create the active operation before calling Mercado Pago."""
        attempt_id = (session.session_id, self._fingerprint(cart))
        attempt = self._attempts.get(attempt_id)
        now = datetime.now(timezone.utc)
        if attempt is None or attempt.expires_at <= now:
            # Outlive the Order's P1D window by a safety margin: rotating before the
            # hosted checkout expires could expose two payable Orders. This is only a
            # missed-webhook backstop; verified terminal state should close it earlier.
            self._discard_attempt(attempt_id)
            attempt = _CheckoutAttempt(
                key=f"order-{uuid4()}",
                external_reference=f"seller-order-{uuid4()}",
                expires_at=now + _ATTEMPT_RETENTION,
            )
            self._attempts[attempt_id] = attempt
            self._attempt_ids_by_reference[attempt.external_reference] = attempt_id
        return attempt_id, attempt

    def _discard_attempt(self, attempt_id: tuple[str, str]) -> None:
        """Remove both indexes for one active attempt."""
        attempt = self._attempts.pop(attempt_id, None)
        if attempt is not None:
            self._attempt_ids_by_reference.pop(attempt.external_reference, None)

    @staticmethod
    def _fingerprint(cart: Cart) -> str:
        """Canonical confirmed-cart identity; never an idempotency key by itself."""
        fingerprint = json.dumps(
            {
                "currency": cart.currency,
                "items": sorted(
                    (line.product_id, line.quantity, line.price) for line in cart.items
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return fingerprint

    async def finish_checkout_attempt(self, external_reference: str) -> None:
        """Close the attempt after a verified paid/canceled/expired Order webhook.

        A later intentional purchase of an identical cart then receives a fresh key.
        Real hosts update the durable attempt row in the same transaction as their local
        order state instead of deleting history.
        """
        attempt_id = self._attempt_ids_by_reference.get(external_reference)
        if attempt_id is not None:
            self._discard_attempt(attempt_id)


# --- afterwards: the part this package deliberately leaves to you ------------------

async def compare_order_with_your_record(
    order_id: str, expected_reference: str, expected_amount: str, expected_currency: str,
    sdk: mercadopago.SDK,
) -> dict | None:
    """**Incomplete on purpose — one step of a webhook handler, not the handler.**

    This does the single part that belongs to this package's contract: fetch the order
    and check it against what you stored. It returns the order, or None when it does not
    match. It deliberately does not return a boolean, because a boolean here reads like
    "safe to fulfil" and this is not enough to authorise fulfilment.

    You must implement, around it:

    1. **Validate the `x-signature` header before calling this.** An unverified
       notification is attacker-controlled input. See the Webhooks guide linked in the
       README; the official SDK exposes ``mercadopago.webhook.WebhookSignatureValidator``.
    2. **Deduplicate by the notification id.** Mercado Pago retries, and a handler that
       is not idempotent will fulfil twice.
    3. **Decide which order status your flow treats as paid**, and apply a valid local
       state transition from whatever state you are in — never a blind overwrite.
    4. **Close the active checkout attempt after that terminal transition** by calling
       ``finish_checkout_attempt(order["external_reference"])`` in the same transaction.
       That is what lets a later purchase of an identical cart receive a new key.

    Never treat a browser redirect or a query parameter as payment evidence.
    """
    result = await asyncio.to_thread(sdk.order().get, order_id)
    if result.get("status") != 200:
        return None
    order = result.get("response") or {}
    matches = (
        order.get("external_reference") == expected_reference
        and order.get("total_amount") == expected_amount
        and order.get("currency") == expected_currency
    )
    return order if matches else None


def _show_sensitive_output() -> bool:
    """Require an explicit flag and an interactive terminal before printing secrets."""
    requested = "--show-sensitive-output" in sys.argv
    if requested and not sys.stdout.isatty():
        raise SystemExit("--show-sensitive-output requires an interactive terminal")
    return requested


async def main() -> None:
    """Create one order for a two-line cart and print what the host would keep."""
    cart = Cart(items=[CartLine("tshirt-m", "49.90", 1), CartLine("mug", "29.90", 2)])
    session = Session(session_id="session-from-your-host")

    if "--create" not in sys.argv:
        print("Cart:", [(line.product_id, line.quantity) for line in cart.items])
        print("\nNo order created. Re-run with --create and MERCADOPAGO_ACCESS_TOKEN set.")
        return

    show_sensitive_output = _show_sensitive_output()

    token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN")
    if not token:
        raise SystemExit("MERCADOPAGO_ACCESS_TOKEN is required with --create")

    backend = SellerBackend(mercadopago.SDK(token))

    # commerce-agents calls the two-argument form; the key lives inside the backend.
    # Reading it here is what a host does to persist it alongside its own order record.
    key = await backend.checkout_key(session, cart)
    external_reference = await backend.checkout_reference(session, cart)
    print("Cart:", [(line.product_id, line.quantity) for line in cart.items])
    if show_sensitive_output:
        print("Idempotency key to store:", key)
        print("External reference to match a webhook against:", external_reference)

    handoffs = await backend.checkout_handoff(session, cart)

    if not handoffs:
        print("\nNo handoff. The adapter refused; your own checkout takes over.")
        print("Enable the `mercadopago_commerce_agents.checkout` logger to see why.")
        return

    if show_sensitive_output:
        print("\nSend the shopper here:", handoffs[0].url)
    else:
        print("\nCheckout created. URL and recovery identifiers were not printed.")
        print("Use --show-sensitive-output in an interactive terminal to display them.")


if __name__ == "__main__":
    asyncio.run(main())
