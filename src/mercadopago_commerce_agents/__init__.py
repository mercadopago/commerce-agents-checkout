# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Mercado Pago Checkout Pro integration for commerce-agents seller backends.

``MercadoPagoCheckout`` is called by a ``StorefrontBackend.checkout_handoff`` wrapper
that owns the durable checkout-attempt identifiers; see its docstring for the wiring and
for why it needs your catalog.
"""

from .checkout import (
    Catalog,
    CheckoutOutcomeUnknown,
    MercadoPagoCheckout,
)
from .types import CheckoutHandoff

__all__ = [
    "Catalog",
    "CheckoutHandoff",
    "CheckoutOutcomeUnknown",
    "MercadoPagoCheckout",
]
