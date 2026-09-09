# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""Mercado Pago as a drop-in payment provider for commerce-agents.

``MercadoPagoCheckout`` fills ``StorefrontBackend.checkout_handoff``; see its docstring
for the wiring and for why it needs your catalog.
"""

from .checkout import Catalog, MercadoPagoCheckout, external_reference_for
from .types import CheckoutHandoff

__all__ = [
    "Catalog",
    "CheckoutHandoff",
    "MercadoPagoCheckout",
    "external_reference_for",
]
