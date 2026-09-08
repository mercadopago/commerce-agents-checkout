# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Mercado Pago Checkout Pro as a ``StorefrontBackend.checkout_handoff`` provider.

commerce-agents' ``checkout`` tool only ever renders the cart — nothing in the agent
places an order or moves money, and the hosted checkout URL is filled in by the backend
*after* the model's tool call, so it never reaches the model. This class fills that one
method with a real Checkout Pro order through ``POST /v1/orders``::

    import os

    import mercadopago

    from mercadopago_commerce_agents import MercadoPagoCheckout

    class MyBackend(StorefrontBackend):
        def __init__(self):
            # `catalog=self` is what makes the charge trustworthy — see below.
            sdk = mercadopago.SDK(os.environ["MERCADOPAGO_ACCESS_TOKEN"])
            self.mercadopago = MercadoPagoCheckout(sdk=sdk, catalog=self)

        async def checkout_handoff(self, session, cart):
            return await self.mercadopago.checkout_handoff(session, cart)

Why ``catalog`` is required
---------------------------
A commerce-agents ``Cart`` is filled by the model's tool calls over the course of a
conversation, and the reference host authenticates nothing — the session travels in a
raw ``X-Session-Id`` header. Sending ``CartItem.price`` to
``POST /v1/orders`` would therefore let whoever drives the conversation
decide what the shopper is charged, on the seller's own ``APP_USR-`` account. So this
class never charges a price from the cart. It re-reads every line from the seller's own
catalog through ``StorefrontBackend.get_product_details`` — an abstract method every
backend already implements, and one that resolves a variant id to that variant. It
creates the Order only when the cart snapshot still matches the authoritative price and
currency, so a changed price goes back through shopper confirmation. The currency is
derived from those same records rather than configured separately. Passing
``catalog=self`` is the whole pricing boundary.

The cart is still what decides *which* products and *how many*: quantity is capped both
upstream and here, and a line whose product, stock, price, or currency cannot be
confirmed aborts the handoff.

