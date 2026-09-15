# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle and output checks for the copyable seller integration example."""

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
from mercadopago.config import RequestOptions

from examples import seller_integration
from examples.seller_integration import Cart, CartLine, SellerBackend, Session
from mercadopago_commerce_agents import (
    CheckoutHandoff,
    CheckoutOutcomeUnknown,
    MercadoPagoCheckout,
)

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
        reference_a = await backend.checkout_reference(session, cart_a)
        self.assertEqual(await backend.checkout_key(session, cart_a), key_a)
        self.assertNotEqual(await backend.checkout_key(session, cart_b), key_a)
        self.assertEqual(await backend.checkout_key(session, cart_a), key_a)

        await backend.finish_checkout_attempt(reference_a)

        self.assertNotEqual(await backend.checkout_key(session, cart_a), key_a)
        self.assertNotEqual(
            await backend.checkout_reference(session, cart_a), reference_a
        )

    async def test_elapsed_deadline_blocks_without_rotating_the_attempt(self):
        backend = self._backend()
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        original_key = await backend.checkout_key(session, cart)
        original_reference = await backend.checkout_reference(session, cart)
        _, attempt = backend._attempt(  # pylint: disable=protected-access
            session, cart
        )
        attempt.reconcile_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        backend.mercadopago.checkout_handoff = mock.AsyncMock()

        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await backend.checkout_handoff(session, cart)

        self.assertEqual(captured.exception.idempotency_key, original_key)
        self.assertEqual(captured.exception.external_reference, original_reference)
        self.assertEqual(captured.exception.reason, "reconciliation_required")
        self.assertEqual(await backend.checkout_key(session, cart), original_key)
        self.assertEqual(
            await backend.checkout_reference(session, cart), original_reference
        )
        backend.mercadopago.checkout_handoff.assert_not_awaited()

    async def test_successful_handoff_is_reused_without_rebuilding_the_order(self):
        backend = self._backend()
        expected = [CheckoutHandoff(url="https://www.mercadopago.com.br/checkout")]
        backend.mercadopago.checkout_handoff = mock.AsyncMock(return_value=expected)
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        key = await backend.checkout_key(session, cart)
        reference = await backend.checkout_reference(session, cart)

        first = await backend.checkout_handoff(session, cart)
        second = await backend.checkout_handoff(session, cart)

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        backend.mercadopago.checkout_handoff.assert_awaited_once_with(
            session,
            cart,
            external_reference=reference,
            idempotency_key=key,
        )

    async def test_reconciliation_deadline_starts_before_the_api_call(self):
        backend = self._backend()
        handoff = [CheckoutHandoff(url="https://www.mercadopago.com.br/checkout")]
        backend.mercadopago.checkout_handoff = mock.AsyncMock(return_value=handoff)
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])

        _, attempt = backend._attempt(  # pylint: disable=protected-access
            session, cart
        )
        self.assertIsNone(attempt.reconcile_after)

        await backend.checkout_handoff(session, cart)

        self.assertIsNotNone(attempt.reconcile_after)
        self.assertGreater(attempt.reconcile_after, datetime.now(timezone.utc))

    async def test_unknown_outcome_is_sticky_until_reconciliation(self):
        backend = self._backend()
        session = Session("session-1")
        cart = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        key = await backend.checkout_key(session, cart)
        reference = await backend.checkout_reference(session, cart)
        adapter_error = CheckoutOutcomeUnknown(
            external_reference=reference,
            idempotency_key=key,
            order_id="ORD-unknown",
            reason="transport_failure",
        )
        backend.mercadopago.checkout_handoff = mock.AsyncMock(side_effect=adapter_error)

        with self.assertRaises(CheckoutOutcomeUnknown) as first:
            await backend.checkout_handoff(session, cart)
        with self.assertRaises(CheckoutOutcomeUnknown) as second:
            await backend.checkout_handoff(session, cart)

        self.assertIs(first.exception, adapter_error)
        self.assertEqual(second.exception.idempotency_key, key)
        self.assertEqual(second.exception.external_reference, reference)
        self.assertEqual(second.exception.order_id, "ORD-unknown")
        self.assertEqual(second.exception.reason, "reconciliation_required")
        backend.mercadopago.checkout_handoff.assert_awaited_once_with(
            session,
            cart,
            external_reference=reference,
            idempotency_key=key,
        )

    async def test_webhook_reference_closes_a_after_navigation_to_b(self):
        backend = self._backend()
        handoff = [CheckoutHandoff(url="https://www.mercadopago.com.br/checkout")]
        backend.mercadopago.checkout_handoff = mock.AsyncMock(return_value=handoff)
        session = Session("session-1")
        cart_a = Cart(items=[CartLine("tshirt-m", "49.90", 1)])
        cart_b = Cart(items=[CartLine("mug", "29.90", 1)])

        original_key = await backend.checkout_key(session, cart_a)
        reference_a = await backend.checkout_reference(session, cart_a)
        await backend.checkout_handoff(session, cart_a)
        await backend.checkout_handoff(session, cart_b)

        await backend.finish_checkout_attempt(reference_a)
        replacement_key = await backend.checkout_key(session, cart_a)
        await backend.checkout_handoff(session, cart_a)

        self.assertNotEqual(replacement_key, original_key)
        self.assertEqual(backend.mercadopago.checkout_handoff.await_count, 3)

    async def test_default_create_output_redacts_url_key_and_reference(self):
        backend = mock.MagicMock()
        backend.checkout_key = mock.AsyncMock(return_value="private-operation-key")
        backend.checkout_reference = mock.AsyncMock(return_value="seller-order-private")
        backend.checkout_handoff = mock.AsyncMock(
            return_value=[CheckoutHandoff(url="https://www.mercadopago.com.br/private")]
        )
        output = io.StringIO()

        with (
            mock.patch.dict(
                os.environ, {"MERCADOPAGO_ACCESS_TOKEN": TOKEN}, clear=True
            ),
            mock.patch.object(seller_integration.sys, "argv", ["example", "--create"]),
            mock.patch.object(seller_integration.mercadopago, "SDK"),
            mock.patch.object(seller_integration, "SellerBackend", return_value=backend),
            redirect_stdout(output),
        ):
            await seller_integration.main()

        rendered = output.getvalue()
        self.assertIn("URL and recovery identifiers were not printed", rendered)
        self.assertNotIn("private-operation-key", rendered)
        self.assertNotIn("seller-order-private", rendered)
        self.assertNotIn("mercadopago.com.br/private", rendered)

    async def test_sensitive_output_requires_an_interactive_terminal(self):
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {"MERCADOPAGO_ACCESS_TOKEN": TOKEN}, clear=True
            ),
            mock.patch.object(
                seller_integration.sys,
                "argv",
                ["example", "--create", "--show-sensitive-output"],
            ),
            redirect_stdout(output),
        ):
            with self.assertRaisesRegex(SystemExit, "interactive terminal"):
                await seller_integration.main()


