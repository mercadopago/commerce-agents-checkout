# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle checks for the copyable seller integration example."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mercadopago.config import RequestOptions

from examples.seller_integration import Cart, CartLine, SellerBackend, Session
from mercadopago_commerce_agents import CheckoutHandoff

TOKEN = "test-access-token"  # no request leaves the process


class SellerIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def _backend(self) -> SellerBackend:
        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        return SellerBackend(sdk)

    async def test_attempt_survives_a_to_b_to_a_and_closes_explicitly(self):
        backend = self._backend()
        session = Session("session-1")
        cart_a = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        cart_b = Cart(items=[CartLine("mug", "29.90", 1)])

        key_a = await backend.checkout_key(session, cart_a)
        self.assertEqual(await backend.checkout_key(session, cart_a), key_a)
        self.assertNotEqual(await backend.checkout_key(session, cart_b), key_a)
        self.assertEqual(await backend.checkout_key(session, cart_a), key_a)

        await backend.finish_checkout_attempt(session, cart_a)

        self.assertNotEqual(await backend.checkout_key(session, cart_a), key_a)

    async def test_expired_attempt_does_not_reuse_the_key_forever(self):
        backend = self._backend()
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        original = await backend.checkout_key(session, cart)
        attempt_id, attempt = backend._attempt(session, cart)  # pylint: disable=protected-access
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        replacement = await backend.checkout_key(session, cart)

        self.assertNotEqual(replacement, original)
        self.assertEqual(
            backend._attempts[attempt_id].key,  # pylint: disable=protected-access
            replacement,
        )

    async def test_successful_handoff_is_reused_without_rebuilding_the_order(self):
        backend = self._backend()
        expected = [CheckoutHandoff(url="https://www.mercadopago.com.br/checkout")]
        backend.mercadopago.checkout_handoff = mock.AsyncMock(return_value=expected)
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])

        first = await backend.checkout_handoff(session, cart)
        second = await backend.checkout_handoff(session, cart)

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        backend.mercadopago.checkout_handoff.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
