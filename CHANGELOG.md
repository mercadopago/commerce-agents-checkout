# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this package sits on a payment boundary, every entry states whether it changes
the request sent to Mercado Pago, the conditions under which a handoff is refused, or the
public constructor surface — those are the three things a consuming host must re-verify
before upgrading.

## [Unreleased] — first release

Mercado Pago Checkout Pro as a `checkout_handoff` provider for
[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents), built on
`POST /v1/orders` with `processing_mode=manual`.

### What it does

- Turns a cart the shopping agent assembled into a hosted Checkout Pro order and returns
  the validated `checkout_url`. Two constructor arguments, one method:
  `MercadoPagoCheckout(*, sdk, catalog)` and
  `checkout_handoff(session, cart, *, idempotency_key=None, external_reference=None)`.
- Prices every line from the host's own catalog rather than from the cart, and freezes the
  cart's lines, quantities and currency before the first `await`. The cart is filled by a
  model's tool calls, so its prices are a claim to verify, not a fact.
- Derives the currency from those catalog records; the cart and the created order must
  agree with them.
- Identifies the integration to Mercado Pago through `integration_data.platform_id`.
- Scopes idempotency to one call: a UUIDv4 when none is given, the caller's value used
  verbatim when it is, and a refusal rather than a silent replacement when it is invalid.
  Callers may pass their seller Order identifier as `external_reference`; when omitted,
  the field is omitted from the Orders payload.
- Retries once with the same key after a transport failure, because a lost response does
  not prove the request had no effect. A later client error remains indeterminate because
  it describes only the retry, not whether the first POST created an Order.
- Raises `CheckoutOutcomeUnknown` instead of returning the host fallback when create or
  cleanup may have taken effect but cannot be proven. The exception carries the key and
  optional seller reference, plus a known Order ID after failed cleanup, for controlled
  recovery without including those identifiers in its message or adapter logs.
- Canonicalizes item order by product ID, so a reordered retry keeps the same Orders body.
- Validates the created order — type, processing mode, status, amount, currency, expiry,
  the optional supplied reference and the checkout host — and cancels the order when that
  check fails, using a separate deterministic idempotency key. Fallback is allowed only
  after cleanup is confirmed for the same Order in `canceled` state.
- Returns `[]` and logs a bounded reason code on definitive refusals. Indeterminate
  remote outcomes stop fallback until the host reconciles them. Payment and recovery
  identifiers are excluded from adapter logs.
- Example scripts redact checkout URLs, idempotency keys and external references by
  default; complete values require explicit opt-in from an interactive terminal.
- `CheckoutHandoff` redacts its URL from `repr` while preserving it in `model_dump()`.

### What it deliberately leaves to the host

Authentication, cart ownership, persistence, Order reconciliation, webhook handling and
payment confirmation. The package creates one order and returns one URL; it stores
nothing and calls nothing back. See [`docs/integration.md`](docs/integration.md) and
[`docs/security.md`](docs/security.md).
