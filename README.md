# Mercado Pago Checkout Pro for commerce-agents

[Anthropic's commerce-agents](https://github.com/anthropics/commerce-agents) is a shopping
agent built on Claude: a customer talks to it, it searches your catalog and fills a cart.
It deliberately stops short of taking money — its `checkout` tool, in Anthropic's words,
"renders the cart for the host to complete".

**This package completes it with Mercado Pago.** Wire it into the backend you already
implement for the agent, and the conversation ends with a real Checkout Pro payment link:

```python
class MyBackend(StorefrontBackend):
    def __init__(self):
        self.mercadopago = MercadoPagoCheckout(sdk=mercadopago.SDK(token), catalog=self)

    async def checkout_handoff(self, session, cart):
        return await self.mercadopago.checkout_handoff(session, cart)
```

That is the whole integration: two arguments and one method. What you get for it:

- **The model never decides the price.** Every line is re-read from your own catalog
  before the order is created — the cart is filled by an LLM's tool calls, so its prices
  are treated as a claim to verify, not a fact. A cart edited to `0.01` does not become a
  payable link.
- **The model never sees the payment URL.** commerce-agents fills it in after the tool
  call, and this package hands it back validated against Mercado Pago's own hosts.
- **Nothing to run or store.** No webhook server, no database, no background job inside
  this library — it creates one order and returns one URL.

> **Pre-release repository.** Not yet published to PyPI. The Orders API flow has been
> exercised end to end against the real API — an order is created, read back, and its
> hosted Checkout Pro URL opens — but completing a payment on that hosted page and the
> WebSec review are still open. Use dedicated test users until both are done.

## Requirements

- Python 3.11 or newer.
- A Mercado Pago application and a backend Access Token.
- A `StorefrontBackend` implementation that can resolve every cart line from a trusted
  catalog, including the currency each record is priced in.
- `mercadopago` Python SDK 3.5.0 or newer.

## Install

After the first PyPI release:

```bash
pip install mercadopago-commerce-agents-checkout
```

The distribution name and the import name differ on purpose — the distribution is
scoped to this checkout adapter, while the import package is the one commerce-agents
hosts already reference:

```python
from mercadopago_commerce_agents import MercadoPagoCheckout  # not ..._checkout
```

Until then, install from a checkout of this repository:

```bash
python -m pip install -e .
```

At runtime the package needs the official
[`mercadopago`](https://pypi.org/project/mercadopago/) SDK, plus `requests` and security
floors for its HTTP stack (`certifi`, `idna`, `urllib3`) — see `pyproject.toml`. What it
deliberately does **not** depend on is anything from Anthropic: `shopping-agent-core` is
intentionally unpublished, so install commerce-agents from Anthropic's own repository.

## The API

The snippet at the top with its imports, and where the token comes from:

```python
import os

import mercadopago

from mercadopago_commerce_agents import MercadoPagoCheckout
from shopping_agent import StorefrontBackend


class MyBackend(StorefrontBackend):
    def __init__(self):
        # In production, load this value from your secrets manager.
        sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
        self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self)

    async def checkout_handoff(self, session, cart):
        return await self.mercadopago.checkout_handoff(session, cart)
```

`MyBackend` is the `StorefrontBackend` commerce-agents already requires you to write —
this adds one method to it. The public surface is two constructor arguments and one
per-call option:

```text
MercadoPagoCheckout(*, sdk, catalog)
checkout_handoff(session, cart, *, idempotency_key=None)
```

- `sdk`: an already configured official Mercado Pago SDK instance. It owns credentials,
  timeouts, retries and headers; the adapter clones its `RequestOptions` per call and
  never mutates the shared instance.
- `catalog`: an object exposing the async `get_product_details(session, product_id)`
  method, returning a record with `title`, `price`, `currency`, and `in_stock is True`.
  A `StorefrontBackend` can pass itself.

There is no `currency` argument. The trusted catalog is already the authority for price,
so it is the authority for currency too: every record must agree with the others and with
the cart, and the created Order is checked against that same value. The currency is never
sent — Mercado Pago resolves it from the seller account.

The package does not read environment variables, and it does not identify itself to
Mercado Pago through configuration: the adapter's Platform ID travels on every order
automatically.

### Idempotency

`idempotency_key` identifies one checkout operation:

```python
handoffs = await checkout.checkout_handoff(
    session, cart, idempotency_key=request_idempotency_key
)
```

- Omitted, a UUID v4 is generated for that call and its internal retries.
- Supplied, it is validated and used exactly as given. An invalid value is refused; the
  adapter never quietly mints a replacement, because that would turn a rejected duplicate
  into a second payable order.
- One key means one operation. A new purchase needs a new key, and Mercado Pago answers a
  reused key carrying a different payload with `HTTP 409 idempotency_key_already_used`.
- The key is never derived from the session id, an email, or any personal data.
- Prefer a high-entropy key. `external_reference` is a deterministic UUIDv5 of it, so a
  guessable key (a sequential order number, say) could be matched back to your internal
  identifier by anyone who can see the seller's Mercado Pago records.

Automatic generation only covers the current call. Idempotency across calls, processes or
restarts means your backend supplying the same key again.

### Out of scope

Webhook handling, persistence, Order reconciliation, fulfillment and payment confirmation
belong to your backend. This package creates one Order and returns one validated URL; it
stores nothing and calls nothing back.

The `external_reference` it sends is derived from the idempotency key, and the same
value is available to you without storing anything here:

```python
from mercadopago_commerce_agents import external_reference_for

reference = external_reference_for(my_key)   # "mpca-" + uuid5(NAMESPACE_URL, my_key)
```

Index your own record by that value and an Order webhook matches without any callback
from this library. It never contains the session id. A key generated internally cannot be
correlated later, because you never see it — pass your own whenever the Order has to be
reconcilable.

## How it talks to Mercado Pago

This version uses `POST /v1/orders` with `processing_mode=manual`. It never calls
`POST /checkout/preferences`, and the resource it creates — the one to look up, cancel or
reconcile — is the **order**. Mercado Pago still mints a preference behind it, visible as
the `pref_id` inside the returned `checkout_url`, but that is an implementation detail of
the hosted page.

## What happens during `checkout_handoff`

1. Reject an empty cart or more than 20 lines, and freeze the cart's lines,
   quantities and currency before anything is awaited. Note that these caps are tighter
   than commerce-agents' own defaults (`max_cart_lines=100`, `max_quantity_per_item=24`):
   configure the upstream gates to 20/10 or a cart valid upstream will silently fall back
   to your own checkout.
2. Validate the idempotency key, or generate a UUID v4 when none was given.
3. Resolve each product through the host's trusted catalog.
4. Reject unknown lines unless stock is explicitly `True`, and validate price, currency,
   and quantity with bounded inputs. Derive the currency from those records and require
   the cart to agree.
5. Compare the cart's price with the catalog. If it changed, return the fallback so the
   host refreshes the cart and asks for confirmation again.
6. Build an Orders API payload with:
   - `type: online`
   - `processing_mode: manual`
   - the order `total_amount` as a two-decimal string, and each item as `title`,
     `quantity`, and `unit_price` only
   - `expiration_time: P1D`
   - an `external_reference` derived from the idempotency key
   - `integration_data` carrying this adapter's Platform ID
7. Send the request through `sdk.order().create(...)` with that key as
   `X-Idempotency-Key`.
8. Validate the returned Order type, processing mode, initial status, ID, amount,
   currency, reference, and HTTPS checkout URL, then return one `CheckoutHandoff`.
9. If that validation fails, cancel the order before returning `[]`. It already exists at
   Mercado Pago, and leaving it would strand a payable order on the seller's account for
   the whole expiry window.

Orders created by this package carry no payer PII, no return URL and no notification
URL. The hosted checkout collects whatever it needs from the shopper.

## Why the trusted catalog is mandatory

A commerce-agents `Cart` is assembled through model tool calls. In the reference host,
the session also travels in a raw `X-Session-Id` header. Forwarding `CartItem.price`
would allow the conversation caller to choose the amount charged on the seller's
account.

This package therefore re-reads each product through
`StorefrontBackend.get_product_details` and uses the catalog title, price, currency,
explicit stock state, and the bounded quantity. The cart price is used only as proof of
what the shopper confirmed; it never overrides the catalog. With no catalog or any
drift, the package refuses to create an order.

The host must still authenticate the shopper, verify ownership of the cart, and enforce
its own inventory reservation and business rules. This library cannot turn an
unauthenticated session header into an authenticated checkout.

## What the seller configures

This package does one thing — turn a validated cart into a hosted checkout URL. Everything
below is configured on the Mercado Pago side, by the seller, and every row links to Mercado
Pago's own documentation so nobody has to take our word for it.

| What | Why you need it | Mercado Pago documentation |
|---|---|---|
| Application and Access Token | The credential this package's SDK instance carries. Its account decides the site and therefore the currency. | [Credentials](https://www.mercadopago.com/developers/en/docs/your-integrations/credentials) · [Developer panel](https://www.mercadopago.com/developers/panel/app) |
| Checkout Pro through Orders enabled | `POST /v1/orders` answers `403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES` for an account that is not authorised for it, before it even validates the payload. | [Create a Checkout Pro order](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/create-order) · [API reference](https://www.mercadopago.com.pe/developers/en/reference/online-payments/checkout-pro/create-order/post) |
| **Order webhook** | **How you learn a shopper actually paid.** This library returns a URL and stops; nothing here polls or notifies. Configure the notification on the application, then validate the `x-signature` header, deduplicate the event and re-fetch the order before trusting it. | [Webhooks and signature validation](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks) · [IPN, the older mechanism](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/ipn) |
| Test users | A test seller and a test buyer must belong to the **same application**, or the hosted page refuses with "one of the parties is a test user". | [Test accounts](https://www.mercadopago.com/developers/en/docs/your-integrations/test/accounts) |
| Test cards | Completing a payment on the hosted page during integration testing. | [Test cards](https://www.mercadopago.com/developers/en/docs/your-integrations/test/cards) · [Integration test guide](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/integration-test-introduction) |

### Checkout options this package does not send

The order it creates carries the items, the amount, an opaque reference and a 24-hour
expiry — nothing else. These are all supported by the Orders API and are **not** exposed
here, so the account defaults apply. If a seller needs them, that is a scope decision to
make deliberately, not something to discover in production:

| Not sent | Consequence today | Reference |
|---|---|---|
| `back_urls` / `auto_return` | The shopper stays on Mercado Pago's page after paying instead of returning to the store. | [Create a Checkout Pro order](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/create-order) |
| Installments (`max_installments`, interest-free ranges) | The account's default installment policy applies. | idem |
| `statement_descriptor` | What the buyer sees on the card statement is the account default. | idem |
| `shipment` (cost, address) | Shipping is not charged; the total is the sum of catalog lines only. | idem |
| `payer` details, `additional_info` | The hosted checkout collects what it needs. Richer payer data feeds Mercado Pago's fraud scoring, so omitting it can raise rejection rates. | idem |

## Confirming payment

A handoff means Mercado Pago created an order and returned a hosted checkout. It does not
mean the buyer paid. Configure the **Order** webhook on your Mercado Pago application,
then validate `x-signature`, deduplicate the event, fetch `/v1/orders/{id}`, and compare
the authoritative amount, currency and `external_reference` against what you stored for
that idempotency key before changing local state. A browser redirect is never payment
evidence.

The official SDK exposes `mercadopago.webhook.WebhookSignatureValidator`. The current
signature contract, the `x-signature` header format and the list of notification topics
live in Mercado Pago's
[Webhooks guide](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks);
subscribe to the **Order** topic for this flow. The older
[IPN](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/ipn)
mechanism is documented separately if an existing integration still uses it.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install pylint isort build twine

.venv/bin/python -m unittest discover -s tests
.venv/bin/pylint --max-line-length=100 src/mercadopago_commerce_agents examples
.venv/bin/isort --check-only --diff src tests examples
```

`tests/test_contract.py` is skipped unless commerce-agents is installed. The CI job
installs the exact pinned upstream commit and runs that contract test as a blocking
check. See the
[testing guide](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/testing.md)
for both the pinned test and the opt-in real API test.

## Real Checkout Pro test

The repository includes an explicit, opt-in script that creates one test order, reads
it back through Orders API, and prints its hosted Checkout Pro URL:

```bash
export MERCADOPAGO_TEST_ACCESS_TOKEN='seller-test-access-token'
export MERCADOPAGO_TEST_CURRENCY='BRL'
export MERCADOPAGO_LIVE_TEST_CONFIRM='create-order'
.venv/bin/python examples/live_checkout.py
```

Do not commit the token or paste it into tickets, logs, or chat. The generated order
expires after `P1D`. Open the printed URL in a browser to verify that it reaches the
Mercado Pago hosted checkout. With Orders API, the expected resource is an **order**,
not a preference. Follow Mercado Pago's
[integration-test guide](https://www.mercadopago.com.pe/developers/en/docs/checkout-pro-orders/integration-test-introduction)
to obtain the seller test credential and buyer account exposed for your application.

## Troubleshooting

Every failure returns `[]` so the host's own checkout takes over, and the reason is
logged under the `mercadopago_commerce_agents.checkout` logger. Enable it at `ERROR` and
`WARNING` to see which gate rejected the handoff.

| What you see | What it usually means |
|---|---|
| `Order creation failed (HTTP 403)` with Mercado Pago's `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` | The account behind the Access Token is not authorised for the Orders API. It is a policy decision taken before the payload is validated, so it says nothing about the request itself — check that the application has Checkout Pro through Orders enabled for that account. |
| `Order creation failed (HTTP 409)` | The idempotency key was already used with a different payload. A new purchase needs a new key. |
| `Order creation failed (HTTP 400)` | The request reached the account but failed schema validation. The logged `causes` are Mercado Pago's own codes; look them up in the Orders API reference. |
| Handoff returns `[]` right after `Order ... did not match the confirmed checkout snapshot` | The catalog's currency is not the seller account's own. Mercado Pago accepts the order and creates it in the account's currency (it is never sent), so the mismatch is only caught on the response — the adapter then cancels that order and falls back. Price the catalog in the account's currency (BRL for MLB, ARS for MLA, ...). |
| `Refusing to create an order: currency_mismatch` | The catalog records disagree with each other or with the cart — rejected locally, before any API call. |
| `Refusing to create an order: cart_reconfirmation_required` | The catalog price moved after the shopper confirmed. Refresh the cart and ask for confirmation again; the library will not silently charge the new amount. |
| `Refusing to create an order: out_of_stock` | The catalog record's `in_stock` is not boolean `True`. A truthy non-boolean (`1`, `"yes"`) fails this check on purpose. |
| `Refusing to create an order: invalid_idempotency_key` | The supplied key was empty, oversized, or non-printable. The adapter fails closed rather than generating another one. |
| The handoff succeeds but the hosted page's pay button stays disabled | This is browser-side, not the order. Mercado Pago's hosted checkout tokenizes the card in a cross-origin iframe; blocked third-party storage (`requestStorageAccessFor: Permission denied` in the console) prevents tokenization. Open the URL in a normal browser window that allows third-party cookies for Mercado Pago, and pay as a test user that is not the collector account. |

Confirm an order independently of the agent flow with the Orders API directly:

```python
sdk.order().get(order_id)  # status "created" means payable, not paid
```

## Documentation

- [Integration and payload contract](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/integration.md)
- [Local, contract, and real API testing](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/testing.md)
- [Security responsibilities and WebSec checklist](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/security.md)
- [PyPI release procedure](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/releasing.md)

## License

Apache License 2.0. See
[LICENSE](https://github.com/mercadopago/commerce-agents-checkout/blob/main/LICENSE) and
[NOTICE](https://github.com/mercadopago/commerce-agents-checkout/blob/main/NOTICE).
