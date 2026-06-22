"""Stage 2 — preliminary triage (cheap Sonnet pass).

For each candidate Stage 1 routed, a single cheap pass answers: "does this
warrant a real (Stage 3) decision today, or is it a no-op?" The no-ops are
killed here so the expensive Opus fleet only runs on genuine survivors. Mirrors
the discovery funnel's QuickEstimatorAgent (Sonnet, single-shot, terse).

The Stage-1 routing reason + signal is given to the model as context so the
triage is grounded in WHY the name surfaced — it is not re-deriving relevance,
it is judging whether the surfaced signal is decision-worthy today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from argosy.agents.base import BaseAgent
from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from argosy.services.decision_funnel.stage0_market import MarketRead
    from argosy.services.decision_funnel.stage1_routing import RoutedCandidate

_log = get_logger("argosy.services.decision_funnel.triage")


class TriageOutput(BaseModel):
    subject: str
    warrants_decision: bool
    urgency: Literal["HIGH", "MED", "LOW"]
    rationale: str


@dataclass(frozen=True)
class TriageOutcome:
    subject: str
    warrants_decision: bool
    urgency: str
    rationale: str
    model: str | None = None
    prompt_hash: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


class Stage2TriageAgent(BaseAgent[TriageOutput]):
    """Single-shot Sonnet triage for ONE routed candidate."""

    agent_role = "decision_funnel_triage"
    output_model = TriageOutput
    require_citations = False

    def __init__(self, *, user_id: str, model: str | None = None) -> None:
        # Pin to Sonnet — the role is not in DEFAULT_MODEL_BY_ROLE so the
        # fallback would otherwise be Opus (mirror quick_estimator).
        super().__init__(user_id=user_id, model=model or "claude-sonnet-4-6")

    def build_prompt(self, *, candidate, market_summary: str, weight_pct, cap_pct):
        system = (
            "You are Argosy's daily decision triage for a LONG-HOLD investor. "
            "Given ONE holding/sleeve that the deterministic router surfaced "
            "today (with the reason it surfaced), decide whether it warrants a "
            "full multi-agent Buy/Sell/Hold decision TODAY, or is a no-op. "
            "Bias toward NO unless the surfaced signal is genuinely "
            "decision-worthy now (a real thesis change, a material move, a cap "
            "breach that should be acted on, imminent earnings worth pre-positioning). "
            "Routine drift or stale news is a no-op. 'Hold / do nothing' is a "
            "legitimate no-op. Be decisive and terse."
        )
        user = (
            f"SUBJECT: {candidate.subject} ({candidate.subject_type})\n"
            f"ROUTER TRIGGERS: {', '.join(candidate.triggers)}\n"
            f"ROUTER REASON: {candidate.reason}\n"
            f"CURRENT WEIGHT %: {weight_pct}\n"
            f"CONCENTRATION CAP %: {cap_pct}\n"
            f"MACRO READ: {market_summary}\n\n"
            "Return a JSON object {\"subject\": str, \"warrants_decision\": bool, "
            "\"urgency\": \"HIGH|MED|LOW\", \"rationale\": str (one clause)}."
        )
        return system, user


def triage_candidate(
    candidate: "RoutedCandidate",
    *,
    market: "MarketRead",
    weight_pct: float | None,
    cap_pct: float | None,
    user_id: str = "ariel",
) -> TriageOutcome:
    """Run the Sonnet triage over one routed candidate."""
    agent = Stage2TriageAgent(user_id=user_id)
    report = agent.run_sync(
        candidate=candidate,
        market_summary=market.summary,
        weight_pct=weight_pct,
        cap_pct=cap_pct,
    )
    out: TriageOutput = report.output
    return TriageOutcome(
        subject=out.subject or candidate.subject,
        warrants_decision=bool(out.warrants_decision),
        urgency=out.urgency,
        rationale=out.rationale,
        model=getattr(report, "model", None),
        prompt_hash=getattr(report, "prompt_hash", None),
        tokens_in=getattr(report, "tokens_in", None),
        tokens_out=getattr(report, "tokens_out", None),
        cost_usd=float(getattr(report, "cost_usd", 0.0) or 0.0),
    )


__all__ = ["Stage2TriageAgent", "TriageOutput", "TriageOutcome", "triage_candidate"]
