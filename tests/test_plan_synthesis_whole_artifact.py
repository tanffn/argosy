"""Wire-test for the whole-artifact reader as the FINAL synthesis stage.

Task 7: the whole-artifact adversarial reader (Task 6,
``whole_artifact_reader.run_whole_artifact_review``) runs AFTER the draft
``PlanVersion`` is persisted (so it reads the just-built artifact) and its
verdict is persisted as an ``agent_reports`` row with
``agent_role="whole_artifact_reader"`` via ``_record_phase_completion``
(phase_n=55, mirroring codex's 45). A BLOCK verdict marks the draft
NOT-auto-promotable through the SAME mechanism the fund_manager uses:
``decision_run.fund_manager_decision == "rejected"`` (the field
``post_draft_accept`` consults to fire its 422 promotion gate).

This module reuses the v4 e2e harness's schema-valid output builders +
the ``run_synthesis`` driving pattern. The reader is monkeypatched on the
package namespace (``flow.run_whole_artifact_review``) exactly as the v4
test monkeypatches ``flow.run_codex_second_opinion`` — under real pytest
the reader short-circuits to ``(None, None)``, so the patch is what makes
the wiring observable.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import AgentReport
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    PlanVersion,
    User,
)

# Reuse the v4 e2e harness's schema-valid output builders so this test
# does not re-implement the (large) Phase-1..5 stub fleet.
from tests.test_plan_synthesis_v4_e2e import (
    _make_agent_report,
    _make_analyst_stub,
    _make_concentration_output,
    _make_equity_comp_output,
    _make_synth_output,
)


@pytest.fixture(autouse=True)
def _reset_global_state_after_each_test():
    """Snapshot + restore ``_PHASE_1_AGENT_NAMES`` (the phase-5 fleet patch
    leaks otherwise) and rebuild the settings cache on teardown."""
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as _orch
    _saved_names = _orch._PHASE_1_AGENT_NAMES
    yield
    _orch._PHASE_1_AGENT_NAMES = _saved_names
    from argosy.config import reload_settings
    reload_settings()


@pytest.fixture
def synth_db(tmp_path, monkeypatch):
    """Per-test file-backed DB at alembic head + both engines bound.

    Mirrors ``test_plan_synthesis_v4_e2e.v4_db``.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
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


def _wire_phase_stubs(monkeypatch, flow, user_id: str):
    """Stub every Phase-1..5 helper + agent class on the package namespace.

    Mirrors the v4 e2e test's stubbing block so ``run_synthesis`` reaches
    the post-persist region (where the reader is wired) without any real
    LLM call.
    """
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

    class _SimplePydantic(SimpleNamespace):
        def model_dump_json(self) -> str:
            return json.dumps({"role": getattr(self, "role", "stub")})

        def model_dump(self) -> dict:
            return {"role": getattr(self, "role", "stub")}

    concentration_output = _make_concentration_output()
    equity_comp_output = _make_equity_comp_output()

    monkeypatch.setattr(
        flow, "ConcentrationAnalystAgent",
        _make_analyst_stub("concentration_analyst", concentration_output),
    )
    monkeypatch.setattr(
        flow, "EquityCompAnalystAgent",
        _make_analyst_stub("equity_comp_analyst", equity_comp_output),
    )
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
            flow, class_name, _make_analyst_stub(role, _SimplePydantic(role=role)),
        )

    synth_output = _make_synth_output()

    def _stub_phase_3(**kw):
        return synth_output, [
            _make_agent_report(
                role="plan_synthesizer",
                user_id=kw.get("user_id", user_id),
                decision_id=kw.get("decision_run_id", "stub-decision"),
                output=synth_output,
            ),
        ]

    def _stub_phase_5(**kw):
        return True, [
            _make_agent_report(
                role="fund_manager",
                user_id=kw.get("user_id", user_id),
                decision_id=kw.get("decision_run_id", "stub-decision"),
                output=_SimplePydantic(role="fund_manager", approved=True),
            ),
        ]

    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: ("(stub)", []))
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", _stub_phase_3)
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: ("(stub)", []))
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", _stub_phase_5)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "(none)")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "(none)")
    monkeypatch.setattr(
        flow, "_run_plan_language_rewriter",
        lambda *, output, user_id, decision_run_id: output,
    )

    async def _no_codex(**kw):
        return None, None

    monkeypatch.setattr(flow, "run_codex_second_opinion", _no_codex)


