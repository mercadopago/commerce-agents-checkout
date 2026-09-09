# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Unit tests. No network: the SDK's ``order().create`` is replaced and the calls
it would have made are asserted against instead.

The commerce-agents types are faked here rather than imported, which is the point of
``types.py`` — the package under test must work with nothing from Anthropic's
repository installed. ``tests/test_contract.py`` covers the real thing.
"""

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import UUID

import requests
from mercadopago.config import RequestOptions

from mercadopago_commerce_agents import CheckoutHandoff, MercadoPagoCheckout
from mercadopago_commerce_agents import checkout as checkout_module

TOKEN = "test-access-token"  # not a credential: no request leaves the process
CHECKOUT_URL = "https://www.mercadopago.com.br/checkout/v1/redirect?order_id=ORD-1"


class _Line:
    def __init__(self, product_id, title="Cart title", price=100.0, quantity=1):
        self.product_id = product_id
        self.title = title
        self.price = price
        self.quantity = quantity


class _Cart:
    def __init__(self, *items, currency="BRL"):
        self.items = list(items)
        self.currency = currency


class _Session:
    def __init__(self, session_id="session-from-a-raw-header"):
        self.session_id = session_id


class _Record:
    def __init__(
        self,
        title="Catalog title",
        price=100.0,
        in_stock=True,
        currency="BRL",
    ):
        self.title = title
        self.price = price
        self.in_stock = in_stock
        self.currency = currency


class _Catalog:
    """Stands in for the seller's StorefrontBackend."""

    def __init__(self, **records):
        self.records = records
        self.looked_up = []

    async def get_product_details(self, session, product_id):
        self.looked_up.append(product_id)
        return self.records.get(product_id)


class _Recorder:
    """Captures what the SDK was asked to create."""

    def __init__(self, response=None, status=201):
        self.response = response
        self.status = status
        self.calls = []

    def create(self, body, request_options=None):
        self.calls.append((body, request_options))
        response = self.response
        if callable(response):
            response = response(body)
        if response is None:
            response = {
                "id": "ORD-1",
                "checkout_url": CHECKOUT_URL,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }
        return {"status": self.status, "response": response}

    @property
    def body(self):
        return self.calls[-1][0]

    @property
    def headers(self):
        return self.calls[-1][1].get_headers()


