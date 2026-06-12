"""QuickEstimatorAgent (Slice 2) — a cheap Sonnet triage screen.

The high-potential discovery funnel sources many radar candidates; running the
full Opus fleet on every one is expensive. This single-shot Sonnet agent gives a
fast fundamentals+thesis+sentiment read per ticker (a go/no-go + conviction +
sentiment + one-liner), so only the survivors escalate to the fleet. Pinned to
Sonnet explicitly (codex #7 — the role is not in the model table, which would
otherwise default to Opus).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from argosy.agents.base import BaseAgent
from argosy.services.contracts import EstimatorVerdict


class EstimatorOutput(BaseModel):
    ticker: str
    go: bool
    conviction: Literal["HIGH", "MED", "LOW"]
    sentiment: float          # -1.0 .. 1.0
    one_line: str


def _radar_context(candidate) -> str:
    """A compact text summary of a radar candidate (TrendCandidate or dict)."""
    def g(name, default=None):
        if isinstance(candidate, dict):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    fams = g("families", ()) or ()
    reasons = g("reasons", ()) or ()
    return (
        f"ticker={g('ticker')} name={g('name')} radar_score={g('score')} "
        f"families={list(fams)} price={g('price')} market_cap={g('market_cap')} "
        f"dollar_volume={g('dollar_volume')} pct_change={g('pct_change')} "
        f"reasons={list(reasons)}"
    )


def _ticker_of(candidate) -> str:
    if isinstance(candidate, dict):
        return candidate.get("ticker", "")
    return getattr(candidate, "ticker", "")


class QuickEstimatorAgent(BaseAgent[EstimatorOutput]):
    """Single-shot Sonnet screen producing an EstimatorVerdict for one ticker."""

    agent_role = "quick_estimator"
    output_model = EstimatorOutput
    require_citations = False

    def __init__(self, *, user_id: str, model: str | None = None) -> None:
        # Pin to Sonnet explicitly — the role is not in DEFAULT_MODEL_BY_ROLE so
        # the fallback would otherwise be Opus (codex #7).
        super().__init__(user_id=user_id, model=model or "claude-sonnet-4-6")

    def build_prompt(self, *, candidate):
        system = (
            "You are Argosy's quick discovery triage. Given ONE trend-radar "
            "candidate, give a fast go/no-go screen for whether it is worth a "
            "full multi-agent grading. Weigh fundamentals + thesis durability + "
            "sentiment; favour names with a real growth story, not just a "
            "momentum spike. Be decisive and terse.\n"
            "Return go=true only for candidates worth deeper research; otherwise "
            "go=false. conviction is HIGH/MED/LOW; sentiment is -1..1; one_line "
            "is a single clause."
        )
        user = (
            "RADAR CANDIDATE:\n"
            f"{_radar_context(candidate)}\n\n"
            "Return a JSON object {\"ticker\": str, \"go\": bool, \"conviction\": "
            "\"HIGH|MED|LOW\", \"sentiment\": float, \"one_line\": str}."
        )
        return system, user


def estimate(candidate, *, user_id: str = "ariel") -> EstimatorVerdict:
    """Run the Sonnet estimator over one radar candidate -> EstimatorVerdict."""
    agent = QuickEstimatorAgent(user_id=user_id)
    out: EstimatorOutput = agent.run_sync(candidate=candidate).output
    return EstimatorVerdict(
        ticker=out.ticker or _ticker_of(candidate), go=out.go,
        conviction=out.conviction, sentiment=out.sentiment, one_line=out.one_line,
    )


def triage(candidates, *, top_k: int | None = None,
           user_id: str = "ariel") -> list[EstimatorVerdict]:
    """Estimate each radar candidate, keep the go=True survivors, ranked by
    conviction then sentiment. ``top_k`` optionally caps the survivor list."""
    _RANK = {"HIGH": 3, "MED": 2, "LOW": 1}
    verdicts = [estimate(c, user_id=user_id) for c in candidates]
    survivors = [v for v in verdicts if v.go]
    survivors.sort(key=lambda v: (_RANK.get(v.conviction, 0), v.sentiment),
                   reverse=True)
    return survivors[:top_k] if top_k else survivors


__all__ = ["QuickEstimatorAgent", "EstimatorOutput", "estimate", "triage"]
