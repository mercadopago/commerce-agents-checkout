# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""No-network checks for both explicitly opt-in live validation modes."""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mercadopago.config import RequestOptions

from examples import live_checkout

TOKEN = "test-access-token"
CHECKOUT_URL = "https://www.mercadopago.com.br/checkout/v1/redirect?order_id=ORD-LIVE"


class _FakeOrder:
    """Behave like the official Order resource without leaving the process."""

    def __init__(self):
        self.create_calls = []
        self.cancel_call = None
        self.order = None

    def create(self, body, request_options=None):
        """Replay the same logical Order for a repeated idempotency key."""
        key = request_options.get_headers()["x-idempotency-key"]
        external_reference = body["external_reference"]
        self.create_calls.append((body, key))
        if self.order is None:
            self.order = {
                "id": "ORD-LIVE",
                "checkout_url": CHECKOUT_URL,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "integration_data": body["integration_data"],
                "external_reference": external_reference,
            }
        return {"status": 201, "response": dict(self.order)}

    def cancel(self, order_id, request_options=None):
        """Record cleanup and expose the resulting terminal state to GET."""
        self.cancel_call = (order_id, request_options)
        self.order = dict(self.order, status="canceled")
        return {"status": 200, "response": dict(self.order)}

    def get(self, order_id, request_options=None):
        """Return the current state used by the live script's read-back."""
        del request_options
        if self.order is None or order_id != self.order["id"]:
            return {"status": 404, "response": {}}
        return {"status": 200, "response": dict(self.order)}


class _FakeSDK:
    """Small SDK facade used before the example installs its recording wrapper."""

    def __init__(self):
        self.request_options = RequestOptions(access_token=TOKEN)
        self.resource = _FakeOrder()

    def order(self):
        """Return the stable fake Order resource."""
        return self.resource


class LiveCheckoutTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the script's happy path and its real-cleanup proof shape."""

    async def _run(self, mode, *, show_checkout_url=False):
        sdk = _FakeSDK()
        output = io.StringIO()
        environment = {
            "MERCADOPAGO_TEST_ACCESS_TOKEN": TOKEN,
            "MERCADOPAGO_TEST_CURRENCY": "BRL",
            "MERCADOPAGO_LIVE_TEST_CONFIRM": mode,
        }
        if show_checkout_url:
            environment["MERCADOPAGO_LIVE_SHOW_CHECKOUT_URL"] = "1"
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(live_checkout.mercadopago, "SDK", return_value=sdk),
            mock.patch.object(output, "isatty", return_value=show_checkout_url),
            redirect_stdout(output),
        ):
            await live_checkout.main()
        return sdk.resource, output.getvalue()

    async def test_create_mode_proves_the_idempotent_replay(self):
        resource, output = await self._run("create-order")

        self.assertEqual(len(resource.create_calls), 2)
        first_body, first_key = resource.create_calls[0]
        retried_body, retried_key = resource.create_calls[1]
        external_reference = first_body["external_reference"]
        self.assertEqual(retried_body, first_body)
        self.assertEqual(retried_key, first_key)
        self.assertEqual(retried_body["external_reference"], external_reference)
        self.assertEqual(resource.order["external_reference"], external_reference)
        self.assertIn("Retry verified", output)
        self.assertIn("URL verified and withheld", output)
        self.assertNotIn(CHECKOUT_URL, output)
        self.assertNotIn("ORD-LIVE", output)
        self.assertNotIn(first_key, output)
        self.assertNotIn(external_reference, output)

    async def test_create_mode_prints_sensitive_values_only_after_interactive_opt_in(self):
        resource, output = await self._run("create-order", show_checkout_url=True)
        body, key = resource.create_calls[0]

        self.assertIn(CHECKOUT_URL, output)
        self.assertIn("ORD-LIVE", output)
        self.assertNotIn(key, output)
        self.assertNotIn(body["external_reference"], output)

    async def test_cancellation_mode_proves_cleanup_without_printing_identifiers(self):
        resource, output = await self._run("verify-cancellation")

        self.assertEqual(len(resource.create_calls), 1)
        order_id, options = resource.cancel_call
        create_key = resource.create_calls[0][1]
        external_reference = resource.create_calls[0][0]["external_reference"]
        cancel_key = options.get_headers()["x-idempotency-key"]
        self.assertEqual(order_id, "ORD-LIVE")
        self.assertNotEqual(cancel_key, create_key)
        self.assertEqual(resource.order["external_reference"], external_reference)
        self.assertEqual(resource.order["status"], "canceled")
        self.assertIn("Cleanup verified", output)
        self.assertNotIn(CHECKOUT_URL, output)
        self.assertNotIn("ORD-LIVE", output)
        self.assertNotIn(create_key, output)
        self.assertNotIn(external_reference, output)


if __name__ == "__main__":
    unittest.main()
