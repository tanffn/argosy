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
    with patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
        agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
        asyncio.run(agent.run(decision_id="dec-1", turn_id="turn-xyz"))

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
    assert finished_payload["agent_report_id"] is None
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
    with patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
        agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
        report = asyncio.run(agent.run(decision_id="dec-corr", turn_id="turn-corr"))

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
    with patch("argosy.api.events.publish_event_threadsafe"):
        agent = _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")
        report = asyncio.run(agent.run(decision_id="dec-prompt", turn_id="turn-prompt"))

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
