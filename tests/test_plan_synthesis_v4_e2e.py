"""v4 Plan-synthesis pipeline — Phase-5-enabled integration smoke test.

Goal: prove the wiring is correct end-to-end without any real LLM call.

What this test exercises:

  * ARGOSY_PHASE5_AGENTS=true → the Phase 1 fleet resolves to the
    13-member shape (10 core + EquityCompAnalyst + WithdrawalSequencer
    + PlanCoverageAnalyst).
  * The full ``run_synthesis`` entry point — opens a real DecisionRun
    row, runs all 5 phases (with each agent's run_sync mocked to a
    schema-valid Pydantic output), persists a role='draft' PlanVersion,
    threads agent_reports + decision_phases rows.
  * The new schemas: ``EquityCompAnalystOutput`` (3 scenarios) and
    ``ConcentrationAnalystOutput`` (4 named constraints whose MIN sets
    ``nvda_cap_pct``) are produced + persisted as ``agent_reports``
    rows visible by ``user_id`` + ``decision_id``.
  * The v4 renderer appendices land in ``horizon_long_md`` (assumption
    ledger + section-by-section evidence + fleet receipts).
  * The deltas-vs-prior block is at the TOP of each horizon's user
    markdown.
  * The plan_synthesizer system prompt carries the FORBIDDEN-derivation
    rule (this asserts the static rule survived; no model call needed).
  * ``Phase1Inputs`` exposes ``tax_payload`` + ``base_salary_usd`` so
    EquityCompAnalystAgent gets routed material inputs.

Patch points: each agent class on the package namespace is replaced
with a small stub whose ``run_sync`` returns a real
``BaseAgent.AgentReport`` carrying a schema-valid Pydantic output. The
BaseAgent LLM-call surface (``_call_via_api_key`` / ``_call_via_
claude_code_inner``) is never reached because ``run_sync`` itself is
the stub seam.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.concentration_analyst_types import (
    ConcentrationAnalystOutput,
    ConstraintRow,
    DelaySensitivityRow,
)
from argosy.agents.equity_comp_analyst_types import (
    EquityCompAnalystOutput,
    GrantRow,
    ScenarioProjection,
    YearVestRow,
)
from argosy.agents.plan_synthesizer_types import (
    Action,
    Citation,
    Delta,
    FactClaim,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthTarget,
    SynthesisInputs,
    Theme,
)
from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    PlanVersion,
    User,
)


# ---------------------------------------------------------------------------
# Schema-valid output builders for the new Phase 5 schemas
# ---------------------------------------------------------------------------


def _make_equity_comp_output() -> EquityCompAnalystOutput:
    """EquityCompAnalystOutput with exactly three named scenarios."""
    def _year_row(year: int, net_nis: float) -> YearVestRow:
        return YearVestRow(
            year=year,
            gross_shares=600.0,
            gross_usd=87_000.0,
            gross_nis=300_000.0,
            net_nis=net_nis,
            net_retention_pct=47.0,
            confidence="HIGH",
            source="contractual",
        )

    def _scenario(name: str, avg_net_nis: float, confidence: str) -> ScenarioProjection:
        return ScenarioProjection(
            name=name,  # type: ignore[arg-type]
            assumptions_md=(
                "- NVDA price: $145 USD planning anchor\n"
                "- USD/NIS: 3.45\n"
                "- Marginal IL: 47% + 3% surtax + Section 102 25% capital track\n"
            ),
            years=[_year_row(y, avg_net_nis) for y in range(2026, 2031)],
            five_year_avg_net_nis=avg_net_nis,
            fi_date_impact_years=0.0,
            confidence=confidence,  # type: ignore[arg-type]
        )

    return EquityCompAnalystOutput(
        active_grants=[
            GrantRow(
                award_id="A-2024-Q1",
                award_date=date(2024, 2, 15),
                quarterly_shares=140.0,
                remaining_quarters=8,
                status="contractual",
            ),
        ],
        scenarios=[
            _scenario("known_grants_only", 500_000.0, "HIGH"),
            _scenario("conservative_decay", 350_000.0, "LOW"),
            _scenario("optimistic_flat", 500_000.0, "LOW"),
        ],
        nvda_sell_on_vest_policy=(
            "Defer the sell with a cap-band rebalance trigger; do not "
            "auto-liquidate at vest."
        ),
        advisor_intake_questions=[
            "Upload pages 2-4 of the RSU portal to verify the full grant list.",
        ],
        confidence=ConfidenceBand.MEDIUM,
        cited_sources=["identity_yaml.rsu_grants", "tax_analyst.effective_cgt"],
    )


def _make_concentration_output() -> ConcentrationAnalystOutput:
    """ConcentrationAnalystOutput with 4 constraints + nvda_cap_pct = MIN()."""
    constraints = [
        ConstraintRow(
            name="sequence_cap",  # type: ignore[arg-type]
            value_pct=0.20,
            derivation_md=(
                "FI delay tolerance = 1 year; sigma_NVDA = 35%; sequence buffer "
                "calculus from withdrawal_sequencer yields 20% binding cap."
            ),
            confidence="MEDIUM",
        ),
        ConstraintRow(
            name="tail_loss_cap",  # type: ignore[arg-type]
            value_pct=0.25,
            derivation_md=(
                "p5 1-year loss = 14% of portfolio at 25% NVDA share, within "
                "the user-stated 15% single-name drawdown tolerance."
            ),
            confidence="MEDIUM",
        ),
        ConstraintRow(
            name="risk_contribution_cap",  # type: ignore[arg-type]
            value_pct=0.22,
            derivation_md=(
                "Marginal-variance contribution of NVDA at 22% portfolio share "
                "is 47% of total variance — at the user's stated bound."
            ),
            confidence="MEDIUM",
        ),
        ConstraintRow(
            name="tax_liquidity_cap",  # type: ignore[arg-type]
            value_pct=0.28,
            derivation_md=(
                "Section 102 24-month windows allow ~3,000 sh/yr divestment at "
                "current price; 28% cap respects per-year liquidity envelope."
            ),
            confidence="HIGH",
        ),
    ]
    # MIN of constraints = 0.20 (sequence_cap). The schema doesn't enforce
    # this equality (defense-in-depth lives in the synth prompt), but a
    # well-behaved analyst would assert it. This mock honours the contract.
    return ConcentrationAnalystOutput(
        current_nvda_pct=0.31,
        current_risk_contribution_pct=0.55,
        tail_loss_p5_1y_pct=0.18,
        constraints=constraints,
        nvda_cap_pct=0.20,
        delay_sensitivities=[
            DelaySensitivityRow(
                delay_tolerance_years=0.0,
                nvda_cap_pct=0.0,
                rationale_md="No delay tolerance -> force-zero cap.",
            ),
            DelaySensitivityRow(
                delay_tolerance_years=1.0,
                nvda_cap_pct=0.20,
                rationale_md="One-year delay tolerance -> sequence_cap binds at 20%.",
            ),
            DelaySensitivityRow(
                delay_tolerance_years=2.0,
                nvda_cap_pct=0.30,
                rationale_md="Two-year delay tolerance -> risk_contribution_cap binds at 30%.",
            ),
        ],
        sell_down_glidepath_md=(
            "Per-quarter NVDA sell sequence assuming Section 102 24-month "
            "windows; realized USD + net NIS at 30% effective CGT."
        ),
        advisor_intake_questions=[
            "Confirm max FI-delay tolerance in years.",
        ],
        confidence=ConfidenceBand.MEDIUM,
        cited_sources=[
            "portfolio_snapshot:NVDA",
            "sigma_calibrator.NVDA",
            "withdrawal_sequencer.fi_year",
        ],
    )


# ---------------------------------------------------------------------------
# Helpers: build a real AgentReport from any Pydantic output.
# ---------------------------------------------------------------------------


def _make_agent_report(
    *, role: str, user_id: str, decision_id: str, output: Any,
) -> AgentReport:
    """Build a real ``AgentReport`` dataclass (what _persist_agent_reports
    + _record_phase_completion expect to see)."""
    payload = (
        output.model_dump_json() if hasattr(output, "model_dump_json") else "{}"
    )
    return AgentReport(
        agent_role=role,
        user_id=user_id,
        model="stub-model",
        response_text=payload,
        tokens_in=100,
        tokens_out=200,
        cost_usd=0.001,
        prompt_hash=f"stub-{role}",
        confidence=ConfidenceBand.MEDIUM,
        output=output,
        decision_id=decision_id,
        run_correlation_id=f"corr-{role}",
        system_prompt="sys",
        user_prompt="usr",
    )


def _make_synth_output() -> PlanSynthesisOutput:
    """Build a valid PlanSynthesisOutput.

    Each horizon carries at least one Delta so the renderer's
    deltas-at-top behaviour produces a visible block (asserted below).
    The flat ``sections`` list carries 3 canonical sections so the
    section-evidence appendix renders.
    """
    deltas = {
        "long": Delta(
            item_kind="theme",
            item_id="long.themes.equity_tilt",
            horizon="long",
            change_kind="modified",
            summary="Tighten the equity tilt rationale.",
            rationale="Concentration_analyst now sets the NVDA cap at 20%.",
            cited_sources=["concentration_analyst:plan-synth-test"],
        ),
        "medium": Delta(
            item_kind="target",
            item_id="medium.targets.nvda",
            horizon="medium",
            change_kind="modified",
            summary="NVDA target now 20% (derived).",
            rationale="Concentration_analyst.nvda_cap_pct = 20%.",
            cited_sources=["concentration_analyst"],
        ),
        "short": Delta(
            item_kind="action",
            item_id="short.actions.sell_glidepath",
            horizon="short",
            change_kind="added",
            summary="Add NVDA sell glidepath this quarter.",
            rationale="Section 102 window opens for the 2024-Q1 lot.",
            cited_sources=["concentration_analyst"],
        ),
    }
    long = HorizonSection(
        horizon="long",
        freshness_expected="annual",
        status="minor_revision",
        posture="Steady accumulation with defensive tilt.",
        targets=[
            SynthTarget(
                label="Equity allocation",
                value=60.0,
                unit="pct_of_portfolio",
                stated_at=date(2026, 1, 1),
                revisit_after=date(2027, 1, 1),
                rationale="Long-horizon risk tolerance.",
            ),
        ],
        deltas_from_prior=[deltas["long"]],
        rationale="Strategic frame holds.",
    )
    medium = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="NVDA cap binds at 20%.",
        targets=[
            SynthTarget(
                label="NVDA share of portfolio",
                value=20.0,
                unit="pct_of_portfolio",
                stated_at=date(2026, 6, 1),
                revisit_after=date(2026, 9, 1),
                rationale="ConcentrationAnalyst.nvda_cap_pct.",
            ),
        ],
        deltas_from_prior=[deltas["medium"]],
        rationale="Cap derivation now analyst-owned.",
    )
    short = HorizonSection(
        horizon="short",
        freshness_expected="monthly",
        status="major_revision",
        posture="Execute Q3 NVDA glidepath.",
        actions=[
            Action(
                label="Sell 750 NVDA shares",
                horizon_kind="dated",
                detail="Section 102 24-month window; 2024-Q1 lot.",
                rationale="ConcentrationAnalyst sell_down_glidepath.",
                trigger_or_date="2026-07-15",
            ),
        ],
        deltas_from_prior=[deltas["short"]],
        rationale="Tactical execution this quarter.",
    )

    # Build 3 canonical Sections (long/medium/short) with valid evidence.
    def _section(section_id: str, horizon: str) -> Section:
        facts = [
            FactClaim(
                text=f"Plan section {section_id} statement (sufficient length).",
                kind="qualitative",
            ),
        ]
        citations = [
            Citation(
                source_kind="plan_doc",
                source_locator=f"distillate.{section_id}[0]",
                extract=f"Verbatim distillate extract for {section_id}.",
                supports_fact_index=0,
            ),
        ]
        return Section(
            section_id=section_id,
            horizon=horizon,  # type: ignore[arg-type]
            title=CANONICAL_SECTION_IDS[section_id],
            body_md=f"Body markdown for **{section_id}** ({horizon}).",
            evidence=SectionEvidence(facts=facts, source_span=citations),
        )

    return PlanSynthesisOutput(
        long=long,
        medium=medium,
        short=short,
        inputs=SynthesisInputs(),
        sections=[
            _section("concentration", "medium"),
            _section("equity_comp", "long"),
            _section("withdrawal", "long"),
        ],
    )


# ---------------------------------------------------------------------------
# Stub agent class factories.
# ---------------------------------------------------------------------------


def _make_analyst_stub(role: str, output: Any):
    """Return a stub agent class whose run_sync emits a real AgentReport.

    Stubs declare ``build_prompt(self, **kw)`` (VAR_KEYWORD) so the
    orchestrator's _safe_run_agent signature-narrowing passes the full
    kwargs bag through; the body is a no-op since run_sync is the
    interception point.
    """
    class _Stub:
        agent_role = role

        def __init__(self, *_args, **_kw):
            self.user_id = _kw.get("user_id", "test_ariel")
            self.model = "stub-model"

        def build_prompt(self, **_kw):  # pragma: no cover - signature only
            return "sys", "usr"

        def run_sync(self, **kw):
            decision_id = kw.get("decision_id", "stub-decision")
            return _make_agent_report(
                role=role,
                user_id=self.user_id,
                decision_id=decision_id,
                output=output,
            )

    _Stub.__name__ = f"_Stub_{role}"
    return _Stub


# ---------------------------------------------------------------------------
# Fixture: file-backed SQLite at alembic head, with both sync + async engines
# bound to the same file.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_state_after_each_test():
    """Test-isolation guard for this file.

    Two pieces of global state this file mutates would otherwise leak into
    later test files (observed: it broke
    test_plan_synthesis_decision_id_propagation's trail tests, which assert
    a fixed phase-1 agent count and write to ``settings.home/logs/synthesis``):

      1. ``orchestrator._PHASE_1_AGENT_NAMES`` — the phase-5 test patches it
         to the 13-member fleet. monkeypatch *should* revert it, but the
         leak was observed empirically, so we snapshot + force-restore the
         module global here as belt-and-suspenders.
      2. The settings lru cache (ARGOSY_HOME + ARGOSY_PHASE5_AGENTS) — rebuilt
         from the clean env after monkeypatch reverts it.

    This fixture is set up before ``v4_db`` requests monkeypatch, so its
    teardown runs AFTER monkeypatch has reverted everything.
    """
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as _orch
    _saved_names = _orch._PHASE_1_AGENT_NAMES
    yield
    _orch._PHASE_1_AGENT_NAMES = _saved_names
    from argosy.config import reload_settings
    reload_settings()


@pytest.fixture
def v4_db(tmp_path, monkeypatch):
    """Per-test file-backed DB at alembic head + both engines bound.

    Mirrors the pattern from
    ``test_plan_synthesis_flow.test_phase_completion_threads_agent_report_ids``
    — sync engine for the orchestrator's session; async engine for
    _record_phase_completion's sub-session writes. Both point at the
    same SQLite file so writes from either side are visible to the
    other.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    # Phase 5 fleet flag — read by _resolve_phase_1_agent_names. Setting
    # it BEFORE reload_settings + before patching _PHASE_1_AGENT_NAMES
    # means downstream consumers (e.g. plan_synthesizer prompt code that
    # reads the flag, ARGOSY_PHASE5_AGENTS-conditioned features) see it.
    monkeypatch.setenv("ARGOSY_PHASE5_AGENTS", "true")
    from argosy.config import reload_settings, get_settings
    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"
    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False},
    )

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    from argosy.state import db as db_mod
    db_mod.init_engine(async_url)

    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="test_ariel", plan="free"))
        sess.add(PlanVersion(
            user_id="test_ariel",
            role="baseline",
            version_label="Jacobs v2.0",
            raw_markdown="# Baseline plan",
            distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
        ))
        sess.commit()
        yield sess
    finally:
        sess.close()
        sync_engine.dispose()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db_mod.dispose_engine())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# The integration test.
