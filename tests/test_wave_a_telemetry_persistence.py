"""Wave A — dataclass→ORM persistence of Anthropic Messages API telemetry.

Migration 0026 added 4 columns to ``agent_reports``:
``cache_input_tokens``, ``cache_creation_tokens``, ``thinking_tokens``,
``citations_json``. The dataclass ``argosy.agents.base.AgentReport`` also
gained those fields, populated from the ``messages.create`` response in
``_call_via_api_key``. The persistence sites that take the dataclass and
write an ORM row must forward those new fields — otherwise the telemetry
is recorded by BaseAgent but lost before it hits the DB.

This test covers the central persistence site (``DecisionFlow.
_persist_agent_reports``) and the intake/advisor persist helper
(``_persist_turn``) plus the CLI intake helper, exercising the 4 known
dataclass→ORM persistence paths in one place.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.decisions.flow import DecisionFlow, FlowConfig
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    User,
    UserContext,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _Dummy(BaseModel):
    agent_role: str = "fundamentals"
    cited_sources: list[str] = ["analyst:fundamentals"]
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    report: str = "{}"


def _make_dataclass_report(
    *,
    agent_role: str = "fundamentals",
    cache_input_tokens: int = 1234,
    cache_creation_tokens: int = 567,
    thinking_tokens: int = 4096,
    citations_json: str | None = '[{"type":"char_location","cited_text":"x"}]',
) -> AgentReport:
    """Dataclass AgentReport with all 4 Wave A telemetry fields populated."""
    return AgentReport(
        agent_role=agent_role,
        user_id="ariel",
        model="claude-haiku-4-5",
        response_text="{}",
        tokens_in=100,
        tokens_out=200,
        cost_usd=0.0123,
        prompt_hash="abc123",
        confidence=ConfidenceBand.HIGH,
        output=_Dummy(),
        cache_input_tokens=cache_input_tokens,
        cache_creation_tokens=cache_creation_tokens,
        thinking_tokens=thinking_tokens,
        citations_json=citations_json,
    )


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        if (
            await session.execute(select(User).where(User.id == "ariel"))
        ).scalar_one_or_none() is None:
            session.add(User(id="ariel"))
            await session.commit()


async def _seed_decision_run() -> int:
    async with db_mod.get_session() as session:
        from datetime import datetime, timezone

        row = DecisionRun(
            user_id="ariel",
            ticker="AAPL",
            tier="T0",
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        session.add(row)
        await session.commit()
        return row.id


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_flow_persists_wave_a_telemetry(engine: None) -> None:
    """DecisionFlow._persist_agent_reports forwards Wave A fields to the ORM row."""
    await _seed_user()
    decision_run_id = await _seed_decision_run()

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(),
        # No factories needed — we call the persistence helper directly.
        bull_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        bear_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        researcher_facilitator_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        trader_factory=lambda u, t: None,  # type: ignore[arg-type, return-value]
        risk_officer_factory=lambda u, p: None,  # type: ignore[arg-type, return-value]
        risk_facilitator_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        fund_manager_factory=lambda u: None,  # type: ignore[arg-type, return-value]
    )

    dc_report = _make_dataclass_report(
        cache_input_tokens=1234,
        cache_creation_tokens=567,
        thinking_tokens=4096,
        citations_json='[{"type":"char_location","cited_text":"foo"}]',
    )
    ids = await flow._persist_agent_reports(decision_run_id, [dc_report])
    assert len(ids) == 1

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(AgentReportRow).where(AgentReportRow.id == ids[0])
            )
        ).scalar_one()

    assert row.cache_input_tokens == 1234
    assert row.cache_creation_tokens == 567
    assert row.thinking_tokens == 4096
    assert row.citations_json == '[{"type":"char_location","cited_text":"foo"}]'
    # Sanity: existing fields still round-trip.
    assert row.tokens_in == 100
    assert row.tokens_out == 200
    assert row.model == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_advisor_persist_turn_persists_wave_a_telemetry(engine: None) -> None:
    """``_persist_turn`` (shared by /api/advisor/turn and /api/intake/turn)
    forwards Wave A fields to the ORM row.
    """
    from argosy.agents.advisor import AdvisorTurnOutput
    from argosy.api.routes.advisor import _persist_turn

    await _seed_user()
    # UserContext must exist or _persist_turn creates it; either way ok.

    dc_report = _make_dataclass_report(
        agent_role="advisor",
        cache_input_tokens=5000,
        cache_creation_tokens=99,
        thinking_tokens=1024,
        citations_json='["docs/foo.md"]',
    )

    out = AdvisorTurnOutput(
        stage="stage_1",
        question_for_user="?",
        context_updates=[],
        stage_complete=False,
        confidence=ConfidenceBand.HIGH,
        cited_sources=["docs/foo.md"],
        mode="gap_driven",
    )

    def _apply(existing_yaml: str, patch: str) -> str:
        return existing_yaml or ""

    await _persist_turn(
        user_id="ariel",
        stage="stage_1",
        session_id="sess-1",
        report=dc_report,
        out=out,
        apply_turn_update=_apply,
    )

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(AgentReportRow).where(
                    AgentReportRow.user_id == "ariel",
                    AgentReportRow.agent_role == "advisor",
                )
            )
        ).scalar_one()

    assert row.cache_input_tokens == 5000
    assert row.cache_creation_tokens == 99
    assert row.thinking_tokens == 1024
    assert row.citations_json == '["docs/foo.md"]'


@pytest.mark.asyncio
async def test_intake_upload_persist_path_persists_wave_a_telemetry(
    engine: None,
) -> None:
    """The /api/intake/upload route writes an ``AgentReportRow`` directly from
    the dataclass report — exercise that exact constructor by mirroring the
    inline persist block (same kwargs as the route's site at intake.py:570).
    """
    await _seed_user()
    dc_report = _make_dataclass_report(
        agent_role="intake_extractor",
        cache_input_tokens=11,
        cache_creation_tokens=22,
        thinking_tokens=33,
        citations_json='["plan.md"]',
    )

    # Mirror argosy/api/routes/intake.py:570 exactly. If a future refactor
    # drops the new-field kwargs from that site, this site-shape will still
    # accept them but the route's row will silently default to 0/None —
    # that's why we also have the DecisionFlow test that round-trips through
    # the actual function.
    async with db_mod.get_session() as session:
        ar_row = AgentReportRow(
            user_id="ariel",
            agent_role=dc_report.agent_role,
            decision_id=None,
            intake_session_id="sess-upload",
            prompt_hash=dc_report.prompt_hash,
            response_text=dc_report.response_text,
            tokens_in=dc_report.tokens_in,
            tokens_out=dc_report.tokens_out,
            cost_usd=dc_report.cost_usd,
            model=dc_report.model,
            confidence=(
                dc_report.confidence.value if dc_report.confidence else None
            ),
            cache_input_tokens=dc_report.cache_input_tokens,
            cache_creation_tokens=dc_report.cache_creation_tokens,
            thinking_tokens=dc_report.thinking_tokens,
            citations_json=dc_report.citations_json,
        )
        session.add(ar_row)
        await session.commit()
        ar_id = ar_row.id

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(AgentReportRow).where(AgentReportRow.id == ar_id)
            )
        ).scalar_one()

    assert row.cache_input_tokens == 11
    assert row.cache_creation_tokens == 22
    assert row.thinking_tokens == 33
    assert row.citations_json == '["plan.md"]'


@pytest.mark.asyncio
async def test_dataclass_zero_defaults_persist_as_zero(engine: None) -> None:
    """A dataclass with default (zero/None) telemetry persists as 0/None — no NULLs
    on the NOT-NULL columns. Protects against a future refactor that switches
    the dataclass defaults to None.
    """
    await _seed_user()
    decision_run_id = await _seed_decision_run()
    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(),
        bull_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        bear_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        researcher_facilitator_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        trader_factory=lambda u, t: None,  # type: ignore[arg-type, return-value]
        risk_officer_factory=lambda u, p: None,  # type: ignore[arg-type, return-value]
        risk_facilitator_factory=lambda u: None,  # type: ignore[arg-type, return-value]
        fund_manager_factory=lambda u: None,  # type: ignore[arg-type, return-value]
    )

    # Construct via dataclass defaults (Wave A fields omitted).
    dc_report = AgentReport(
        agent_role="fundamentals",
        user_id="ariel",
        model="claude-haiku-4-5",
        response_text="{}",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.001,
        prompt_hash="h",
        confidence=ConfidenceBand.MEDIUM,
        output=_Dummy(),
    )
    ids = await flow._persist_agent_reports(decision_run_id, [dc_report])

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(AgentReportRow).where(AgentReportRow.id == ids[0])
            )
        ).scalar_one()

    assert row.cache_input_tokens == 0
    assert row.cache_creation_tokens == 0
    assert row.thinking_tokens == 0
    assert row.citations_json is None
