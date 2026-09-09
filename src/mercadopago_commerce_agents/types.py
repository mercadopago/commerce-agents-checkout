# Copyright 2026 Mercado Pago
# SPDX-License-Identifier: Apache-2.0

"""The one type this package hands back, without an Anthropic runtime dependency.

``shopping_agent.types.CheckoutHandoff`` is a three-field pydantic model, and the only
thing the shopping agent ever does with a handoff is call ``.model_dump(exclude_none=True)``
on it (``shopping-agent/core/shopping_agent/enrichment.py``, ``enrich_checkout``) —
there is no ``isinstance`` check and no pydantic validation of the returned list. So a
structurally identical object is accepted, and declaring our own keeps
``shopping-agent-core`` out of our dependencies entirely.

That matters because ``shopping-agent-core`` is deliberately unpublishable: it is
versioned ``0.1.0.dev0`` and pins ``commerce-common==0.1.0.dev0`` so that, as its own
``pyproject.toml`` puts it, "a lone install fails instead of resolving a public
distribution of the same name". Depending on it would make this package either
uninstallable from PyPI or a dependency-confusion risk.

The catch is that this rests on how ``enrich_checkout`` is written rather than on a
contract anyone promised — Anthropic documents the interface as "the docstrings in
backend.py and types.py" and stabilises nothing. ``tests/test_contract.py`` is what
keeps that honest: it runs the real ``enrich_checkout`` against this class, so an
upstream release that starts validating the type fails our CI instead of a seller's
checkout.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class CheckoutHandoff:
    """Where the customer completes the purchase. Field-for-field identical to
    ``shopping_agent.types.CheckoutHandoff``; ``seller`` is set only by a marketplace
    whose sellers check out separately."""

    url: str
    label: str | None = None
    seller: str | None = None

    def model_dump(self, *, exclude_none: bool = False, **unsupported: Any) -> dict[str, Any]:
        """Mirrors the pydantic method ``enrich_checkout`` calls.

        Only ``exclude_none`` is implemented. Silently accepting the other pydantic
        keywords (``exclude``, ``by_alias``, ``mode``, ...) would let an upstream release
        change the meaning of a handoff while the contract test stayed green, so anything
        else raises and the contract test fails loudly instead."""
        if unsupported:
            raise TypeError(
                "model_dump() received unsupported keyword arguments: "
                + ", ".join(sorted(unsupported))
            )
        dumped = asdict(self)
        if exclude_none:
            return {key: value for key, value in dumped.items() if value is not None}
        return dumped