# ---------------------------------------------------------------------------


def test_v4_phase5_pipeline_runs_end_to_end(v4_db, monkeypatch):
    """End-to-end: run_synthesis with all agent classes mocked.

    Asserts the wiring carries the new Phase 5 schemas through the
    orchestrator → agent_reports → PlanVersion → renderer chain.
    """
    session = v4_db
    user_id = "test_ariel"

    # --- Assertion A: Phase1Inputs carries tax_payload + base_salary_usd.
    # This is a static schema check — confirms the sister sub-agent's
    # field extension landed. Done early so the test fails loudly here
    # before any other moving piece runs.
    from argosy.orchestrator.flows.plan_synthesis.inputs import Phase1Inputs
    p1_fields = {f.name for f in Phase1Inputs.__dataclass_fields__.values()}
    assert "tax_payload" in p1_fields, (
        "Phase1Inputs is missing tax_payload — EquityCompAnalystAgent "
        "needs the tax analyst's payload routed in via signature narrowing."
    )
    assert "base_salary_usd" in p1_fields, (
        "Phase1Inputs is missing base_salary_usd — EquityCompAnalystAgent "
        "needs the salary anchor routed in via signature narrowing."
    )

    # --- Assertion B: the plan_synthesizer system prompt carries the
    # FORBIDDEN-derivation rule that bans synth-invented NVDA cap /
    # retirement-year numbers. Static check on the agent's prompt
    # builder; no model call needed.
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    synth_agent = PlanSynthesizerAgent(user_id=user_id)
    sys_prompt, _ = synth_agent.build_prompt(
        baseline_distillate_md="# baseline\n",
        prior_current_md="",
        analyst_reports_text="",
        debate_outcomes_text="",
        portfolio_snapshot_summary="",
        recent_fills_summary="",
    )
    assert "FORBIDDEN" in sys_prompt, (
        "plan_synthesizer system prompt missing the FORBIDDEN rule — "
        "the derivation-ownership guardrail regressed."
    )
    assert "concentration_analyst.nvda_cap_pct" in sys_prompt, (
        "FORBIDDEN block must explicitly route the NVDA cap through "
        "concentration_analyst.nvda_cap_pct."
    )

    from argosy.orchestrator.flows import plan_synthesis as flow

    # --- Phase 5 fleet expansion. The module-level
    # ``_PHASE_1_AGENT_NAMES`` is resolved at import time so flipping
    # the env var alone isn't enough — patch the tuple directly on the
    # orchestrator submodule.
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch_mod
    expanded_names = (
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
        "PlanCoverageAnalyst",
        "WithdrawalSequencerAgent",
        "EquityCompAnalystAgent",
    )
    monkeypatch.setattr(orch_mod, "_PHASE_1_AGENT_NAMES", expanded_names)

    # --- Mock every Phase 1 agent class on the package namespace with a
    # stub whose run_sync returns a real AgentReport. The two stars of
    # this test (concentration + equity_comp) get their canonical
    # schemas; the others get simple SimpleNamespace outputs (the
    # orchestrator only looks at model_dump_json + persistence — it
    # doesn't introspect the schema).
    concentration_output = _make_concentration_output()
    equity_comp_output = _make_equity_comp_output()

    class _SimplePydantic(SimpleNamespace):
        """Object with the duck-type shape ``BaseAgent.run_sync``
        consumers rely on (``model_dump_json``)."""

        def model_dump_json(self) -> str:
            return json.dumps({"role": getattr(self, "role", "stub")})

        def model_dump(self) -> dict:
            return {"role": getattr(self, "role", "stub")}

    def _generic_output(role: str) -> Any:
        return _SimplePydantic(role=role)

    # Stars: real schemas.
    monkeypatch.setattr(
        flow,
        "ConcentrationAnalystAgent",
        _make_analyst_stub("concentration_analyst", concentration_output),
    )
    monkeypatch.setattr(
        flow,
        "EquityCompAnalystAgent",
        _make_analyst_stub("equity_comp_analyst", equity_comp_output),
    )

    # Everyone else: generic stubs.
    for class_name, role in (
        ("FxAnalystAgent", "fx_analyst"),
        ("FundamentalsAnalystAgent", "fundamentals_analyst"),
        ("HouseholdBudgetAnalystAgent", "household_budget_analyst"),
        ("MacroAnalystAgent", "macro_analyst"),
        ("NewsAnalystAgent", "news_analyst"),
        ("PlanCritiqueAgent", "plan_critique"),
        ("SentimentAnalystAgent", "sentiment_analyst"),
        ("TaxAnalystAgent", "tax_analyst"),
        ("TechnicalAnalystAgent", "technical_analyst"),
        ("PlanCoverageAnalyst", "plan_coverage_analyst"),
        ("WithdrawalSequencerAgent", "withdrawal_sequencer"),
    ):
        monkeypatch.setattr(
            flow,
            class_name,
            _make_analyst_stub(role, _generic_output(role)),
        )

    # --- Phase 2 / 3 / 4 / 5: stub the per-phase helpers wholesale.
    # Phase 1 runs through the REAL ``_run_phase_1_analysts`` (so we
    # exercise the resolution path + the 13-member fleet expansion +
    # _safe_run_agent). Phases 2/3/4/5 are stubbed because their inner
    # agent fan-out is not what this test is verifying.

    synth_output = _make_synth_output()

    def _stub_phase_2(**kw):
        return "(stub debate outcomes)", []

    def _stub_phase_3(**kw):
        # Reuse the user_id from kwargs to keep AgentReport.user_id
        # consistent with the orchestrator's threading.
        return synth_output, [
            _make_agent_report(
                role="plan_synthesizer",
                user_id=kw.get("user_id", user_id),
                decision_id=kw.get("decision_run_id", "stub-decision"),
                output=synth_output,
            ),
        ]

    def _stub_phase_4(**kw):
        return "(stub risk verdict)", []

    def _stub_phase_5(**kw):
        return True, [
            _make_agent_report(
                role="fund_manager",
                user_id=kw.get("user_id", user_id),
                decision_id=kw.get("decision_run_id", "stub-decision"),
                output=_SimplePydantic(role="fund_manager", approved=True),
            ),
        ]

    monkeypatch.setattr(flow, "_run_phase_2_debates", _stub_phase_2)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", _stub_phase_3)
    monkeypatch.setattr(flow, "_run_phase_4_risk", _stub_phase_4)
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", _stub_phase_5)

    # --- Quiet the legacy assemblers (irrelevant noise for this test).
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "(none)")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "(none)")

    # --- Bypass the language rewriter (it does its own LLM call when
    # not stubbed). Pass output through unchanged.
    monkeypatch.setattr(
        flow, "_run_plan_language_rewriter",
        lambda *, output, user_id, decision_run_id: output,
    )

    # --- Skip the codex Phase 4.5 (it talks to external gpt-5 by
    # default). Returns (None, None) — flow handles that.
    async def _no_codex(**kw):
        return None, None

    monkeypatch.setattr(flow, "run_codex_second_opinion", _no_codex)

    # --- Drive the synthesis to completion.
    result = flow.run_synthesis(
        session, user_id=user_id, trigger="scheduled",
    )

    # =====================================================================
    # ASSERTIONS
    # =====================================================================

    # --- (1) DecisionRun row created with status='completed'.
    assert result.decision_run_id is not None
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr is not None, "DecisionRun row was not persisted"
    assert dr.status == "completed", (
        f"DecisionRun.status should be 'completed'; got {dr.status!r}"
    )
    assert dr.fund_manager_decision == "approved", (
        f"FM verdict should be 'approved'; got {dr.fund_manager_decision!r}"
    )

    # --- (2) PlanVersion persisted with role='draft'.
    assert result.draft_id is not None
    pv = session.get(PlanVersion, result.draft_id)
    assert pv is not None, "draft PlanVersion row not persisted"
    assert pv.role == "draft", f"expected role='draft'; got {pv.role!r}"
    assert pv.user_id == user_id

    # --- (3) agent_reports rows present for equity_comp + concentration
    # with the new schemas.
    decision_audit_token = f"plan-synth-{result.decision_run_id}"

    equity_rows = session.execute(
        select(AgentReportRow).where(
            AgentReportRow.user_id == user_id,
            AgentReportRow.decision_id == decision_audit_token,
            AgentReportRow.agent_role == "equity_comp_analyst",
        )
    ).scalars().all()
    assert len(equity_rows) >= 1, (
        f"expected >=1 equity_comp_analyst agent_reports row for "
        f"decision_id={decision_audit_token!r}; got {len(equity_rows)}"
    )
    # The persisted response_text must round-trip through the schema —
    # i.e. it's actually a valid EquityCompAnalystOutput with 3 scenarios.
    eq_payload = json.loads(equity_rows[0].response_text)
    rehydrated = EquityCompAnalystOutput.model_validate(eq_payload)
    scenario_names = sorted(s.name for s in rehydrated.scenarios)
    assert scenario_names == sorted([
        "known_grants_only", "conservative_decay", "optimistic_flat",
    ]), (
        f"equity_comp_analyst output must carry the 3 canonical scenarios; "
        f"got {scenario_names}"
    )

    conc_rows = session.execute(
        select(AgentReportRow).where(
            AgentReportRow.user_id == user_id,
            AgentReportRow.decision_id == decision_audit_token,
            AgentReportRow.agent_role == "concentration_analyst",
        )
    ).scalars().all()
    assert len(conc_rows) >= 1, (
        f"expected >=1 concentration_analyst agent_reports row for "
        f"decision_id={decision_audit_token!r}; got {len(conc_rows)}"
    )
    conc_payload = json.loads(conc_rows[0].response_text)
    rehydrated_c = ConcentrationAnalystOutput.model_validate(conc_payload)
    constraint_names = sorted(c.name for c in rehydrated_c.constraints)
    assert constraint_names == sorted([
        "sequence_cap",
        "tail_loss_cap",
        "risk_contribution_cap",
        "tax_liquidity_cap",
    ]), (
        f"concentration_analyst output must carry all 4 named constraints; "
        f"got {constraint_names}"
    )
    # nvda_cap_pct must equal MIN over the constraint values.
    expected_min = min(c.value_pct for c in rehydrated_c.constraints)
    assert abs(rehydrated_c.nvda_cap_pct - expected_min) < 1e-9, (
        f"nvda_cap_pct ({rehydrated_c.nvda_cap_pct}) must equal "
        f"MIN(constraints[*].value_pct) = {expected_min}"
    )

    # --- (4) Renderer appendices in horizon_long_md.
    assert pv.horizon_long_md, "horizon_long_md should not be empty"
    # The section-by-section evidence appendix is appended after the
    # horizon body.
    assert "## Appendix — Section-by-section evidence" in pv.horizon_long_md, (
        "expected the section-evidence appendix in horizon_long_md; "
        f"got: {pv.horizon_long_md[:500]!r}"
    )
    # And the assumption ledger appendix.
    assert "## Appendix — Assumption ledger" in pv.horizon_long_md, (
        "expected the assumption-ledger appendix in horizon_long_md"
    )
    # Generic shape check the task spec calls out — "Appendix — Section"
    # is the common substring across the section-evidence header.
    assert "## Appendix — Section" in pv.horizon_long_md

    # --- (5) Deltas block at TOP of each horizon's user-facing md.
    for label, md_text in (
        ("long", pv.horizon_long_md),
        ("medium", pv.horizon_medium_md),
        ("short", pv.horizon_short_md),
    ):
        assert md_text, f"horizon_{label}_md should not be empty"
        # The H1 (e.g. "# Long horizon") is line 1; the deltas block
        # should be the first ## heading. We check that "## Deltas vs.
        # prior current" appears BEFORE any other ## heading.
        lines = md_text.splitlines()
        h2_positions = [
            i for i, ln in enumerate(lines) if ln.startswith("## ")
        ]
        assert h2_positions, (
            f"horizon_{label}_md has no H2 sections; expected at least the "
            f"Deltas block"
        )
        first_h2 = lines[h2_positions[0]]
        assert first_h2.startswith("## Deltas vs. prior current"), (
            f"horizon_{label}_md must put the Deltas block at the TOP "
            f"(first H2); first H2 was {first_h2!r}"
        )

    # --- (6) Phase 5 agents actually fired. ``PlanCoverageAnalyst``,
    # ``WithdrawalSequencerAgent``, ``EquityCompAnalystAgent`` rows
    # should be persisted because the env var + tuple expansion routed
    # them into the Phase 1 fan-out.
    p5_roles = {"equity_comp_analyst", "withdrawal_sequencer", "plan_coverage_analyst"}
    p5_rows = session.execute(
        select(AgentReportRow).where(
            AgentReportRow.user_id == user_id,
            AgentReportRow.decision_id == decision_audit_token,
            AgentReportRow.agent_role.in_(p5_roles),
        )
    ).scalars().all()
    seen_roles = {r.agent_role for r in p5_rows}
    missing_roles = p5_roles - seen_roles
    assert not missing_roles, (
        f"Phase 5 agents did not all fire — missing {sorted(missing_roles)} "
        f"out of {sorted(p5_roles)}. ARGOSY_PHASE5_AGENTS or "
        f"_PHASE_1_AGENT_NAMES expansion regressed."
    )
