# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Create one test Checkout Pro order and print its hosted checkout URL.

This is intentionally opt-in because it calls the real Mercado Pago API and creates a
payable test order. It never prints the access token. See ``docs/testing.md``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

import mercadopago

from mercadopago_commerce_agents import MercadoPagoCheckout


@dataclass(frozen=True)
class _Product:
    title: str = "Commerce Agents checkout validation"
    price: str = "10.00"
    in_stock: bool = True
    currency: str = "BRL"


@dataclass(frozen=True)
class _Line:
    product_id: str = "mpca-live-validation"
    title: str = "Commerce Agents checkout validation"
    price: str = "10.00"
    quantity: int = 1


@dataclass(frozen=True)
class _Cart:
    items: tuple[_Line, ...] = (_Line(),)
    currency: str = "BRL"


@dataclass(frozen=True)
class _Session:
    session_id: str


class _Catalog:  # pylint: disable=too-few-public-methods
    """The trusted catalog this validation prices against."""

    def __init__(self, currency: str):
        self._currency = currency

    async def get_product_details(self, _session: _Session, product_id: str):
        """Return the one trusted product used by this validation."""
        return _Product(currency=self._currency) if product_id == _Line.product_id else None


class _RecordingOrder:  # pylint: disable=too-few-public-methods
    """Delegate to the real SDK while retaining only the created order response."""

    def __init__(self, resource):
        self._resource = resource
        self.created = None

    def create(self, body, request_options=None):
        """Create the real order and retain its response for the read-back check."""
        result = self._resource.create(body, request_options)
        response = result.get("response")
        if isinstance(response, dict):
            self.created = response
        return result

    def cancel(self, order_id, request_options=None):
        """Delegate to the real resource. Without this the adapter's cleanup after a
        refused order silently fails and leaves that order payable."""
        return self._resource.cancel(order_id, request_options)

    def get(self, order_id, request_options=None):
        """Delegate the read-back used by this validation."""
        return self._resource.get(order_id, request_options)


class _RecordingSDK:  # pylint: disable=too-few-public-methods
    """Expose the SDK surface used by the adapter and capture its Order resource."""

    def __init__(self, sdk):
        self.request_options = sdk.request_options
        self._order = _RecordingOrder(sdk.order())

    def order(self):
        """Return the recording wrapper around the official Order resource."""
        return self._order


async def main() -> None:
    """Create and read back one explicitly authorized test order."""
    token = os.environ.get("MERCADOPAGO_TEST_ACCESS_TOKEN")
    currency = os.environ.get("MERCADOPAGO_TEST_CURRENCY", "BRL")
    confirmation = os.environ.get("MERCADOPAGO_LIVE_TEST_CONFIRM")
    if not token:
        raise SystemExit("MERCADOPAGO_TEST_ACCESS_TOKEN is required")
    if confirmation != "create-order":
        raise SystemExit(
            "Set MERCADOPAGO_LIVE_TEST_CONFIRM=create-order to create one test order"
        )

    sdk = mercadopago.SDK(token)
    recording_sdk = _RecordingSDK(sdk)
    idempotency_key = str(uuid4())

    checkout = MercadoPagoCheckout(sdk=recording_sdk, catalog=_Catalog(currency))
    handoffs = await checkout.checkout_handoff(
        _Session(session_id=f"live-session-{uuid4()}"),
        _Cart(currency=currency),
        idempotency_key=idempotency_key,
    )
    if len(handoffs) != 1:
        raise SystemExit("Mercado Pago did not return a usable Checkout Pro URL")

    checkout_url = handoffs[0].url
    created = recording_sdk.order().created
    order_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(order_id, str) or not order_id:
        raise SystemExit("Created order response did not contain an order id")
    if created.get("checkout_url") != checkout_url:
        raise SystemExit("Created order URL did not match the checkout handoff")

    result = await asyncio.to_thread(sdk.order().get, order_id)
    response = result.get("response")
    if result.get("status") != 200 or not isinstance(response, dict):
        raise SystemExit("Created order could not be read back from Mercado Pago")
    if response.get("id") != order_id:
        raise SystemExit("Read-back order id did not match the checkout URL")

    print(f"Order id: {order_id}")
    print(f"Order status: {response.get('status')}")
    print(f"Integration data: {response.get('integration_data')}")
    print(f"Checkout Pro URL: {checkout_url}")
    print("Retrying with the same idempotency key returns this same order.")
    print("The test order expires after P1D; do not use this script with production data.")


if __name__ == "__main__":
    asyncio.run(main())
