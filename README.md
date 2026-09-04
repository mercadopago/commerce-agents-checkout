# commerce-agents-checkout

Mercado Pago Checkout Pro as a `checkout_handoff` provider for
[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents).

> **Staging repository.** This is the Mercado Pago organization repository, but the
> package has not been published or exercised against the real Mercado Pago API yet.
> Do not point production traffic at it. `HANDOFF-sdk-team.md` records the remaining
> work and decisions for the SDK team.

## Where this stands

Last updated 2026-09-04. Written for whoever picks this up next.

**Verified.** 26 tests pass; CI runs them on Python 3.11 and 3.12. `pylint` is
10.00/10 and `isort` is clean. The contract test runs the *real* `enrich_checkout` from
commerce-agents against this package's own `CheckoutHandoff` and passes against pinned
commit `fd4d5922` — that is what justifies not depending on `shopping-agent-core`. The
built wheel installs into a clean environment with nothing from Anthropic present.
Tested against `mercadopago` 3.5.0.

**Not verified — the real gap.** No request has ever reached the Mercado Pago API. Every
test replaces the SDK call, so the preference body here is *believed* correct, not
*known* correct. **Do this first:** run one handoff against a sandbox `TEST-` token and
confirm the body is accepted without `items[].currency_id`, along with
`expiration_date_to`'s format and `items[].id` as sent. Ideally repeat this with test
accounts from two different Mercado Pago sites. The adapter does no currency
conversion: catalog prices must already be in the seller account's currency.

**Open items.**

1. **The package name is provisional and not registered on PyPI.** Confirm the final
   distribution name, then claim it under a Mercado Pago PyPI **organization**, never
   a personal account.
2. **Webhook verification is not in this package.** Configure the webhook URL on the
   Mercado Pago application. Verifying `x-signature` and re-fetching the payment
   server-side is the host's job today. If this package should own it, that is a
   deliberate scope decision to make.

**Two choices made here that the SDK team may overrule**, both flagged in the hand-off:
Trusted Publishing (OIDC) instead of a long-lived `PYPI_TOKEN`, and Apache-2.0 where the
SDK is MIT.

commerce-agents' shopping agent deliberately stops before payment: its `checkout` tool
renders the cart, and the hosted checkout URL is filled in by the backend *after* the
model's tool call, so the URL never reaches the model. This package fills that one
method with a real Checkout Pro preference.

## Install

```bash
# After the first PyPI release:
pip install mercadopago-commerce-agents
```

That is the whole install. This package depends only on the official
[`mercadopago`](https://pypi.org/project/mercadopago/) SDK — nothing from Anthropic's
repository, which is why it installs from PyPI on its own. (You still get
commerce-agents itself the way Anthropic ships it: from a clone. They do not publish
`shopping-agent-core`, on purpose.)

## Use

```python
import os

import mercadopago

from mercadopago_commerce_agents import MercadoPagoCheckout
from shopping_agent import StorefrontBackend

class MyBackend(StorefrontBackend):
    def __init__(self):
        # `catalog=self` is what makes the charge trustworthy — see below.
        sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
        self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self)

    async def checkout_handoff(self, session, cart):
        return await self.mercadopago.checkout_handoff(session, cart)
```

If your application keeps credentials in environment variables, it needs:

```bash
MERCADOPAGO_ACCESS_TOKEN=APP_USR-...
```

The package itself does not read the environment. Your application may source the token
from an environment variable, a secrets manager, or dependency injection and configures
the official SDK once. That SDK is the single source of truth for credentials,
timeouts, retries and Mercado Pago headers; this adapter preserves those options.

The constructor's normal path has only two required arguments: `sdk` and `catalog`.
`reference_store` is the one optional advanced integration described below. Preference
expiry (24 hours), checkout-host validation and the UI label policy are internal
invariants rather than public configuration.

## Why it needs your catalog

A commerce-agents `Cart` is filled by the model's tool calls over a conversation, and
the reference host authenticates nothing: the session travels in a raw `X-Session-Id`
header. Sending `CartItem.price` to the preference API would therefore let whoever
drives the conversation decide what the shopper is charged, on your own `APP_USR-`
account — a cart priced at `0.01` for a real product would produce a valid, payable
link.

So this package never reads a price from the cart. It re-reads every line from your
catalog through `StorefrontBackend.get_product_details` — an abstract method your
backend already implements — and prices the preference from that. Passing
`catalog=self` is the entire wiring. With no catalog configured it refuses to create a
preference rather than falling back to cart prices.

The cart still decides *which* products and *how many*. A line whose product is unknown
or out of stock aborts the handoff.

## Correlating payments back to a session

`external_reference` is an opaque, per-preference value — never the session id, which
is caller-supplied and would let a payment be bound to a session its payer does not
own. To find the cart again from a webhook, give the package somewhere to record the
mapping:

```python
async def remember(reference: str, session_id: str) -> None:
    await my_store.put(reference, session_id)

sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
MercadoPagoCheckout(sdk=sdk, catalog=self, reference_store=remember)
```

Configure notifications on the Mercado Pago application rather than per checkout.
Verifying them is your handler's job and is not in this package yet: verify the
`x-signature` HMAC, then re-fetch the payment from the API by id and trust only that
server-side `status` and amount — never the notification body.

## What this package does not fix

It cannot authenticate the shopper; only your host can. Authenticate the session before
wiring this in — an unauthenticated `X-Session-Id` is still an unauthenticated cart,
whatever the checkout does.

## Other behaviour worth knowing

- Preferences expire after 24 hours, so a stale link cannot be paid at an old price.
- Item currency is omitted. Mercado Pago is expected to resolve it from the seller
  account; the catalog remains responsible for returning prices in that currency.
- Retrying the same cart reuses the same idempotency key, so a looping agent gets the
  original preference back instead of a second payable link. Its opaque
  `external_reference` stays stable across the retry as well.
- Per-request idempotency is added to a copy of the SDK's `RequestOptions`; the SDK
  instance's timeout, retry and custom-header settings are preserved and never mutated.
- `init_point` is checked against Mercado Pago's own hosts before being handed over, so
  a response pointing elsewhere is dropped rather than rendered as your payment button.
- Every failure path returns `[]` and logs; an outage degrades checkout instead of
  breaking the turn. Rejections log Mercado Pago's error identifiers only, never the
  rejected payload.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pip install pylint isort
.venv/bin/python -m unittest discover -s tests
```

`tests/test_contract.py` is skipped unless commerce-agents is installed. To run it:

```bash
git clone https://github.com/anthropics/commerce-agents.git
.venv/bin/pip install ./commerce-agents/commerce-common ./commerce-agents/shopping-agent/core
.venv/bin/python -m unittest tests.test_contract -v
```

It runs the real `enrich_checkout` against this package's `CheckoutHandoff`. That
matters because declaring our own type is what keeps `shopping-agent-core` out of the
dependency list, and commerce-agents accepts a handoff structurally rather than by a
promised contract. If an upstream release starts validating the type, this test fails in
CI instead of a seller's checkout failing in production. CI runs it against a pinned
commit, plus weekly against upstream `main` as a non-blocking warning.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
