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

# Risk-reducing triggers: acting reduces concentration / thesis / drawdown risk
# -> a safer path to retirement. A risk-reducing trigger justifies a SELL/TRIM,
# not a BUY (buying into a cap breach is contradictory).
_RISK_REDUCING = {
    "thesis_broken",
    "thesis_weakened",
    "concentration_cap_breach",
    "concentration_unverified",
    "drift_band_breach",
    "big_drawdown",
}

# Opportunity / event-driven triggers: acting (in either direction) protects or
# advances finances; these justify a BUY as well as a SELL.
_OPPORTUNITY = {
    "big_move",
    "earnings_imminent",
    "high_materiality_news",
    "macro_risk_off",
}


@dataclass(frozen=True)
class AlignmentVerdict:
    aligned: bool
    justification: str


def assess_alignment(
    *,
    triggers: list[str],
    action: str | None,
    proposed: bool = True,
) -> AlignmentVerdict:
    """Judge whether a PROPOSED action traces to the prime directive (maximize
    finances + earliest safe retirement) and is directionally coherent.

    Called only for proposals the full T2 fleet already approved, so the bar is
    "is this directionally sensible + worth the client's attention", not a
    re-derivation. Returns aligned=False to KEEP a proposal out of the active
    surface (it is still recorded for transparency).
    """
    if not proposed:
        return AlignmentVerdict(False, "no actionable proposal")
    if not triggers:
        return AlignmentVerdict(False, "no routing trigger — not surfaced")

    act = (action or "").lower()
    risk_reducing = [t for t in triggers if t in _RISK_REDUCING]
    opportunity = [t for t in triggers if t in _OPPORTUNITY]
    only_audit = bool(triggers) and triggers == ["audit_sample"]

    # An audit-sample that yielded an APPROVED proposal is exactly the value of
    # the false-drop audit — surface it.
    if only_audit:
        return AlignmentVerdict(
            True, "audit re-route surfaced a real, fleet-approved trade (caught a false drop)"
        )

    # Contradiction guard: a BUY justified ONLY by risk-reduction triggers (a
    # cap breach / over-target drift / thesis deterioration) is incoherent —
    # those call for a trim, not an add.
    if act == "buy" and risk_reducing and not opportunity:
        return AlignmentVerdict(
            False,
            f"incoherent: BUY against risk-reduction trigger(s) {', '.join(risk_reducing)} "
            "— recorded, not surfaced",
        )

    if risk_reducing and act in ("sell", "trim"):
        return AlignmentVerdict(
            True,
            f"reduces risk ({', '.join(risk_reducing)}) -> safer path to retirement",
        )
    if opportunity:
        return AlignmentVerdict(
            True, f"event-driven ({', '.join(opportunity)}) -> protects/advances finances"
        )
    if risk_reducing:
        return AlignmentVerdict(
            True, f"keeps the book aligned to plan ({', '.join(risk_reducing)})"
        )
    # A fleet-approved proposal with no recognised trigger still passed the full
    # risk + fund-manager gate; surface it (the fleet is the primary authority).
    return AlignmentVerdict(True, "fleet-approved trade with no contra-indication")


__all__ = ["AlignmentVerdict", "assess_alignment"]