def _reader_stub(verdict: WholeArtifactVerdict):
    """Build an async ``run_whole_artifact_review`` stub returning a
    (verdict, AgentReport) tuple, like the codex dispatcher's contract."""

    async def _stub(**kw):
        row = AgentReport(
            agent_role="whole_artifact_reader",
            user_id=kw.get("user_id", "test_ariel"),
            model="gpt-5-codex",
            response_text=verdict.model_dump_json(),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="",
            confidence=None,
            output=verdict,
            decision_id=f"plan-synth-{kw.get('decision_run_id')}",
            run_correlation_id="corr-whole-artifact",
            system_prompt="",
            user_prompt="(prompt)",
        )
        return verdict, row

    return _stub


def test_whole_artifact_reader_row_recorded(synth_db, monkeypatch):
    """After a synthesis run, a ``whole_artifact_reader`` agent_reports row
    exists for the run's decision_id (the reader ran as the final stage)."""
    session = synth_db
    user_id = "test_ariel"

    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)

    verdict = WholeArtifactVerdict(overall_assessment="APPROVE", findings=[])
    monkeypatch.setattr(
        flow, "run_whole_artifact_review", _reader_stub(verdict),
    )

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    decision_audit_token = f"plan-synth-{result.decision_run_id}"
    rows = session.execute(
        select(AgentReportRow).where(
            AgentReportRow.user_id == user_id,
            AgentReportRow.decision_id == decision_audit_token,
            AgentReportRow.agent_role == "whole_artifact_reader",
        )
    ).scalars().all()
    assert len(rows) >= 1, (
        "expected >=1 whole_artifact_reader agent_reports row for "
        f"decision_id={decision_audit_token!r}; got {len(rows)}"
    )
    # The persisted verdict round-trips through the schema.
    payload = json.loads(rows[0].response_text)
    rehydrated = WholeArtifactVerdict.model_validate(payload)
    assert rehydrated.overall_assessment == "APPROVE"

    # APPROVE must NOT clobber the FM's approval.
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "approved"


def test_whole_artifact_reader_block_marks_not_auto_promotable(synth_db, monkeypatch):
    """A BLOCK verdict marks the draft NOT-auto-promotable via the SAME
    mechanism the fund_manager uses: ``decision_run.fund_manager_decision``
    flips to 'rejected' (the field ``post_draft_accept`` consults)."""
    session = synth_db
    user_id = "test_ariel"

    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)

    verdict = WholeArtifactVerdict(
        overall_assessment="BLOCK",
        findings=[CoherenceFinding(
            kind="contradiction",
            severity="BLOCKER",
            detail="net worth stated as two different values in two sections",
            surfaces_cited=["NW = 11.95M", "NW = 14.44M"],
        )],
    )
    monkeypatch.setattr(
        flow, "run_whole_artifact_review", _reader_stub(verdict),
    )

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    # FM approved (stub), but the reader BLOCK must override the
    # decision_run verdict to 'rejected' so post_draft_accept's 422 gate
    # fires — the draft is not auto-promotable without an explicit override.
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "rejected", (
        "a reader BLOCK must mark the draft not-auto-promotable via the "
        f"fund_manager_decision field; got {dr.fund_manager_decision!r}"
    )

    # The reader row is still recorded.
    decision_audit_token = f"plan-synth-{result.decision_run_id}"
    rows = session.execute(
        select(AgentReportRow).where(
            AgentReportRow.user_id == user_id,
            AgentReportRow.decision_id == decision_audit_token,
            AgentReportRow.agent_role == "whole_artifact_reader",
        )
    ).scalars().all()
    assert len(rows) >= 1
