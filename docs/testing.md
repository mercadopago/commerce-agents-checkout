# Testing

## Unit tests

The default suite has no network access. It replaces `sdk.order().create` and verifies
the body, attempt-scoped idempotency header, price/currency/stock confirmation, bounded
quantity conversion, Order snapshot persistence, URL allowlist, failure paths, and log
redaction.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

## commerce-agents contract test

The package owns a structural `CheckoutHandoff` type because Anthropic does not publish
`shopping-agent-core`. Validate the real consumer at the commit pinned in
`.github/workflows/ci.yml`:

```bash
git clone https://github.com/anthropics/commerce-agents.git /tmp/commerce-agents
git -C /tmp/commerce-agents checkout fd4d59224ab96b43c6dc6888207c67b3bd5a24cf
.venv/bin/pip install \
  /tmp/commerce-agents/commerce-common \
  /tmp/commerce-agents/shopping-agent/core
REQUIRE_COMMERCE_AGENTS=1 \
  .venv/bin/python -m unittest tests.test_contract -v
```

Use a disposable path of your choice instead of `/tmp/commerce-agents` when necessary.

## Integration example

`examples/seller_integration.py` is the readable one: a catalog, a backend, the wiring and
a sketch of the webhook reconciliation. It prints the wiring with no credentials, and
creates one order with `--create` and a test-seller token.

## Real Orders API and Checkout Pro test

This test performs an external write: it creates one Mercado Pago test order for
`10.00`, reads that order back, and prints its hosted URL. Use a test seller and buyer created under the **same application** and country —
a buyer from another application is refused with "one of the parties is a test user".
Never use a real seller account, production credential, or real buyer data. Follow the
[official integration-test guide](https://www.mercadopago.com.pe/developers/en/docs/checkout-pro-orders/integration-test-introduction)
because credential availability can vary by account rollout.

```bash
export MERCADOPAGO_TEST_ACCESS_TOKEN='seller-test-access-token'
export MERCADOPAGO_TEST_CURRENCY='BRL'
export MERCADOPAGO_LIVE_TEST_CONFIRM='create-order'
.venv/bin/python examples/live_checkout.py
```

Expected evidence:

1. The command prints exactly one order ID beginning with the Orders identifier used by
   the API.
2. `sdk.order().get(order_id)` returns HTTP 200 and the same ID.
3. Opening the URL displays the Mercado Pago hosted Checkout Pro page and the expected
   item/amount.
4. The order carries `integration_data.platform_id`.
5. The URL uses HTTPS and a Mercado Pago hostname.

The script does **not** cover these; verify them in a host that persists state, or as
separate manual steps:

- replaying the same `idempotency_key` with the same cart and confirming Mercado Pago
  returns the same Order rather than a second one;
- persisting the Order snapshot and matching it to `external_reference_for(key)`.

With Orders API, this validation creates an **order** and returns `checkout_url`. A
preference and `init_point` belong to the legacy Preferences API and are not expected.

The order expires after `P1D`. Do not paste the token or complete checkout URL into
public issues or CI logs.

## Distribution checks

```bash
.venv/bin/pip install pylint isort build twine
.venv/bin/pylint --max-line-length=100 src/mercadopago_commerce_agents examples
.venv/bin/isort --check-only --diff src tests examples
.venv/bin/python -m build
.venv/bin/twine check dist/*
```
