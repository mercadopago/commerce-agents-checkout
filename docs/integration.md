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

```text
MercadoPagoCheckout(*, sdk: mercadopago.SDK, catalog: Catalog)
checkout_handoff(
    session,
    cart,
    *,
    idempotency_key: str | None = None,
    external_reference: str | None = None,
)
CheckoutOutcomeUnknown(
    *,
    idempotency_key: str,
    reason: str,
    external_reference: str | None = None,
    order_id: str | None = None,
)
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
- A supplied key is limited to 64 characters by the supported SDK. Product and Order
  identifiers have their own 256-character validation boundary.
- `external_reference` is an optional seller business identifier and may be an ecommerce
  Order number; it does not need to be a UUID. A supplied value accepts 1–64 letters,
  digits, hyphens and underscores. When omitted, the adapter omits the field from the
  request. It must not contain shopper PII or a session ID.

The adapter does not own authentication, secrets management, persistence, reconciliation
or webhook delivery, and exposes no callbacks for them. Production hosts must persist the
operation key before calling. The normal production path also passes, stores, and indexes
the host's own Order reference; omit it only when the host has another authoritative
Order-to-attempt mapping maintained outside this adapter.

## Orders API request

For an accepted cart, the package calls `sdk.order().create(body, request_options)`.
The SDK sends `POST https://api.mercadopago.com/v1/orders`.
Checkout Pro requires `processing_mode=manual`; it is the only supported value for this
flow in the
[Orders API reference](https://www.mercadopago.com.pe/developers/en/reference/online-payments/checkout-pro/create-order/post).

The relevant payload shape is:

```json
{
  "type": "online",
  "processing_mode": "manual",
  "total_amount": "40.50",
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

When the seller passes `external_reference="seller-order-1234"`, the adapter adds exactly
`"external_reference": "seller-order-1234"` to that payload. It does not add the field
otherwise.

An item carries `title`, `quantity`, and `unit_price` only. Orders validates items with
`additionalProperties: false`, so a per-item `total_amount` or `unit_measure` is
rejected with HTTP 400; the order-level `total_amount` is what covers the quantity.

### Attribution (`integration_data`)

The package always sends its registered `platform_id` in `integration_data`; hosts do not
configure attribution. It does not send other attribution fields. A retry must reuse the
same idempotency key and the same payload, including the seller's `external_reference`
when one was supplied.

The package intentionally omits:

- cart-authored title and price;
- raw session ID;
- payer PII of any kind;
- credentials;
- return or notification URLs;
- payment-method data and card data.

## Orders API response

Mercado Pago returns an order `id` and `checkout_url`. The adapter first verifies the
expected `online` type, `manual` processing mode, `created` initial status, amount, and
currency. It also compares `external_reference` when the seller supplied one. It accepts
the URL only when it uses HTTPS, has no embedded credentials, uses port 443 or the default
HTTPS port, and matches an explicit Mercado Pago hostname.

The accepted URL becomes:

```python
[CheckoutHandoff(url=checkout_url)]
```

When that validation fails the order has already been created, so the adapter cancels it
through `sdk.order().cancel(order_id, request_options)` with a separate deterministic
idempotency key. It returns `[]` only after Mercado Pago confirms the cancellation. If the
response does not identify that same Order in `canceled` state, cleanup fails, is
interrupted, or the order ID cannot be read, it raises
`CheckoutOutcomeUnknown` so the host cannot silently expose its fallback while an Order
may remain payable. When cleanup had a valid Order ID, the exception exposes it as
`order_id` for controlled GET/cancel recovery; its message and adapter logs do not.

The adapter's public success result is the returned `checkout_url`; it does not expose an
Order ID separately or ask hosts to parse one from that URL. On Mercado Pago's side, the
Order ID remains the resource for status lookup, cancellation, refunds, and webhooks.
This flow never calls the Preferences API. Production hosts should persist the
idempotency key and expected Order snapshot before calling, plus their
`external_reference` when they choose to send one.

## Failure behavior

Local validation failures and definitive API rejections return `[]`, allowing the host's
own checkout to take over. The 20-line and quantity-10 limits are package safety budgets,
not Mercado Pago API limits; configure the host consistently if it should reject those
carts before handoff.

After a POST may have reached Mercado Pago, fallback is allowed only when the result is
proven or a refused Order is confirmed canceled. Otherwise the method raises
`CheckoutOutcomeUnknown`, including when a retry receives a client error after the first
response was lost. The exception exposes the operation key and reference for controlled
reconciliation; the reference attribute is `None` when the seller did not supply one. Its
`order_id` attribute is populated only when a failed cleanup had already identified the
Order. Its message and adapter logs expose none of those identifiers; do not convert it
to `[]`.

API error codes and SDK behavior can evolve. Use the sanitized logger fields to locate a
failure, then confirm its current meaning in the
[Orders API reference](https://www.mercadopago.com.pe/developers/en/reference/online-payments/checkout-pro/create-order/post)
and the supported SDK instead of depending on response descriptions in this document.

Logs contain reason codes and Mercado Pago error codes only. They omit tokens, session
IDs, idempotency keys, seller references, prices, product identifiers, payloads, response
bodies, and checkout URLs.

### Observing refusals

There is no callback for definitive refusals: the adapter returns `[]` and the host cannot
tell those reasons apart from the return value alone. Indeterminate outcomes are distinct
exceptions. Observability for refusals therefore goes through logging, and these two
things are treated as a public contract that will not change without a minor version bump
and a changelog entry:

- the logger name `mercadopago_commerce_agents.checkout`;
- the reason codes themselves — `currency_mismatch`,
  `too_many_items`, `invalid_product_id`, `product_not_found`, `out_of_stock`,
  `invalid_price`, `invalid_currency`, `invalid_quantity`, `cart_reconfirmation_required`,
  `invalid_title`, `duplicate_product`, `amount_out_of_range`, `unreadable_cart`,
  `invalid_catalog_record`, `missing_cart_currency`, `invalid_idempotency_key`, and
  `invalid_external_reference`.

A local refusal is logged at `WARNING` as `Refusing to create an order: <code>`; a
Mercado Pago rejection or an infrastructure failure is logged at `ERROR`. Attach a
handler to that logger to turn either into a metric:

```python
logging.getLogger("mercadopago_commerce_agents.checkout").addHandler(my_handler)
```

## Authoritative payment state

A handoff means only that Mercado Pago created an order and returned a hosted checkout.
It does not mean the buyer paid.

The host must validate the Order webhook signature using Mercado Pago's
[Webhooks guide](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks),
deduplicate the event, and associate its Order with exactly one persisted checkout
attempt before changing local state. Supplying and storing `external_reference` is the
normal production correlation path; omit it only when the host already persists an
independent Mercado Pago Order-ID-to-attempt mapping outside this adapter. After that
association, fetch `/v1/orders/{id}`, verify the expected amount and currency, and apply
a valid local state transition. Amount, currency, browser redirects, and query parameters
are never correlation keys or payment evidence.

What the seller configures on the Mercado Pago side — credentials, the Order webhook,
test users — is listed with links in the README under "What the seller configures".

Current Mercado Pago references:

- [Create a Checkout Pro order](https://www.mercadopago.com/developers/en/docs/checkout-pro-orders/create-order)
- [Checkout Pro Orders API reference](https://www.mercadopago.com.pe/developers/en/reference/online-payments/checkout-pro/create-order/post)
  (the API reference only resolves under a country subdomain; the guides also work on the
  neutral one)
- [Webhooks and signature validation](https://www.mercadopago.com/developers/en/docs/your-integrations/notifications/webhooks)
- [Credentials](https://www.mercadopago.com/developers/en/docs/your-integrations/credentials)
- [Test accounts](https://www.mercadopago.com/developers/en/docs/your-integrations/test/accounts)
  and [test cards](https://www.mercadopago.com/developers/en/docs/your-integrations/test/cards)
