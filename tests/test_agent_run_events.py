"""Test that BaseAgent.run() emits agent.run.started and agent.run.finished WebSocket events."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from argosy.agents.base import BaseAgent, ConfidenceBand, ModelCall


class _Out(BaseModel):
    confidence: ConfidenceBand


class _DummyAgent(BaseAgent):
    agent_role = "news"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **_):
        return ("system", "user")

    async def _call_model(self, **_):
        return ModelCall(
            text=json.dumps({"confidence": "HIGH"}),
            tokens_in=100,
            tokens_out=50,
            model="claude-sonnet-4-6",
            cache_input_tokens=20,
            cache_creation_tokens=0,
            thinking_tokens=0,
            citations_json=None,
        )


def test_run_emits_started_and_finished_events():
    # W1.C — BaseAgent.run now persists when decision_id is set. The test
    # passes decision_id="dec-1" so it triggers the persistence path; that
    # in turn requires an initialised DB engine + schema + a User row.
    from argosy.state import db as db_mod
    from argosy.state.models import Base, User

    async def _setup():
        db_mod.init_engine("sqlite+aiosqlite:///:memory:")
        engine = db_mod.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_mod.get_session() as session:
            session.add(User(id="ariel", plan="free"))
            await session.commit()

    async def _run():
        await _setup()
        with patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
            agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
            await agent.run(decision_id="dec-1", turn_id="turn-xyz")
        return mock_pub

    try:
        mock_pub = asyncio.run(_run())
    finally:
        asyncio.run(db_mod.dispose_engine())

    assert mock_pub.call_count == 2, (
        f"Expected 2 publish_event_threadsafe calls, got {mock_pub.call_count}"
    )

    started_call, finished_call = mock_pub.call_args_list
    assert started_call.args[0] == "agent.run.started"
    assert finished_call.args[0] == "agent.run.finished"

    started_payload = started_call.args[1]
    finished_payload = finished_call.args[1]

    # Both events share the same run_correlation_id
    assert started_payload["run_correlation_id"] == finished_payload["run_correlation_id"]

    # Started payload fields
    assert started_payload["turn_id"] == "turn-xyz"
    assert started_payload["decision_id"] == "dec-1"
    assert started_payload["user_id"] == "ariel"
    assert started_payload["agent_role"] == "news"
    assert "model" in started_payload
    assert "started_at" in started_payload
    assert "run_correlation_id" in started_payload

    # Required telemetry keys in finished payload
    for key in (
        "tokens_in",
        "tokens_out",
        "cache_input_tokens",
        "cache_creation_tokens",
        "thinking_tokens",
        "citations_count",
        "cost_usd",
        "confidence",
        "agent_report_id",
    ):
        assert key in finished_payload, f"{key!r} missing from finished payload"

    assert finished_payload["confidence"] == "HIGH"
    assert finished_payload["citations_count"] == 0  # no citations_json
    # W1.C — persistence path was hit (decision_id="dec-1"), so the WS
    # finished payload must carry the persisted row's int primary key.
    assert isinstance(finished_payload["agent_report_id"], int), (
        f"expected int agent_report_id, got "
        f"{finished_payload['agent_report_id']!r}"
    )
    assert finished_payload["tokens_in"] == 100
    assert finished_payload["tokens_out"] == 50
    assert finished_payload["cache_input_tokens"] == 20
    assert finished_payload["cache_creation_tokens"] == 0
    assert finished_payload["thinking_tokens"] == 0
    assert finished_payload["user_id"] == "ariel"
    assert finished_payload["agent_role"] == "news"
    assert "finished_at" in finished_payload
    assert finished_payload["turn_id"] == "turn-xyz"
    assert finished_payload["status"] == "done"

    # decision_id must flow through to the finished payload so the UI cascade
    # panel can filter both started and finished events by it (plan-tab
    # synthesis button feature).
    assert finished_payload["decision_id"] == "dec-1"
    assert finished_payload["intake_session_id"] is None  # not passed in this test


def test_agent_report_carries_run_correlation_id():
    """The returned AgentReport dataclass has the same run_correlation_id as
    the emitted WS events (Wave B-UI follow-up Item 2 — migration 0028).
    """
    # W1.C — decision_id is set, so BaseAgent.run will attempt to persist.
    # Initialise an isolated in-memory DB so the persistence call doesn't
    # silently fall back to the dev DB.
    from argosy.state import db as db_mod
    from argosy.state.models import Base, User

    async def _setup():
        db_mod.init_engine("sqlite+aiosqlite:///:memory:")
        async with db_mod.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_mod.get_session() as session:
            session.add(User(id="ariel", plan="free"))
            await session.commit()

    async def _run():
        await _setup()
        with patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
            agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
            report = await agent.run(decision_id="dec-corr", turn_id="turn-corr")
        return mock_pub, report

    try:
        mock_pub, report = asyncio.run(_run())
    finally:
        asyncio.run(db_mod.dispose_engine())

    # Two events published.
    assert mock_pub.call_count == 2
    started_payload = mock_pub.call_args_list[0].args[1]
    finished_payload = mock_pub.call_args_list[1].args[1]

    ws_correlation_id = started_payload["run_correlation_id"]
    assert ws_correlation_id == finished_payload["run_correlation_id"]

    # The returned AgentReport dataclass must carry the identical id.
    assert report.run_correlation_id is not None
    assert report.run_correlation_id == ws_correlation_id


def test_agent_report_carries_system_and_user_prompt():
    """The returned AgentReport dataclass carries non-empty system_prompt and
    user_prompt — these are the full strings built in run() and passed into
    the AgentReport constructor (Wave B-UI follow-up Item B — migration 0029).
    """
    # W1.C — decision_id is set, so BaseAgent.run will attempt to persist.
    # Set up an isolated DB to avoid touching the dev DB.
    from argosy.state import db as db_mod
    from argosy.state.models import Base, User

    async def _setup():
        db_mod.init_engine("sqlite+aiosqlite:///:memory:")
        async with db_mod.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_mod.get_session() as session:
            session.add(User(id="ariel", plan="free"))
            await session.commit()

    async def _run():
        await _setup()
        with patch("argosy.api.events.publish_event_threadsafe"):
            agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
            return await agent.run(decision_id="dec-prompt", turn_id="turn-prompt")

    try:
        report = asyncio.run(_run())
    finally:
        asyncio.run(db_mod.dispose_engine())

    # system_prompt = BOILERPLATE_SYSTEM + "\n\n" + "system" (from build_prompt)
    # user_prompt = "user" (from build_prompt)
    assert report.system_prompt is not None, "system_prompt must not be None"
    assert len(report.system_prompt) > 0, "system_prompt must be non-empty"
    # The dummy build_prompt returns ("system", "user") — system_prompt is
    # BOILERPLATE_SYSTEM + "\n\n" + "system" so it must contain the role label.
    assert "system" in report.system_prompt

    assert report.user_prompt is not None, "user_prompt must not be None"
    assert report.user_prompt == "user", (
        f"user_prompt must be 'user' (the value returned by build_prompt), "
        f"got {report.user_prompt!r}"
    )


class _BrokenAgent(BaseAgent):
    """Agent whose _call_model always raises to test the failure terminal event."""

    agent_role = "broken"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **_):
        return ("system", "user")

    async def _call_model(self, **_):
        raise RuntimeError("simulated model failure")


def test_run_emits_finished_with_failed_status_on_exception():
    """BaseAgent.run() must emit agent.run.finished with status='failed' when _call_model raises.

    The UI's useDecisionStream / AgentRunCard rely on a terminal finished event
    to finalize a row; without it a crashed agent leaves a row stuck in 'running'
    forever.
    """
    with patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
        agent = _BrokenAgent(user_id="ariel", model="claude-sonnet-4-6")
        try:
            asyncio.run(agent.run(decision_id="dec-fail", turn_id="turn-fail"))
        except RuntimeError:
            pass  # expected — the exception is re-raised after the finished event

    # Should have emitted started + finished (2 calls total).
    assert mock_pub.call_count == 2, (
        f"Expected 2 publish_event_threadsafe calls (started + failed finished), "
        f"got {mock_pub.call_count}"
    )

    started_call, finished_call = mock_pub.call_args_list
    assert started_call.args[0] == "agent.run.started"
    assert finished_call.args[0] == "agent.run.finished"

    started_payload = started_call.args[1]
    finished_payload = finished_call.args[1]

    # Correlation ID must match so the UI can close the right row.
    assert started_payload["run_correlation_id"] == finished_payload["run_correlation_id"]

    assert finished_payload["status"] == "failed"
    assert "error" in finished_payload
    assert "simulated model failure" in finished_payload["error"]
    assert finished_payload["tokens_in"] == 0
    assert finished_payload["tokens_out"] == 0
    assert finished_payload["cost_usd"] == 0.0
    assert finished_payload["confidence"] is None
    assert finished_payload["agent_report_id"] is None
    assert finished_payload["turn_id"] == "turn-fail"
    assert "finished_at" in finished_payload

    # decision_id must flow through on the failure path too, so a crashed
    # agent still shows up in the decision_id-filtered cascade view
    # (plan-tab synthesis button feature).
    assert finished_payload["decision_id"] == "dec-fail"
    assert finished_payload["intake_session_id"] is None


# ---------------------------------------------------------------------------
# W1.C — synthesis-flow forensic trail.
#
# Before W1.C the 9 phase-1 analysts (and downstream debate/risk/FM agents)
# of the synthesis flow returned an AgentReport dataclass but never wrote a
# row to agent_reports. These tests pin the new behaviour:
#   - decision_id set      → row is written, persisted id flows through WS
#   - decision_id is None  → no row (advisor/intake's own _persist_turn writes)
# ---------------------------------------------------------------------------


def test_run_persists_agent_report_when_decision_id_set():
    """When BaseAgent.run is called with decision_id, the AgentReport
    dataclass is mirrored to an agent_reports DB row."""
    import asyncio
    from unittest.mock import patch
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow, Base, User
    from sqlalchemy import select

    async def _setup():
        db_mod.init_engine("sqlite+aiosqlite:///:memory:")
        engine = db_mod.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_mod.get_session() as session:
            session.add(User(id="ariel", plan="free"))
            await session.commit()

    async def _run():
        await _setup()
        with patch("argosy.api.events.publish_event_threadsafe"):
            agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
            report = await agent.run(decision_id="plan-synth-42", turn_id=None)
        async with db_mod.get_session() as session:
            rows = (await session.execute(select(AgentReportRow))).scalars().all()
        return report, rows

    try:
        report, rows = asyncio.run(_run())
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
        assert rows[0].decision_id == "plan-synth-42"
        assert rows[0].agent_role == "news"
        assert rows[0].user_id == "ariel"
        assert rows[0].run_correlation_id == report.run_correlation_id
        assert rows[0].response_text == report.response_text
    finally:
        asyncio.run(db_mod.dispose_engine())


def test_run_does_not_persist_when_decision_id_is_none():
    """When decision_id is None (advisor/intake path), BaseAgent.run does
    NOT write an agent_reports row — the caller's own _persist_turn handles
    it. Prevents double-write regression."""
    import asyncio
    from unittest.mock import patch
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow, Base, User
    from sqlalchemy import select

    async def _setup():
        db_mod.init_engine("sqlite+aiosqlite:///:memory:")
        async with db_mod.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db_mod.get_session() as session:
            session.add(User(id="ariel", plan="free"))
            await session.commit()

    async def _run():
        await _setup()
        with patch("argosy.api.events.publish_event_threadsafe"):
            agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
            await agent.run(decision_id=None, turn_id="some-turn-id")
        async with db_mod.get_session() as session:
            rows = (await session.execute(select(AgentReportRow))).scalars().all()
        return rows

    try:
        rows = asyncio.run(_run())
        assert len(rows) == 0, f"expected 0 rows (no decision_id), got {len(rows)}"
    finally:
        asyncio.run(db_mod.dispose_engine())
