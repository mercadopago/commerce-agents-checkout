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
            # Persist this pair before the first API call and load it on every retry.
            attempt = await self.checkout_attempts.get_or_create(session, cart)
            return await self.mercadopago.checkout_handoff(
                session,
                cart,
                external_reference=attempt.external_reference,
                idempotency_key=attempt.idempotency_key,
                # Set once the attempt has already ended in CheckoutOutcomeUnknown.
                # Without it a later local refusal — a catalog price that moved in the
                # meantime is enough — comes back as `[]`, telling the host it may
                # charge elsewhere while the first attempt's Order may still be
                # payable. `examples/seller_integration.py` takes the stricter route
                # and refuses to call the adapter again until the attempt is
                # reconciled; either is safe, silently retrying without both is not.
                recovering=attempt.outcome_unknown,
            )

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
It cannot authenticate the shopper — only the host can. It never derives payment fields
from the caller-supplied session id: the seller must provide its own persisted
``external_reference`` and idempotency key. A deployment that leaves ``X-Session-Id``
unauthenticated still has an unauthenticated cart. Authenticate the session in the host
before wiring this in.
"""

# pylint: disable=too-many-lines
# One payment boundary, kept in one readable unit. Most of the length is the reasoning
# behind each refusal, which is the part a reviewer of a payment path actually needs;
# splitting it to satisfy a line count would scatter that reasoning across modules.

from __future__ import annotations

import asyncio
import logging
import re
from copy import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

import mercadopago
import requests

from .types import CheckoutHandoff

if TYPE_CHECKING:  # imported for typing only; never present at runtime
    from shopping_agent import Cart, ShoppingSessionContext

logger = logging.getLogger(__name__)

# MP caps an order item's title; longer titles are rejected outright.
_MAX_TITLE = 256
# These are adapter safety budgets, not Orders API limits. They bound catalog work and
# monetary exposure even when the host keeps commerce-agents' more permissive defaults.
_MAX_CART_ITEMS = 20
_MAX_QUANTITY = 10
_MAX_IDENTIFIER = 256
# mercadopago 3.5.0 rejects a longer x-idempotency-key when RequestOptions is built,
# which would raise out of the adapter instead of failing closed.
_MAX_IDEMPOTENCY_KEY = 64
_IDEMPOTENCY_HEADER = "x-idempotency-key"
# Orders answers 423 when the idempotency key is locked by a request still in flight.
# It is a "repeat later", not a rejection, so it never means "no Order was created".
_HTTP_LOCKED = 423
# What a catalog record has to expose. Checked as a group so that a record which is not
# a record at all — a dict is the usual slip — says so, instead of being reported as
# whichever attribute happened to be read first.
_CATALOG_FIELDS = ("title", "price", "currency", "in_stock")
_MAX_CHECKOUT_URL = 2048
_MAX_DECIMAL_TEXT = 64
_MAX_LOG_CODES = 20
_MAX_NUMERIC_ERROR_CODE = 2**31 - 1
_AMOUNT_QUANTUM = Decimal("0.01")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_EXTERNAL_REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Visible ASCII only. Anything outside it is rejected downstream rather than here —
# ``requests`` refuses a header value with leading whitespace and ``http.client``
# encodes header values as latin-1 — and that rejection arrives as a transport error,
# so the adapter would report that an Order may exist for a request that never left the
# process. Every key shape the Orders API documents fits in this set.
_IDEMPOTENCY_KEY = re.compile(rf"[!-~]{{1,{_MAX_IDEMPOTENCY_KEY}}}\Z")

# The create-Order codes documented by Mercado Pago, plus stable gateway/legacy codes
# observed for the same endpoint. A syntax-only filter is insufficient here: a seller
# reference or token-shaped value can also consist only of identifier characters.
_LOGGABLE_ORDER_ERROR_CODES = frozenset(
    {
        "PA_UNAUTHORIZED_RESULT_FROM_POLICIES",
        "bad_request",
        "empty_required_header",
        "forbidden",
        "idempotency_key_already_used",
        "idempotency_validation_failed",
        "internal_error",
        "invalid_credentials",
        "invalid_email_for_sandbox",
        "invalid_header_value",
        "invalid_idempotency_key_length",
        "invalid_order_type",
        "invalid_properties",
        "invalid_token",
        "invalid_total_amount",
        "json_syntax_error",
        "maximum_items",
        "minimum_items",
        "minimum_properties",
        "order_builder_without_transactions",
        "order_invalid_sponsor_id",
        "property_type",
        "property_value",
        "required_properties",
        "resource_locked",
        "too_many_requests",
        "unsupported_properties",
        "usage_quota_exceeded",
    }
)

# Checkout Pro Orders uses an ISO 8601 duration, not an absolute preference timestamp.
# Keeping it relative also makes retries carry an identical body.
_ORDER_EXPIRATION = "P1D"

# Mercado Pago's fixed Platform ID for this adapter. Its human-readable registry label
# is managed separately and is not part of the package's public contract. The ID
# identifies the integration rather than the seller, so it is sent on every order and is
# not configurable: attribution must not depend on a host remembering to set it.
# `application_id` is deliberately absent — Orders rejects a caller-supplied one and
# derives it from the Access Token — and `sponsor` needs a real account id that only a
# marketplace deployment has.
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


_UNREACHABLE = object()
"""A transport failure: the request may or may not have reached Mercado Pago."""


class _Refused(Exception):
    """A line that cannot be priced honestly. Aborts the handoff; the host's own
    checkout card takes over."""


class CheckoutOutcomeUnknown(RuntimeError):
    """The create or cleanup request may have taken effect remotely.

    Returning the host's fallback checkout in this state could leave two payable
    checkouts for one purchase. The host must stop that fallback and reuse
    the same ``idempotency_key`` and ``external_reference`` if it retries. Both
    identifiers are available for reconciliation.

    The exception text contains no recovery identifier. The key, seller reference, and
    an Order ID when cleanup had already identified one remain available as attributes
    for controlled recovery without leaking into an ordinary exception log.
    """

    def __init__(
        self,
        *,
        external_reference: str,
        idempotency_key: str,
        reason: str,
        order_id: str | None = None,
    ):
        self.idempotency_key = idempotency_key
        self.external_reference = external_reference
        self.order_id = order_id
        self.reason = reason
        super().__init__(
            "Checkout outcome is unknown; reconcile the operation before retrying or "
            "falling back."
        )


@dataclass(frozen=True)
class _CartLine:
    """One cart line, copied out of the caller's object before anything is awaited."""

    product_id: Any
    price: Any
    quantity: Any


