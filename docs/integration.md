# Integration and payload contract

## Boundary

`MercadoPagoCheckout` implements only the payment handoff expected by
`StorefrontBackend.checkout_handoff(session, cart)`. The agent chooses products and
quantities; the seller backend remains authoritative for identity, cart ownership,
catalog data, price, currency, stock, and whether checkout is allowed.

The adapter never receives card data and never gives payment authority to the model.
It creates a Mercado Pago Checkout Pro order server-side and returns a hosted URL after
the model call has completed.

## Public surface

```python
MercadoPagoCheckout(*, sdk: mercadopago.SDK, catalog: Catalog)

await checkout.checkout_handoff(session, cart, *, idempotency_key: str | None = None)
```

- `sdk` must already contain a backend Access Token.
- `catalog.get_product_details(session, product_id)` must return a trusted record with
  `title`, `price`, `currency`, and `in_stock is True`.
- The currency is derived from those records, not configured. Every record must agree
  with the others and with the cart, and the created Order is checked against it.
- `idempotency_key` scopes one operation. Omitted, a UUID v4 covers this call and its
  internal retries. Supplied, it is validated and used verbatim; an invalid value fails
  closed instead of being replaced. Reusing a key with a different payload is answered by
  Mercado Pago with `HTTP 409 idempotency_key_already_used`.

The adapter does not own authentication, secrets management, persistence, reconciliation
or webhook delivery, and exposes no callbacks for them. A host correlates a webhook
through the `external_reference`, which is a UUIDv5 of the idempotency key it already
holds.

## Orders API request

For an accepted cart, the package calls `sdk.order().create(body, request_options)`.
The SDK sends `POST https://api.mercadopago.com/v1/orders`.

The relevant payload shape is:

```json
{
  "type": "online",
  "processing_mode": "manual",
  "total_amount": "40.50",
  "external_reference": "mpca-00000000-0000-0000-0000-000000000000",
  "expiration_time": "P1D",
  "integration_data": {
    "platform_id": "dev_9e28fa65abb111f189e77e2ccf36aeec"
  },
  "items": [
    {
      "title": "Catalog title",
      "quantity": 2,
      "unit_price": "20.25"
    }
  ]
}
```

An item carries `title`, `quantity`, and `unit_price` only. Orders validates items with
`additionalProperties: false`, so a per-item `total_amount` or `unit_measure` is
rejected with HTTP 400; the order-level `total_amount` is what covers the quantity.

### Attribution (`integration_data`)

Mercado Pago stores `integration_data` on the Order and returns it on reads, which is
where an integration is identified. Verified against the live API:

| Field | Sent by the package | Behaviour |
|---|---|---|
| `platform_id` | always | This adapter's own Platform ID, registered as "Commerce Agents Claude". Not configurable, so attribution never depends on host setup. |
| `integrator_id` | never | Accepted and persisted by the API, but it identifies a partner rather than this adapter, and the public surface stays minimal. |
| `application_id` | never | Read-only. Mercado Pago fills it from the access token; sending it returns HTTP 400 `unsupported_properties`. |
| `sponsor.id` | never | Requires a real Mercado Pago account id; an unowned value returns HTTP 400 `order_invalid_sponsor_id`. |
| `product` | never | The property exists but its accepted values are not public; `"CHO PRO"` returns HTTP 400 `does not match pattern`. |

The SDK's `x-integrator-id` and `x-platform-id` request headers are a separate channel:
an order created with those headers and no body `integration_data` comes back carrying
`application_id` only. Attribution that must persist on the Order has to travel in the
body.

The `external_reference` shown above is a UUIDv5 of the idempotency key, so a retry of
the same operation carries a byte-identical body — which is what the Orders API requires
of a reused key. Deriving it rather than sending the key itself keeps host-internal
identifiers out of Mercado Pago's records.

The package intentionally omits:

- cart-authored title and price;
- raw session ID;
- payer PII of any kind;
- credentials;
- return or notification URLs;
- payment-method data and card data.

## Orders API response

Mercado Pago returns an order `id` and `checkout_url`. The adapter first verifies the
expected `online` type, `manual` processing mode, `created` initial status, reference,
amount, and currency. It accepts the URL only when it uses HTTPS, has no embedded
credentials, uses port 443 or the default HTTPS port, and matches an explicit Mercado
Pago hostname.

The accepted URL becomes:

```python
[CheckoutHandoff(url=checkout_url)]
```

When that validation fails the order has already been created, so the adapter cancels it
through `sdk.order().cancel(order_id)` before returning `[]`. Cancellation is best effort:
if it does not land, the order id is logged at `ERROR` so the seller can reconcile by
hand, and the handoff still falls back. An order whose id could not even be read cannot be
cancelled — that case is logged too.

The order ID is the authoritative resource identifier for status lookup, cancellation,
refunds, and Order webhooks. This flow never calls the Preferences API and never receives
an `init_point`; Mercado Pago still mints a preference behind the order, which surfaces as
the `pref_id` query parameter inside `checkout_url`, but that value is an implementation
detail of the hosted page — do not build on it. Production hosts should persist their idempotency key before calling,
which is what later ties a webhook back to the operation.

## Failure behavior

The method returns `[]` without creating an order when:

- the cart is empty or contains more than 20 lines;
- a product identifier or a supplied idempotency key is empty, contains control
  characters, or is longer than 256 characters;
- a quantity is not an integer between 1 and 10;
- the catalog does not know a product or does not report `in_stock is True`;
- a catalog price is non-positive, non-finite, or has more than two decimals;
- the catalog records disagree on currency, or the cart disagrees with them;
- the cart price differs from the catalog price and needs shopper reconfirmation;
- Mercado Pago rejects the request or cannot be reached;
- the response does not match the confirmed snapshot or contain a valid Order ID and
  Mercado Pago `checkout_url`.

Logs contain reason codes and Mercado Pago error codes only. They omit tokens, session
IDs, prices, product identifiers, payloads, response bodies, and checkout URLs.

### Observing refusals

There is no refusal callback: the adapter returns `[]` and the host cannot tell the
reasons apart from the return value alone. Observability therefore goes through logging,
and these two things are treated as a public contract that will not change without a
minor version bump and a changelog entry:

- the logger name `mercadopago_commerce_agents.checkout`;
- the reason codes themselves — `currency_mismatch`,
  `too_many_items`, `invalid_product_id`, `product_not_found`, `out_of_stock`,
  `invalid_price`, `invalid_currency`, `invalid_quantity`, `cart_reconfirmation_required`,
  `invalid_title`, and `invalid_idempotency_key`.

A local refusal is logged at `WARNING` as `Refusing to create an order: <code>`; a
Mercado Pago rejection or an infrastructure failure is logged at `ERROR`. Attach a
handler to that logger to turn either into a metric:

```python
logging.getLogger("mercadopago_commerce_agents.checkout").addHandler(my_handler)
```

## Authoritative payment state

A handoff means only that Mercado Pago created an order and returned a hosted checkout.
It does not mean the buyer paid.

The host must validate the Order webhook signature, deduplicate the event, fetch
`/v1/orders/{id}`, match its `external_reference` against the one derived from the stored
idempotency key, verify the expected amount and currency, and then apply a valid local
state transition. Browser redirects and query parameters are never payment evidence.

Current Mercado Pago references:

- [Create a Checkout Pro order](https://www.mercadopago.com.pe/developers/en/docs/checkout-pro-orders/create-order)
- [Checkout Pro Orders API reference](https://www.mercadopago.com.pe/developers/en/reference/online-payments/checkout-pro/create-order/post)
