"""North-star verification (P5).

Before a funnel proposal reaches the client's "needs me now" surface, it must
trace to the prime directive — maximize finances + earliest safe retirement.
This is the adversarial pass: "does the client actually need this, today, to
retire sooner / safer?" A proposal that doesn't clearly advance the directive is
recorded (auditable in the transparency view) but NOT pushed to the active list,
so the client surface stays signal, not noise.

Deterministic by design (no extra LLM): alignment is judged from the routing
triggers + the proposed action + the IPS, all of which already exist. The fleet
already did the analysis; this is the final "is it worth the client's
attention" gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from argosy.services.ips import InvestmentPolicyStatement

# Triggers that materially advance the prime directive when acted on:
# - risk-reducing (concentration / thesis deterioration) -> safer retirement
# - opportunity / event-driven (big move, earnings) -> better finances
# - drift correction -> keeps the plan on its glide to FI
_MATERIAL_TRIGGERS = {
    "thesis_broken",
    "thesis_weakened",
    "concentration_cap_breach",
    "concentration_unverified",
    "drift_band_breach",
    "big_move",
    "big_drawdown",
    "earnings_imminent",
    "high_materiality_news",
    "macro_risk_off",
}

# A pure audit re-route is a false-drop SAFETY CHECK, not a directive signal —
# if it happens to produce a proposal it is recorded, but it does not by itself
# justify pushing the proposal to the client.
_NON_DIRECTIVE_ONLY = {"audit_sample"}


@dataclass(frozen=True)
class AlignmentVerdict:
    aligned: bool
    justification: str


def assess_alignment(
    *,
    triggers: list[str],
    action: str | None,
    ips: "InvestmentPolicyStatement | None" = None,
) -> AlignmentVerdict:
    """Judge whether a proposed action traces to the prime directive.

    Aligned when at least one MATERIAL trigger fired. A proposal whose only
    trigger is the audit sample is NOT aligned (it was a false-drop check).
    """
    material = [t for t in triggers if t in _MATERIAL_TRIGGERS]
    only_audit = bool(triggers) and all(t in _NON_DIRECTIVE_ONLY for t in triggers)

    if only_audit or not material:
        return AlignmentVerdict(
            aligned=False,
            justification=(
                "no material directive signal — surfaced via audit/no concrete "
                "trigger; recorded for transparency, not pushed"
            ),
        )

    # Risk-reduction framing for the common single-name cases.
    risk_reducing = any(
        t in ("thesis_broken", "thesis_weakened", "concentration_cap_breach",
              "concentration_unverified", "drift_band_breach", "big_drawdown")
        for t in material
    )
    act = (action or "").lower()
    if risk_reducing and act in ("sell", "trim"):
        why = "reduces single-name / thesis risk -> safer path to retirement"
    elif "earnings_imminent" in material or "big_move" in material:
        why = "event-driven repositioning -> protects/advances finances"
    elif "high_materiality_news" in material:
        why = "responds to material news affecting a holding"
    else:
        why = "keeps the book aligned to the plan's glide toward FI"
    return AlignmentVerdict(
        aligned=True,
        justification=f"material trigger(s) {', '.join(material)}: {why}",
    )


__all__ = ["AlignmentVerdict", "assess_alignment"]
