# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this package sits on a payment boundary, every entry states whether it changes
the request sent to Mercado Pago, the conditions under which a handoff is refused, or the
public constructor surface — those are the three things a consuming host must re-verify
before upgrading.

## [Unreleased]

### Changed — breaking

Review feedback on the public API: the package is now the smallest thing that turns a
catalog-validated cart into a hosted checkout URL. The surface is

```python
MercadoPagoCheckout(*, sdk, catalog)
await checkout.checkout_handoff(session, cart, *, idempotency_key=None)
```

- Removed `currency`. It is derived from the trusted catalog instead, which removes a
  second source of truth: every record must agree with the others and with the cart, and
  the created Order is validated against that value. The currency was never sent — Mercado
  Pago resolves it from the seller account. *Refusal conditions change.*
- Removed `attempt_id_provider` in favour of a keyword-only `idempotency_key` on
  `checkout_handoff`, because idempotency is a property of an operation rather than of the
  instance. Omitted, a UUID v4 covers the call and its internal retries; supplied, it is
  used verbatim and an invalid value fails closed rather than being replaced. Idempotency
  across calls, processes or restarts now means the backend passing the same key again.
  *Public surface change; refusal conditions change.*
- Removed `reference_store` and `order_store`. Persistence, reconciliation, webhook
  handling and fulfillment belong to the seller's backend. `external_reference` is now a
  UUIDv5 of the idempotency key, so a host correlates a webhook from the key it already
  holds, with no callback. *Changes the request sent to Mercado Pago.*
- Removed `payer_email_provider`; the hosted checkout collects what it needs. No payer PII
  is sent at all now.
- Removed `label` and `integrator_id`, along with the `CreatedOrder` and
  `CheckoutOrderItem` exports, which had no consumer once `order_store` was gone.

### Added

- `integration_data` carrying this adapter's registered Platform ID
  (`dev_9e28fa65abb111f189e77e2ccf36aeec`, "Commerce Agents Claude") on every order.
  Mercado Pago persists it on the Order; the SDK's `x-platform-id`/`x-integrator-id`
  headers do not populate it, so attribution has to travel in the body. It is a constant,
  not an argument: attribution must not depend on host configuration.
  *Changes the request sent to Mercado Pago; public surface change.*
- Troubleshooting guidance for Mercado Pago's opaque `403`
  `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` (the account is not authorised for the Orders
  API), for a catalog priced in a currency the seller account does not use, for the local
  refusal reason codes, and for the disabled hosted-checkout button.
- A concrete reference implementation for `attempt_id_provider` in the README.
- This changelog and a security policy.

### Fixed

- The distribution is published as `mercadopago-commerce-agents-checkout`, which differs
  from the `mercadopago_commerce_agents` import package on purpose.
- Corrected the claim that this flow creates no preference. It calls Orders, not the
  Preferences API, but Mercado Pago mints a preference behind the order and exposes it as
  `pref_id` inside `checkout_url`.

- Cancel an order that fails post-creation validation. The order exists at Mercado Pago by
  the time the response is checked, so refusing the handoff used to strand a payable order
  on the seller's account for the full expiry window — including the case where the catalog
  is priced in a currency the account does not use, which Mercado Pago accepts and creates
  in its own currency. Found by exercising the real API rather than a mock.
  *Changes behaviour on every refusal that happens after creation.*
- Guard `cart.items` so a malformed cart returns `[]` like every other refusal instead of
  raising out of the adapter.

- Corrected `integration_data`. The previous `product`/`technology` shape was rejected
  outright, failing every real order with `HTTP 400`; verified against the live API,
  `platform_id` and `integrator_id` are accepted and persisted, `application_id` is
  read-only, and `sponsor.id` needs a real account id.
  *Changes the request sent to Mercado Pago.*
- Removed the per-item `total_amount` and `unit_measure` fields. The Orders item schema
  validates with `additionalProperties: false` and rejected both; an item now carries
  `title`, `quantity`, and `unit_price` only. *Changes the request sent to Mercado Pago.*

## [0.1.0] - Unreleased

Initial implementation: Mercado Pago Checkout Pro as a `checkout_handoff` provider for
[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents), built on
`POST /v1/orders` with `processing_mode=manual`.

Cart lines are re-priced from the host's trusted catalog rather than from the cart, the
`external_reference` is opaque, the returned `checkout_url` is validated against explicit
Mercado Pago hosts, and every rejected path returns `[]` so an outage degrades checkout
instead of breaking the turn. See [docs/security.md](docs/security.md) for the full list
of controls and the responsibilities that remain with the host.
