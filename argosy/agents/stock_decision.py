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
    # ABSTAIN = insufficient evidence — distinct from HOLD (thesis intact).
    verdict: Literal["BUY", "HOLD", "SELL", "TRIM", "ABSTAIN"]
    confidence: Literal["HIGH", "MED", "LOW"]
    reason: str
    evidence: list[str] = Field(default_factory=list)   # the fetched data points that drove it
    data_gaps: list[str] = Field(default_factory=list)  # wanted-but-missing inputs (honest)


# HOLD is deliberately NOT here: it means "thesis intact, no action" and must not
# surface to the client (it is stored for audit only). ABSTAIN is also not
# actionable — it is honesty about empty evidence, not a decision.
ACTIONABLE_VERDICTS = frozenset({"BUY", "SELL", "TRIM"})


def is_actionable(verdict: str) -> bool:
    """True only for verdicts that need the client. HOLD/ABSTAIN stay silent."""
    return (verdict or "").strip().upper() in ACTIONABLE_VERDICTS


def is_decision(verdict: str) -> bool:
    """True for BUY/HOLD/SELL/TRIM — ABSTAIN is not a decision."""
    v = (verdict or "").strip().upper()
    return v in {"BUY", "HOLD", "SELL", "TRIM"}


# Fresh market-research fields. Thesis/plan stance alone is not enough to
# decide; a bare last price is also insufficient (Aug-2026 empty-bundle wave).
_RESEARCH_EVIDENCE_FIELDS: tuple[str, ...] = (
    "news", "fundamentals", "sentiment",
)

# Placeholders that look truthy but carry no usable content (must not open the gate).
_UNUSABLE_EVIDENCE_MARKERS: tuple[str, ...] = (
    "scores unavailable",
    "not available",
    "(empty)",
    "no data",
)


def evidence_field_is_usable(value: Any) -> bool:
    """True when a research-field value is real content, not a hollow placeholder."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict)) and not value:
        return False
    text = str(value).strip()
    if not text:
        return False
    low = text.lower()
    return not any(m in low for m in _UNUSABLE_EVIDENCE_MARKERS)


def bundle_has_sufficient_evidence(bundle: dict[str, Any] | None) -> bool:
    """True when the research bundle carries at least one *usable* evidence field.

    Empty bundles, price/thesis-only bundles, and truthy-but-hollow strings
    (e.g. ``"social mentions=N (scores unavailable)"``) are insufficient — the
    agent must not mint a HOLD that is indistinguishable from a reasoned hold.
    """
    if not bundle:
        return False
    return any(
        evidence_field_is_usable(bundle.get(k)) for k in _RESEARCH_EVIDENCE_FIELDS
    )


def abstain_insufficient_evidence(
    ticker: str, *, bundle: dict[str, Any] | None = None,
) -> StockDecisionOutput:
    """First-class abstention when no research evidence was fetched."""
    present = sorted(k for k, v in (bundle or {}).items() if v)
    gaps = [
        f for f in ("price", "fundamentals", "news", "sentiment", "thesis")
        if not (bundle or {}).get(f)
    ]
    reason = (
        "ABSTAIN — insufficient evidence fetched; not a HOLD. "
        "No news/fundamentals/sentiment in the research bundle"
        + (f" (present: {', '.join(present)})" if present else " (bundle empty)")
        + ". Refusing to mint a decision from absence."
    )
    return StockDecisionOutput(
        ticker=ticker,
        verdict="ABSTAIN",
        confidence="LOW",
        reason=reason,
        evidence=[],
        data_gaps=gaps or ["research bundle empty"],
    )


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
            "  SELL — exit\n"
            "  ABSTAIN — evidence is present but still too thin/ambiguous to "
            "decide honestly (distinct from HOLD)\n\n"
            "HOLD IS A FIRST-CLASS ANSWER. If the thesis is intact and nothing "
            "material changed, the correct verdict is HOLD — do NOT manufacture a "
            "trade to look active. Only BUY / TRIM / SELL when the fetched "
            "evidence genuinely warrants it. ABSTAIN remains reachable when the "
            "bundle has some fields but you still cannot responsibly decide.\n"
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
            'Return a JSON object {"ticker": str, "verdict": '
            '"BUY|HOLD|SELL|TRIM|ABSTAIN", '
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
    """Run the decision agent for one stock and return its verdict.

    Empty / research-empty bundles short-circuit to ABSTAIN (not HOLD) so an
    evidence-feed failure is never stored as a decision. ``agent_factory``
    is injectable so the orchestration is testable without a live LLM.
    """
    if not bundle_has_sufficient_evidence(bundle):
        return abstain_insufficient_evidence(ticker, bundle=bundle)
    factory = agent_factory or (lambda: StockDecisionAgent(user_id=user_id))
    agent = factory()
    return agent.run_sync(ticker=ticker, context=context, bundle=bundle).output


__all__ = [
    "StockDecisionAgent",
    "StockDecisionOutput",
    "ACTIONABLE_VERDICTS",
    "abstain_insufficient_evidence",
    "bundle_has_sufficient_evidence",
    "evidence_field_is_usable",
    "decide_stock",
    "is_actionable",
    "is_decision",
]
