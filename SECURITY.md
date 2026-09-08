# Security policy

This package sits on a payment boundary. A vulnerability here can let a caller influence
what a shopper is charged on a seller's own Mercado Pago account, so reports are welcome
and take priority over feature work.

## Reporting a vulnerability

> **Maintainers: confirm this channel before the first public release.** Replace the line
> below with Mercado Pago's official vulnerability-disclosure address or program URL. Do
> not leave a placeholder in a published package.

Report privately to Mercado Pago's security disclosure channel. Do **not** open a public
GitHub issue, pull request, or discussion for a suspected vulnerability, and do not
include a real Access Token, a complete `checkout_url`, or any buyer data in the report.

GitHub private vulnerability reporting is also enabled on this repository and is an
acceptable channel: use **Security → Report a vulnerability**.

Please include:

- the affected version (`pip show mercadopago-commerce-agents`);
- a minimal reproduction, ideally as a failing test against the mocked SDK;
- the impact you believe it has on the charged amount, the created order, or the data
  reaching Mercado Pago or the logs.

You should get an acknowledgement within a few business days. We will confirm the issue,
agree on a disclosure timeline with you, and credit you in the release notes unless you
prefer otherwise.

## Supported versions

Until `1.0.0`, only the latest released minor version receives security fixes. The
project is pre-release today; see [CHANGELOG.md](CHANGELOG.md).

## What is in scope

- Anything that lets a cart, session, model-authored text, or Mercado Pago response
  change the amount, currency, items, or destination of a created order.
- Bypasses of the trusted-catalog repricing, the confirmation gate, the idempotency
  derivation, or the `checkout_url` host allowlist.
- Credentials, session identifiers, payloads, PII, or complete checkout URLs reaching
  logs or exceptions raised out of the adapter.

## What is out of scope here

These are real risks, but they belong to the consuming host or to Mercado Pago rather
than to this library, and [docs/security.md](docs/security.md) records the split:

- shopper authentication, cart ownership, and session integrity in the host;
- webhook signature validation and order-state transitions in the host;
- Access Token storage and rotation in the host;
- the behaviour of the Mercado Pago Orders API and its hosted checkout page — report
  those through Mercado Pago's own channels.

Reports against a deployment's own host code should go to that deployment's owner.
