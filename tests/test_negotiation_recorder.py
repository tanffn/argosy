"""Tests for argosy.services.negotiation_recorder + transcript_writer (Wave C)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.agents.fund_manager import (
    FundManagerDecision,
    FundManagerPlanRevisionDecision,
)
from argosy.agents.researcher_facilitator import DebateOutcome
from argosy.agents.risk_facilitator import RiskOutcome
from argosy.agents.base import ConfidenceBand
from argosy.services.negotiation_recorder import record_negotiation_phase
from argosy.services.transcript_writer import (
    ParticipantRef,
    render_sequence_mmd,
    render_tldr,
    render_transcript,
    write_phase_bundle,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport,
    AuditLog,
    DecisionPhase,
    DecisionRun,
)


# ----------------------------------------------------------------------
# transcript_writer template tests (no DB needed; pure rendering)
# ----------------------------------------------------------------------


def test_tldr_for_debate_outcome_renders_winner_and_synthesis():
    v = DebateOutcome(
        winning_side="bull",
        synthesis="The bull case prevails on valuation.",
        cited_evidence=["DCF supports +20%", "Analyst note 2026-05-01"],
        rounds_run=2,
        confidence=ConfidenceBand.HIGH,
        cited_sources=["docs/equities/AAPL.md"],
    )
    md = render_tldr(v, "researcher_debate")
    assert "winning_side" not in md  # uses formatted label
    assert "Winning side" in md
    assert "`bull`" in md
    assert "Rounds run:** 2" in md
    assert "valuation" in md
    assert "DCF supports +20%" in md


def test_tldr_for_risk_outcome_renders_consensus_and_dissent():
    v = RiskOutcome(
        consensus_verdict="APPROVE_WITH_CONDITIONS",
        consolidated_conditions=["cash floor $100k", "position cap 5%"],
        dissent_summary="Conservative officer flagged wash-sale risk.",
        rounds_run=1,
        confidence=ConfidenceBand.MEDIUM,
        cited_sources=["docs/risk/wash_sale.md"],
    )
    md = render_tldr(v, "risk_team")
    assert "`APPROVE_WITH_CONDITIONS`" in md
    assert "cash floor $100k" in md
    assert "wash-sale risk" in md


def test_tldr_for_fund_manager_decision_renders_decision_and_conditions():
    v = FundManagerDecision(
        decision="green_light",
        reason="All constraints satisfied; risk APPROVE.",
        required_conditions=["concentration < 65% post-fill"],
        post_execution_checks=["verify cash >= reserve"],
        confidence=ConfidenceBand.HIGH,
        cited_sources=["docs/policy/concentration.md"],
    )
    md = render_tldr(v, "fund_manager")
    assert "`green_light`" in md
    assert "concentration < 65%" in md


def test_tldr_for_fund_manager_plan_revision_renders_approved():
    v = FundManagerPlanRevisionDecision(
        approved=True,
        reasons=["coheres with hard constraints"],
        cited_sources=["docs/plan/structure.md"],
    )
    md = render_tldr(v, "plan_synth_p5")
    assert "`True`" in md
    assert "coheres" in md


def test_tldr_falls_back_for_unknown_dto():
    """Generic JSON dump fallback for any pydantic model not in the dispatch table."""
    from pydantic import BaseModel

    class CustomDTO(BaseModel):
        foo: str
        bar: int

    md = render_tldr(CustomDTO(foo="hi", bar=42), "custom_phase")
    assert "CustomDTO" in md
    assert "```json" in md
    assert "hi" in md


def test_tldr_for_none_returns_no_verdict_marker():
    md = render_tldr(None, "analysts")
    assert "no facilitator verdict" in md.lower()


def test_render_transcript_includes_each_participant():
    parts = [
        ParticipantRef(
            agent_role="bull_researcher", agent_report_id=1,
            response_text="Bull thesis: AAPL is undervalued.",
            side="bull", round=1, confidence="HIGH", model="opus",
        ),
        ParticipantRef(
            agent_role="bear_researcher", agent_report_id=2,
            response_text="Bear thesis: margin compression.",
            side="bear", round=1, confidence="MEDIUM", model="opus",
        ),
    ]
    md = render_transcript(parts, "researcher_debate")
    assert "bull_researcher" in md
    assert "bear_researcher" in md
    assert "side=bull" in md
    assert "AAPL is undervalued" in md
    assert "margin compression" in md


def test_render_sequence_mmd_emits_participants_and_arrows():
    parts = [
        ParticipantRef(
            agent_role="bull_researcher", agent_report_id=1,
            response_text="...", side="bull", round=1, confidence="HIGH",
        ),
        ParticipantRef(
            agent_role="researcher_facilitator", agent_report_id=2,
            response_text="...", confidence="MEDIUM",
        ),
    ]
    v = DebateOutcome(
        winning_side="bull", synthesis="x", cited_evidence=[], rounds_run=1,
        confidence=ConfidenceBand.HIGH, cited_sources=["docs/x.md"],
    )
    mmd = render_sequence_mmd(parts, "researcher_debate", verdict=v)
    assert mmd.startswith("sequenceDiagram")
    assert "participant U as User" in mmd
    assert "participant bull_researcher" in mmd
    assert "participant researcher_facilitator" in mmd
    assert "DebateOutcome" in mmd
    assert "winner=bull" in mmd


def test_write_phase_bundle_creates_four_files(argosy_home_db):
    parts = [
        ParticipantRef(
            agent_role="bull_researcher", agent_report_id=1,
            response_text="bull case", side="bull", round=1,
        ),
    ]
    v = DebateOutcome(
        winning_side="bull", synthesis="ok", cited_evidence=[], rounds_run=1,
        confidence=ConfidenceBand.MEDIUM, cited_sources=["docs/x.md"],
    )
    started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=42)
    bundle, tldr, mmd = write_phase_bundle(
        user_id="ariel", decision_run_id=42, phase_kind="researcher_debate",
        started_at=started, finished_at=finished, verdict=v, participants=parts,
    )
    assert bundle.exists()
    assert (bundle / "TLDR.md").exists()
    assert (bundle / "transcript.md").exists()
    assert (bundle / "verdict.json").exists()
    assert (bundle / "sequence.mmd").exists()
    # Bundle path layout: <home>/transcripts/<user>/<YYYY-MM-DD>/<run>__<kind>/
    rel = bundle.relative_to(argosy_home_db / "transcripts" / "ariel")
    assert str(rel.parts[0]) == "2026-05-08"
    assert "42__researcher_debate" in rel.parts[1]


# ----------------------------------------------------------------------
# negotiation_recorder integration tests (DB-backed)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_inserts_phase_row(argosy_home_db):
    # Seed: one decision_run + two agent_reports.
    async with db_mod.get_session() as session:
        run = DecisionRun(
            user_id="ariel", ticker="AAPL", tier="T2",
            status="running", decision_kind="trade_proposal",
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        a = AgentReport(
            user_id="ariel", agent_role="bull_researcher",
            decision_id=str(run_id), response_text="Bull case",
            confidence="HIGH", model="opus",
        )
        b = AgentReport(
            user_id="ariel", agent_role="researcher_facilitator",
            decision_id=str(run_id), response_text='{"winning_side":"bull"}',
            confidence="MEDIUM", model="sonnet",
        )
        session.add_all([a, b])
        await session.flush()
        ids = [a.id, b.id]
        await session.commit()

    v = DebateOutcome(
        winning_side="bull", synthesis="ok", cited_evidence=[], rounds_run=1,
        confidence=ConfidenceBand.MEDIUM, cited_sources=["docs/x.md"],
    )
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    phase_id = await record_negotiation_phase(
        user_id="ariel", decision_run_id=run_id, kind="researcher_debate",
        started_at=started, agent_report_ids=ids, verdict=v,
        side_by_id={ids[0]: "bull"},
    )
    assert phase_id > 0

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(DecisionPhase).where(DecisionPhase.id == phase_id)
            )
        ).scalar_one()
        assert row.kind == "researcher_debate"
        assert row.seq == 1
        assert row.verdict_kind == "DebateOutcome"
        verdict_data = json.loads(row.verdict_json)
        assert verdict_data["winning_side"] == "bull"
        assert row.tldr_md is not None and "bull" in row.tldr_md
        assert row.bundle_dir is not None
        assert Path(row.bundle_dir).exists()

        # Participants_json round-trips.
        parts = json.loads(row.participants_json)
        assert len(parts) == 2
        assert parts[0]["agent_role"] == "bull_researcher"
        assert parts[0]["side"] == "bull"


@pytest.mark.asyncio
async def test_recorder_back_fills_agent_reports_phase_id(argosy_home_db):
    async with db_mod.get_session() as session:
        run = DecisionRun(
            user_id="ariel", ticker="NVDA", tier="T1",
            status="running", decision_kind="trade_proposal",
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        a = AgentReport(
            user_id="ariel", agent_role="trader",
            decision_id=str(run_id), response_text="trader proposal",
        )
        session.add(a)
        await session.flush()
        a_id = a.id
        await session.commit()

    phase_id = await record_negotiation_phase(
        user_id="ariel", decision_run_id=run_id, kind="trader",
        started_at=datetime.now(timezone.utc),
        agent_report_ids=[a_id], verdict=None,
    )

    async with db_mod.get_session() as session:
        ar = (
            await session.execute(
                select(AgentReport).where(AgentReport.id == a_id)
            )
        ).scalar_one()
        assert ar.phase_id == phase_id


@pytest.mark.asyncio
async def test_recorder_emits_audit_event(argosy_home_db):
    async with db_mod.get_session() as session:
        run = DecisionRun(
            user_id="ariel", ticker="MSFT", tier="T3",
            status="running", decision_kind="trade_proposal",
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    await record_negotiation_phase(
        user_id="ariel", decision_run_id=run_id, kind="risk_team",
        started_at=datetime.now(timezone.utc),
        agent_report_ids=[],
        verdict=RiskOutcome(
            consensus_verdict="APPROVE",
            consolidated_conditions=[], dissent_summary="",
            rounds_run=1, confidence=ConfidenceBand.HIGH,
            cited_sources=["docs/risk/x.md"],
        ),
    )

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "provenance.phase.finished",
                    AuditLog.user_id == "ariel",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)
    assert payload["phase_kind"] == "risk_team"
    assert payload["verdict_kind"] == "RiskOutcome"


@pytest.mark.asyncio
async def test_recorder_seq_monotonic_per_run(argosy_home_db):
    """Multiple phases for one decision_run get seq=1, 2, 3..."""
    async with db_mod.get_session() as session:
        run = DecisionRun(
            user_id="ariel", ticker="GOOG", tier="T2",
            status="running", decision_kind="trade_proposal",
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    for i, kind in enumerate(["analysts", "researcher_debate", "risk_team"]):
        await record_negotiation_phase(
            user_id="ariel", decision_run_id=run_id, kind=kind,
            started_at=datetime.now(timezone.utc),
            agent_report_ids=[], verdict=None,
        )

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(DecisionPhase)
                .where(DecisionPhase.decision_run_id == run_id)
                .order_by(DecisionPhase.seq)
            )
        ).scalars().all()
    assert [r.seq for r in rows] == [1, 2, 3]
    assert [r.kind for r in rows] == ["analysts", "researcher_debate", "risk_team"]
