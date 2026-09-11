# Mercado Pago Checkout Pro for commerce-agents

`mercadopago-commerce-agents-checkout` is an independent Mercado Pago integration that
helps a seller backend implement the `StorefrontBackend` handoff from
[`anthropics/commerce-agents`](https://github.com/anthropics/commerce-agents). It creates a
Checkout Pro Order and returns its validated hosted payment link.

**Not affiliated with, endorsed by, or maintained by Anthropic.** Wire the adapter into
the backend you already implement for the agent:

```python
class MyBackend(StorefrontBackend):
    def __init__(self, token, attempt_store):
        self.mercadopago = MercadoPagoCheckout(sdk=mercadopago.SDK(token), catalog=self)
        self.attempt_store = attempt_store  # implemented durably by the seller

    async def checkout_handoff(self, session, cart):
        attempt = await self.attempt_store.get_or_create(session, cart)
        return await self.mercadopago.checkout_handoff(
            session,
            cart,
            external_reference=attempt.external_reference,
            idempotency_key=attempt.idempotency_key,
        )
```

commerce-agents still calls the backend method with exactly two arguments. The backend
must create and durably persist both identifiers before calling the inner adapter, then
reuse that same pair for every retry of the confirmed purchase. What you get from the
package:

- **The model never decides the price.** Every line is re-read from your own catalog
  before the order is created — the cart is filled by an LLM's tool calls, so its prices
  are treated as a claim to verify, not a fact. A cart edited to `0.01` does not become a
  payable link.
- **The model never sees the payment URL.** commerce-agents fills it in after the tool
  call, and this package hands it back validated against Mercado Pago's own hosts.
- **Nothing extra to deploy inside the library.** It has no webhook server, database or
  background job. Your backend still persists the attempt, reconciles the Order and owns
  the webhook state transition.

## Requirements

- Python 3.11 or newer.
- A [Mercado Pago application](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/create-application)
  and its backend [Access Token](https://www.mercadopago.com/developers/en/docs/your-integrations/credentials).
- A `StorefrontBackend` implementation that can resolve every cart line from a trusted
  catalog, including the currency each record is priced in.
- `mercadopago` Python SDK 3.5.0 or newer.

## Compatibility

The contract suite validates this adapter against
[`anthropics/commerce-agents` commit `fd4d592`](https://github.com/anthropics/commerce-agents/commit/fd4d59224ab96b43c6dc6888207c67b3bd5a24cf).
The upstream packages are needed only for that compatibility test, not at runtime. See
the [testing guide](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/testing.md)
for the reproducible command.

## Install

Install from PyPI:

```bash
pip install mercadopago-commerce-agents-checkout
```

The distribution name and the import name differ on purpose — the distribution is
scoped to this checkout adapter, while the import package is the one commerce-agents
hosts already reference:

```python
from mercadopago_commerce_agents import MercadoPagoCheckout  # not ..._checkout
```

For local development or unreleased changes, install from a checkout of this repository:

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
    def __init__(self, attempt_store):
        # In production, load this value from your secrets manager.
        sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
        self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self)
        self.attempt_store = attempt_store  # implemented durably by the seller

    async def checkout_handoff(self, session, cart):
        # commerce-agents calls this method with only session and cart.
        attempt = await self.attempt_store.get_or_create(session, cart)
        return await self.mercadopago.checkout_handoff(
            session,
            cart,
            external_reference=attempt.external_reference,
            idempotency_key=attempt.idempotency_key,
        )
```

`MyBackend` is the `StorefrontBackend` commerce-agents already requires you to write —
this adds one method to it. For a complete file you can read top to bottom and run,
including what to do with the webhook afterwards, see
[`examples/seller_integration.py`](https://github.com/mercadopago/commerce-agents-checkout/blob/main/examples/seller_integration.py):

```bash
python examples/seller_integration.py            # print the wiring and exit
python examples/seller_integration.py --create   # create one order against a test seller
python examples/seller_integration.py --create --show-sensitive-output  # interactive only
```

Run those from a clone or the sdist — `examples/` ships in the source archive, not in the
wheel.

The adapter surface is two constructor arguments and two required keyword-only
identifiers per call:

```text
MercadoPagoCheckout(*, sdk, catalog)
checkout_handoff(session, cart, *, external_reference, idempotency_key)
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
    session,
    cart,
    external_reference=attempt.external_reference,
    idempotency_key=attempt.idempotency_key,
)
```

- It is required, validated, and used exactly as given. An invalid value is refused; the
  adapter never quietly mints a replacement, because that would turn a rejected duplicate
  into a second payable order.
- Keys are limited to 64 characters by the supported Mercado Pago SDK.
- One key means one operation. A new purchase needs a new key, and Mercado Pago answers a
  reused key carrying a different payload with `HTTP 409 idempotency_key_already_used`.
- The key is never derived from the session id, an email, or any personal data.
- Prefer a high-entropy key and never derive it from shopper PII or the session ID.

Your backend must create and persist the idempotency key together with the seller reference
before the first call. Idempotency across calls, processes or restarts means supplying that
same pair again. The complete seller example keeps one active attempt per session and
confirmed-cart fingerprint, preserves A → B → A, caches a successful handoff, and
explicitly closes the attempt only after a terminal webhook. As a missed-webhook backstop,
it sets a reconciliation deadline slightly beyond the Order's `P1D` window and blocks
after that deadline until the host verifies a terminal state. It never rotates identifiers
because time passed: a clock is not proof that no payable Order remains. Use a durable
table rather than its in-memory dictionary.

If a create or cleanup POST may have taken effect but cannot be confirmed, the package
raises `CheckoutOutcomeUnknown` rather than returning `[]`. Its `idempotency_key` lets
the host retry the same operation; `external_reference` contains the required seller
reference, and `order_id` is populated only when failed cleanup had already identified the
Order. Do not catch it as an ordinary fallback: another checkout could leave two payable
paths. Recovery identifiers are never included in the exception message or adapter logs.

### Seller order reference

`external_reference` is the required seller correlation identifier, independent from the
idempotency key. Create and persist it with the checkout attempt before the first call. An
ecommerce order number is valid and does not need to be a UUID:

```python
handoffs = await checkout.checkout_handoff(
    session,
    cart,
    external_reference=str(seller_order_id),
    idempotency_key=operation_key,
)
```

The adapter accepts 1–64 letters, digits, hyphens and underscores. It does not derive this
business identifier from the idempotency key, session, or shopper data. Persist an opaque
seller Order identifier, never an email, session ID, or other shopper PII.

The published Orders API reference and the supported Python SDK currently describe
`external_reference` as optional. However, an opt-in live Checkout Pro Orders request made
during integration validation without this field returned HTTP 400 with
`required_properties` and identified `external_reference` as missing. This package
therefore requires it as a stricter invariant for the validated Checkout Pro path and for
reconciliation. That observation is specific to the tested path; it is not a claim that
every Orders product or account rejects omission.

### Out of scope

Webhook handling, persistence, Order reconciliation, fulfillment and payment confirmation
belong to your backend. This package creates one Order and returns one validated URL; it
stores nothing and calls nothing back.

## How it talks to Mercado Pago

`POST /v1/orders` with `processing_mode=manual`, the only processing mode supported for
Checkout Pro. The public resource to look up, cancel or reconcile is the **Order** and the
redirect is the returned `checkout_url`; this flow never calls the Preferences API.

## What happens during `checkout_handoff`

1. Reject an empty cart or more than 20 lines, and freeze the cart's lines,
   quantities and currency before anything is awaited. Note that these caps are tighter
   than commerce-agents' own defaults (`max_cart_lines=100`, `max_quantity_per_item=24`):
   configure the upstream gates to 20/10 or a cart valid upstream will silently fall back
   to your own checkout.
2. Validate the required idempotency key and seller `external_reference`. Both must have
   been persisted by the host before the call.
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
   - the seller's required `external_reference`
   - `integration_data` carrying this adapter's Platform ID
7. Send the request through `sdk.order().create(...)` with that key as
   `X-Idempotency-Key`.
8. Validate the returned Order type, processing mode, initial status, ID, amount,
   currency, required seller reference, and the HTTPS checkout URL; then return one
   `CheckoutHandoff`.
9. If that validation fails, cancel the order with its own deterministic idempotency key.
   Return `[]` only after cancellation is confirmed; otherwise raise
   `CheckoutOutcomeUnknown` and require reconciliation.

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
| **Order webhook** | **How you learn a shopper actually paid.** This library returns a URL and stops; nothing here polls or notifies. Configure the notification on the application, then validate the `x-signature` header, deduplicate the event and re-fetch the order before trusting it. | [Webhooks and signature validation](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks) · [IPN, the older mechanism](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/ipn) |
| Test users | A test seller and a test buyer must belong to the **same application**, or the hosted page refuses with "one of the parties is a test user". | [Test accounts](https://www.mercadopago.com/developers/en/docs/your-integrations/test/accounts) |
| Test cards | Completing a payment on the hosted page during integration testing. | [Test cards](https://www.mercadopago.com/developers/en/docs/your-integrations/test/cards) · [Integration test guide](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/integration-test-introduction) |

### Checkout options this package does not send

The order it creates always carries the items, the amount, the required seller reference,
and a 24-hour expiry. The options below are supported by the Orders API but are **not**
exposed here, so the account defaults apply. If a seller needs them, that is a scope
decision to make deliberately, not something to discover in production:

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
then validate `x-signature`, deduplicate the event, and associate its Order with exactly
one persisted checkout attempt before changing local state. Match the required
`external_reference` to the attempt, then fetch `/v1/orders/{id}` and compare the
authoritative amount and currency. Amount, currency, browser redirects, and query
parameters are never correlation keys or payment evidence.

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

`tests/test_contract.py` is skipped unless commerce-agents is installed. The documented
contract command installs the exact pinned upstream commit and makes the test mandatory.
See the
[testing guide](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/testing.md)
for both the pinned test and the opt-in real API test.

## Real Checkout Pro test

The repository includes an explicit, opt-in script that creates one test order and reads
it back through Orders API. The complete hosted URL is withheld by default:

```bash
export MERCADOPAGO_TEST_ACCESS_TOKEN='seller-test-access-token'
export MERCADOPAGO_TEST_CURRENCY='BRL'
export MERCADOPAGO_LIVE_TEST_CONFIRM='create-order'
.venv/bin/python examples/live_checkout.py
```

That mode also replays the same idempotency key and external reference and verifies that
the original Order is returned. The Order ID and URL are withheld by default. To display
them for a manual browser check, run from an interactive terminal with a second explicit
opt-in:

```bash
export MERCADOPAGO_LIVE_SHOW_CHECKOUT_URL=1
.venv/bin/python examples/live_checkout.py
```

The script refuses to reveal it when stdout is redirected, piped, or captured by CI. To
create one deliberately refused Order and prove its cleanup through a GET:

```bash
export MERCADOPAGO_LIVE_TEST_CONFIRM='verify-cancellation'
.venv/bin/python examples/live_checkout.py
```

Do not commit the token or paste it into tickets, logs, or chat. The generated order
expires after `P1D`. When explicitly displayed, open the URL in a browser to verify that
it reaches the Mercado Pago hosted checkout. With Orders API, the expected resource is
an **order**, not a preference. Follow Mercado Pago's
[integration-test guide](https://www.mercadopago.com.pe/developers/en/docs/checkout-pro-orders/integration-test-introduction)
to obtain the seller test credential and buyer account exposed for your application.

## Troubleshooting

Definitive refusals return `[]` so the host's own checkout takes over, and the reason is
logged under the `mercadopago_commerce_agents.checkout` logger. An outcome that may have
created or left an Order raises `CheckoutOutcomeUnknown` and must block that fallback.
Enable the logger at `ERROR` and `WARNING` to observe both paths.

| What you see | What it usually means |
|---|---|
| `Order creation failed (HTTP 403)` with `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` | Mercado Pago rejected the account under its current policies; verify the account status and contact Mercado Pago support if it remains blocked. It is not an Orders-API enablement requirement. |
| `Order creation failed (HTTP 403)` with `forbidden` | The application does not have the permissions/scopes required for the operation. Verify the application and credential configuration. See [Orders integration errors](https://www.mercadopago.com.br/developers/pt/docs/checkout-api-orders/payment-management/integration-errors). |
| `CheckoutOutcomeUnknown` after HTTP 409 | The key already identifies an Order but the adapter cannot safely prove which checkout the caller intended. Stop fallback and determine whether this is a retry or a new purchase by reconciling the persisted key and seller reference. |
| `CheckoutOutcomeUnknown` | A create or cleanup POST may have taken effect. Stop fallback and reuse the exception's `idempotency_key` and `external_reference` for controlled recovery. `order_id` is available only when cleanup had already identified the Order. |
| `Order creation failed (HTTP 400)` | The request reached the account but failed schema validation. The logged `causes` are Mercado Pago's own codes; look them up in the Orders API reference. |
| Handoff returns `[]` right after `Mercado Pago returned an order that did not match the snapshot` | The catalog's currency is not the seller account's own. Mercado Pago accepts the order and creates it in the account's currency (it is never sent), so the mismatch is only caught on the response — the adapter then cancels that order and falls back. Price the catalog in the account's currency (BRL for MLB, ARS for MLA, ...). |
| `Refusing to create an order: currency_mismatch` | The catalog records disagree with each other or with the cart — rejected locally, before any API call. |
| `Refusing to create an order: cart_reconfirmation_required` | The catalog price moved after the shopper confirmed. Refresh the cart and ask for confirmation again; the library will not silently charge the new amount. |
| `Refusing to create an order: invalid_catalog_record` | `get_product_details` returned something without `title`, `price`, `currency` or `in_stock` — returning a `dict` instead of an object is the usual cause. |
| `Refusing to create an order: missing_cart_currency` | The cart object has no `currency` attribute at all. |
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
- [Security responsibilities and review checklist](https://github.com/mercadopago/commerce-agents-checkout/blob/main/docs/security.md)

## License

Apache License 2.0. See
[LICENSE](https://github.com/mercadopago/commerce-agents-checkout/blob/main/LICENSE) and
[NOTICE](https://github.com/mercadopago/commerce-agents-checkout/blob/main/NOTICE).