@dataclass(frozen=True)
class _CartSnapshot:
    """The confirmed cart, frozen at entry.

    The cart belongs to the caller and stays mutable while this coroutine awaits the
    catalog. Reading it again after an ``await`` would let a line, a quantity or the
    currency change between validation and the payload — and iterating it live lets a
    catalog that appends to it loop forever past the size cap. Everything is copied
    once, up front, and only this snapshot is used afterwards.
    """

    lines: tuple[_CartLine, ...]
    currency: Any


@dataclass(frozen=True)
class _ResponseCheck:  # pylint: disable=too-few-public-methods
    """The two independent verdicts on a created-order response.

    ``url`` is present only when the Order is safe to hand to the shopper.
    ``correlated`` says whether the response is about this attempt at all, which is a
    separate question: it is what decides whether cancelling the returned id is our
    business or someone else's.
    """

    url: str | None
    correlated: bool


@dataclass(frozen=True)
class _PricedItem:  # pylint: disable=too-few-public-methods
    """A cart line resolved against the trusted catalog.

    The product id is deliberately absent: Orders does not require ``external_code``,
    whose constraints are a separate seller concern, and nothing else here needs it.
    """

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

    The constructor surface is deliberately two arguments: the SDK that calls Mercado
    Pago and the trusted catalog that prices cart lines. Everything else is either
    derived (the currency comes from the catalog) or scoped to one checkout call.

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
        external_reference: str,
        idempotency_key: str,
        recovering: bool = False,
    ) -> list[CheckoutHandoff]:
        """Create a handoff for a seller backend's two-argument wrapper.

        The many early returns are the design: every branch that is not an order we
        are confident in leaves through one of them.

        ``idempotency_key`` identifies one checkout operation and is used exactly as
        supplied — never replaced by a fresh one, because silently minting another key
        would turn a rejected duplicate into a second payable order. Reusing a key with
        a different payload is refused by Mercado Pago with HTTP 409, so a new purchase
        needs a new key. The seller backend must persist it before the first call and
        supply the same value across retries and process restarts.

        ``external_reference`` is the seller business identifier required by this
        Checkout Pro Orders flow. It may be an ecommerce order number and does not need
        to be a UUID. The backend must persist it with the idempotency key. The adapter
        accepts up to 64 letters, digits, hyphens and underscores; invalid values fail
        closed before any catalog lookup or API call.

        Returns an empty list — letting the host's own checkout card take over — only
        when no order was created or when a refused order was confirmed cancelled.
        Raises :class:`CheckoutOutcomeUnknown` after any POST whose outcome cannot be
        proven, because falling back in that state could expose two payable checkouts.
        """
        if not _valid_idempotency_key(idempotency_key):
            # Fail closed: generating a replacement here would create an order the
            # caller believes it already deduplicated.
            logger.warning("Refusing to create an order: invalid_idempotency_key")
            return self._refused(
                "invalid_idempotency_key",
                recovering=recovering,
                idempotency_key=idempotency_key if isinstance(idempotency_key, str) else "",
                external_reference=external_reference,
            )
        if not _valid_external_reference(external_reference):
            logger.warning("Refusing to create an order: invalid_external_reference")
            return self._refused(
                "invalid_external_reference",
                recovering=recovering,
                idempotency_key=idempotency_key,
                external_reference=(
                    external_reference if isinstance(external_reference, str) else ""
                ),
            )
        snapshot = _snapshot(cart)
        if snapshot is None or not snapshot.lines:
            return self._refused(
                "unusable_cart",
                recovering=recovering,
                idempotency_key=idempotency_key,
                external_reference=external_reference,
            )

        # The reason leaves the handler as a value, and the refusal happens after it.
        # While recovering, `_refused` raises — and raising inside this `except` would
        # attach the catalog's own exception to `CheckoutOutcomeUnknown.__context__`,
        # which is the same leak the SDK path already had to close. The catalog is a
        # caller-owned boundary that may use any transport, so its exception text is
        # exactly what must not become reachable.
        refusal_reason: str | None = None
        try:
            items, currency = await self._priced_items(session, snapshot)
        except _Refused as refusal:
            logger.warning("Refusing to create an order: %s", refusal)
            refusal_reason = str(refusal)
        except Exception:  # pylint: disable=broad-exception-caught
            # Do not log its exception text or traceback: either can contain a
            # sensitive URL.
            logger.error("Catalog lookup failed; refusing to create an order.")
            refusal_reason = "catalog_unavailable"
        if refusal_reason is not None:
            return self._refused(
                refusal_reason,
                recovering=recovering,
                idempotency_key=idempotency_key,
                external_reference=external_reference,
            )

        total_amount = _order_total(items)
        if total_amount is None:
            logger.warning("Refusing to create an order: amount_out_of_range")
            return self._refused(
                "amount_out_of_range",
                recovering=recovering,
                idempotency_key=idempotency_key,
                external_reference=external_reference,
            )
        order = await self._create(
            _order_body(items, total_amount, external_reference),
            idempotency_key,
            external_reference,
        )
        if order is None:
            # Mercado Pago rejected *this* request. That says nothing about whether an
            # earlier attempt under the same key created an Order, so recovery still
            # may not fall back.
            return self._refused(
                "order_rejected",
                recovering=recovering,
                idempotency_key=idempotency_key,
                external_reference=external_reference,
            )

        checked = self._checked_response(
            order, external_reference, total_amount, currency
        )
        if checked.url is None:
            order_id = order.get("id")
            if not checked.correlated:
                # Cleanup is only ours to perform on an Order we proved is ours. A
                # response whose reference does not match ours describes some other
                # operation, so cancelling the id it carries could cancel a stranger's
                # payable Order. Stop instead, and let the host reconcile.
                logger.error(
                    "Mercado Pago returned an order that is not correlated to this "
                    "attempt; refusing to cancel it."
                )
                self._raise_outcome_unknown(
                    idempotency_key,
                    external_reference,
                    "uncorrelated_response",
                    order_id=order_id if _valid_identifier(order_id) else None,
                )
            # The order exists at Mercado Pago even though we refuse to hand it over.
            # Leaving it would strand a payable order on the seller's account for the
            # whole expiry window, so cancel it before falling back.
            if not await self._cancel(order_id, idempotency_key, external_reference):
                self._raise_outcome_unknown(
                    idempotency_key,
                    external_reference,
                    "cleanup_not_confirmed",
                    order_id=order_id if _valid_identifier(order_id) else None,
                )
            return []
        # No adapter-specific label: the commerce-agents host owns its UI.
        return [CheckoutHandoff(url=checked.url)]

    # Never call this from inside an ``except`` block: while recovering it raises, and
    # the interpreter would attach the exception being handled to ``__context__``.
    # Record the reason in the handler and refuse after it returns.
    def _refused(
        self,
        reason: str,
        *,
        recovering: bool,
        idempotency_key: str,
        external_reference: str,
    ) -> list[CheckoutHandoff]:
        """The single place a definitive refusal turns into a return value.

        Outside recovery an empty list is honest: nothing in these paths reached
        Mercado Pago, so no Order can exist and the host's own checkout is safe.

        During recovery the same empty list would be a lie. The host only retries after
        a :class:`CheckoutOutcomeUnknown`, which means an earlier attempt carrying this
        same key may already have created a payable Order — and a refusal here is
        decided locally, without asking Mercado Pago about it. A catalog price that
        moved between the two calls is enough to reach this point. So recovery never
        releases the fallback; it hands the host something to reconcile instead.
        """
        if recovering:
            self._raise_outcome_unknown(idempotency_key, external_reference, reason)
        return []

    # -- internals ---------------------------------------------------------------

    async def _priced_items(  # pylint: disable=too-many-branches
        self, session: "ShoppingSessionContext", snapshot: _CartSnapshot
    ) -> tuple[list[_PricedItem], str]:
        """One order item per cart line, priced from the catalog record rather than
        from the line, plus the currency those records agree on.

        Reads only the frozen snapshot: the caller's cart may change while this awaits.

        Raises :class:`_Refused` on anything that cannot be priced.
        """
        items: list[_PricedItem] = []
        seen: set[str] = set()
        for line in snapshot.lines:
            if not _valid_identifier(line.product_id):
                raise _Refused("invalid_product_id")
            if line.product_id in seen:
                # The caps below are per line, so the same product spread over several
                # lines would multiply past them. Aggregating silently would change what
                # the shopper confirmed, so this is a refusal.
                raise _Refused("duplicate_product")
            seen.add(line.product_id)

        # The same confirmed products must produce a byte-identical Orders body even if
        # the host reconstructs its cart in a different insertion order for a retry.
        # product_id is seller-owned and unique at this point, so it is the stable key.
        currency: str | None = None
        for line in sorted(snapshot.lines, key=lambda line: line.product_id):
            product_id = line.product_id
            record = await self._catalog.get_product_details(session, product_id)
            if record is None:
                raise _Refused("product_not_found")
            if any(not hasattr(record, field) for field in _CATALOG_FIELDS):
                # Reporting this as `out_of_stock` sent integrators looking through their
                # inventory for a record that was simply the wrong shape.
                raise _Refused("invalid_catalog_record")
            if record.in_stock is not True:
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
                    # The catalog's title, not the cart's: the cart's is model-authored
                    # text and this is rendered on an MP-branded page.
                    title=title,
                    quantity=quantity,
                    unit_price=catalog_price,
                )
            )

        if currency is None:  # unreachable: an empty snapshot returns before this
            raise _Refused("invalid_currency")
        if snapshot.currency is None:
            # Distinct from a mismatch: nothing was set, so "they disagree" would send
            # the integrator comparing two values when one does not exist.
            raise _Refused("missing_cart_currency")
        if snapshot.currency != currency:
            raise _Refused("currency_mismatch")
        return items, currency

    def _checked_response(
        self,
        order: dict[str, Any],
        external_reference: str,
        total_amount: str,
        currency: str,
    ) -> _ResponseCheck:
        """What the created-order response proved about this attempt.

        Two different questions, and the caller needs both. ``url`` answers whether the
        order is safe to hand to the shopper. ``correlated`` answers whether this
        response is about *our* attempt at all — which decides whether cleaning it up
        is our business.
        """
        correlated = order.get("external_reference") == external_reference
        order_id = order.get("id")
        if not _valid_identifier(order_id):
            logger.error("Mercado Pago returned an order without a valid id.")
            return _ResponseCheck(url=None, correlated=correlated)
        checkout_url = order.get("checkout_url")
        if not isinstance(checkout_url, str) or not self._is_checkout_url(
            checkout_url, order_id
        ):
            logger.error("Mercado Pago returned an order without a usable checkout URL.")
            return _ResponseCheck(url=None, correlated=correlated)
        expected_state = ("online", "manual", "created", currency, _ORDER_EXPIRATION)
        returned_state = (
            order.get("type"),
            order.get("processing_mode"),
            order.get("status"),
            order.get("currency"),
            # The 24-hour window is what stops a stale link being paid at an old price,
            # so an order that came back without it is not the order we asked for.
            order.get("expiration_time"),
        )
        if (
            returned_state != expected_state
            or not correlated
            or _money(order.get("total_amount")) != Decimal(total_amount)
        ):
            logger.error("Mercado Pago returned an order that did not match the snapshot.")
            return _ResponseCheck(url=None, correlated=correlated)
        return _ResponseCheck(url=checkout_url, correlated=True)

    async def _cancel(
        self, order_id: Any, idempotency_key: str, external_reference: str
    ) -> bool:
        """Cancel a refused order and return whether the API confirmed the cleanup.

        Orders requires an idempotency key on cancellation too. A deterministic key
        makes a retry safe without reusing the create operation's key. Failure is not a
        safe fallback: the caller raises :class:`CheckoutOutcomeUnknown` instead.
        """
        if not _valid_identifier(order_id):
            logger.error("Refused an order whose id could not be read; cannot cancel it.")
            return False
        try:
            options = self._request_options(_cancel_key(idempotency_key))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error("Could not prepare cancellation for a refused order.")
            return False
        # Same reason as _post_once: raised after the handler, never inside it, so the
        # sanitized exception cannot carry the SDK's own error in ``__context__``.
        interrupted = False
        try:
            result = await asyncio.to_thread(self._sdk.order().cancel, order_id, options)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:  # pylint: disable=broad-exception-caught
            # Same reasoning as _create: SDK errors can carry request URLs and headers.
            logger.error("Could not cancel a refused order.")
            return False
        if interrupted:
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "cleanup_interrupted",
                order_id=order_id,
            )
        status = result.get("status") if isinstance(result, dict) else None
        response = result.get("response") if isinstance(result, dict) else None
        cleanup_confirmed = (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 200 <= status < 300
            and isinstance(response, dict)
            and response.get("id") == order_id
            and response.get("status") == "canceled"
        )
        if not cleanup_confirmed:
            logged_status = (
                status
                if isinstance(status, int) and not isinstance(status, bool)
                else None
            )
            logger.error(
                "Could not confirm cancellation of a refused order (HTTP %s).",
                logged_status,
            )
            return False
        logger.info("Cancelled a refused order.")
        return True

    async def _create(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        external_reference: str,
    ) -> dict[str, Any] | None:
        """The API call. Uses the official SDK, which also means the base URL is not
        configurable here — it is a private constant in ``mercadopago.config.Config`` —
        so the seller's credential cannot be pointed at another host by configuration.

        A transport failure does not prove the POST had no effect, so it is retried once
        with the same key and the same body. Mercado Pago replays an identical request
        instead of duplicating it: the retry either creates the order or returns the one
        the lost response described. Both attempts failing is the residual window, and it
        must be surfaced so the host can reconcile.
        """
        try:
            # RequestOptions is mutable and shared by an SDK instance. Clone it (including
            # current and future SDK-level settings) and clone its headers before adding
            # the request-scoped idempotency key. Building the options can itself raise —
            # the SDK bounds the header — so it stays inside the handled boundary.
            options = self._request_options(idempotency_key)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error("Could not prepare the request; checkout was not created.")
            return None

        result = await self._post_once(
            body, options, idempotency_key, external_reference
        )
        prior_transport_failure = result is _UNREACHABLE
        if prior_transport_failure:
            logger.warning("Mercado Pago was unreachable; retrying the same request once.")
            result = await self._post_once(
                body, options, idempotency_key, external_reference
            )
        if result is _UNREACHABLE:
            logger.error(
                "Mercado Pago stayed unreachable. An order may exist; reconcile the "
                "operation before charging again."
            )
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "transport_failure",
            )

        if not isinstance(result, dict):
            logger.error("Mercado Pago returned an invalid SDK response.")
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "invalid_sdk_response",
            )
        raw_status = result.get("status")
        status = (
            raw_status
            if isinstance(raw_status, int) and not isinstance(raw_status, bool)
            else None
        )
        payload = result.get("response")
        if status is None:
            logger.error("Mercado Pago returned an invalid HTTP status.")
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "invalid_sdk_response",
            )
        if status == _HTTP_LOCKED:
            # Orders holds the idempotency key while a concurrent request for it is
            # still in flight, and tells the caller to repeat later. That concurrent
            # request may already have created a payable Order, so this is the one 4xx
            # that must never release the host fallback. The host owns the key and the
            # scheduling, so it repeats with the same key rather than us sleeping
            # inside a payment call.
            logger.error(
                "Mercado Pago is still processing a request for this idempotency key "
                "(HTTP %s); retry the same key before falling back.",
                status,
            )
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "resource_locked",
            )
        if 400 <= status < 500 and status not in (408, 409):
            if prior_transport_failure:
                # This rejection describes only the retry. The first POST may have
                # succeeded before its response was lost, so fallback is still unsafe.
                logger.error(
                    "Retry after a transport failure was rejected (HTTP %s); the "
                    "original Order outcome is unknown.",
                    status,
                )
                self._raise_outcome_unknown(
                    idempotency_key,
                    external_reference,
                    "retry_inconclusive",
                )
            logger.error(
                "Order creation failed (HTTP %s): codes=%s",
                status,
                # Only MP's own error identifiers are logged. The rest of a 4xx body
                # echoes the rejected payload — item titles, prices, the reference —
                # which does not belong in logs.
                _error_codes(payload),
            )
            return None
        if not 200 <= status < 300 or not isinstance(payload, dict):
            logger.error("Order creation outcome is unknown (HTTP %s).", status)
            self._raise_outcome_unknown(
                idempotency_key,
                external_reference,
                "ambiguous_response",
            )
        return payload

    async def _post_once(
        self,
        body: dict[str, Any],
        options: Any,
        idempotency_key: str,
        external_reference: str,
    ) -> Any:
        """One attempt. ``_UNREACHABLE`` marks a transport failure, which may still have
        reached Mercado Pago and is therefore safe to replay with the same key."""
        # The reason is recorded here and raised after the handler returns. Raising
        # inside an ``except`` block would set ``__context__`` on the sanitized
        # exception, and ``raise ... from None`` only hides that from the printed
        # traceback: the raw SDK error object, request URL and headers included, stays
        # reachable to any host logging or instrumentation that walks the chain.
        unknown: str | None = None
        try:
            # The SDK is synchronous (requests); keep the event loop free. Note that
            # cancelling this coroutine does not cancel the thread: the POST may still
            # land, which is another reason the key must stay stable.
            return await asyncio.to_thread(self._sdk.order().create, body, options)
        except asyncio.CancelledError:
            unknown = "create_interrupted"
        except requests.RequestException:
            return _UNREACHABLE
        except Exception:  # pylint: disable=broad-exception-caught
            # SDK errors can contain request URLs and headers. Keep the fallback safe
            # and avoid propagating or logging those details through the host.
            logger.error("Unexpected Mercado Pago SDK failure; outcome is unknown.")
            unknown = "sdk_failure"
        self._raise_outcome_unknown(idempotency_key, external_reference, unknown)

    def _request_options(self, idempotency_key: str) -> Any:
        """Clone the SDK options and install exactly one request-scoped key.

        ``SDK.order()`` otherwise reuses ``sdk.request_options``. Its ``custom_headers``
        mapping is mutable, so changing it in place would leak one checkout's key into
        concurrent calls. Building blank options would lose caller-configured timeout and
        retry settings; copying preserves them while isolating the request header.
        """
        options = copy(self._sdk.request_options)
        # Drop any inherited spelling first: requests matches headers
        # case-insensitively, so a lingering `X-Idempotency-Key` could otherwise win
        # on the wire while the adapter tracks a different operation key.
        custom_headers = {
            name: value
            for name, value in (options.custom_headers or {}).items()
            if name.lower() != _IDEMPOTENCY_HEADER
        }
        custom_headers[_IDEMPOTENCY_HEADER] = idempotency_key
        options.custom_headers = custom_headers
        return options

    @staticmethod
    def _raise_outcome_unknown(
        idempotency_key: str,
        external_reference: str,
        reason: str,
        *,
        order_id: str | None = None,
    ) -> NoReturn:
        """Raise a sanitized recovery signal without chaining SDK secrets."""
        raise CheckoutOutcomeUnknown(
            idempotency_key=idempotency_key,
            external_reference=external_reference,
            order_id=order_id,
            reason=reason,
        ) from None

    @staticmethod
    def _is_checkout_url(url: str, order_id: str) -> bool:
        """Whether this URL is Mercado Pago's *and* is the link for this Order.

        The host allowlist only proves the shopper lands on Mercado Pago. It does not
        prove the link pays the Order we just validated: a response carrying our
        ``id`` alongside a checkout URL for a different order is well-formed, passes
        every host check, and would hand the shopper someone else's payment. Checkout
        Pro carries the Order in the link's ``order_id``, so that is what binds them.
        """
        if len(url) > _MAX_CHECKOUT_URL or any(
            ord(character) < 32 or ord(character) == 127 for character in url
        ):
            return False
        try:
            parts = urlsplit(url)
            if not (
                parts.scheme == "https"
                and parts.hostname in _CHECKOUT_HOSTS
                and parts.username is None
                and parts.password is None
                and parts.port in (None, 443)
            ):
                return False
            # parse_qs decodes percent-escapes, so a re-encoded id cannot slip past the
            # comparison. Requiring exactly one value rejects a duplicated parameter,
            # whose precedence is a parser detail rather than something we can rely on.
            order_ids = parse_qs(parts.query, keep_blank_values=True).get("order_id", ())
            return len(order_ids) == 1 and order_ids[0] == order_id
        except ValueError:
            # Invalid bracket/port syntax must fail closed, not escape the adapter.
            return False


