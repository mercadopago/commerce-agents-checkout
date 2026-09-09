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
    already implements ``get_product_details``; wiring the checkout adds one method."""

    def __init__(self, sdk: mercadopago.SDK) -> None:
        self.catalog = SellerCatalog()
        self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self.catalog)

    async def get_product_details(self, session: object, product_id: str) -> Product | None:
        """Delegate to the catalog, as a StorefrontBackend already does."""
        return await self.catalog.get_product_details(session, product_id)

    async def checkout_handoff(self, session: Session, cart: Cart, *, idempotency_key: str):
        """What commerce-agents calls after the model's `checkout` tool call.

        Pass your own idempotency key: one per intentional purchase, reused only when
        retrying that same purchase. Keep it — it is what ties the webhook back.
        """
        return await self.mercadopago.checkout_handoff(
            session, cart, idempotency_key=idempotency_key
        )


# --- afterwards: the part this package deliberately leaves to you ------------------

async def handle_order_webhook(order_id: str, stored_key: str, sdk: mercadopago.SDK) -> bool:
    """Sketch of the reconciliation the host owns. Not called by the example.

    A handoff means an order exists, never that it was paid. Validate the webhook's
    `x-signature` first (see the README), then fetch the order and compare it with what
    you stored. `external_reference_for` gives you the reference without this package
    storing anything for you.
    """
    result = await asyncio.to_thread(sdk.order().get, order_id)
    order = result.get("response") or {}
    return (
        result.get("status") == 200
        and order.get("external_reference") == external_reference_for(stored_key)
        and order.get("status") == "processed"  # whatever your flow treats as paid
    )


async def main() -> None:
    """Create one order for a two-line cart and print what the host would keep."""
    cart = Cart(items=[CartLine("tshirt-m", "49.90", 1), CartLine("mug", "29.90", 2)])
    # One key per intentional purchase. Persist it before calling: it is the only thing
    # that ties this checkout to the webhook that arrives later.
    idempotency_key = f"order-{uuid4()}"

    print("Cart:", [(line.product_id, line.quantity) for line in cart.items])
    print("Idempotency key:", idempotency_key)
    print("External reference to store:", external_reference_for(idempotency_key))

    if "--create" not in sys.argv:
        print("\nNo order created. Re-run with --create and MERCADOPAGO_ACCESS_TOKEN set.")
        return

    token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN")
    if not token:
        raise SystemExit("MERCADOPAGO_ACCESS_TOKEN is required with --create")

    backend = SellerBackend(mercadopago.SDK(token))
    handoffs = await backend.checkout_handoff(
        Session(session_id="session-from-your-host"), cart, idempotency_key=idempotency_key
    )

    if not handoffs:
        print("\nNo handoff. The adapter refused; your own checkout takes over.")
        print("Enable the `mercadopago_commerce_agents.checkout` logger to see why.")
        return

    print("\nSend the shopper here:", handoffs[0].url)


if __name__ == "__main__":
    asyncio.run(main())
