# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Mercado Pago Checkout Pro as a ``StorefrontBackend.checkout_handoff`` provider.

commerce-agents' ``checkout`` tool only ever renders the cart — nothing in the agent
places an order or moves money, and the hosted checkout URL is filled in by the backend
*after* the model's tool call, so it never reaches the model. This class fills that one
method with a real Checkout Pro preference::

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
``POST /checkout/preferences`` would therefore let whoever drives the conversation
decide what the shopper is charged, on the seller's own ``APP_USR-`` account. So this
class never reads a price from the cart. It re-reads every line from the seller's own
catalog through ``StorefrontBackend.get_product_details`` — an abstract method every
backend already implements, and one that resolves a variant id to that variant — and
prices the preference from that. Passing ``catalog=self`` is the whole wiring.

The cart is still what decides *which* products and *how many*: quantity is capped
upstream by the executor's gates, and a line whose product is unknown or out of stock
aborts the handoff.

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
import hashlib
import json
import logging
from copy import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

import mercadopago
import requests

from .types import CheckoutHandoff

if TYPE_CHECKING:  # imported for typing only; never present at runtime
    from shopping_agent import Cart, ShoppingSessionContext

logger = logging.getLogger(__name__)

# MP caps a preference item's title; longer titles are rejected outright.
_MAX_TITLE = 256

# Checkout configuration that is deliberately not part of the public constructor.
_PREFERENCE_TTL = timedelta(hours=24)

# ``init_point`` comes back from the API, but it is rendered to the shopper as the
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


