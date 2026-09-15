# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Unit tests. No network: the SDK's ``order().create`` is replaced and the calls
it would have made are asserted against instead.

The commerce-agents types are faked here rather than imported, which is the point of
``types.py`` — the package under test must work with nothing from Anthropic's
repository installed. ``tests/test_contract.py`` covers the real thing.
"""

import ast
import asyncio
import inspect
import json
import pathlib
import re
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import mercadopago
import requests
from mercadopago.config import RequestOptions
from mercadopago.http.http_client import HttpClient
from mercadopago.resources.order import Order

from mercadopago_commerce_agents import (
    CheckoutHandoff,
    CheckoutOutcomeUnknown,
    MercadoPagoCheckout,
)
from mercadopago_commerce_agents import checkout as checkout_module

TOKEN = "test-access-token"  # not a credential: no request leaves the process
CHECKOUT_URL = "https://www.mercadopago.com.br/checkout/v1/redirect?order_id=ORD-1"
# The hosted link is bound to the Order it pays, so a fixture that returns a
# different id needs the matching link.
FLOOR_CHECKOUT_URL = (
    "https://www.mercadopago.com.br/checkout/v1/redirect?order_id=ORD-FLOOR"
)
EXTERNAL_REFERENCE = "seller-order-1"
# Distinguishes "the API omitted this field" from "the API sent null".
_ABSENT = object()
ORDER_ID = "ORD-1"
IDEMPOTENCY_KEY = "operation-1"


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
            }
            response["external_reference"] = body["external_reference"]
        return {"status": self.status, "response": response}

    @property
    def body(self):
        return self.calls[-1][0]

    @property
    def headers(self):
        return self.calls[-1][1].get_headers()


class _FakeHttpClient(HttpClient):
    """No-network transport that keeps the official SDK Order resource in the path."""

    def __init__(self, *, checkout_url=FLOOR_CHECKOUT_URL):
        self.checkout_url = checkout_url
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["url"].endswith("/cancel"):
            return {
                "status": 200,
                "response": {"id": "ORD-FLOOR", "status": "canceled"},
            }
        body = json.loads(kwargs["data"])
        response = {
            "status": 201,
            "response": {
                "id": "ORD-FLOOR",
                "checkout_url": self.checkout_url,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
            },
        }
        response["response"]["external_reference"] = body["external_reference"]
        return response


class CheckoutHandoffTest(unittest.IsolatedAsyncioTestCase):
    def _checkout(self, recorder, **kwargs):
        request_options = kwargs.pop(
            "request_options", RequestOptions(access_token=TOKEN)
        )
        sdk = kwargs.pop("sdk", mock.MagicMock())
        sdk.request_options = request_options
        sdk.order.return_value.create = recorder.create
        cancel = sdk.order.return_value.cancel
        if isinstance(cancel.return_value, mock.Mock):
            cancel.return_value = {
                "status": 200,
                "response": {"id": "ORD-1", "status": "canceled"},
            }

        return MercadoPagoCheckout(sdk=sdk, **kwargs)

    async def _handoff(self, checkout, session, cart, **kwargs):
        """Call the real public method with one stable, valid attempt pair by default."""
        kwargs.setdefault("external_reference", EXTERNAL_REFERENCE)
        kwargs.setdefault("idempotency_key", IDEMPOTENCY_KEY)
        return await checkout.checkout_handoff(session, cart, **kwargs)

    # -- the Critical finding: the charge must not come from the cart --------------

    async def test_price_change_requires_fresh_confirmation(self):
        """The catalog remains authoritative, but a changed amount needs consent."""
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=100.0))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1", price=0.01))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_uses_the_catalog_title_not_the_model_authored_one(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(title="Catalog title"))
        checkout = self._checkout(recorder, catalog=catalog)

        await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1", title="<model authored>"))
        )

        self.assertEqual(recorder.body["items"][0]["title"], "Catalog title")

    async def test_invalid_cart_price_requires_fresh_confirmation(self):
        recorder = _Recorder()
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record(price="100.00"))
        )

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1", price="not-a-number"))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    def test_requires_a_catalog_at_construction(self):
        """There is no state in which the adapter can fall back to cart prices."""
        with self.assertRaises(TypeError):
            self._checkout(_Recorder())

    def test_public_surface_stays_minimal(self):
        """The reviewed contract: two constructor arguments, two required per-call
        identifiers, and one optional recovery flag that defaults to off."""
        self.assertEqual(
            list(inspect.signature(MercadoPagoCheckout).parameters), ["sdk", "catalog"]
        )
        self.assertEqual(
            list(inspect.signature(MercadoPagoCheckout.checkout_handoff).parameters),
            [
                "self",
                "session",
                "cart",
                "external_reference",
                "idempotency_key",
                "recovering",
            ],
        )
        # The upstream two-argument call must keep working unchanged, so recovery has
        # to be opt-in and off by default.
        recovering = inspect.signature(
            MercadoPagoCheckout.checkout_handoff
        ).parameters["recovering"]
        self.assertIs(recovering.default, False)
        self.assertEqual(recovering.kind, inspect.Parameter.KEYWORD_ONLY)
        # Names alone would stay green if the `*` markers were dropped, which would
        # change the contract the README documents.
        constructor = inspect.signature(MercadoPagoCheckout).parameters
        for name in ("sdk", "catalog"):
            self.assertEqual(constructor[name].kind, inspect.Parameter.KEYWORD_ONLY)
        handoff = inspect.signature(MercadoPagoCheckout.checkout_handoff).parameters
        for name in ("session", "cart"):
            self.assertEqual(handoff[name].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name in ("idempotency_key", "external_reference"):
            self.assertEqual(handoff[name].kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIs(handoff[name].default, inspect.Parameter.empty)

    async def test_requires_both_attempt_identifiers_as_keywords(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))
        session = _Session()
        cart = _Cart(_Line("sku1"))

        for supplied in (
            {},
            {"external_reference": EXTERNAL_REFERENCE},
            {"idempotency_key": IDEMPOTENCY_KEY},
        ):
            with self.subTest(supplied=supplied):
                with self.assertRaises(TypeError):
                    await checkout.checkout_handoff(session, cart, **supplied)

        with self.assertRaises(TypeError):
            await checkout.checkout_handoff(
                session, cart, EXTERNAL_REFERENCE, IDEMPOTENCY_KEY
            )
        self.assertEqual(recorder.calls, [])

    async def test_invalid_attempt_identifiers_fail_before_the_cart_is_read(self):
        class _UnreadableCart:
            @property
            def items(self):
                raise AssertionError("the cart must not be read")

        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))
        for supplied in (
            {"external_reference": None, "idempotency_key": IDEMPOTENCY_KEY},
            {"external_reference": EXTERNAL_REFERENCE, "idempotency_key": None},
        ):
            with self.subTest(supplied=supplied):
                with self.assertLogs(checkout_module.logger, "WARNING"):
                    handoffs = await checkout.checkout_handoff(
                        _Session(), _UnreadableCart(), **supplied
                    )
                self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_refuses_unknown_product(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog())

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("ghost")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_refuses_out_of_stock_product(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(in_stock=False))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

                handoffs = await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line("sku1"))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_a_record_of_the_wrong_shape_says_so(self):
        """Returning a dict is the usual slip; reporting it as out of stock sent people
        looking through their inventory."""
        recorder = _Recorder()

        class _DictCatalog:
            async def get_product_details(self, _session, _product_id):
                return {"title": "T", "price": "10.00", "currency": "BRL", "in_stock": True}

        checkout = self._checkout(recorder, catalog=_DictCatalog())

        with self.assertLogs(checkout_module.logger, level="WARNING") as logged:
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])
        self.assertTrue(any("invalid_catalog_record" in line for line in logged.output))

    async def test_a_cart_without_a_currency_says_so(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))
        cart = SimpleNamespace(items=[_Line("sku1")])  # no `currency` at all

        with self.assertLogs(checkout_module.logger, level="WARNING") as logged:
            handoffs = await self._handoff(checkout, _Session(), cart)

        self.assertEqual(handoffs, [])
        self.assertTrue(any("missing_cart_currency" in line for line in logged.output))

    async def test_prices_every_line_from_the_catalog(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=10.0), sku2=_Record(price=20.5))
        checkout = self._checkout(recorder, catalog=catalog)

        await self._handoff(
            checkout,
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

        await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

        handoffs = await self._handoff(
            checkout,
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

        handoffs = await self._handoff(
            checkout,
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

                handoffs = await self._handoff(checkout, _Session(), cart)

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    # -- the High finding: the caller's session id must stay out of the payment ----

    async def test_sends_the_seller_reference_without_deriving_it_from_session(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))
        session = _Session("session-from-a-raw-header")

        handoffs = await self._handoff(
            checkout,
            session,
            _Cart(_Line("sku1")),
            external_reference="seller-order-safe",
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(recorder.body["external_reference"], "seller-order-safe")
        self.assertNotIn(session.session_id, json.dumps(recorder.body))

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

        await self._handoff(checkout, _Session(), cart)

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
            self._handoff(checkout, _Session(), cart), timeout=5
        )

        self.assertEqual(len(recorder.body["items"]), 1)

    async def test_refuses_the_same_product_on_several_lines(self):
        """The caps are per line, so duplicates would multiply past them."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(*[_Line("sku1", quantity=10) for _ in range(20)])
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_canonicalizes_item_order_for_a_reused_key(self):
        """A semantically identical cart must keep the Orders body byte-identical."""
        recorder = _Recorder()
        checkout = self._checkout(
            recorder,
            catalog=_Catalog(
                sku_a=_Record(title="A"),
                sku_b=_Record(title="B"),
            ),
        )

        await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku_a"), _Line("sku_b")),
            idempotency_key="same-operation",
        )
        await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku_b"), _Line("sku_a")),
            idempotency_key="same-operation",
        )

        first, second = recorder.calls
        self.assertEqual(first[0], second[0])
        self.assertEqual(
            [item["title"] for item in first[0]["items"]],
            ["A", "B"],
        )

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

        await self._handoff(
            checkout,
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

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1")), idempotency_key="k" * 65
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_retries_once_with_the_same_key_after_a_transport_failure(self):
        """A lost response does not prove the POST had no effect; Mercado Pago replays
        an identical request rather than duplicating it."""
        attempts = []

        def create(body, request_options=None):
            attempts.append(
                (
                    json.dumps(body, sort_keys=True),
                    request_options.get_headers()["x-idempotency-key"],
                )
            )
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

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1")), idempotency_key="op-1"
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual({key for _, key in attempts}, {"op-1"})
        self.assertEqual(attempts[0][0], attempts[1][0])
        self.assertEqual(
            json.loads(attempts[0][0])["external_reference"], EXTERNAL_REFERENCE
        )

    async def test_two_transport_failures_raise_an_indeterminate_outcome(self):
        effects = []

        def create(body, request_options=None):
            # A timeout can happen after Mercado Pago accepted the POST. Simulate that
            # remote effect so returning [] would demonstrably be an unsafe fallback.
            effects.append(
                (
                    json.dumps(body, sort_keys=True),
                    request_options.get_headers()["x-idempotency-key"],
                )
            )
            raise requests.ConnectionError("lost")

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create = create
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line("sku1")), idempotency_key="op-1"
                )

        self.assertEqual(len(effects), 2)
        self.assertEqual(effects[0], effects[1])
        self.assertEqual(captured.exception.idempotency_key, "op-1")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertEqual(captured.exception.reason, "transport_failure")
        self.assertIsNone(captured.exception.order_id)
        self.assertFalse(any("op-1" in line for line in logged.output))

    async def test_a_rejected_retry_cannot_erase_the_first_posts_unknown_outcome(self):
        """A 400 describes the retry only; the first POST may already have succeeded."""
        attempts = 0

        def create(_body, request_options=None):
            nonlocal attempts
            del request_options
            attempts += 1
            if attempts == 1:
                raise requests.ConnectionError("response lost")
            return {"status": 400, "response": {"error": "bad_request"}}

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create = create
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await self._handoff(
                checkout,
                _Session(),
                _Cart(_Line("sku1")),
                idempotency_key="op-lost-response",
                external_reference="seller-order-42",
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(captured.exception.reason, "retry_inconclusive")
        self.assertEqual(captured.exception.idempotency_key, "op-lost-response")
        self.assertEqual(captured.exception.external_reference, "seller-order-42")
        self.assertIsNone(captured.exception.order_id)

    async def test_cancelling_the_coroutine_exposes_the_key_for_a_safe_retry(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        keys = []

        def create(body, request_options=None):
            keys.append(request_options.get_headers()["x-idempotency-key"])
            if len(keys) == 1:
                started.set()
                release.wait(timeout=5)
                finished.set()
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
                    "total_amount": "100.00",
                    "external_reference": body["external_reference"],
                },
            }

        sdk = mock.MagicMock()
        sdk.request_options = RequestOptions(access_token=TOKEN)
        sdk.order.return_value.create = create
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        task = asyncio.create_task(
            self._handoff(checkout, _Session(), _Cart(_Line("sku1")))
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        task.cancel()
        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await task
        release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 5))

        handoffs = await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku1")),
            idempotency_key=captured.exception.idempotency_key,
        )

        self.assertEqual(captured.exception.reason, "create_interrupted")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertIsNone(captured.exception.order_id)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    # -- amounts and the response snapshot ------------------------------------------

    async def test_refuses_an_amount_that_cannot_be_represented(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record(price="1E+93")))

        handoffs = await self._handoff(
            checkout,
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

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.body["external_reference"], EXTERNAL_REFERENCE)
        sdk.order.return_value.cancel.assert_called_once()
        order_id, options = sdk.order.return_value.cancel.call_args.args
        self.assertEqual(order_id, "ORD-1")
        self.assertEqual(
            options.get_headers()["x-idempotency-key"],
            checkout_module._cancel_key(recorder.headers["x-idempotency-key"]),
        )

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

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.body["external_reference"], EXTERNAL_REFERENCE)
        sdk.order.return_value.cancel.assert_called_once()
        order_id, options = sdk.order.return_value.cancel.call_args.args
        self.assertEqual(order_id, "ORD-1")
        create_key = recorder.headers["x-idempotency-key"]
        cancel_key = options.get_headers()["x-idempotency-key"]
        self.assertEqual(cancel_key, checkout_module._cancel_key(create_key))
        self.assertNotEqual(cancel_key, create_key)
        self.assertLessEqual(len(cancel_key), 64)

    async def test_reports_a_cancellation_the_api_rejects(self):
        """A MagicMock returns a truthy object, not a 2xx — the success and failure
        paths have to be told apart explicitly."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.return_value = {"status": 409, "response": {}}
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(captured.exception.reason, "cleanup_not_confirmed")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertEqual(captured.exception.order_id, "ORD-1")
        self.assertTrue(any("409" in line for line in logged.output))

    async def test_logs_a_cancellation_the_api_accepts(self):
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.return_value = {
            "status": 200,
            "response": {"id": "ORD-1", "status": "canceled"},
        }
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="INFO") as logged:
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])
        self.assertTrue(any("Cancelled a refused order" in line for line in logged.output))

    async def test_cancellation_status_cannot_inject_logs(self):
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.return_value = {
            "status": "409\nforged-log-entry",
            "response": {},
        }
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(captured.exception.order_id, "ORD-1")
        self.assertFalse(any("forged-log-entry" in line for line in logged.output))
        self.assertFalse(any("ORD-1" in line for line in logged.output))

    async def test_unconfirmed_cancellation_body_blocks_the_fallback(self):
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        for response in (
            {},
            {"id": "ORD-2", "status": "canceled"},
            {"id": "ORD-1", "status": "created"},
        ):
            with self.subTest(response=response):
                sdk = mock.MagicMock()
                sdk.order.return_value.cancel.return_value = {
                    "status": 200,
                    "response": response,
                }
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk
                )

                with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                    await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

                self.assertEqual(captured.exception.reason, "cleanup_not_confirmed")
                self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
                self.assertEqual(captured.exception.order_id, "ORD-1")

    async def test_does_not_cancel_an_order_it_hands_over(self):
        recorder = _Recorder()
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(len(handoffs), 1)
        sdk.order.return_value.cancel.assert_not_called()

    async def test_a_failed_cancellation_blocks_the_fallback(self):
        """An unconfirmed cleanup may leave an order payable, so [] is unsafe."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        sdk.order.return_value.cancel.side_effect = RuntimeError("cancel exploded")
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertLogs(checkout_module.logger, level="ERROR") as logged:
            with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(captured.exception.reason, "cleanup_not_confirmed")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertEqual(captured.exception.order_id, "ORD-1")
        # Neither the payment identifier nor the SDK's own exception text — which can
        # carry request URLs — belongs in logs.
        self.assertFalse(any("ORD-1" in line for line in logged.output))
        self.assertFalse(any("cancel exploded" in line for line in logged.output))

    async def test_cancelling_during_cleanup_reports_an_indeterminate_outcome(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()

        def cancel(order_id, request_options=None):
            del order_id, request_options
            started.set()
            release.wait(timeout=5)
            finished.set()
            return {
                "status": 200,
                "response": {"id": "ORD-1", "status": "canceled"},
            }

        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)
        sdk.order.return_value.cancel = cancel
        task = asyncio.create_task(
            self._handoff(
                checkout,
                _Session(),
                _Cart(_Line("sku1")),
                idempotency_key="operation-1",
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 5))

        task.cancel()
        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await task
        release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 5))

        self.assertEqual(captured.exception.reason, "cleanup_interrupted")
        self.assertEqual(captured.exception.idempotency_key, "operation-1")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertEqual(captured.exception.order_id, "ORD-1")

    async def test_an_unreadable_order_id_blocks_the_fallback(self):
        recorder = _Recorder(
            response=lambda body: {
                "id": None,
                "external_reference": body["external_reference"],
            }
        )
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(captured.exception.reason, "cleanup_not_confirmed")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertIsNone(captured.exception.order_id)
        sdk.order.return_value.cancel.assert_not_called()

    async def test_a_cart_without_items_is_a_quiet_no_op(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for cart in (object(), SimpleNamespace(currency="BRL")):
            with self.subTest(cart=cart):
                self.assertEqual(await self._handoff(checkout, _Session(), cart), [])
                self.assertEqual(recorder.calls, [])

    @staticmethod
    def _response_but_for_the_url(checkout_url):
        """A response that is valid in every respect except the link.

        Omitting any other required field would make `_checked_response` refuse for
        that reason instead, and the test would stay green even with the URL check
        removed — which is exactly what it must not do.
        """
        def response(body):
            return {
                "id": "ORD-1",
                "checkout_url": checkout_url,
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }

        return response

    async def test_rejects_a_checkout_url_outside_mercadopago(self):
        """`checkout_url` is rendered as the official payment button, so a
        response pointing anywhere else is dropped rather than handed over."""
        recorder = _Recorder(
            response=self._response_but_for_the_url("https://evil.example/pay")
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_rejects_a_checkout_url_that_pays_another_order(self):
        """The host allowlist proves the link is Mercado Pago's, not that it is ours.

        A response carrying our ``id`` next to a link for a different order is
        well-formed and passes every host check, so the ``order_id`` in the link is
        what binds the two.
        """
        base = "https://www.mercadopago.com.br/checkout/v1/redirect"
        for label, checkout_url in (
            ("divergent", f"{base}?order_id=ORD-OTHER"),
            ("duplicated", f"{base}?order_id=ORD-1&order_id=ORD-OTHER"),
            ("absent", f"{base}?pref_id=1234-abcd"),
            ("empty", f"{base}?order_id="),
            ("no query at all", base),
        ):
            with self.subTest(checkout_url=label):
                recorder = _Recorder(
                    response=lambda body, url=checkout_url: {
                        "id": "ORD-1",
                        "checkout_url": url,
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
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk
                )

                with self.assertLogs(checkout_module.logger, "ERROR"):
                    handoffs = await self._handoff(
                        checkout, _Session(), _Cart(_Line("sku1"))
                    )

                self.assertEqual(handoffs, [])
                # Correlated, so the stranded Order is ours to cancel.
                sdk.order.return_value.cancel.assert_called_once()
                self.assertEqual(
                    sdk.order.return_value.cancel.call_args.args[0], "ORD-1"
                )

    async def test_accepts_the_checkout_url_that_pays_this_order(self):
        """The shape Mercado Pago actually returns, confirmed against the live API."""
        recorder = _Recorder(
            response=lambda body: {
                "id": "ORD-1",
                "checkout_url": (
                    "https://www.mercadopago.com.br/checkout/v1/redirect"
                    "?order_id=ORD-1&pref_id=3497261639-47d42578-7894"
                ),
                "type": "online",
                "processing_mode": "manual",
                "status": "created",
                "currency": "BRL",
                "expiration_time": "P1D",
                "total_amount": body["total_amount"],
                "external_reference": body["external_reference"],
            }
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(len(handoffs), 1)
        self.assertIn("order_id=ORD-1", handoffs[0].url)

    async def test_rejects_a_plain_http_checkout_url(self):
        recorder = _Recorder(
            response=self._response_but_for_the_url(
                f"http://www.mercadopago.com.br/checkout/v1/redirect?order_id={ORDER_ID}"
            )
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_rejects_checkout_urls_with_credentials_or_non_https_port(self):
        bound = f"/checkout/v1/redirect?order_id={ORDER_ID}"
        urls = (
            f"https://user:password@www.mercadopago.com.br{bound}",
            f"https://www.mercadopago.com.br:8443{bound}",
            f"https://www.mercadopago.com.br:not-a-port{bound}",
            f"https://www.mercadopago.com.br{bound}\x1b[31m",
            "https://www.mercadopago.com.br/" + "a" * 2048 + bound,
        )
        for checkout_url in urls:
            with self.subTest(checkout_url=checkout_url):
                recorder = _Recorder(
                    response=self._response_but_for_the_url(checkout_url)
                )
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                with self.assertLogs(checkout_module.logger, "ERROR"):
                    handoffs = await self._handoff(
                        checkout,
                        _Session(), _Cart(_Line("sku1"))
                    )

                self.assertEqual(handoffs, [])

    async def test_rejects_an_order_that_does_not_match_the_confirmed_snapshot(self):
        def response_with(**overrides):
            def response(body):
                # The expiry belongs in the baseline: without it every subcase below
                # would be refused for the missing expiry before reaching the field it
                # means to test, and those checks could regress while staying green.
                payload = {
                    "id": "ORD-1",
                    "checkout_url": CHECKOUT_URL,
                    "type": "online",
                    "processing_mode": "manual",
                    "status": "created",
                    "currency": "BRL",
                    "expiration_time": "P1D",
                    "total_amount": body["total_amount"],
                }
                payload["external_reference"] = body["external_reference"]
                payload.update(overrides)
                return payload

            return response

        for response in (
            response_with(currency="USD"),
            response_with(total_amount="99.00"),
            response_with(type="point"),
            response_with(processing_mode="automatic"),
            response_with(status="cancelled"),
        ):
            with self.subTest(response=response):
                recorder = _Recorder(response=response)
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                with self.assertLogs(checkout_module.logger, "ERROR"):
                    handoffs = await self._handoff(
                        checkout,
                        _Session(), _Cart(_Line("sku1"))
                    )

                self.assertEqual(handoffs, [])

        recorder = _Recorder(response=response_with(id=""))
        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record())
        )
        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))
        self.assertEqual(captured.exception.reason, "cleanup_not_confirmed")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        self.assertIsNone(captured.exception.order_id)

    async def test_reuses_a_caller_supplied_attempt_pair_verbatim(self):
        """The same operation retried carries the same pair and the same body."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for _ in range(2):
            await self._handoff(
                checkout,
                _Session(),
                _Cart(_Line("sku1")),
                external_reference="seller-order-42",
                idempotency_key="op-42",
            )

        first, second = recorder.calls
        self.assertEqual(
            first[1].get_headers()["x-idempotency-key"], "op-42"
        )
        self.assertEqual(
            second[1].get_headers()["x-idempotency-key"], "op-42"
        )
        self.assertEqual(first[0]["external_reference"], "seller-order-42")
        self.assertEqual(second[0]["external_reference"], "seller-order-42")
        # A reused key must carry a byte-identical body, which Orders requires.
        self.assertEqual(first[0], second[0])

    async def test_an_invalid_key_fails_closed_without_minting_another(self):
        """Silently replacing a rejected key would create a second payable order."""
        for key in ("", "x" * 257, "with\ncontrol", 7):
            with self.subTest(key=key):
                recorder = _Recorder()
                checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

                handoffs = await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line("sku1")), idempotency_key=key
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_an_uncorrelated_response_is_never_cancelled(self):
        """A response we cannot tie to this attempt is not ours to clean up.

        Cancelling the id it carries would act on an Order that may belong to someone
        else, so the adapter stops and hands the host something to reconcile with.
        Covers both shapes: a reference that differs from ours, and one that is absent.
        """
        def response_with(**overrides):
            def response(body):
                payload = {
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
                payload.update(overrides)
                payload = {
                    key: value for key, value in payload.items() if value is not _ABSENT
                }
                return payload

            return response

        for label, response in (
            ("divergent", response_with(external_reference="not-requested-by-the-seller")),
            ("absent", response_with(external_reference=_ABSENT)),
        ):
            with self.subTest(reference=label):
                recorder = _Recorder(response=response)
                sdk = mock.MagicMock()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk
                )

                with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                    await self._handoff(
                        checkout,
                        _Session(session_id="session-from-a-raw-header"),
                        _Cart(_Line("sku1")),
                        external_reference="expected-reference",
                        idempotency_key="op-42",
                    )

                self.assertEqual(captured.exception.reason, "uncorrelated_response")
                self.assertEqual(
                    captured.exception.external_reference, "expected-reference"
                )
                self.assertEqual(captured.exception.idempotency_key, "op-42")
                # The host still needs something to reconcile against.
                self.assertEqual(captured.exception.order_id, "ORD-1")
                sdk.order.return_value.cancel.assert_not_called()
                self.assertEqual(recorder.body["external_reference"], "expected-reference")

    async def test_uses_a_seller_supplied_external_reference_verbatim(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku1")),
            idempotency_key="opaque-operation-key",
            external_reference="123456",
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(recorder.body["external_reference"], "123456")

    async def test_invalid_external_reference_fails_before_the_api_call(self):
        for reference in ("", "x" * 65, "contains spaces", "slash/value", 123):
            with self.subTest(reference=reference):
                recorder = _Recorder()
                checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

                handoffs = await self._handoff(
                    checkout,
                    _Session(),
                    _Cart(_Line("sku1")),
                    idempotency_key="operation-1",
                    external_reference=reference,
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

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

        await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

        await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

    async def test_minimum_sdk_real_order_resource_receives_create_and_cancel_headers(self):
        """Exercise mercadopago 3.5.0's real Order/MPBase boundary without network."""
        http = _FakeHttpClient(checkout_url="https://evil.example/checkout")
        sdk = mercadopago.SDK(TOKEN, http_client=http)
        checkout = MercadoPagoCheckout(
            sdk=sdk,
            catalog=_Catalog(sku1=_Record()),
        )

        handoffs = await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku1")),
            idempotency_key="floor-create-key",
        )

        self.assertIsInstance(sdk.order(), Order)
        self.assertEqual(handoffs, [])
        self.assertEqual(len(http.calls), 2)
        create, cancel = http.calls
        self.assertEqual(create["url"], "https://api.mercadopago.com/v1/orders")
        self.assertEqual(create["headers"]["x-idempotency-key"], "floor-create-key")
        self.assertEqual(
            json.loads(create["data"])["external_reference"], EXTERNAL_REFERENCE
        )
        self.assertEqual(
            cancel["url"],
            "https://api.mercadopago.com/v1/orders/ORD-FLOOR/cancel",
        )
        self.assertEqual(
            cancel["headers"]["x-idempotency-key"],
            checkout_module._cancel_key("floor-create-key"),
        )
        self.assertNotEqual(
            create["headers"]["x-idempotency-key"],
            cancel["headers"]["x-idempotency-key"],
        )

    async def test_minimum_sdk_real_order_resource_preserves_required_reference(self):
        """The supported SDK must put the seller reference on the wire unchanged."""
        http = _FakeHttpClient()
        sdk = mercadopago.SDK(TOKEN, http_client=http)
        checkout = MercadoPagoCheckout(sdk=sdk, catalog=_Catalog(sku1=_Record()))

        handoffs = await self._handoff(
            checkout,
            _Session(),
            _Cart(_Line("sku1")),
            external_reference="seller-order-42",
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(
            json.loads(http.calls[0]["data"])["external_reference"],
            "seller-order-42",
        )

    async def test_uses_the_host_default_checkout_label(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await self._handoff(
            checkout,
            _Session(), _Cart(_Line("sku1"))
        )

        self.assertIsNone(handoffs[0].label)
        self.assertNotIn("label", handoffs[0].model_dump(exclude_none=True))

    # -- attribution: Mercado Pago stores integration_data on the Order --------------

    async def test_always_sends_the_adapter_platform_id(self):
        """Attribution must not depend on a host remembering to configure it."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

        await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        logged = "\n".join(captured.output)
        self.assertEqual(handoffs, [])
        self.assertIn("bad_request", logged)
        self.assertIn("2034", logged)
        self.assertNotIn("1234.56", logged)
        self.assertNotIn("Secret product", logged)

    async def test_orders_api_rejection_logs_only_safe_error_codes(self):
        recorder = _Recorder(
            status=400,
            response={
                "errors": [
                    {
                        "code": "required_properties",
                        "message": "external_reference seller-order-secret is required",
                    },
                    {"code": "invalid_token"},
                    {"code": "invalid_token\nforged log line"},
                    {"code": "seller-order-private"},
                    {"code": "required_properties"},
                    {"code": 42},
                    "not-an-error-object",
                ]
            },
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            handoffs = await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        logged = "\n".join(captured.output)
        self.assertEqual(handoffs, [])
        self.assertEqual(logged.count("required_properties"), 1)
        self.assertIn("invalid_token", logged)
        self.assertNotIn("seller-order-secret", logged)
        self.assertNotIn("seller-order-private", logged)
        self.assertNotIn("forged log line", logged)
        self.assertNotIn("42", logged)

    async def test_orders_api_error_codes_are_bounded(self):
        documented_codes = (
            "empty_required_header",
            "invalid_idempotency_key_length",
            "required_properties",
            "unsupported_properties",
            "minimum_properties",
            "property_type",
            "minimum_items",
            "maximum_items",
            "property_value",
            "json_syntax_error",
            "invalid_properties",
            "invalid_total_amount",
            "invalid_email_for_sandbox",
            "order_invalid_sponsor_id",
            "invalid_header_value",
            "order_builder_without_transactions",
            "invalid_order_type",
            "invalid_credentials",
            "forbidden",
            "PA_UNAUTHORIZED_RESULT_FROM_POLICIES",
            "resource_locked",
        )
        recorder = _Recorder(
            status=400,
            response={
                "errors": [{"code": code} for code in documented_codes]
            },
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        logged = "\n".join(captured.output)
        self.assertIn("PA_UNAUTHORIZED_RESULT_FROM_POLICIES", logged)
        self.assertNotIn("resource_locked", logged)

    async def test_api_error_codes_are_arguments_to_a_fixed_log_template(self):
        recorder = _Recorder(
            status=400,
            response={
                "error": "bad_request",
                "errors": [
                    {"code": "required_properties"},
                    {"code": "bad_request"},
                ],
                "cause": [{"code": 2034}, {"code": 2034}],
            },
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with mock.patch.object(checkout_module.logger, "error") as log_error:
            await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        log_error.assert_called_once_with(
            "Order creation failed (HTTP %s): codes=%s",
            400,
            ["bad_request", "required_properties", "2034"],
        )

    async def test_rejects_fractional_or_excessive_quantities(self):
        for quantity in (1.5, 0, 11, "not-a-number"):
            with self.subTest(quantity=quantity):
                recorder = _Recorder()
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                handoffs = await self._handoff(
                    checkout,
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
            handoffs = await self._handoff(
                checkout,
                _Session(), _Cart(_Line("sku1", quantity="1E+300000"))
            )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_rejects_more_than_twenty_lines(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog())

        handoffs = await self._handoff(
            checkout,
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

                handoffs = await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line("sku1"))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_rejects_invalid_product_identifiers(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        for product_id in ("", "x" * 257, "with\ncontrol", None):
            with self.subTest(product_id=product_id):
                handoffs = await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line(product_id))
                )

                self.assertEqual(handoffs, [])
                self.assertEqual(recorder.calls, [])

    async def test_unexpected_sdk_results_block_the_fallback(self):
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
                    with self.assertRaises(CheckoutOutcomeUnknown) as outcome:
                        await self._handoff(
                            checkout,
                            _Session(), _Cart(_Line("sku1"))
                        )
                self.assertEqual(outcome.exception.reason, "invalid_sdk_response")
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
            with self.assertRaises(CheckoutOutcomeUnknown) as outcome:
                await self._handoff(
                    checkout,
                    _Session(), _Cart(_Line("sku1"))
                )
        self.assertEqual(outcome.exception.reason, "sdk_failure")
        self.assertNotIn("secret.example", "\n".join(captured.output))

    async def test_http_409_requires_reconciliation_instead_of_fallback(self):
        recorder = _Recorder(
            status=409,
            response={"error": "idempotency_key_already_used"},
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await self._handoff(
                checkout,
                _Session(), _Cart(_Line("sku1")), idempotency_key="existing-attempt"
            )

        self.assertEqual(captured.exception.reason, "ambiguous_response")
        self.assertEqual(captured.exception.idempotency_key, "existing-attempt")

    async def test_recovery_never_releases_the_fallback(self):
        """`unknown -> catalog moves -> same key` must not come back as an empty list.

        The host only retries after `CheckoutOutcomeUnknown`, so an Order carrying this
        key may already be payable. A refusal decided locally never asked Mercado Pago
        about it, and `[]` would tell the host it is safe to charge again elsewhere.
        """
        class _Unreachable:
            def __init__(self):
                self.posts = 0

            def create(self, body, request_options=None):
                self.posts += 1
                raise requests.ConnectionError("response lost")

            def cancel(self, *args):  # pragma: no cover - must never be reached
                raise AssertionError("nothing to cancel")

        recorder = _Unreachable()
        catalog = _Catalog(sku1=_Record(price=100.0))
        checkout = self._checkout(recorder, catalog=catalog)

        with self.assertRaises(CheckoutOutcomeUnknown) as first:
            await self._handoff(
                checkout, _Session(), _Cart(_Line("sku1", price=100.0))
            )
        self.assertEqual(first.exception.reason, "transport_failure")
        self.assertEqual(recorder.posts, 2)

        # The price moves before the host gets to retry.
        catalog.records["sku1"] = _Record(price=120.0)
        posts_before = recorder.posts

        with self.assertRaises(CheckoutOutcomeUnknown) as second:
            await self._handoff(
                checkout,
                _Session(),
                _Cart(_Line("sku1", price=100.0)),
                recovering=True,
            )

        self.assertEqual(second.exception.reason, "cart_reconfirmation_required")
        self.assertEqual(second.exception.idempotency_key, IDEMPOTENCY_KEY)
        self.assertEqual(second.exception.external_reference, EXTERNAL_REFERENCE)
        # The refusal was local: nothing asked Mercado Pago whether the Order exists.
        self.assertEqual(recorder.posts, posts_before)

    async def test_outside_recovery_a_local_refusal_is_still_an_empty_list(self):
        """Recovery is opt-in; the ordinary path keeps its documented contract."""
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=120.0))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await self._handoff(
            checkout, _Session(), _Cart(_Line("sku1", price=100.0))
        )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])

    async def test_a_sanitized_outcome_carries_no_sdk_exception(self):
        """`raise ... from None` hides the chain from tracebacks but keeps the object.

        Host logging and error-tracking walk `__context__`, so the raw SDK error — and
        the request URL and headers it can carry — has to be gone, not just hidden.
        """
        secret = "https://user:password@internal.example/v1/orders"

        class _Exploding:
            def create(self, body, request_options=None):
                raise RuntimeError(f"SDK failure against {secret}")

            def cancel(self, *args):  # pragma: no cover - must never be reached
                raise AssertionError("nothing to cancel")

        checkout = self._checkout(_Exploding(), catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            with self.assertRaises(CheckoutOutcomeUnknown) as outcome:
                await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

        error = outcome.exception
        self.assertEqual(error.reason, "sdk_failure")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, "\n".join(captured.output))

    async def _refuses_key_without_calling_the_api(self, key):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "WARNING"):
            handoffs = await self._handoff(
                checkout, _Session(), _Cart(_Line("sku1")), idempotency_key=key
            )

        self.assertEqual(handoffs, [])
        # Refused before any network work, so no outcome is ever left in doubt.
        self.assertEqual(recorder.calls, [])

    async def test_a_catalog_failure_while_recovering_carries_no_context(self):
        """The recovery path must not reintroduce the leak the SDK path just closed.

        `_refused` raises while recovering, so calling it inside the catalog `except`
        would attach the catalog's own exception — which the code comments themselves
        note can carry a sensitive URL — to `__context__`.
        """
        secret = "https://internal.example/token?k=secret"

        class _ExplodingCatalog:
            async def get_product_details(self, session, product_id):
                raise RuntimeError(f"catalog failed against {secret}")

        checkout = self._checkout(_Recorder(), catalog=_ExplodingCatalog())

        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            with self.assertRaises(CheckoutOutcomeUnknown) as outcome:
                await self._handoff(
                    checkout, _Session(), _Cart(_Line("sku1")), recovering=True
                )

        error = outcome.exception
        self.assertEqual(error.reason, "catalog_unavailable")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_the_documented_signature_matches_the_real_one(self):
        """The published surface drifted once; this keeps it honest mechanically.

        `docs/integration.md` and the README summary are what an integrator reads
        before writing a payment call, so a parameter that exists in code and not there
        is a contract that only half of the readers get.
        """
        root = pathlib.Path(__file__).resolve().parent.parent
        documented = {
            "docs/integration.md": re.compile(
                r"checkout_handoff\(\n(?P<params>.*?)\n\)", re.DOTALL
            ),
            "README.md": re.compile(
                r"```text\n"
                r"MercadoPagoCheckout\(\*, sdk, catalog\)\n"
                r"checkout_handoff\(\n(?P<params>.*?)\n\)\n"
                r"```",
                re.DOTALL,
            ),
        }
        real = inspect.signature(MercadoPagoCheckout.checkout_handoff).parameters
        expected = [name for name in real if name != "self"]

        for relative, pattern in documented.items():
            path = root / relative
            if not path.exists():  # running against an installed wheel
                self.skipTest(f"{relative} is not part of the distribution")
            match = pattern.search(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(match, f"no signature block found in {relative}")
            names = [
                name
                for name in (
                    line.strip().rstrip(",").split(":")[0].split("=")[0].strip()
                    for line in match.group("params").splitlines()
                )
                if name and name != "*"
            ]
            self.assertEqual(names, expected, f"{relative} is out of date")
            self.assertRegex(
                match.group("params"),
                r"(?m)^\s*recovering(?:\s*:\s*bool)?\s*=\s*False,\s*$",
                f"{relative} must document recovering=False",
            )

        readme = (root / "README.md").read_text(encoding="utf-8")
        wrapper = re.search(
            r"```python\n"
            r"class MyBackend\(StorefrontBackend\):.*?"
            r"return await self\.mercadopago\.checkout_handoff\(\n"
            r"(?P<call>.*?)\n"
            r"\s*\)\n"
            r"```",
            readme,
            re.DOTALL,
        )
        self.assertIsNotNone(
            wrapper, "no published MyBackend wrapper found in README.md"
        )
        self.assertRegex(
            wrapper.group("call"),
            r"(?m)^\s*recovering=attempt\.outcome_unknown,\s*$",
            "README.md must forward the persisted recovery state",
        )

    def test_no_sanitized_refusal_is_raised_from_inside_an_except(self):
        """Structural guard: this class of leak has now appeared twice.

        Both `_refused` and `_raise_outcome_unknown` raise the sanitized exception, and
        the interpreter attaches whatever is being handled to `__context__` when that
        happens inside an `except`. Reviewing each new call site by eye is what failed,
        so the module is checked as a whole instead.
        """
        source = inspect.getsource(checkout_module)
        raising = {"_refused", "_raise_outcome_unknown"}
        offenders = []

        class _Visitor(ast.NodeVisitor):
            def __init__(self):
                self.handlers = 0

            def visit_ExceptHandler(self, node):
                self.handlers += 1
                self.generic_visit(node)
                self.handlers -= 1

            def visit_Call(self, node):
                name = getattr(node.func, "attr", None) or getattr(
                    node.func, "id", None
                )
                if name in raising and self.handlers:
                    offenders.append((name, node.lineno))
                self.generic_visit(node)

        _Visitor().visit(ast.parse(source))
        self.assertEqual(
            offenders,
            [],
            "record the reason inside the handler and refuse after it returns",
        )

    async def test_a_key_the_http_stack_would_reject_is_refused_locally(self):
        """Accepting it would turn a caller mistake into "an Order may exist".

        These two shapes never reach Mercado Pago: `requests` refuses a header value
        with leading whitespace, and `http.client` encodes header values as latin-1.
        Both failures arrive as exceptions the adapter would read as a lost response —
        so it would retry and then report an indeterminate outcome for a request that
        never left the process.
        """
        for key in (" operation-1", "operation-\u65e5"):
            with self.subTest(idempotency_key=key):
                with self.assertRaises(Exception):
                    prepared = requests.Request(
                        "POST",
                        "https://api.mercadopago.com/v1/orders",
                        headers={"x-idempotency-key": key},
                    ).prepare()
                    prepared.headers["x-idempotency-key"].encode("latin-1")

                await self._refuses_key_without_calling_the_api(key)

    async def test_the_key_contract_is_narrower_than_the_http_stack(self):
        """Deliberately stricter than `requests`, which would transmit these.

        A key is an opaque operation identifier that the host persists and replays; the
        shapes below survive one round trip and then differ after any normalization
        that trims or collapses whitespace, which is how a retry silently becomes a
        second purchase. Visible ASCII removes the question.
        """
        for key in ("operation-1 ", "op\tone", "opera\u00e7\u00e3o-1"):
            with self.subTest(idempotency_key=key):
                # These do reach the wire, so the narrowing is ours, not the stack's.
                prepared = requests.Request(
                    "POST",
                    "https://api.mercadopago.com/v1/orders",
                    headers={"x-idempotency-key": key},
                ).prepare()
                prepared.headers["x-idempotency-key"].encode("latin-1")

                await self._refuses_key_without_calling_the_api(key)

    async def test_http_423_is_a_locked_key_rather_than_a_rejection(self):
        """423 says a request for this key is still in flight, not that none exists.

        The concurrent request it refers to may already have created a payable Order,
        so this is the one 4xx that must never release the host fallback.
        """
        recorder = _Recorder(status=423, response={"error": "resource_locked"})
        sdk = mock.MagicMock()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()), sdk=sdk)

        with self.assertRaises(CheckoutOutcomeUnknown) as captured:
            await self._handoff(
                checkout,
                _Session(),
                _Cart(_Line("sku1")),
                idempotency_key="operation-1",
            )

        self.assertEqual(captured.exception.reason, "resource_locked")
        self.assertEqual(captured.exception.idempotency_key, "operation-1")
        self.assertEqual(captured.exception.external_reference, EXTERNAL_REFERENCE)
        # Nothing to clean up: we never learned of an Order to cancel.
        self.assertIsNone(captured.exception.order_id)
        sdk.order.return_value.cancel.assert_not_called()

    async def test_http_timeout_or_server_error_blocks_the_fallback(self):
        for status in (408, 500, 503):
            with self.subTest(status=status):
                recorder = _Recorder(status=status, response={"error": "unavailable"})
                checkout = self._checkout(
                    recorder, catalog=_Catalog(sku1=_Record())
                )

                with self.assertRaises(CheckoutOutcomeUnknown) as captured:
                    await self._handoff(
                        checkout,
                        _Session(), _Cart(_Line("sku1")), idempotency_key="operation-1"
                    )

                self.assertEqual(captured.exception.reason, "ambiguous_response")
                self.assertEqual(captured.exception.idempotency_key, "operation-1")

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
            await self._handoff(checkout, _Session(), _Cart(_Line("sku1")))

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

        self.assertEqual(await self._handoff(checkout, _Session(), _Cart()), [])
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

    def test_indeterminate_exception_keeps_recovery_ids_out_of_its_message(self):
        key = "private-host-operation-id"
        reference = "seller-order-private"
        error = CheckoutOutcomeUnknown(
            idempotency_key=key,
            external_reference=reference,
            order_id="ORD-private",
            reason="transport_failure",
        )

        self.assertEqual(error.idempotency_key, key)
        self.assertEqual(error.external_reference, reference)
        self.assertEqual(error.order_id, "ORD-private")
        for sensitive in (key, reference, "ORD-private"):
            self.assertNotIn(sensitive, str(error))
            self.assertNotIn(sensitive, repr(error))

    def test_indeterminate_exception_requires_an_external_reference(self):
        with self.assertRaises(TypeError):
            CheckoutOutcomeUnknown(
                idempotency_key="private-host-operation-id",
                reason="transport_failure",
            )


class HandoffTypeTest(unittest.TestCase):
    def test_repr_redacts_the_complete_checkout_url(self):
        handoff = CheckoutHandoff(url=CHECKOUT_URL, label="Pay")

        self.assertNotIn(CHECKOUT_URL, repr(handoff))
        self.assertEqual(
            handoff.model_dump(exclude_none=True),
            {"url": CHECKOUT_URL, "label": "Pay"},
        )

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