class CheckoutHandoffTest(unittest.IsolatedAsyncioTestCase):
    def _checkout(self, recorder, **kwargs):
        request_options = kwargs.pop(
            "request_options", RequestOptions(access_token=TOKEN)
        )
        sdk = kwargs.pop("sdk", mock.MagicMock())
        sdk.request_options = request_options
        sdk.order.return_value.create = recorder.create

        return MercadoPagoCheckout(sdk=sdk, **kwargs)

    # -- the Critical finding: the charge must not come from the cart --------------

    async def test_price_change_requires_fresh_confirmation(self):
        """The catalog remains authoritative, but a changed amount needs consent."""
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=100.0))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", price=0.01))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_uses_the_catalog_title_not_the_model_authored_one(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(title="Catalog title"))
        checkout = self._checkout(recorder, catalog=catalog)

        await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", title="<model authored>"))
        )

        self.assertEqual(recorder.body["items"][0]["title"], "Catalog title")

    async def test_invalid_cart_price_requires_fresh_confirmation(self):
        recorder = _Recorder()
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record(price="100.00"))
        )

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", price="not-a-number"))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    def test_requires_a_catalog_at_construction(self):
        """There is no state in which the adapter can fall back to cart prices."""
        with self.assertRaises(TypeError):
            self._checkout(_Recorder())

    def test_public_surface_stays_minimal(self):
        """The reviewed contract: two constructor arguments, one per-call option."""
        self.assertEqual(
            list(inspect.signature(MercadoPagoCheckout).parameters), ["sdk", "catalog"]
        )
        self.assertEqual(
            list(inspect.signature(MercadoPagoCheckout.checkout_handoff).parameters),
            ["self", "session", "cart", "idempotency_key"],
        )
        # Names alone would stay green if the `*` markers were dropped, which would
        # change the contract the README documents.
        constructor = inspect.signature(MercadoPagoCheckout).parameters
        for name in ("sdk", "catalog"):
            self.assertEqual(constructor[name].kind, inspect.Parameter.KEYWORD_ONLY)
        handoff = inspect.signature(MercadoPagoCheckout.checkout_handoff).parameters
        for name in ("session", "cart"):
            self.assertEqual(handoff[name].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertEqual(handoff["idempotency_key"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(handoff["idempotency_key"].default)

    async def test_refuses_unknown_product(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog())

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("ghost")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_refuses_out_of_stock_product(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(in_stock=False))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_stock_must_be_explicitly_true(self):
        for record in (
            SimpleNamespace(title="Catalog title", price=100.0, currency="BRL"),
            _Record(in_stock="false"),
            _Record(in_stock=1),
            _Record(in_stock=None),
        ):
            with self.subTest(record=record):
                recorder = _Recorder()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=record)
                )

                handoffs = await checkout.checkout_handoff(
                    _Session(), _Cart(_Line("sku1"))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_prices_every_line_from_the_catalog(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=10.0), sku2=_Record(price=20.5))
        checkout = self._checkout(recorder, catalog=catalog)

        await checkout.checkout_handoff(
            _Session(),
            _Cart(_Line("sku1", price=10.0, quantity=2), _Line("sku2", price=20.5)),
        )

        prices = [item["unit_price"] for item in recorder.body["items"]]
        self.assertEqual(prices, ["10.00", "20.50"])
        self.assertEqual(recorder.body["items"][0]["quantity"], 2)
        # The order total still covers the quantity; a per-item total_amount is not part
        # of the Orders item schema and is rejected by the API.
        self.assertNotIn("total_amount", recorder.body["items"][0])
        self.assertEqual(recorder.body["total_amount"], "40.50")
        self.assertEqual(catalog.looked_up, ["sku1", "sku2"])

    async def test_validates_currency_but_leaves_it_to_mercado_pago(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertNotIn("currency_id", recorder.body["items"][0])

    async def test_derives_the_currency_from_the_catalog(self):
        """There is no `currency` argument: the trusted catalog is the only source."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "checkout_url": CHECKOUT_URL,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "ARS",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }
        )
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record(currency="ARS"))
        )

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1"), currency="ARS")
        )

        self.assertEqual(len(handoffs), 1)
        # The derived currency is what the response is checked against, and it is still
        # never sent: Mercado Pago resolves it from the seller account.
        self.assertNotIn("currency", recorder.body)
        self.assertNotIn("currency_id", recorder.body["items"][0])

    async def test_refuses_a_catalog_that_mixes_currencies(self):
        recorder = _Recorder()
        checkout = self._checkout(
            recorder,
            catalog=_Catalog(sku1=_Record(currency="BRL"), sku2=_Record(currency="ARS")),
        )

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1"), _Line("sku2"))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_rejects_cart_or_catalog_currency_mismatch(self):
        cases = (
            (_Cart(_Line("sku1"), currency="USD"), _Record()),
            (_Cart(_Line("sku1")), _Record(currency="USD")),
            (_Cart(_Line("sku1")), SimpleNamespace(title="Title", price=100.0, in_stock=True)),
        )
        for cart, record in cases:
            with self.subTest(cart=cart, record=record):
                recorder = _Recorder()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=record)
                )

                handoffs = await checkout.checkout_handoff(_Session(), cart)

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    # -- the High finding: the caller's session id must stay out of the payment ----

    async def test_external_reference_is_not_the_session_id(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))
        session = _Session("session-from-a-raw-header")

        await checkout.checkout_handoff(session, _Cart(_Line("sku1")))

        reference = recorder.body["external_reference"]
        self.assertNotEqual(reference, session.session_id)
        self.assertNotIn(session.session_id, reference)
        self.assertTrue(reference.startswith("mpca-"))
        UUID(reference.removeprefix("mpca-"))

    # -- the remaining hardening --------------------------------------------------

    # -- the cart is the caller's object and stays mutable while we await ----------

    async def test_freezes_the_cart_before_awaiting_the_catalog(self):
        """A catalog that mutates the cart must not change what is charged."""
        cart = _Cart(_Line("sku1", quantity=1))

        class _Mutating:
            def __init__(self):
                self.calls = 0

            async def get_product_details(self, _session, _product_id):
                self.calls += 1
                if self.calls == 1:
                    cart.items.append(_Line("sku2"))
                    cart.items[0].quantity = 10
                    cart.currency = "USD"
                return _Record()

        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Mutating())

        await checkout.checkout_handoff(_Session(), cart)

        self.assertEqual(len(recorder.body["items"]), 1)
        self.assertEqual(recorder.body["items"][0]["quantity"], 1)
        self.assertEqual(recorder.body["total_amount"], "100.00")

    async def test_a_catalog_that_grows_the_cart_cannot_loop_forever(self):
        """Iterating the live list would never reach the size cap."""
        cart = _Cart(_Line("sku1"))

        class _Growing:
            async def get_product_details(self, _session, _product_id):
                cart.items.append(_Line(f"sku{len(cart.items) + 1}"))
                return _Record()

        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Growing())

        await asyncio.wait_for(
            checkout.checkout_handoff(_Session(), cart), timeout=5
        )

        self.assertEqual(len(recorder.body["items"]), 1)

    async def test_refuses_the_same_product_on_several_lines(self):
        """The caps are per line, so duplicates would multiply past them."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(*[_Line("sku1", quantity=10) for _ in range(20)])
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    # -- idempotency on the wire ----------------------------------------------------

    async def test_an_inherited_header_cannot_win_over_this_call_key(self):
        """requests matches headers case-insensitively, so a stale spelling would."""
        recorder = _Recorder()
        checkout = self._checkout(
            recorder,
            catalog=_Catalog(sku1=_Record()),
            request_options=RequestOptions(
                access_token=TOKEN, custom_headers={"X-Idempotency-Key": "inherited"}
            ),
        )

        await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1")), idempotency_key="this-call"
        )

        sent = recorder.headers
        keys = [name for name in sent if name.lower() == "x-idempotency-key"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(sent[keys[0]], "this-call")

    async def test_refuses_a_key_longer_than_the_sdk_accepts(self):
        """mercadopago 3.5.0 caps the header at 64; building options would raise."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1")), idempotency_key="k" * 65
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_retries_once_with_the_same_key_after_a_transport_failure(self):
        """A lost response does not prove the POST had no effect; Mercado Pago replays
        an identical request rather than duplicating it."""
        attempts = []

        def create(body, request_options=None):
            attempts.append((body, request_options.get_headers()["x-idempotency-key"]))
            if len(attempts) == 1:
                raise requests.ConnectionError("lost")
            return {
                "status": 201,
                "response": {
                    "id": "ORD-1",
                    "checkout_url": CHECKOUT_URL,
                    "type": "online",
                    "processing_mode": "manual",
                    "status": "created",
                    "currency": "BRL",
                    "expiration_time": "P1D",
                    "total_amount": body["total_amount"],
                    "external_reference": body["external_reference"],
                },
            }

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create = create
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1")), idempotency_key="op-1"
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual({key for _, key in attempts}, {"op-1"})
        self.assertEqual(attempts[0][0], attempts[1][0])

    async def test_gives_up_after_two_transport_failures_naming_the_reference(self):
        def create(body, request_options=None):
            raise requests.ConnectionError("lost")

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create = create
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            handoffs = await checkout.checkout_handoff(
                _Session(), _Cart(_Line("sku1")), idempotency_key="op-1"
            )

        self.assertEqual(handoffs, [])
        reference = checkout_module._reference("op-1")
        self.assertTrue(any(reference in line for line in logged.output))

    # -- amounts and the response snapshot ------------------------------------------

    async def test_refuses_an_amount_that_cannot_be_represented(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record(price="1E+93")))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", price="1E+93", quantity=10))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_refuses_an_order_that_came_back_without_the_expiry(self):
        """The 24-hour window is what stops a stale link being paid at an old price."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "checkout_url": CHECKOUT_URL,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        sdk.order.return_value.cancel.assert_called_once_with("ORD-1")

    # -- an order we refuse must not stay payable on the seller's account ----------

    async def test_cancels_an_order_it_refuses_to_hand_over(self):
        """The order exists at Mercado Pago even when validation fails afterwards."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "checkout_url": "https://evil.example.com/checkout",
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        sdk.order.return_value.cancel.assert_called_once_with("ORD-1")

    async def test_reports_a_cancellation_the_api_rejects(self):
        """A MagicMock returns a truthy object, not a 2xx — the success and failure
        paths have to be told apart explicitly."""
        recorder = _Recorder(response=lambda body: {"id": "ORD-1"})
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.return_value = {"status": 409, "response": {}}
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertTrue(any("409" in line for line in logged.output))

    async def test_logs_a_cancellation_the_api_accepts(self):
        recorder = _Recorder(response=lambda body: {"id": "ORD-1"})
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.return_value = {"status": 200, "response": {}}
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="INFO") as logged:
            await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertTrue(any("Cancelled refused order" in line for line in logged.output))

    async def test_does_not_cancel_an_order_it_hands_over(self):
        recorder = _Recorder()
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(len(handoffs), 1)
        sdk.order.return_value.cancel.assert_not_called()

    async def test_a_failed_cancellation_still_falls_back_quietly(self):
        """Cancellation is best effort: the handoff already failed either way."""
        recorder = _Recorder(response=lambda body: {"id": "ORD-1"})
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.side_effect = RuntimeError("cancel exploded")
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        # The order id is logged so the seller can reconcile by hand, and the SDK's own
        # exception text — which can carry request URLs — is not.
        self.assertTrue(any("ORD-1" in line for line in logged.output))
        self.assertFalse(any("cancel exploded" in line for line in logged.output))

    async def test_an_unreadable_order_id_cannot_be_cancelled(self):
        recorder = _Recorder(response=lambda body: {"id": None})
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        sdk.order.return_value.cancel.assert_not_called()

    async def test_a_cart_without_items_is_a_quiet_no_op(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for cart in (object(), SimpleNamespace(currency="BRL")):
            with self.subTest(cart=cart):
                self.assertEqual(await checkout.checkout_handoff(_Session(), cart), [])
                self.assertEqual(recorder.calls, [])

    async def test_rejects_a_checkout_url_outside_mercadopago(self):
        """`checkout_url` is rendered as the official payment button, so a
        response pointing anywhere else is dropped rather than handed over."""
        recorder = _Recorder(
            response={"id": "ORD-1", "checkout_url": "https://evil.example/pay"}
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_rejects_a_plain_http_checkout_url(self):
        recorder = _Recorder(
            response={
                "id": "ORD-1",
                "checkout_url": "http://www.mercadopago.com.br/checkout",
            }
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_rejects_checkout_urls_with_credentials_or_non_https_port(self):
        urls = (
            "https://user:password@www.mercadopago.com.br/checkout",
            "https://www.mercadopago.com.br:8443/checkout",
            "https://www.mercadopago.com.br:not-a-port/checkout",
            "https://www.mercadopago.com.br/checkout\x1b[31m",
            "https://www.mercadopago.com.br/" + "a" * 2048,
        )
        for checkout_url in urls:
            with self.subTest(checkout_url=checkout_url):
                recorder = _Recorder(
                    response={"id": "ORD-1", "checkout_url": checkout_url}
                )
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                with self.assertLogs(checkout_module.logger, "ERROR"):
                    handoffs = await checkout.checkout_handoff(
                        _Session(), _Cart(_Line("sku1"))
                    )

                self.assertEqual(handoffs, [])

    async def test_rejects_an_order_that_does_not_match_the_confirmed_snapshot(self):
        def response_with(**overrides):
            def response(body):
                payload = {
                    "id": "ORD-1",
                    "checkout_url": CHECKOUT_URL,
                    "type": "online",
                    "processing_mode": "manual",
                    "status": "created",
                    "currency": "BRL",
                    "total_amount": body["total_amount"],
                    "external_reference": body["external_reference"],
                }
                payload.update(overrides)
                return payload

            return response

        for response in (
            response_with(currency="USD"),
            response_with(total_amount="99.00"),
            response_with(external_reference="another-reference"),
            response_with(type="point"),
            response_with(processing_mode="automatic"),
            response_with(status="cancelled"),
            response_with(id=""),
        ):
            with self.subTest(response=response):
                recorder = _Recorder(response=response)
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                with self.assertLogs(checkout_module.logger, "ERROR"):
                    handoffs = await checkout.checkout_handoff(
                        _Session(), _Cart(_Line("sku1"))
                    )

                self.assertEqual(handoffs, [])

    async def test_reuses_a_caller_supplied_idempotency_key_verbatim(self):
        """The same operation retried carries the same key and the same body."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for _ in range(2):
            await checkout.checkout_handoff(
                _Session(), _Cart(_Line("sku1")), idempotency_key="op-42"
            )

        first, second = recorder.calls
        self.assertEqual(
            first[1].get_headers()["x-idempotency-key"], "op-42"
        )
        self.assertEqual(
            second[1].get_headers()["x-idempotency-key"], "op-42"
        )
        # A reused key must carry a byte-identical body, which Orders requires.
        self.assertEqual(first[0], second[0])

    async def test_generates_a_uuid4_key_when_the_caller_omits_one(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for _ in range(2):
            await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        keys = [call[1].get_headers()["x-idempotency-key"] for call in recorder.calls]
        for key in keys:
            self.assertEqual(UUID(key).version, 4)
        # A separate call is a separate purchase, so it must not reuse the key.
        self.assertNotEqual(keys[0], keys[1])

    async def test_an_invalid_key_fails_closed_without_minting_another(self):
        """Silently replacing a rejected key would create a second payable order."""
        for key in ("", "x" * 257, "with\ncontrol", 7):
            with self.subTest(key=key):
                recorder = _Recorder()
                checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

                handoffs = await checkout.checkout_handoff(
                    _Session(), _Cart(_Line("sku1")), idempotency_key=key
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_external_reference_is_derived_from_the_key_not_the_session(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(
            _Session(session_id="session-from-a-raw-header"),
            _Cart(_Line("sku1")),
            idempotency_key="op-42",
        )

        reference = recorder.body["external_reference"]
        self.assertNotIn("session-from-a-raw-header", reference)
        self.assertNotIn("op-42", reference)
        self.assertEqual(reference, checkout_module._reference("op-42"))

    async def test_preserves_sdk_request_options_without_mutating_them(self):
        recorder = _Recorder()
        sdk_options = RequestOptions(
            access_token=TOKEN,
            connection_timeout=17.0,
            custom_headers={"x-seller-header": "kept"},
            integrator_id="integrator",
            max_retries=8,
        )
        checkout = self._checkout(
            recorder,
            catalog=_Catalog(sku1=_Record()),
            request_options=sdk_options,
        )

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        sent_options = recorder.calls[-1][1]
        self.assertIsNot(sent_options, sdk_options)
        self.assertEqual(sent_options.connection_timeout, 17.0)
        self.assertEqual(sent_options.max_retries, 8)
        self.assertEqual(sent_options.integrator_id, "integrator")
        self.assertEqual(sent_options.custom_headers["x-seller-header"], "kept")
        self.assertIn("x-idempotency-key", sent_options.custom_headers)
        self.assertEqual(sdk_options.custom_headers, {"x-seller-header": "kept"})

    async def test_uses_checkout_pro_orders_contract(self):
        recorder = _Recorder()
        sdk = mock.MagicMock()
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk
        )

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        sdk.order.assert_called_once_with()
        sdk.preference.assert_not_called()
        self.assertEqual(recorder.body["type"], "online")
        self.assertEqual(recorder.body["processing_mode"], "manual")
        self.assertEqual(recorder.body["expiration_time"], "P1D")
        # Orders accepts platform_id/integrator_id here, but rejects a caller-supplied
        # application_id, which Mercado Pago fills from the access token itself.
        self.assertEqual(
            recorder.body["integration_data"],
            {"platform_id": checkout_module._PLATFORM_ID},
        )
        # The item schema accepts these three fields only.
        self.assertEqual(set(recorder.body["items"][0]), {"title", "quantity", "unit_price"})
        self.assertNotIn("expires", recorder.body)
        self.assertNotIn("expiration_date_to", recorder.body)

    async def test_uses_the_host_default_checkout_label(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1"))
        )

        self.assertIsNone(handoffs[0].label)
        self.assertNotIn("label", handoffs[0].model_dump(exclude_none=True))

    # -- attribution: Mercado Pago stores integration_data on the Order --------------

    async def test_always_sends_the_adapter_platform_id(self):
        """Attribution must not depend on a host remembering to configure it."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        # Literal on purpose: comparing against the constant would let an accidental
        # edit change production and test together.
        self.assertEqual(
            recorder.body["integration_data"],
            {"platform_id": "dev_9e28fa65abb111f189e77e2ccf36aeec"},
        )

    async def test_never_sends_application_id_or_sponsor(self):
        """Orders rejects a caller-supplied application_id and an unowned sponsor id,
        so neither may creep back into the payload."""
        recorder = _Recorder()
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record())
        )

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertNotIn("application_id", recorder.body["integration_data"])
        self.assertNotIn("sponsor", recorder.body["integration_data"])

    async def test_api_rejection_does_not_log_the_payload(self):
        """A 4xx body echoes the rejected payload — titles, prices, the reference. Only
        MP's own error identifiers may reach the log."""
        recorder = _Recorder(
            status=400,
            response={
                "error": "bad_request",
                "message": "unit_price 1234.56 invalid for currency",
                "cause": [{"code": 2034, "description": "title 'Secret product' invalid"}],
            },
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        logged = "\n".join(captured.output)
        self.assertEqual(handoffs, [])
        self.assertIn("bad_request", logged)
        self.assertIn("2034", logged)
        self.assertNotIn("1234.56", logged)
        self.assertNotIn("Secret product", logged)

    async def test_rejects_fractional_or_excessive_quantities(self):
        for quantity in (1.5, 0, 11, "not-a-number"):
            with self.subTest(quantity=quantity):
                recorder = _Recorder()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                handoffs = await checkout.checkout_handoff(
                    _Session(), _Cart(_Line("sku1", quantity=quantity))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_rejects_huge_quantity_before_integer_conversion(self):
        recorder = _Recorder()
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record())
        )

        with mock.patch.object(
            checkout_module,
            "int",
            side_effect=AssertionError("integer conversion must not run"),
            create=True,
        ):
            handoffs = await checkout.checkout_handoff(
                _Session(), _Cart(_Line("sku1", quantity="1E+300000"))
            )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_rejects_more_than_twenty_lines(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog())

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(*[_Line(f"sku-{index}") for index in range(21)])
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_rejects_non_finite_or_overprecision_prices(self):
        for price in ("NaN", "Infinity", "1E+999999999", "10.001", 0, -1):
            with self.subTest(price=price):
                recorder = _Recorder()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record(price=price))
                )

                handoffs = await checkout.checkout_handoff(
                    _Session(), _Cart(_Line("sku1"))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_rejects_invalid_product_identifiers(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for product_id in ("", "x" * 257, "with\ncontrol", None):
            with self.subTest(product_id=product_id):
                handoffs = await checkout.checkout_handoff(
                    _Session(), _Cart(_Line(product_id))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_unexpected_sdk_results_fail_closed(self):
        catalog = _Catalog(sku1=_Record())

        for result in (
            None,
            {"status": True, "response": {}},
            {"status": "201\nforged log line", "response": {}},
        ):
            with self.subTest(result=result):
                sdk = mock.MagicMock()
                sdk.request_options = RequestOptions(access_token=TOKEN)
                sdk.order.return_value.create.return_value = result
                checkout = MercadoPagoCheckout(
                    sdk=sdk,
                    catalog=catalog,
                )
                with self.assertLogs(checkout_module.logger, "ERROR") as captured:
                    handoffs = await checkout.checkout_handoff(
                        _Session(), _Cart(_Line("sku1"))
                    )
                self.assertEqual(handoffs, [])
                self.assertNotIn("forged log line", "\n".join(captured.output))

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create.side_effect = RuntimeError(
            "must not leak https://secret.example/token"
        )
        checkout = MercadoPagoCheckout(
            sdk=sdk,
            catalog=catalog,
        )
        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            handoffs = await checkout.checkout_handoff(
                _Session(), _Cart(_Line("sku1"))
            )
        self.assertEqual(handoffs, [])
        self.assertNotIn("secret.example", "\n".join(captured.output))

    async def test_api_error_identifiers_cannot_inject_logs(self):
        recorder = _Recorder(
            status=400,
            response={
                "error": "bad_request\nforged log line",
                "cause": [{"code": "secret product id"}, {"code": 17}],
            },
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        logged = "\n".join(captured.output)
        self.assertNotIn("forged log line", logged)
        self.assertNotIn("secret product id", logged)
        self.assertIn("17", logged)

    def test_rejects_an_sdk_without_an_access_token(self):
        with self.assertRaises(ValueError):
            self._checkout(
                _Recorder(),
                catalog=_Catalog(sku1=_Record()),
                request_options=RequestOptions(),
            )

    async def test_empty_cart_is_a_no_op(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog())

        self.assertEqual(await checkout.checkout_handoff(_Session(), _Cart()), [])
        self.assertEqual(recorder.calls, [])

    def test_access_token_is_not_in_the_repr(self):
        class _SecretReprSDK:
            request_options = RequestOptions(access_token=TOKEN)

            def __repr__(self):
                return f"<SDK access_token={TOKEN}>"

        checkout = MercadoPagoCheckout(
            sdk=_SecretReprSDK(),
            catalog=_Catalog(),
        )
        self.assertNotIn(TOKEN, repr(checkout))


class HandoffTypeTest(unittest.TestCase):
    def test_model_dump_matches_what_enrich_checkout_asks_for(self):
        dumped = CheckoutHandoff(url=CHECKOUT_URL, label="Pay").model_dump(
            exclude_none=True
        )
        self.assertEqual(dumped, {"url": CHECKOUT_URL, "label": "Pay"})

    def test_model_dump_rejects_options_it_does_not_implement(self):
        """Swallowing them would let upstream change a handoff's meaning while this
        contract test stayed green."""
        handoff = CheckoutHandoff(url="https://example.com")

        for option in ("exclude", "by_alias", "mode"):
            with self.subTest(option=option):
                with self.assertRaises(TypeError):
                    handoff.model_dump(**{option: True})

        self.assertEqual(
            handoff.model_dump(exclude_none=True), {"url": "https://example.com"}
        )
