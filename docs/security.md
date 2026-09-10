# Security responsibilities and review checklist

This package sits on a payment boundary. A successful `checkout_handoff` only creates a
hosted Mercado Pago order; it does not authenticate a shopper, reserve inventory, or
confirm payment.

## Controls implemented by the package

- Catalog title is authoritative; a cart/catalog price mismatch requires a new shopper
  confirmation instead of silently charging the new value.
- The cart's lines, quantities and currency are frozen before the first `await`, so a
  catalog that mutates the caller's cart cannot change what is charged.
- Cart, catalog and returned Order currencies must agree; there is no separate seller
  currency setting to get wrong.
- The same product on several lines is refused, so the per-line caps cannot be multiplied.
- Stock fails closed and is accepted only when the catalog returns boolean `True`.
- Cart line count, quantity, and caller-controlled identifier lengths are bounded.
- Prices must be finite, positive, and have at most two decimals.
- Quantity magnitude is bounded before integer conversion, including exponent notation.
- The idempotency key is scoped to one operation: generated as a UUID v4 when absent, and
  never silently replaced when a supplied one is invalid.
- `external_reference` is either the seller's validated business identifier or a stable
  UUIDv5 default derived from the operation key. UUIDv5 is deterministic, not a
  confidentiality control; neither value may be derived from shopper PII or an
  unauthenticated session ID.
- Checkout URLs are restricted to HTTPS and explicit Mercado Pago hosts.
- `CheckoutHandoff.__repr__` redacts its complete URL while `model_dump()` preserves it
  for the host's intended redirect path.
- The Mercado Pago SDK's request options are copied before adding request headers.
- Dependency floors exclude currently known vulnerable releases in the Requests HTTP
  stack while retaining compatible version ranges.
- Logs and exception messages omit credentials, sessions, product identifiers, seller
  references, idempotency keys, prices, payloads, full API responses, and checkout URLs.
- The created Order's ID, reference, amount, and currency are validated before handoff;
  an order that fails that check is cancelled rather than left payable on the account.
- No card data, return URL, notification URL, or payer PII is sent at all.

## Responsibilities of the host application

- Authenticate the shopper from a trusted server-side context.
- Verify cart ownership and seller scope.
- Supply a new `idempotency_key` for every intentional purchase and reuse it only for
  retries of that same confirmed snapshot. Reconfirmation means a new key.
- Re-read price, currency, stock, shipping, discounts, and tax server-side, then refresh
  the cart and obtain confirmation again after any material change.
- Reserve or revalidate stock according to the seller's business process.
- Store the idempotency key and seller `external_reference` durably. When the explicit
  reference is omitted, store the default returned by `external_reference_for(key)`;
  enforce attempt expiry beyond an HTTP header.
- Treat `CheckoutOutcomeUnknown` as a hard stop: reconcile its external reference and
  reuse its key for controlled recovery instead of rendering another checkout.
- Give each attempt an explicit lifecycle. Keep it active across retries and cart
  navigation, close it after a verified paid, canceled, or expired Order state, and bound
  retention beyond the Order's expiry in case a terminal webhook is missed. Never rotate
  its key while the previous checkout can still be payable.
- Keep Access Tokens and webhook secrets in an approved secrets manager.
- Apply rate limits and abuse detection before calling `checkout_handoff`.
- Validate Order webhook `x-signature`, deduplicate events, and retrieve the order by ID.
  The signature contract is in Mercado Pago's
  [Webhooks guide](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks).
- Compare the authoritative amount/reference before changing local order state.
- Treat redirects as navigation only, never proof of payment.

## Security review checklist

- [ ] Threat model the host-to-library-to-Mercado-Pago data flow.
- [ ] Confirm the session identity and cart ownership implementation in the host.
- [ ] Confirm seller/account isolation for multi-tenant deployments.
- [ ] Review Access Token and webhook-secret storage and rotation.
- [ ] Verify rate limiting and replay/idempotency behavior under concurrent calls.
- [ ] Verify the Mercado Pago checkout-host allowlist for every supported country.
- [ ] Exercise malformed quantities, prices, catalog failures, API failures, and hostile
      `checkout_url` responses.
- [ ] Validate webhook signatures, deduplication, order lookup, amount comparison, and
      allowed state transitions end to end.
- [ ] Confirm logs and monitoring never persist tokens, session IDs, idempotency keys,
      seller references, PII, financial payloads, or complete checkout URLs.
- [ ] Run dependency/SAST scanning against the release artifact.
- [ ] Obtain security approval before production enablement.

Security reports should review the consuming host as well as this library; reviewing the
adapter alone cannot establish authentication, authorization, or payment correctness.

The package keeps its dependency surface small on purpose: the official Mercado Pago SDK
for transport, `requests` for the exception types raised by it, and security floors for
that HTTP stack (`certifi`, `idna`, `urllib3`) — the full list is in `pyproject.toml`.
There is no runtime Pydantic. Its external boundary is validated explicitly with bounded
inputs and exercised by the hostile-input tests.
