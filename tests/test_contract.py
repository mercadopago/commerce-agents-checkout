# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""The drift detector.

``src/mercadopago_commerce_agents/types.py`` declares its own ``CheckoutHandoff`` so the
package installs from PyPI without ``shopping-agent-core``. That is safe only for as
long as commerce-agents keeps consuming a handoff structurally — today
``enrich_checkout`` just calls ``.model_dump(exclude_none=True)``, with no ``isinstance``
check and no pydantic validation. Anthropic stabilises nothing here, so this file runs
the *real* ``enrich_checkout`` against our own type: if an upstream release starts
validating it, CI fails here instead of a seller's checkout failing in production.

Skipped when commerce-agents is not installed, so the normal test run needs nothing
from Anthropic's repository. CI runs it in a job that clones the repo at a pinned
commit — see ``.github/workflows/ci.yml``.
"""

import os
import unittest
from dataclasses import MISSING
from types import SimpleNamespace

from mercadopago_commerce_agents import CheckoutHandoff

try:  # commerce-agents is an optional, unpublished dependency
    from shopping_agent.enrichment import enrich_checkout
    from shopping_agent.types import Cart, CartItem
    from shopping_agent.types import CheckoutHandoff as UpstreamCheckoutHandoff

    UPSTREAM = True
except ImportError:  # pragma: no cover - the default local run
    if os.environ.get("REQUIRE_COMMERCE_AGENTS") == "1":
        raise
    UPSTREAM = False


@unittest.skipUnless(UPSTREAM, "commerce-agents not installed; see the module docstring")
class ContractTest(unittest.IsolatedAsyncioTestCase):
    def test_fields_match_upstream(self):
        """Same field names, same optionality. A new required field upstream would mean
        our handoff is silently incomplete."""
        upstream = UpstreamCheckoutHandoff.model_fields
        ours = CheckoutHandoff.__dataclass_fields__

        self.assertEqual(set(ours), set(upstream))
        for name, spec in upstream.items():
            self.assertEqual(
                spec.is_required(),
                ours[name].default is MISSING,
                f"optionality of {name!r} diverged from upstream",
            )

    async def test_real_enrich_checkout_accepts_our_handoff(self):
        """The actual consumer, unmodified, fed our type."""
        cart = Cart(items=[CartItem(product_id="sku1", title="A thing", price=10.0, quantity=1)])
        handoff = CheckoutHandoff(url="https://www.mercadopago.com.br/checkout", label="Pay")

        backend = SimpleNamespace(
            get_cart=_returning(cart),
            checkout_handoff=_returning([handoff]),
        )
        context = SimpleNamespace(backend=backend, session=SimpleNamespace(session_id="s"))

        enriched = await enrich_checkout(_Payload(), context)

        self.assertEqual(
            enriched["handoffs"],
            [{"url": "https://www.mercadopago.com.br/checkout", "label": "Pay"}],
        )


class _Payload:
    """``enrich_checkout`` only ever calls ``model_dump`` on the payload."""

    def model_dump(self, **_):
        return {"summary": "checkout"}


def _returning(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call


if __name__ == "__main__":
    unittest.main()
