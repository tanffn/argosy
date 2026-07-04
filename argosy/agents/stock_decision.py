"""StockDecisionAgent — reach a BUY/HOLD/SELL/TRIM verdict on ONE stock from a
bundle of FRESHLY FETCHED data.

The gap this closes: Argosy monitored well but never *decided* per-name — a
weakening holding became a watchlist note, and buy candidates were judged on a
static packet with no ability to pull fresh data on the specific ticker. This
agent is the missing "fetch → decide" step.

HOLD is a FIRST-CLASS outcome. If the thesis is intact and nothing material
changed, the correct answer is HOLD (no action) — Argosy does not manufacture a
trade to look active. HOLD verdicts are not actionable, so they stay silent
(stored for audit); the client is in the loop only when something needs them.

This class only DECIDES from a supplied research bundle (pure prompt → verdict);
assembling that bundle from the analyst fleet (news / fundamentals / sentiment /
technical / price), tiered + verified, is the service layer's job.
"""
from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class StockDecisionOutput(BaseModel):
    ticker: str
    verdict: Literal["BUY", "HOLD", "SELL", "TRIM"]
    confidence: Literal["HIGH", "MED", "LOW"]
    reason: str
    evidence: list[str] = Field(default_factory=list)   # the fetched data points that drove it
    data_gaps: list[str] = Field(default_factory=list)  # wanted-but-missing inputs (honest)


# HOLD is deliberately NOT here: it means "thesis intact, no action" and must not
# surface to the client (it is stored for audit only).
ACTIONABLE_VERDICTS = frozenset({"BUY", "SELL", "TRIM"})


def is_actionable(verdict: str) -> bool:
    """True only for verdicts that need the client. HOLD stays silent."""
    return (verdict or "").strip().upper() in ACTIONABLE_VERDICTS


_BUNDLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("price", "Price / technical"),
    ("fundamentals", "Fundamentals"),
    ("news", "Recent news"),
    ("sentiment", "Sentiment"),
    ("thesis", "Plan thesis / prior notes"),
)


def _bundle_lines(bundle: dict[str, Any]) -> str:
    """Render the fetched bundle, naming absent fields explicitly so the agent
    lowers confidence + records the gap rather than guessing."""
    out = []
    for key, label in _BUNDLE_FIELDS:
        val = (bundle or {}).get(key)
        out.append(f"  - {label}: {val}" if val else f"  - {label}: (not available)")
    return "\n".join(out)


class StockDecisionAgent(BaseAgent[StockDecisionOutput]):
    """Single-stock BUY/HOLD/SELL/TRIM decision from a fetched research bundle."""

    agent_role = "stock_decision"
    output_model = StockDecisionOutput
    require_citations = False

    def build_prompt(self, *, ticker: str, context: str, bundle: dict[str, Any]):
        system = (
            "You are Argosy's per-stock decision analyst for a long-hold, "
            "Israeli-resident (non-US-person) investor. Given ONE holding or "
            "candidate and a bundle of FRESHLY FETCHED data, decide what to DO:\n"
            "  BUY  — open or add to the position\n"
            "  HOLD — thesis intact, NO action\n"
            "  TRIM — reduce (partial) \n"
            "  SELL — exit\n\n"
            "HOLD IS A FIRST-CLASS ANSWER. If the thesis is intact and nothing "
            "material changed, the correct verdict is HOLD — do NOT manufacture a "
            "trade to look active. Only BUY / TRIM / SELL when the fetched "
            "evidence genuinely warrants it.\n"
            "Decide from the EVIDENCE in the bundle, not from priors or training "
            "memory. If a field you would want is missing, do NOT guess — record "
            "it in data_gaps and LOWER your confidence accordingly.\n"
            "Fill `reason` (why this verdict) and `evidence` (the specific fetched "
            "data points that drove it). Be decisive and terse."
        )
        user = (
            f"TICKER: {ticker}\n"
            f"POSITION CONTEXT: {context}\n\n"
            f"FRESHLY FETCHED RESEARCH BUNDLE:\n{_bundle_lines(bundle)}\n\n"
            'Return a JSON object {"ticker": str, "verdict": "BUY|HOLD|SELL|TRIM", '
            '"confidence": "HIGH|MED|LOW", "reason": str, "evidence": [str], '
            '"data_gaps": [str]}.'
        )
        return system, user


def decide_stock(
    ticker: str,
    *,
    context: str,
    bundle: dict[str, Any],
    user_id: str = "ariel",
    agent_factory: Callable[[], StockDecisionAgent] | None = None,
) -> StockDecisionOutput:
    """Run the decision agent for one stock and return its verdict. ``agent_factory``
    is injectable so the orchestration is testable without a live LLM."""
    factory = agent_factory or (lambda: StockDecisionAgent(user_id=user_id))
    agent = factory()
    return agent.run_sync(ticker=ticker, context=context, bundle=bundle).output


__all__ = [
    "StockDecisionAgent",
    "StockDecisionOutput",
    "ACTIONABLE_VERDICTS",
    "is_actionable",
    "decide_stock",
]
