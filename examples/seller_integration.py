# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""A minimal, complete seller integration — the shape of a real one, in one file.

Read it top to bottom: a catalog, a backend, the wiring, and what to do with the
webhook afterwards. Nothing here is scaffolding for the example's sake.

    python examples/seller_integration.py            # print the wiring and exit
    python examples/seller_integration.py --create   # create one real order

``--create`` needs ``MERCADOPAGO_ACCESS_TOKEN`` and calls the live API, so point it at a
test seller. It creates one order for the cart below and prints its checkout URL; the
order expires after 24 hours and nothing is charged until someone pays it.

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
from uuid import uuid4

import mercadopago

from mercadopago_commerce_agents import MercadoPagoCheckout, external_reference_for

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
        # A dict is enough to read; a real deployment needs a durable table, because a
        # retry may cross a process restart and must find the same key.
        self._attempts: dict[str, tuple[str, str]] = {}

    async def get_product_details(self, session: object, product_id: str) -> Product | None:
        """Delegate to the catalog, as a StorefrontBackend already does."""
        return await self.catalog.get_product_details(session, product_id)

    async def checkout_handoff(self, session: Session, cart: Cart):
        """Exactly the signature commerce-agents calls."""
        key = await self.checkout_key(session, cart)
        return await self.mercadopago.checkout_handoff(
            session, cart, idempotency_key=key
        )

    async def checkout_key(self, session: Session, cart: Cart) -> str:
        """One key per confirmed cart, reused only while that cart is unchanged.

        Retrying the same confirmed cart must reuse it, or Mercado Pago creates a second
        payable order. Any change to the cart is a different purchase and needs a new one.
        Store it before returning: `external_reference_for(key)` is what a webhook will
        match against later.
        """
        fingerprint = json.dumps(
            sorted((line.product_id, line.quantity, line.price) for line in cart.items),
            separators=(",", ":"),
        )
        stored = self._attempts.get(session.session_id)
        if stored is not None and stored[0] == fingerprint:
            return stored[1]
        key = f"order-{uuid4()}"
        self._attempts[session.session_id] = (fingerprint, key)
        return key


# --- afterwards: the part this package deliberately leaves to you ------------------

async def compare_order_with_your_record(
    order_id: str, stored_key: str, expected_amount: str, expected_currency: str,
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

    Never treat a browser redirect or a query parameter as payment evidence.
    """
    result = await asyncio.to_thread(sdk.order().get, order_id)
    if result.get("status") != 200:
        return None
    order = result.get("response") or {}
    matches = (
        order.get("external_reference") == external_reference_for(stored_key)
        and order.get("total_amount") == expected_amount
        and order.get("currency") == expected_currency
    )
    return order if matches else None


async def main() -> None:
    """Create one order for a two-line cart and print what the host would keep."""
    cart = Cart(items=[CartLine("tshirt-m", "49.90", 1), CartLine("mug", "29.90", 2)])
    session = Session(session_id="session-from-your-host")

    if "--create" not in sys.argv:
        print("Cart:", [(line.product_id, line.quantity) for line in cart.items])
        print("\nNo order created. Re-run with --create and MERCADOPAGO_ACCESS_TOKEN set.")
        return

    token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN")
    if not token:
        raise SystemExit("MERCADOPAGO_ACCESS_TOKEN is required with --create")

    backend = SellerBackend(mercadopago.SDK(token))

    # commerce-agents calls the two-argument form; the key lives inside the backend.
    # Reading it here is what a host does to persist it alongside its own order record.
    key = await backend.checkout_key(session, cart)
    print("Cart:", [(line.product_id, line.quantity) for line in cart.items])
    print("Idempotency key to store:", key)
    print("External reference to match a webhook against:", external_reference_for(key))

    handoffs = await backend.checkout_handoff(session, cart)

    if not handoffs:
        print("\nNo handoff. The adapter refused; your own checkout takes over.")
        print("Enable the `mercadopago_commerce_agents.checkout` logger to see why.")
        return

    print("\nSend the shopper here:", handoffs[0].url)


if __name__ == "__main__":
    asyncio.run(main())
