# Hand-off: `commerce-agents-checkout`

**To:** Mercado Pago SDK team · **From:** Agentic Checkout · **Date:** 2026-09-04
**Ask:** review the adapter, validate it against the API, and publish the package on PyPI.

---

## What it is

Anthropic published [`anthropics/commerce-agents`](https://github.com/anthropics/commerce-agents)
on 2026-09-02: a reference shopping agent that deliberately stops before payment. Its
`checkout` tool renders the cart and leaves the hosted checkout URL to the backend,
filled in *after* the model's tool call so it never reaches the model.

This package fills that one method with a Checkout Pro preference. A seller wires it in
with three lines and never calls the Mercado Pago API directly. It is one class over
`sdk.preference().create()`.

The adapter receives an already configured official SDK plus the seller's trusted
catalog:

```python
sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
checkout = MercadoPagoCheckout(sdk=sdk, catalog=self)
```

There is no adapter-specific environment loader and no second credential/configuration
source. The application owns token loading and SDK `RequestOptions`; the adapter copies
those options per request only to add its stable idempotency key. It does not expose
currency, notification URL, timeout, retry count, TTL or button label as constructor
arguments. Webhooks are configured on the Mercado Pago application, currency is omitted
from preference items, preferences have a fixed 24-hour expiry, and the host owns its UI
label.

Sellers building on commerce-agents are choosing their payment provider right now, at
the point where the blueprint leaves a gap. This is the cheapest thing we can put in
that gap.

## Why it is not a PR against `sdk-python`

Three reasons, in order of weight:

1. **Scope.** The SDK is the transport for the Mercado Pago API. This is an adapter
   shaped like one specific third-party agent framework's interface. That shape is not
   Mercado Pago API surface, and it will change on Anthropic's schedule, not ours.
2. **Compatibility contract.** Anthropic documents the interface as "the docstrings in
   `backend.py` and `types.py`" and stabilises nothing. Putting that inside an LTS SDK
   means an upstream rename becomes an SDK breaking change. In a separate package with
   its own version, it is a minor bump.
3. **Async.** `checkout_handoff` is `async`; the SDK is synchronous `requests`
   throughout. Here it is one `asyncio.to_thread` around the SDK call — in the SDK it
   would be the start of an async story the SDK does not have.

It **depends on** `mercadopago>=3.5.0`, so it inherits the SDK's transport, retries and
the `x-product-id` / `x-tracking-id` headers — meaning these integrations show up in
Mercado Pago's own attribution instead of being invisible raw HTTP.

## Why it does not depend on Anthropic's package

`shopping-agent-core` is deliberately unpublishable. Its `pyproject.toml` pins
`commerce-common==0.1.0.dev0` at version `0.1.0.dev0` so that, in its own words, "a lone
install fails instead of resolving a public distribution of the same name" — a
dependency-confusion guard. Depending on it would make this package either
uninstallable from PyPI or a supply-chain risk.

So `src/mercadopago_commerce_agents/types.py` declares the one type we hand back.
commerce-agents consumes a handoff structurally — `enrich_checkout` only calls
`.model_dump(exclude_none=True)`, with no `isinstance` check and no pydantic validation
— so a structurally identical dataclass is accepted. Verified against the real
`enrich_checkout`, not assumed.

That is a bet on an implementation detail, and `tests/test_contract.py` is the hedge: it
runs the real `enrich_checkout` against our type, pinned to a commerce-agents commit in
CI, plus weekly against upstream `main` as a non-blocking warning. Upstream tightening
the type fails our CI, not a seller's checkout.

The built artifact installs on its own; after the first PyPI release the result is a
single `pip install mercadopago-commerce-agents`.

## Import provenance

The prototype was developed in
[`gforgab/mercadopago-commerce-agents`](https://github.com/gforgab/mercadopago-commerce-agents)
through commit `b384600`. The organization requires signed commits and pull requests,
so this repository receives the current tree in one GitHub-signed import commit. The
original three-commit development history remains available at that link, including the
rationale for each security decision.

## Security decisions already made

The first draft of this module had two findings that had to be closed before it could
create a real preference. Both are fixed here, with tests.

| Finding | Fix |
|---|---|
| **Price tampering (critical).** `unit_price` was copied from the cart. The cart is filled by model tool calls and the reference host authenticates nothing, so a cart priced at `0.01` for a real product yielded a payable link on the seller's account. | Every line is re-priced from the seller's own catalog via `get_product_details`, an abstract method every backend implements. No catalog configured means refusal, not fallback. |
| **Caller-controlled payment key (high).** `external_reference` was `session.session_id`, which arrives in a raw `X-Session-Id` header — so a payment could be bound to someone else's session. | `external_reference` is opaque per preference; correlation is opt-in through a `reference_store` callback. |

Also closed: the adapter no longer accepts a standalone access token and its object
representation does not expose the token held by the SDK; rejections log Mercado Pago's
error identifiers only, never the echoed payload; `init_point` is checked against
Mercado Pago hosts before being rendered as the payment button; preferences expire;
retries of one cart share both an idempotency key and an opaque external reference.
Request-scoped headers are added without mutating the shared SDK options. Using the SDK
also removes a configurable base URL, so the credential cannot be pointed at another
host.

Still the host's responsibility, and stated in the README: authenticating the session,
and verifying webhook `x-signature` before trusting a notification.

## What we are asking for

1. **Review and own this repository.** The implementation, tests, CI, package build and
   security rationale are here; the prototype history is linked above.
2. **A PyPI project, owned by a Mercado Pago PyPI organization** rather than by any
   individual account. We have deliberately not registered the name from a personal
   account. Confirm the provisional `mercadopago-commerce-agents` distribution name
   before claiming it.
3. **A review of two choices we made for you to overrule:**
   - **Trusted Publishing (OIDC) instead of `secrets.PYPI_TOKEN`.** `cd.yml` uses it.
     Since the repo is new, there is no migration cost, and it means no long-lived
     publishing credential. Happy to match `sdk-python` instead if you prefer uniformity.
   - **Apache-2.0, where the SDK is MIT.** Chosen to sit alongside the Apache-2.0
     upstream. Permissive either way; say the word if the org should be uniformly MIT.

## State

- 26 tests, `unittest` only — same `python -m unittest discover` shape as `sdk-python`,
  no new test framework.
- `pylint` 10.00/10, `isort` clean, matching `sdk-python`'s CI steps.
- Contract test verified green against commerce-agents `fd4d5922`.
- Not yet exercised against a live Mercado Pago account: every test replaces the SDK
  call. A sandbox run against a real `TEST-` token is the obvious next step and needs
  credentials we do not have. It must specifically confirm that preferences are
  accepted without `items[].currency_id`; running against two Mercado Pago sites would
  verify that account-derived currency behaves as intended.
