"""The NVDA policy-sell assessor — the glide (policy) half of the period directive.

The plan's glide is the base policy: deconcentrate NVDA toward its cap over the
optimizer-chosen horizon. This module turns the (codex-verified) breach-tranche
money-math into a proactive, read-only verdict:

  * ``sell_due``  — NVDA is over its cap; here is THIS quarter's paced tranche.
  * ``no_action`` — NVDA is within its cap; nothing to sell this period (an
                    explicit stance, per the no-action-memo contract).

Read-only: it NEVER writes a proposal or executes. Persisting the tranche as an
approval-pending proposal stays with ``breach_router.route_breach_tranche`` (the
monthly cycle); surfacing it proactively is this assessor's job.

This is the POLICY sell only. The risk-budget / thesis-break sells (the exception
protocol) come from the Assess brain in Step 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from argosy.services.breach_router import compute_breach_tranche

# A sell realizes Israeli CGT on §102 equity lots; the glide's whole point is to
# pace the deconcentration so the tax is spread rather than taken in one hit.
_TAX_NOTE = (
    "Realizes Israeli CGT on §102 equity lots — the tranche is paced over the "
    "glide so the tax is spread, not taken in one hit."
)


@dataclass(frozen=True)
class NvdaPolicySell:
    status: str            # "sell_due" | "no_action"
    category: str          # "policy" (glide). Exception categories come from Step 3.
    tranche_nis: float
    nvda_current_pct: float
    nvda_cap_pct: float
    n_quarters: int
    headline: str
    tax_note: str


def assess_nvda_policy_sell(
    *, session, user_id: str, today: date | None = None
) -> NvdaPolicySell:
    """Read-only glide-sell verdict for ``user_id`` as of ``today``."""
    tranche = compute_breach_tranche(session, user_id, today)
    if tranche is None:
        return NvdaPolicySell(
            status="no_action", category="policy", tranche_nis=0.0,
            nvda_current_pct=0.0, nvda_cap_pct=0.0, n_quarters=0,
            headline=(
                "NVDA is within its concentration cap — no deconcentration sell "
                "is due this period."
            ),
            tax_note="",
        )
    return NvdaPolicySell(
        status="sell_due", category="policy",
        tranche_nis=tranche.tranche_nis,
        nvda_current_pct=tranche.nvda_current_pct,
        nvda_cap_pct=tranche.nvda_cap_pct,
        n_quarters=tranche.n_quarters,
        headline=(
            f"Trim NVDA ~₪{tranche.tranche_nis:,.0f} this quarter — "
            f"{tranche.nvda_current_pct:.0f}% is over your {tranche.nvda_cap_pct:.0f}% "
            f"cap; the over-cap position is spread over {tranche.n_quarters} quarters "
            "per your glide."
        ),
        tax_note=_TAX_NOTE,
    )


__all__ = ["NvdaPolicySell", "assess_nvda_policy_sell"]