if __name__ == "__main__":
    unittest.main()


class PublishedWrapperTest(unittest.IsolatedAsyncioTestCase):
    """The wrapper printed in README.md and in the module docstring, run for real.

    The strict route — refusing to call the adapter again until the attempt is
    reconciled — is covered by `test_unknown_outcome_is_sticky_until_reconciliation`
    above. This covers the other published route: a host that does retry through the
    adapter and forwards the stored inconclusive state. A snippet that a seller copies
    into a payment path has to be exercised, not just read.
    """

    class _Product:
        def __init__(self, price):
            self.title = "Mug"
            self.price = price
            self.currency = "BRL"
            self.in_stock = True

    class _Catalog:
        def __init__(self, price):
            self.price = price

        async def get_product_details(self, session, product_id):
            return PublishedWrapperTest._Product(self.price)

    class _Attempt:
        """What the snippet's `attempt_store` has to persist."""

        def __init__(self):
            self.external_reference = "seller-order-1"
            self.idempotency_key = "operation-1"
            self.outcome_unknown = False

    class _Backend:
        """Verbatim shape of the published wrapper."""

        def __init__(self, sdk, catalog, attempt):
            self.attempt = attempt
            self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=catalog)

        async def checkout_handoff(self, session, cart):
            attempt = self.attempt
            return await self.mercadopago.checkout_handoff(
                session,
                cart,
                external_reference=attempt.external_reference,
                idempotency_key=attempt.idempotency_key,
                recovering=attempt.outcome_unknown,
            )

    async def test_the_published_wrapper_survives_unknown_then_a_catalog_change(self):
        posts = []

        class _Order:
            def create(self, body, request_options=None):
                posts.append(body)
                raise requests.ConnectionError("response lost")

            def cancel(self, *args):  # pragma: no cover - never reached
                raise AssertionError("nothing to cancel")

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value = _Order()

        catalog = self._Catalog(price="10.00")
        attempt = self._Attempt()
        backend = self._Backend(sdk, catalog, attempt)
        session = Session("session-1")
        cart = Cart(currency="BRL", items=[CartLine("sku-1", "10.00", 1)])

        with self.assertRaises(CheckoutOutcomeUnknown) as first:
            await backend.checkout_handoff(session, cart)
        self.assertEqual(first.exception.reason, "transport_failure")

        # What the snippet requires the store to remember.
        attempt.outcome_unknown = True
        # And the world moves on in between.
        catalog.price = "12.00"
        posts_before = len(posts)

        with self.assertRaises(CheckoutOutcomeUnknown) as second:
            await backend.checkout_handoff(session, cart)

        self.assertEqual(second.exception.reason, "cart_reconfirmation_required")
        self.assertEqual(second.exception.idempotency_key, attempt.idempotency_key)
        self.assertEqual(
            second.exception.external_reference, attempt.external_reference
        )
        self.assertIsNone(second.exception.__context__)
        # Refused locally: nothing asked Mercado Pago whether the Order exists.
        self.assertEqual(len(posts), posts_before)

    async def test_forgetting_the_stored_state_is_what_reopens_the_gap(self):
        """Names the failure mode the snippet's comment warns about."""
        posts = []

        class _Order:
            def create(self, body, request_options=None):
                posts.append(body)
                raise requests.ConnectionError("response lost")

            def cancel(self, *args):  # pragma: no cover - never reached
                raise AssertionError("nothing to cancel")

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value = _Order()

        catalog = self._Catalog(price="10.00")
        attempt = self._Attempt()
        backend = self._Backend(sdk, catalog, attempt)
        session = Session("session-1")
        cart = Cart(currency="BRL", items=[CartLine("sku-1", "10.00", 1)])

        with self.assertRaises(CheckoutOutcomeUnknown):
            await backend.checkout_handoff(session, cart)

        # The store persisted the identifiers but not that the outcome was unknown.
        catalog.price = "12.00"

        handoffs = await backend.checkout_handoff(session, cart)

        # This is the documented consequence, asserted so the guidance cannot drift
        # back to "persist the pair" without "persist the inconclusive state".
        self.assertEqual(handoffs, [])