class MercadoPagoCheckout:  # pylint: disable=too-few-public-methods
    """Checkout Pro backed by an already configured official Mercado Pago SDK.

    The application owns credentials and transport configuration. This adapter keeps
    the public setup intentionally small: the SDK that calls Mercado Pago, the trusted
    catalog that prices cart lines, and an optional callback for correlating an opaque
    payment reference back to the host's session.
    """

    def __init__(
        self,
        *,
        sdk: mercadopago.SDK,
        catalog: Catalog,
        reference_store: Callable[[str, str], Awaitable[None]] | None = None,
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
        self._reference_store = reference_store

    async def checkout_handoff(  # pylint: disable=too-many-return-statements
        self,
        session: "ShoppingSessionContext",
        cart: "Cart",
    ) -> list[CheckoutHandoff]:
        """Drop-in for ``StorefrontBackend.checkout_handoff``.

        The many early returns are the design: every branch that is not a preference we
        are confident in leaves through one of them.

        Returns an empty list — letting the host's own checkout card take over — for
        every case that is not a preference we are confident in: nothing to charge, a
        line that cannot be priced, an API rejection, or MP being unreachable.
        """
        if not cart.items:
            return []

        try:
            items = await self._priced_items(session, cart)
        except _Refused as refusal:
            logger.warning("Refusing to create a preference: %s", refusal)
            return []
        except requests.RequestException:
            logger.exception("Catalog lookup failed while pricing the cart.")
            return []

        idempotency_key = self._idempotency_key(session.session_id, items)
        reference = self._reference(idempotency_key)
        if self._reference_store is not None:
            # Persisted by the caller so its webhook handler can find the session again
            # from `external_reference`. Without a store the preference is still created
            # — it just cannot be correlated back, which is the caller's choice to make.
            try:
                await self._reference_store(reference, session.session_id)
            except Exception:  # pylint: disable=broad-exception-caught
                # This is a caller-provided boundary. Fail before creating a payment
                # that its webhook handler would be unable to correlate, and do not log
                # exception text that could contain session data.
                logger.error(
                    "Reference storage failed; refusing to create a preference."
                )
                return []

        body: dict[str, Any] = {
            "items": items,
            "external_reference": reference,
            # A preference with no expiry stays payable at yesterday's price after the
            # cart has moved on.
            "expires": True,
            "expiration_date_to": self._expiry(),
        }

        preference = await self._create(body, idempotency_key)
        if preference is None:
            return []

        init_point = preference.get("init_point")
        if not isinstance(init_point, str) or not self._is_checkout_url(init_point):
            logger.error(
                "Preference %s came back without a usable Mercado Pago checkout URL.",
                preference.get("id"),
            )
            return []
        # No adapter-specific label: the commerce-agents host owns its UI fallback.
        return [CheckoutHandoff(url=init_point)]

    # -- internals ---------------------------------------------------------------

    async def _priced_items(
        self, session: "ShoppingSessionContext", cart: "Cart"
    ) -> list[dict[str, Any]]:
        """One preference item per cart line, priced from the catalog record rather than
        from the line. Raises :class:`_Refused` on anything that cannot be priced."""
        items: list[dict[str, Any]] = []
        for line in cart.items:
            record = await self._catalog.get_product_details(session, line.product_id)
            if record is None:
                raise _Refused(f"product {line.product_id!r} is not in the catalog")
            if not getattr(record, "in_stock", True):
                raise _Refused(f"product {line.product_id!r} is out of stock")

            # Decimal via str: Decimal(float) would carry the float's binary error into
            # the comparison below and report drift that isn't there.
            catalog_price = Decimal(str(record.price))
            if catalog_price <= 0:
                raise _Refused(f"product {line.product_id!r} has no positive price")

            quantity = int(line.quantity)
            if quantity < 1:
                raise _Refused(f"product {line.product_id!r} has quantity {quantity}")

            if catalog_price != Decimal(str(line.price)):
                # Either the catalog moved under the cart or the cart was tampered with.
                # Both are charged at the catalog price; the product id is logged so it
                # can be told apart later, and no price is logged.
                logger.warning(
                    "Cart price for %r disagrees with the catalog; charging the "
                    "catalog price.",
                    line.product_id,
                )

            items.append(
                {
                    "id": line.product_id,
                    # The catalog's title, not the cart's: the cart's is model-authored
                    # text and this is rendered on an MP-branded page.
                    "title": str(record.title)[:_MAX_TITLE],
                    "quantity": quantity,
                    # float at the boundary because that is the wire format; the
                    # arithmetic above stays in Decimal.
                    "unit_price": float(catalog_price),
                }
            )
        return items

    @staticmethod
    def _reference(idempotency_key: str) -> str:
        """An opaque ``external_reference``.

        Never ``session.session_id``: that id is caller-supplied in the reference host,
        so using it would let a caller bind a payment to a session it does not own, and
        would publish session ids into MP-side records. Deriving it from the hashed
        idempotency key keeps the request body stable across retries too.
        """
        return f"mpca-{idempotency_key[:32]}"

    @staticmethod
    def _expiry() -> str:
        return (datetime.now(timezone.utc) + _PREFERENCE_TTL).isoformat(
            timespec="milliseconds"
        )

    @staticmethod
    def _idempotency_key(session_id: str, items: list[dict[str, Any]]) -> str:
        """Stable across retries of the same cart, so a retrying or looping agent gets
        the original preference back instead of a second payable link for one cart.

        The SDK sets a fresh ``x-idempotency-key`` per call, and ``get_headers`` applies
        ``custom_headers`` last, so this overrides it.
        """
        canonical = json.dumps([session_id, items], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
            result = await asyncio.to_thread(
                self._sdk.preference().create, body, options
            )
        except requests.RequestException:
            logger.exception(
                "Mercado Pago was unreachable; the host's own checkout card takes over."
            )
            return None

        status = result.get("status")
        payload = result.get("response")
        if not isinstance(status, int) or status >= 300 or not isinstance(payload, dict):
            logger.error(
                "Preference creation failed (HTTP %s): error=%s causes=%s",
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
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in _CHECKOUT_HOSTS


def _safe_str(value: Any) -> str | None:
    """MP's ``error`` field is a short identifier (``bad_request``); anything longer is
    not that field and is dropped rather than logged."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    return value


def _cause_codes(payload: Any) -> list[Any]:
    """The numeric codes from MP's ``cause`` list — enough to look the rejection up in
    the API reference, without the descriptions that quote the payload."""
    if not isinstance(payload, dict):
        return []
    causes = payload.get("cause")
    if not isinstance(causes, list):
        return []
    return [c.get("code") for c in causes if isinstance(c, dict) and "code" in c]
