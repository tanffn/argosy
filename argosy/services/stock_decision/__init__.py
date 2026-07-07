"""Per-stock research -> verdict service: assemble a fetched bundle, then decide.

The decision AGENT lives in ``argosy.agents.stock_decision``; this package is the
orchestration: assemble the research bundle from best-effort fetchers, run a cheap
triage so only MATERIAL names get deep research (the tiering that avoids the
fleet-timeout), and reach a per-name verdict where HOLD stays silent.
"""
from argosy.services.stock_decision.service import (
    actionable_verdicts,
    decide_holdings,
    load_elevated_thesis_flags,
    research_bundle,
    run_holdings_review,
    verify_verdict,
    write_stock_decision_proposal,
)

__all__ = [
    "research_bundle",
    "decide_holdings",
    "actionable_verdicts",
    "load_elevated_thesis_flags",
    "run_holdings_review",
    "verify_verdict",
    "write_stock_decision_proposal",
]