def _safe_error_code(value: Any) -> str | None:
    """Return only a documented or explicitly recognized Order error code."""
    if not isinstance(value, str) or value not in _LOGGABLE_ORDER_ERROR_CODES:
        return None
    return value


def _snapshot(cart: Any) -> _CartSnapshot | None:
    """Copy the cart's primitives before anything is awaited.

    Returns None when the cart cannot be read at all, or when it holds more lines than
    the adapter accepts. Reading the list once also bounds the work: a catalog that
    appends to the caller's list cannot make the pricing loop run forever.
    """
    try:
        items = list(getattr(cart, "items", None) or ())
        if len(items) > _MAX_CART_ITEMS:
            logger.warning("Refusing to create an order: too_many_items")
            return None
        lines = tuple(
            _CartLine(
                product_id=getattr(line, "product_id", None),
                price=getattr(line, "price", None),
                quantity=getattr(line, "quantity", None),
            )
            for line in items
        )
        return _CartSnapshot(lines=lines, currency=getattr(cart, "currency", None))
    except Exception:  # pylint: disable=broad-exception-caught
        # The cart is the caller's object; reading it must not raise out of the adapter.
        logger.warning("Refusing to create an order: unreadable_cart")
        return None


def _valid_idempotency_key(value: Any) -> bool:
    """Bounded by what the supported SDK will actually transmit, not by our own limit.

    Accepting a value the HTTP stack later refuses is worse than refusing it here: the
    refusal surfaces as a transport failure, and a transport failure means "the POST
    may have reached Mercado Pago". Validating the header contract locally keeps a
    caller mistake a caller mistake.
    """
    return isinstance(value, str) and _IDEMPOTENCY_KEY.fullmatch(value) is not None


