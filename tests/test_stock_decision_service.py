"""Orchestration: bundle assembly (best-effort), tiered holdings review, and the
HOLD-stays-silent filter. No live LLM/network — fetchers + decide injected."""
from __future__ import annotations

from argosy.agents.stock_decision import StockDecisionOutput
from argosy.services.stock_decision import (
    actionable_verdicts,
    decide_holdings,
    research_bundle,
)


def test_research_bundle_is_best_effort():
    def ok(t): return f"news for {t}"
    def empty(t): return None
    def boom(t): raise RuntimeError("source down")

    b = research_bundle("RKT", fetchers={"news": ok, "fundamentals": empty, "sentiment": boom})
    assert b == {"news": "news for RKT"}  # empty + failing sources simply absent


def test_triage_skips_immaterial_names():
    researched = []

    def _decide(ticker, *, context, bundle, user_id="ariel"):
        researched.append(ticker)
        return StockDecisionOutput(ticker=ticker, verdict="HOLD", confidence="LOW", reason="x")

    decide_holdings(
        {"BIG": 100_000.0, "TINY": 200.0},
        fetchers={},
        context_of=lambda t, u: f"{t} ${u:,.0f}",
        triage=lambda t, usd: usd >= 1_000.0,   # only research material positions
        decide=_decide,
    )
    assert researched == ["BIG"]  # TINY skipped by the tiering gate


def test_hold_stays_silent_only_actionable_surface():
    verdicts_by_ticker = {
        "RKT": StockDecisionOutput(ticker="RKT", verdict="TRIM", confidence="MED", reason="weakening"),
        "CSPX": StockDecisionOutput(ticker="CSPX", verdict="HOLD", confidence="HIGH", reason="intact"),
    }

    def _decide(ticker, *, context, bundle, user_id="ariel"):
        return verdicts_by_ticker[ticker]

    verdicts = decide_holdings(
        {"RKT": 42_000.0, "CSPX": 156_000.0},
        fetchers={"news": lambda t: f"headline {t}"},
        context_of=lambda t, u: f"{t}",
        decide=_decide,
    )
    assert len(verdicts) == 2                      # both decided (audit trail)
    surfaced = actionable_verdicts(verdicts)
    assert [v.ticker for v in surfaced] == ["RKT"]  # only the TRIM surfaces; HOLD silent