What this class does not fix
---------------------------
It cannot authenticate the shopper — only the host can. It keeps the caller-supplied
session id out of the payment record (``external_reference`` is opaque, see
``_reference``), which stops the payment from being bound to a session id someone else
chose, but a deployment that leaves ``X-Session-Id`` unauthenticated still has an
unauthenticated cart. Authenticate the session in the host before wiring this in.
"""

from __future__ import annotations

import asyncio
import logging
import re
from copy import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import mercadopago
import requests

from .types import CheckoutHandoff

if TYPE_CHECKING:  # imported for typing only; never present at runtime
    from shopping_agent import Cart, ShoppingSessionContext

logger = logging.getLogger(__name__)

# MP caps an order item's title; longer titles are rejected outright.
_MAX_TITLE = 256
_MAX_CART_ITEMS = 20
_MAX_QUANTITY = 10
_MAX_IDENTIFIER = 256
_MAX_CHECKOUT_URL = 2048
_MAX_DECIMAL_TEXT = 64
_AMOUNT_QUANTUM = Decimal("0.01")
_LOG_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")

# Checkout Pro Orders uses an ISO 8601 duration, not an absolute preference timestamp.
# Keeping it relative also makes retries carry an identical body.
_ORDER_EXPIRATION = "P1D"

# Mercado Pago's Platform ID for this adapter, registered as "Commerce Agents Claude".
# It identifies the integration itself rather than the seller, so it is sent on every
# order and is not configurable: attribution must not depend on a host remembering to
# set it. `application_id` is deliberately absent — Orders rejects a caller-supplied one
# and derives it from the Access Token — and `sponsor` needs a real account id that only
# a marketplace deployment has.
_PLATFORM_ID = "dev_9e28fa65abb111f189e77e2ccf36aeec"

# ``checkout_url`` comes back from the API, but it is rendered to the shopper as the
# seller's official payment button, so it is checked against this list before being
# handed over rather than trusted for being in a response body.
_CHECKOUT_HOSTS = frozenset(
    {
        "www.mercadopago.com",
        "www.mercadopago.com.ar",
        "www.mercadopago.com.br",
        "www.mercadopago.cl",
        "www.mercadopago.com.co",
        "www.mercadopago.com.mx",
        "www.mercadopago.com.pe",
        "www.mercadopago.com.uy",
        "www.mercadopago.com.ve",
    }
)


class Catalog(Protocol):  # pylint: disable=too-few-public-methods
    """The single method this package needs from the seller's ``StorefrontBackend``.
    Declared structurally so that nothing here imports ``shopping_agent``."""

    async def get_product_details(self, session: Any, product_id: str) -> Any:
        """The catalog record for one id, or None when the id is unknown. A variant's id
        returns that variant."""


class _Refused(Exception):
    """A line that cannot be priced honestly. Aborts the handoff; the host's own
    checkout card takes over."""


@dataclass(frozen=True)
class _PricedItem:  # pylint: disable=too-few-public-methods
    """A cart line resolved against the trusted catalog.

    ``product_id`` is retained for retry identity but deliberately omitted from the
    wire payload: Orders does not require ``external_code``, whose constraints are a
    separate seller concern.
    """

    product_id: str
    title: str
    quantity: int
    unit_price: Decimal

    def order_payload(self) -> dict[str, Any]:
        """Return the Orders API representation of this trusted line.

        ``unit_measure`` and a per-item ``total_amount`` are not part of the real
        Orders API item schema — both are rejected outright (``additionalProperties``)
        — so only the three fields the API documents for an item are sent.
        """
        return {
            "title": self.title,
            "quantity": self.quantity,
            "unit_price": _amount(self.unit_price),
        }


class MercadoPagoCheckout:  # pylint: disable=too-few-public-methods
    """Checkout Pro backed by an already configured official Mercado Pago SDK.

    The public surface is deliberately two arguments: the SDK that calls Mercado Pago
    and the trusted catalog that prices cart lines. Everything else is either derived
    (the currency comes from the catalog) or scoped to one call (the idempotency key).

    Persistence, webhook handling, Order reconciliation and payment confirmation belong
    to the seller's backend, not here.
    """

    def __init__(
        self,
        *,
        sdk: mercadopago.SDK,
        catalog: Catalog,
    ) -> None:
        if sdk is None:
            raise TypeError("sdk is required")
        if not getattr(getattr(sdk, "request_options", None), "access_token", None):
            raise ValueError("sdk must be configured with an access token")
        if catalog is None:
            raise TypeError("catalog is required")
        if not callable(getattr(catalog, "get_product_details", None)):
            raise TypeError("catalog must provide get_product_details")

        self._sdk = sdk
        self._catalog = catalog

    # Every rejected boundary exits immediately; keeping the sequence linear makes the
    # payment gate auditable even though it has more branches than usual.
    # pylint: disable=too-many-return-statements
    async def checkout_handoff(
        self,
        session: "ShoppingSessionContext",
        cart: "Cart",
        *,
        idempotency_key: str | None = None,
    ) -> list[CheckoutHandoff]:
        """Drop-in for ``StorefrontBackend.checkout_handoff``.

        The many early returns are the design: every branch that is not an order we
        are confident in leaves through one of them.

        ``idempotency_key`` identifies one checkout operation. Left unset, a UUID v4 is
        generated for this call and its internal retries. Supplied, it is validated and
        used exactly as given — never replaced by a fresh one, because silently minting
        another key would turn a rejected duplicate into a second payable order. Reusing
        a key with a different payload is refused by Mercado Pago with HTTP 409, so a new
        purchase needs a new key; idempotency across processes or restarts means the
        backend supplying the same key again.

        Returns an empty list — letting the host's own checkout card take over — for
        every case that is not an order we are confident in: nothing to charge, a
        line that cannot be priced, an API rejection, or MP being unreachable.
        """
        if not cart.items:
            return []
        if idempotency_key is None:
            idempotency_key = str(uuid4())
        elif not _valid_identifier(idempotency_key):
            # Fail closed: generating a replacement here would create an order the
            # caller believes it already deduplicated.
            logger.warning("Refusing to create an order: invalid_idempotency_key")
            return []

        try:
            items, currency = await self._priced_items(session, cart)
        except _Refused as refusal:
            logger.warning("Refusing to create an order: %s", refusal)
            return []
        except Exception:  # pylint: disable=broad-exception-caught
            # The catalog is a caller-owned boundary and may use any transport. Do not
            # log its exception text or traceback: either can contain a sensitive URL.
            logger.error("Catalog lookup failed; refusing to create an order.")
            return []

        total_amount = _order_total(items)
        body: dict[str, Any] = {
            "type": "online",
            "processing_mode": "manual",
            "total_amount": total_amount,
            "items": [item.order_payload() for item in items],
            "external_reference": _reference(idempotency_key),
            # An order with no expiry stays payable at yesterday's price after the cart
            # has moved on.
            "expiration_time": _ORDER_EXPIRATION,
            "integration_data": {"platform_id": _PLATFORM_ID},
        }

        order = await self._create(body, idempotency_key)
        if order is None:
            return []

        checkout_url = self._validated_url(
            order, body["external_reference"], total_amount, currency
        )
        if checkout_url is None:
            return []
        # No adapter-specific label: the commerce-agents host owns its UI.
        return [CheckoutHandoff(url=checkout_url)]

    # -- internals ---------------------------------------------------------------

    async def _priced_items(  # pylint: disable=too-many-branches
        self, session: "ShoppingSessionContext", cart: "Cart"
    ) -> tuple[list[_PricedItem], str]:
        """One order item per cart line, priced from the catalog record rather than
        from the line, plus the currency those records agree on.

        The currency is derived here rather than configured: the trusted catalog is
        already the authority for price, so making it the authority for currency too
        removes a second source of truth. Every record must agree with the first one and
        with the cart, and the created Order is checked against the same value.

        Raises :class:`_Refused` on anything that cannot be priced.
        """
        if len(cart.items) > _MAX_CART_ITEMS:
            raise _Refused("too_many_items")

        items: list[_PricedItem] = []
        currency: str | None = None
        for line in cart.items:
            product_id = getattr(line, "product_id", None)
            if not _valid_identifier(product_id):
                raise _Refused("invalid_product_id")
            record = await self._catalog.get_product_details(session, product_id)
            if record is None:
                raise _Refused("product_not_found")
            if getattr(record, "in_stock", None) is not True:
                raise _Refused("out_of_stock")

            record_currency = getattr(record, "currency", None)
            if not isinstance(record_currency, str) or _CURRENCY.fullmatch(
                record_currency
            ) is None:
                raise _Refused("invalid_currency")
            if currency is None:
                currency = record_currency
            elif record_currency != currency:
                raise _Refused("currency_mismatch")

            # Decimal via str: Decimal(float) would carry the float's binary error into
            # the comparison below and report drift that isn't there.
            catalog_price = _money(record.price)
            if catalog_price is None:
                raise _Refused("invalid_price")

            raw_quantity = _decimal(line.quantity)
            if (
                raw_quantity is None
                or not raw_quantity.is_finite()
                or not Decimal(1) <= raw_quantity <= Decimal(_MAX_QUANTITY)
                or raw_quantity != raw_quantity.to_integral()
            ):
                raise _Refused("invalid_quantity")
            quantity = int(raw_quantity)

            cart_price = _money(line.price)
            if catalog_price != cart_price:
                # The catalog remains authoritative, but silently charging a changed
                # price would bypass the shopper's confirmation. The host must refresh
                # its cart and ask the shopper to confirm the new amount.
                raise _Refused("cart_reconfirmation_required")

            title = str(record.title).strip()[:_MAX_TITLE]
            if not title:
                raise _Refused("invalid_title")
            items.append(
                _PricedItem(
                    product_id=product_id,
                    # The catalog's title, not the cart's: the cart's is model-authored
                    # text and this is rendered on an MP-branded page.
                    title=title,
                    quantity=quantity,
                    unit_price=catalog_price,
                )
            )

        if currency is None:  # unreachable: an empty cart returns before this
            raise _Refused("invalid_currency")
        if getattr(cart, "currency", None) != currency:
            raise _Refused("currency_mismatch")
        return items, currency

    def _validated_url(
        self,
        order: dict[str, Any],
        reference: str,
        total_amount: str,
        currency: str,
    ) -> str | None:
        """Return the hosted checkout URL only when the response matches intent."""
        order_id = order.get("id")
        if not _valid_identifier(order_id):
            logger.error("Mercado Pago returned an order without a valid id.")
            return None
        checkout_url = order.get("checkout_url")
        if not isinstance(checkout_url, str) or not self._is_checkout_url(checkout_url):
            logger.error(
                "Order %s came back without a usable Mercado Pago checkout URL.",
                _safe_str(order_id),
            )
            return None
        expected_state = ("online", "manual", "created", reference, currency)
        returned_state = (
            order.get("type"),
            order.get("processing_mode"),
            order.get("status"),
            order.get("external_reference"),
            order.get("currency"),
        )
        if returned_state != expected_state or _money(
            order.get("total_amount")
        ) != Decimal(total_amount):
            logger.error(
                "Order %s did not match the confirmed checkout snapshot.",
                _safe_str(order_id),
            )
            return None
        return checkout_url

    async def _create(
        self,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """The API call. Uses the official SDK, which also means the base URL is not
        configurable here — it is a private constant in ``mercadopago.config.Config`` —
        so the seller's credential cannot be pointed at another host by configuration."""
        # RequestOptions is mutable and shared by an SDK instance. Clone it (including
        # current and future SDK-level settings) and clone its headers before adding the
        # request-scoped idempotency key.
        options = copy(self._sdk.request_options)
        custom_headers = dict(options.custom_headers or {})
        custom_headers["x-idempotency-key"] = idempotency_key
        options.custom_headers = custom_headers
        try:
            # The SDK is synchronous (requests); keep the event loop free.
            result = await asyncio.to_thread(self._sdk.order().create, body, options)
        except requests.RequestException:
            logger.error(
                "Mercado Pago was unreachable; the host's own checkout card takes over."
            )
            return None
        except Exception:  # pylint: disable=broad-exception-caught
            # SDK errors can contain request URLs and headers. Keep the fallback safe
            # and avoid propagating or logging those details through the host.
            logger.error("Unexpected Mercado Pago SDK failure; checkout was not created.")
            return None

        if not isinstance(result, dict):
            logger.error("Mercado Pago returned an invalid SDK response.")
            return None
        raw_status = result.get("status")
        status = (
            raw_status
            if isinstance(raw_status, int) and not isinstance(raw_status, bool)
            else None
        )
        payload = result.get("response")
        if (
            status is None
            or not 200 <= status < 300
            or not isinstance(payload, dict)
        ):
            logger.error(
                "Order creation failed (HTTP %s): error=%s causes=%s",
                status,
                # Only MP's own error identifiers are logged. The rest of a 4xx body
                # echoes the rejected payload — item titles, prices, the reference —
                # which does not belong in logs.
                _safe_str(payload.get("error")) if isinstance(payload, dict) else None,
                _cause_codes(payload),
            )
            return None
        return payload

    @staticmethod
    def _is_checkout_url(url: str) -> bool:
        if len(url) > _MAX_CHECKOUT_URL or any(
            ord(character) < 32 or ord(character) == 127 for character in url
        ):
            return False
        try:
            parts = urlsplit(url)
            return (
                parts.scheme == "https"
                and parts.hostname in _CHECKOUT_HOSTS
                and parts.username is None
                and parts.password is None
                and parts.port in (None, 443)
            )
        except ValueError:
            # Invalid bracket/port syntax must fail closed, not escape the adapter.
            return False