def _valid_identifier(value: Any) -> bool:
    """Accept an opaque host/API identifier only when it is bounded and printable."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_IDENTIFIER
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_external_reference(value: Any) -> bool:
    """Apply the adapter's bounded, opaque seller-reference contract locally."""
    return isinstance(value, str) and _EXTERNAL_REFERENCE.fullmatch(value) is not None


def _cancel_key(idempotency_key: str) -> str:
    """A stable, operation-specific key for cancelling the created order."""
    return str(uuid5(NAMESPACE_URL, f"mpca-cancel:{idempotency_key}"))


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


def _order_body(
    items: list[_PricedItem], total_amount: str, external_reference: str
) -> dict[str, Any]:
    """The exact Orders payload this adapter sends, in one place.

    Keeping it here rather than inline is what makes "the same confirmed cart produces
    a byte-identical body on a retry" something a reader can check at a glance.
    """
    return {
        "type": "online",
        "processing_mode": "manual",
        "total_amount": total_amount,
        "items": [item.order_payload() for item in items],
        # An order with no expiry stays payable at yesterday's price after the cart has
        # moved on.
        "expiration_time": _ORDER_EXPIRATION,
        "integration_data": {"platform_id": _PLATFORM_ID},
        "external_reference": external_reference,
    }


def _order_total(items: list[_PricedItem]) -> str | None:
    """Sum line totals exactly within the bounded cart size.

    Returns None when the amount cannot be represented: a catalog price near the Decimal
    limits can overflow once multiplied by a quantity, and that must fail closed like
    every other refusal rather than raise out of the adapter.
    """
    try:
        with localcontext() as context:
            context.prec = 96
            total = sum(
                (item.unit_price * item.quantity for item in items), Decimal("0")
            )
            return _amount(total)
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _error_codes(payload: Any) -> list[str]:
    """Collect bounded error identifiers across current and legacy API envelopes.

    Messages and details can echo request data, so textual values must be explicitly
    recognized and legacy numeric causes are bounded. Codes are globally deduplicated
    and capped to prevent a malformed SDK response from amplifying logs.
    """
    if not isinstance(payload, dict):
        return []

    codes: list[str] = []
    legacy_error = _safe_error_code(payload.get("error"))
    if legacy_error is not None:
        codes.append(legacy_error)

    errors = payload.get("errors")
    if isinstance(errors, list):
        for error in errors[:_MAX_LOG_CODES]:
            code = (
                _safe_error_code(error.get("code"))
                if isinstance(error, dict)
                else None
            )
            if code is not None and code not in codes:
                codes.append(code)
                if len(codes) >= _MAX_LOG_CODES:
                    return codes

    causes = payload.get("cause")
    if isinstance(causes, list):
        for cause in causes[:_MAX_LOG_CODES]:
            raw_code = cause.get("code") if isinstance(cause, dict) else None
            code = (
                str(raw_code)
                if isinstance(raw_code, int)
                and not isinstance(raw_code, bool)
                and 0 <= raw_code <= _MAX_NUMERIC_ERROR_CODE
                else None
            )
            if code is not None and code not in codes:
                codes.append(code)
                if len(codes) >= _MAX_LOG_CODES:
                    return codes
    return codes
