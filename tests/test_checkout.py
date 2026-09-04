# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Unit tests. No network: the SDK's ``preference().create`` is replaced and the calls
it would have made are asserted against instead.

The commerce-agents types are faked here rather than imported, which is the point of
``types.py`` — the package under test must work with nothing from Anthropic's
repository installed. ``tests/test_contract.py`` covers the real thing.
"""

import inspect
import unittest
from datetime import datetime, timezone
from unittest import mock

from mercadopago.config import RequestOptions

from mercadopago_commerce_agents import CheckoutHandoff, MercadoPagoCheckout
from mercadopago_commerce_agents import checkout as checkout_module

TOKEN = "APP_USR-test-token"  # not a credential: no request leaves the process
INIT_POINT = "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=1"


class _Line:
    def __init__(self, product_id, title="Cart title", price=100.0, quantity=1):
        self.product_id = product_id
        self.title = title
        self.price = price
        self.quantity = quantity


class _Cart:
    def __init__(self, *items):
        self.items = list(items)


class _Session:
    def __init__(self, session_id="session-from-a-raw-header"):
        self.session_id = session_id


class _Record:
    def __init__(self, title="Catalog title", price=100.0, in_stock=True):
        self.title = title
        self.price = price
        self.in_stock = in_stock


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
        self.response = {"id": "pref-1", "init_point": INIT_POINT} if response is None else response
        self.status = status
        self.calls = []

    def create(self, body, request_options=None):
        self.calls.append((body, request_options))
        return {"status": self.status, "response": self.response}

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
        sdk.preference.return_value.create = recorder.create
        return MercadoPagoCheckout(sdk=sdk, **kwargs)

    # -- the Critical finding: the charge must not come from the cart --------------

    async def test_charges_the_catalog_price_not_the_cart_price(self):
        """A cart line priced at 0.01 for a product the catalog sells at 100 must be
        charged at 100. This is the whole reason `catalog` exists."""
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=100.0))
        checkout = self._checkout(recorder, catalog=catalog)

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", price=0.01))
        )

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(recorder.body["items"][0]["unit_price"], 100.0)

    async def test_uses_the_catalog_title_not_the_model_authored_one(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(title="Catalog title"))
        checkout = self._checkout(recorder, catalog=catalog)

        await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1", title="<model authored>"))
        )

        self.assertEqual(recorder.body["items"][0]["title"], "Catalog title")

    def test_requires_a_catalog_at_construction(self):
        """There is no state in which the adapter can fall back to cart prices."""
        with self.assertRaises(TypeError):
            self._checkout(_Recorder())

    def test_constructor_keeps_only_the_intended_configuration_surface(self):
        parameters = inspect.signature(MercadoPagoCheckout).parameters
        self.assertEqual(list(parameters), ["sdk", "catalog", "reference_store"])

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

    async def test_prices_every_line_from_the_catalog(self):
        recorder = _Recorder()
        catalog = _Catalog(sku1=_Record(price=10.0), sku2=_Record(price=20.5))
        checkout = self._checkout(recorder, catalog=catalog)

        await checkout.checkout_handoff(
            _Session(),
            _Cart(_Line("sku1", price=0.01, quantity=2), _Line("sku2", price=0.01)),
        )

        prices = [item["unit_price"] for item in recorder.body["items"]]
        self.assertEqual(prices, [10.0, 20.5])
        self.assertEqual(recorder.body["items"][0]["quantity"], 2)
        self.assertEqual(catalog.looked_up, ["sku1", "sku2"])

    async def test_leaves_currency_to_mercado_pago(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertNotIn("currency_id", recorder.body["items"][0])

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

    async def test_reference_store_gets_the_reference_and_the_session(self):
        recorder = _Recorder()
        stored = []

        async def store(reference, session_id):
            stored.append((reference, session_id))

        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record()), reference_store=store
        )
        await checkout.checkout_handoff(_Session("s-1"), _Cart(_Line("sku1")))

        self.assertEqual(stored, [(recorder.body["external_reference"], "s-1")])

    async def test_reference_store_failure_prevents_an_uncorrelated_payment(self):
        recorder = _Recorder()

        async def store(_reference, _session_id):
            raise RuntimeError("do not expose this callback detail")

        checkout = self._checkout(
            recorder, catalog=_Catalog(sku1=_Record()), reference_store=store
        )
        with self.assertLogs(checkout_module.logger, "ERROR") as captured:
            handoffs = await checkout.checkout_handoff(
                _Session("s-1"), _Cart(_Line("sku1"))
            )

        self.assertEqual(handoffs, [])
        self.assertEqual(recorder.calls, [])
        self.assertNotIn("callback detail", "\n".join(captured.output))

    # -- the remaining hardening --------------------------------------------------

    async def test_rejects_an_init_point_outside_mercadopago(self):
        """`init_point` is rendered to the shopper as the official payment button, so a
        response pointing anywhere else is dropped rather than handed over."""
        recorder = _Recorder(response={"id": "p", "init_point": "https://evil.example/pay"})
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_rejects_a_plain_http_init_point(self):
        recorder = _Recorder(
            response={"id": "p", "init_point": "http://www.mercadopago.com.br/checkout"}
        )
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        with self.assertLogs(checkout_module.logger, "ERROR"):
            handoffs = await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertEqual(handoffs, [])

    async def test_idempotency_key_is_stable_for_the_same_cart(self):
        """A retrying or looping agent must not mint a second payable link for one cart."""
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session("s"), _Cart(_Line("sku1")))
        first = recorder.headers["x-idempotency-key"]
        first_reference = recorder.body["external_reference"]
        await checkout.checkout_handoff(_Session("s"), _Cart(_Line("sku1")))
        second = recorder.headers["x-idempotency-key"]
        second_reference = recorder.body["external_reference"]

        self.assertEqual(first, second)
        self.assertEqual(first_reference, second_reference)

    async def test_idempotency_key_changes_with_the_cart(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session("s"), _Cart(_Line("sku1", quantity=1)))
        first = recorder.headers["x-idempotency-key"]
        await checkout.checkout_handoff(_Session("s"), _Cart(_Line("sku1", quantity=2)))
        second = recorder.headers["x-idempotency-key"]

        self.assertNotEqual(first, second)

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

    async def test_preference_expires(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        await checkout.checkout_handoff(_Session(), _Cart(_Line("sku1")))

        self.assertTrue(recorder.body["expires"])
        expiry = datetime.fromisoformat(recorder.body["expiration_date_to"])
        self.assertGreater(expiry, datetime.now(timezone.utc))

    async def test_uses_the_host_default_checkout_label(self):
        recorder = _Recorder()
        checkout = self._checkout(recorder, catalog=_Catalog(sku1=_Record()))

        handoffs = await checkout.checkout_handoff(
            _Session(), _Cart(_Line("sku1"))
        )

        self.assertIsNone(handoffs[0].label)

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
        checkout = self._checkout(_Recorder(), catalog=_Catalog())
        self.assertNotIn(TOKEN, repr(checkout))


class HandoffTypeTest(unittest.TestCase):
    def test_model_dump_matches_what_enrich_checkout_asks_for(self):
        dumped = CheckoutHandoff(url=INIT_POINT, label="Pay").model_dump(exclude_none=True)
        self.assertEqual(dumped, {"url": INIT_POINT, "label": "Pay"})

    def test_model_dump_tolerates_extra_pydantic_keywords(self):
        handoff = CheckoutHandoff(url=INIT_POINT)
        self.assertEqual(handoff.model_dump(exclude_none=True, mode="json"), {"url": INIT_POINT})


if __name__ == "__main__":
    unittest.main()
