"""plan_synthesis — five-phase orchestration that produces a
draft long/medium/short plan from current state + agent fleet review.

Triggers (one of):
  - scheduled (monthly_cycle on the 1st)
  - check_in (user-initiated via /api/advisor/check-in)
  - quarterly (extra prompt weight on medium horizon)
  - annual   (extra prompt weight on long horizon)

Phases:
  1. Analyst reports (parallel) — 9 specialists run concurrently
  2. Researcher debate (per-horizon) — 3 horizons in parallel
  3. Synthesizer — produces the three HorizonSection drafts
  4. Risk team review — plan-level verdict
  5. Fund manager integrity check — green-lights as role=draft

Per spec §4. Output: a new role='draft' PlanVersion row.

Idempotency: if a draft already exists for the user, it is moved to
role='superseded' and a fresh draft is written.

Phase implementations are pluggable (each has a default that calls
the existing fleet agents with plan-revision prompts; tests stub them).

Monkeypatch semantics
---------------------
Tests do::

    from argosy.orchestrator.flows import plan_synthesis as flow
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", ...)

All call sites in orchestrator.py resolve helpers via this package
namespace (``_pkg.<name>``), so the patch is always intercepted.
"""

from __future__ import annotations

# Public types — imported first (no dependency on other submodules).
from argosy.orchestrator.flows.plan_synthesis._types import (
    NoBaselineError,
    SynthesisResult,
    Trigger,
)

# Re-export agent class names that Phase 1 looks up via the package
# namespace.  Tests monkeypatch these on the ``flow`` (package) object.
from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent
from argosy.agents.fx_analyst import FXAnalystAgent as FxAnalystAgent
from argosy.agents.household_budget_analyst import HouseholdBudgetAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.tax_analyst import TaxAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent

# Orchestration entry point and all monkeypatchable phase helpers.
from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    run_synthesis,
    _run_phase_1_analysts,
    _run_phase_2_debates,
    _run_one_horizon_debate,
    _run_phase_3_synthesizer,
    _enforce_speculation_cap,
    _run_phase_4_risk,
    _run_one_risk_perspective,
    _make_risk_officer,
    _run_phase_5_fund_manager,
    _make_fund_manager,
    _safe_run_agent,
    # W1.C-v4 — JSONL forensic trail writer (replaces W1.C-v3 raw sqlite3
    # bulk persist).  Exposed on the package namespace so tests can
    # monkeypatch and the orchestrator's call sites (which resolve via
    # _pkg) honour the patch.
    _persist_agent_reports,
    # W1.C-v4 — end-of-synthesis JSONL → agent_reports ingest helper.
    # Called from run_synthesis after the orchestrator's session has
    # finished its own writes (so the writer lock is clean).
    _ingest_synthesis_trail,
    # T2.1 — per-run cost cap helpers. Exposed on the package namespace
    # so the orchestrator's _pkg.* resolution finds them, and so tests
    # can monkeypatch _check_cost_cap to force/skip the cap.
    _check_cost_cap,
    _read_synthesis_trail_costs,
)

# Input-assembly helpers (monkeypatched in tests).
from argosy.orchestrator.flows.plan_synthesis.inputs import (
    _assemble_portfolio_summary,
    _assemble_fills_summary,
    _load_user_context_yaml,
)

# Rendering helper (imported directly in one test).
from argosy.orchestrator.flows.plan_synthesis.render import _horizon_md

__all__ = [
    # Public API
    "NoBaselineError",
    "SynthesisResult",
    "Trigger",
    "run_synthesis",
    # Monkeypatchable phase helpers
    "_run_phase_1_analysts",
    "_run_phase_2_debates",
    "_run_one_horizon_debate",
    "_run_phase_3_synthesizer",
    "_enforce_speculation_cap",
    "_run_phase_4_risk",
    "_run_one_risk_perspective",
    "_make_risk_officer",
    "_run_phase_5_fund_manager",
    "_make_fund_manager",
    "_safe_run_agent",
    "_persist_agent_reports",
    "_ingest_synthesis_trail",
    "_check_cost_cap",
    "_read_synthesis_trail_costs",
    # Input helpers (monkeypatched in tests)
    "_assemble_portfolio_summary",
    "_assemble_fills_summary",
    "_load_user_context_yaml",
    # Rendering helper
    "_horizon_md",
    # Agent classes (monkeypatched in phase-1 test)
    "ConcentrationAnalystAgent",
    "FxAnalystAgent",
    "FundamentalsAnalystAgent",
    "HouseholdBudgetAnalystAgent",
    "MacroAnalystAgent",
    "NewsAnalystAgent",
    "PlanCritiqueAgent",
    "SentimentAnalystAgent",
    "TaxAnalystAgent",
    "TechnicalAnalystAgent",
]
