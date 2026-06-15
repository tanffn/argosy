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

# Phase 5 — gated behind ARGOSY_PHASE5_AGENTS env var (see
# argosy.orchestrator.flows.plan_synthesis.orchestrator
# ::_resolve_phase_1_agent_names). Always imported so the resolver
# can find the classes via getattr; whether they actually run
# depends on the flag.
from argosy.agents.plan_coverage_analyst import PlanCoverageAnalyst
from argosy.agents.withdrawal_sequencer_agent import (
    WithdrawalSequencerAgent,
)
from argosy.agents.equity_comp_analyst import EquityCompAnalystAgent

# Orchestration entry point and all monkeypatchable phase helpers.
from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    run_synthesis,
    _run_phase_1_analysts,
    _run_phase_2_debates,
    _run_one_horizon_debate,
    _run_phase_3_synthesizer,
    # Phase 2 — prose rewriter wired between phase-3 and the
    # speculation-cap enforcer. Exposed on the package namespace so
    # tests can monkeypatch via ``flow._run_plan_language_rewriter``.
    _run_plan_language_rewriter,
    RewriterInvariantError,
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
    _record_phase_completion,
    _load_completed_phase_outputs,
    # Fire-and-forget cache warmer for FM objection translations —
    # eliminates the 100+ second first-load latency on /plan by warming
    # ``fm_objection_translations`` at synthesis completion. Exposed on
    # the package namespace so the orchestrator's ``_pkg.<name>`` call
    # site resolves through monkeypatches in tests.
    _schedule_fm_objection_translation_precompute,
    _precompute_fm_objection_translations,
)

# Argosy ZigZag — Phase 4.5 codex (gpt-5) second-opinion reviewer.
# Exposed on the package namespace so the orchestrator's
# ``_pkg.run_codex_second_opinion`` resolution finds it (and so tests
# can monkeypatch the dispatcher without touching the submodule).
from argosy.orchestrator.flows.plan_synthesis.codex_second_opinion import (
    CodexAgreement,
    CodexFinding,
    CodexSecondOpinion,
    run_codex_second_opinion,
)

# Whole-artifact adversarial reader — the holistic final pre-promotion stage.
# Exposed on the package namespace so the orchestrator resolves it via
# ``_pkg.run_whole_artifact_review`` (and tests can monkeypatch the
# dispatcher without touching the submodule), mirroring the codex export.
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
    run_whole_artifact_review,
)

# Input-assembly helpers (monkeypatched in tests).
from argosy.orchestrator.flows.plan_synthesis.inputs import (
    _assemble_portfolio_summary,
    _assemble_fills_summary,
    _load_user_context_yaml,
    resolve_risk_inputs,
)

# Rendering helpers (Phase 1: user vs audit split; back-compat alias
# ``_horizon_md`` resolves to the user variant for stale imports).
# v4 (block B1) appendix builders are exposed on the package namespace
# so orchestrator + amendment workers + tests can pull them via
# ``_pkg.render_plan_appendices`` and friends.
from argosy.orchestrator.flows.plan_synthesis.render import (
    _horizon_md,
    _horizon_md_audit,
    _horizon_md_user,
    _strip_history_leak,
    _strip_jargon,
    render_assumption_ledger_appendix,
    render_fleet_receipts_appendix,
    render_plan_appendices,
    render_section_evidence_appendix,
    render_trajectory_reconciliation_appendix,
)

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
    "_run_plan_language_rewriter",
    "RewriterInvariantError",
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
    "_record_phase_completion",
    "_load_completed_phase_outputs",
    "_schedule_fm_objection_translation_precompute",
    "_precompute_fm_objection_translations",
    # Argosy ZigZag — Phase 4.5 codex second-opinion reviewer
    "CodexAgreement",
    "CodexFinding",
    "CodexSecondOpinion",
    "run_codex_second_opinion",
    # Whole-artifact adversarial reader — final pre-promotion stage
    "CoherenceFinding",
    "WholeArtifactVerdict",
    "run_whole_artifact_review",
    # Input helpers (monkeypatched in tests)
    "_assemble_portfolio_summary",
    "_assemble_fills_summary",
    "_load_user_context_yaml",
    "resolve_risk_inputs",
    # Rendering helpers (Phase 1 split + v4 block B1 appendices)
    "_horizon_md",
    "_horizon_md_audit",
    "_horizon_md_user",
    "_strip_history_leak",
    "_strip_jargon",
    "render_assumption_ledger_appendix",
    "render_fleet_receipts_appendix",
    "render_plan_appendices",
    "render_section_evidence_appendix",
    "render_trajectory_reconciliation_appendix",
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
    # Phase 5 — included in the fleet only when ARGOSY_PHASE5_AGENTS=true
    "PlanCoverageAnalyst",
    "WithdrawalSequencerAgent",
    "EquityCompAnalystAgent",
]
