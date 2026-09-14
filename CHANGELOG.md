# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this package sits on a payment boundary, every entry states whether it changes
the request sent to Mercado Pago, the conditions under which a handoff is refused, or the
public constructor surface — those are the three things a consuming host must re-verify
before upgrading.

## [0.1.0b2] - 2026-09-14

Security hardening for the Checkout Pro handoff introduced in `0.1.0b1`.

### Fixed

- Binds the hosted link to the Order it pays: the checkout URL is accepted only when it
  carries exactly one `order_id` equal to the created Order's `id`. The host allowlist
  proves the link is Mercado Pago's; this proves it is not another order's.
- Cleans up only what it can prove is its own. A created-order response whose
  `external_reference` differs from the one sent, or omits it, is never cancelled —
  cancelling the id it carries could act on another Order — and raises
  `CheckoutOutcomeUnknown` with reason `uncorrelated_response` instead.
- Treats HTTP 423 as a locked idempotency key rather than a rejection. Orders answers 423
  while a concurrent request for the same key is still in flight, which may already have
  created a payable Order, so it raises `CheckoutOutcomeUnknown` with reason
  `resource_locked` and never releases the host fallback.

## [0.1.0b1] - 2026-09-14

Mercado Pago Checkout Pro as a `checkout_handoff` provider for
[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents), built on
`POST /v1/orders` with `processing_mode=manual`.

### What it does

- Turns a cart the shopping agent assembled into a hosted Checkout Pro order and returns
  the validated `checkout_url`. Two constructor arguments, one adapter method with two
  required keyword-only identifiers:
  `MercadoPagoCheckout(*, sdk, catalog)` and
  `checkout_handoff(session, cart, *, external_reference, idempotency_key)`. The upstream
  `StorefrontBackend` wrapper keeps its two-argument `(session, cart)` contract.
- Prices every line from the host's own catalog rather than from the cart, and freezes the
  cart's lines, quantities and currency before the first `await`. The cart is filled by a
  model's tool calls, so its prices are a claim to verify, not a fact.
- Derives the currency from those catalog records; the cart and the created order must
  agree with them.
- Identifies the integration to Mercado Pago through `integration_data.platform_id`.
- Requires the host to create and persist an idempotency key and seller Order reference
  before calling, validates both, and uses them verbatim. Invalid values are refused rather
  than silently replaced, and the same pair must be reused for retries.
- Retries once with the same key after a transport failure, because a lost response does
  not prove the request had no effect. A later client error remains indeterminate because
  it describes only the retry, not whether the first POST created an Order.
- Raises `CheckoutOutcomeUnknown` instead of returning the host fallback when create or
  cleanup may have taken effect but cannot be proven. The exception carries the key and
  required seller reference, plus a known Order ID after failed cleanup, for controlled
  recovery without including those identifiers in its message or adapter logs.
- Canonicalizes item order by product ID, so a reordered retry keeps the same Orders body.
- Validates the created order — type, processing mode, status, amount, currency, expiry,
  the required seller reference and the checkout host — and cancels the order when that
  check fails, using a separate deterministic idempotency key. Fallback is allowed only
  after cleanup is confirmed for the same Order in `canceled` state.
- Records the live integration observation that omitting `external_reference` returned
  HTTP 400 `required_properties` on the tested Checkout Pro Orders path, while avoiding a
  broader claim than the current published API and SDK contracts support.
- Returns `[]` and logs a bounded reason code on definitive refusals. Indeterminate
  remote outcomes stop fallback until the host reconciles them. Payment and recovery
  identifiers are excluded from adapter logs.
- Parses recognized Orders API `errors[].code` values without logging response messages,
  details or request data, while preserving the legacy `error` and numeric `cause`
  diagnostics.
- Example scripts redact checkout URLs, idempotency keys and external references by
  default; complete values require explicit opt-in from an interactive terminal. SDK
  failures during Order read-back terminate with a fixed message rather than exposing
  the underlying request URL or headers.
- `CheckoutHandoff` redacts its URL from `repr` while preserving it in `model_dump()`.

### What it deliberately leaves to the host

Authentication, cart ownership, persistence, Order reconciliation, webhook handling and
payment confirmation. The package creates one order and returns one URL; it stores
nothing and calls nothing back. See [`docs/integration.md`](docs/integration.md) and
[`docs/security.md`](docs/security.md).