def _safe_str(value: Any) -> str | None:
    """MP's ``error`` field is a short identifier (``bad_request``); anything longer is
    not that field and is dropped rather than logged."""
    if not isinstance(value, str) or _LOG_IDENTIFIER.fullmatch(value) is None:
        return None
    return value


def _valid_identifier(value: Any) -> bool:
    """Accept an opaque host/API identifier only when it is bounded and printable."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_IDENTIFIER
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _reference(idempotency_key: str) -> str:
    """An opaque ``external_reference``, derived from the operation's idempotency key.

    Deriving it keeps a retry's body byte-identical, which is what the Orders API
    requires of a reused key. It is a UUIDv5 of the key rather than the key itself: the
    key may be a host-internal identifier, and Mercado Pago's records are not the place
    to publish one. Never the session id, which is caller-supplied in the reference host
    and would let a payment be bound to a session its payer does not own.
    """
    return f"mpca-{uuid5(NAMESPACE_URL, idempotency_key)}"


def _decimal(value: Any) -> Decimal | None:
    """Parse a bounded decimal representation without accepting unbounded input."""
    try:
        text = str(value)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if len(text) > _MAX_DECIMAL_TEXT:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _money(value: Any) -> Decimal | None:
    """Return a positive finite two-decimal amount, or None when it is unsafe."""
    parsed = _decimal(value)
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        return None
    try:
        with localcontext() as context:
            context.prec = 96
            return parsed if parsed == parsed.quantize(_AMOUNT_QUANTUM) else None
    except InvalidOperation:
        return None


def _amount(value: Decimal) -> str:
    """Orders represents monetary values as fixed two-decimal JSON strings."""
    with localcontext() as context:
        context.prec = 96
        return format(value.quantize(_AMOUNT_QUANTUM), "f")


def _order_total(items: list[_PricedItem]) -> str:
    """Sum line totals exactly within the bounded cart size."""
    with localcontext() as context:
        context.prec = 96
        total = sum(
            (item.unit_price * item.quantity for item in items), Decimal("0")
        )
        return _amount(total)


def _cause_codes(payload: Any) -> list[Any]:
    """The numeric codes from MP's ``cause`` list — enough to look the rejection up in
    the API reference, without the descriptions that quote the payload."""
    if not isinstance(payload, dict):
        return []
    causes = payload.get("cause")
    if not isinstance(causes, list):
        return []
    return [
        code
        for cause in causes
        if isinstance(cause, dict)
        and isinstance((code := cause.get("code")), int)
        and not isinstance(code, bool)
    ]
