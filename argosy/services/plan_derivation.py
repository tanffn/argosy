# argosy/services/plan_derivation.py
"""Pure derivation functions: compute the load-bearing plan targets FROM inputs only.

The arithmetic here is the codex-zigzag-confirmed methodology (2026-06-18): NVDA
deconcentration to the 12% IPS target and FI sufficiency on the HONEST liquid basis.
Each function returns ``Derived`` values carrying their formula + the inputs consumed,
so the re-derivation reviewer can recompute them blind.
"""
from __future__ import annotations

import math

from argosy.quality.plan_model import Derived


def derive_nvda_deconcentration(
    *, nvda_sh: int, nvda_px_usd: float, nvda_weight: float,
    target_w: float, cap: float,
) -> dict[str, Derived]:
    """From the current NVDA position + book weight, derive the target share count and
    the shares to sell to reach ``target_w`` of the investable book. Strict ``<=target_w``
    => RETAIN ``floor(target)`` shares (retaining the ceiling would exceed the target)."""
    nvda_usd = nvda_sh * nvda_px_usd
    book_usd = nvda_usd / nvda_weight
    target_sh = math.floor(target_w * book_usd / nvda_px_usd)
    sell_sh = nvda_sh - target_sh
    cap_breach = nvda_weight / cap
    used = ("nvda_sh", "nvda_px_usd", "nvda_weight", "target_w", "cap")
    return {
        "nvda_target_sh": Derived(
            key="nvda_target_sh", value=target_sh, unit="shares",
            formula="floor(target_w * (nvda_sh*nvda_px_usd/nvda_weight) / nvda_px_usd)",
            inputs_used=used,
        ),
        "nvda_sell_sh": Derived(
            key="nvda_sell_sh", value=sell_sh, unit="shares",
            formula="nvda_sh - nvda_target_sh", inputs_used=used,
        ),
        "nvda_cap_breach_x": Derived(
            key="nvda_cap_breach_x", value=round(cap_breach, 2), unit="x",
            formula="nvda_weight / cap", inputs_used=("nvda_weight", "cap"),
        ),
    }


def derive_fi_margin_liquid(
    *, liquid_nw_nis: float, fi_total_capital_nis: float,
) -> Derived:
    """FI sufficiency margin on the HONEST liquid basis (NOT the investable basis — the
    investable basis is what overstated FI and triggered the codex BLOCK)."""
    margin = liquid_nw_nis - fi_total_capital_nis
    return Derived(
        key="fi_margin_liquid_nis", value=round(margin, 2), unit="nis",
        formula="liquid_nw_nis - fi_total_capital_nis",
        inputs_used=("liquid_nw_nis", "fi_total_capital_nis"),
    )
