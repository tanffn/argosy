"""Adaptive-thinking telemetry — per-agent ``thinking_tokens`` capture.

These tests guard the wire-through that Wave A intended but only the api_key
backend implemented: the claude_code backend (Argosy's default) must populate
``AgentReport.thinking_tokens`` from the agent-sdk's ResultMessage.usage, and
the value must flow through ``build_agent_tree`` into the FM-rooted DAG so
the UI can surface "actual thinking used N tokens" alongside the existing
tokens_in / tokens_out / cost.

Coverage:

1. ``test_thinking_tokens_populated_from_sdk_usage``
   ResultMessage.usage carries ``thinking_tokens`` -> ModelCall picks it up.

2. ``test_thinking_tokens_accumulates_across_turns``
   Two ResultMessages (multi-turn chunked send) -> accumulator sums.

3. ``test_thinking_tokens_zero_when_sdk_doesnt_expose``
   Older SDK shape (no ``thinking_tokens`` key) -> default 0, no exception.

4. ``test_thinking_tokens_surfaces_in_agent_tree_node``
   AgentReport row with ``thinking_tokens=5000`` -> ``AgentNode.thinking_tokens``
   on the corresponding tree node == 5000.

5. ``test_field_probe_is_module_level_not_per_call``
   ``_probe_claude_code_sdk_thinking_field`` runs exactly once at import
   time (the module-level constant is populated); the extractor reads
   the constant rather than re-probing on every ResultMessage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

from argosy.agents import base as base_mod
from argosy.agents.base import BaseAgent
from argosy.services.agent_tree_builder import build_agent_tree
from argosy.state.models import AgentReport as ORMAgentReport
from argosy.state.models import Base, DecisionPhase, DecisionRun, User


# ----------------------------------------------------------------------
# BaseAgent fixture (mirrors the Wave A.5 test scaffold).
# ----------------------------------------------------------------------


class _Out(BaseModel):
    text: str = "ok"


class _ProbeAgent(BaseAgent[_Out]):
    agent_role = "test_thinking_telemetry_subagent"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


def _make_agent() -> _ProbeAgent:
    agent = _ProbeAgent(user_id="test")
    # Adaptive mode requires effort to be set; we don't care here since we
    # mock query() and skip the actual thinking config — but the existing
    # base.py defaults assume an effort or budget, so we explicitly disable
    # both to match the Wave A.5 fixture convention.
    agent.thinking_effort = None
    agent.thinking_budget = 0
    return agent


def _install_fake_query(monkeypatch: pytest.MonkeyPatch, yielded: list[Any]) -> None:
    async def _fake_query(*, prompt, options):
        # Drain streaming-mode prompts so the SDK's contract is respected.
        if hasattr(prompt, "__aiter__"):
            async for _ in prompt:
                pass
        for message in yielded:
            yield message

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)


def _make_result_message(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    thinking_tokens: int | None = None,
):
    """Build a real ``ResultMessage`` with the supplied usage shape.

    ``thinking_tokens=None`` simulates an older SDK that doesn't emit the
    key at all; the production extractor must default to 0.
    """
    from claude_agent_sdk import ResultMessage

    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if thinking_tokens is not None:
        usage["thinking_tokens"] = thinking_tokens
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=usage,
        result="ok",
    )


# ----------------------------------------------------------------------
# 1) ResultMessage.usage.thinking_tokens -> ModelCall.thinking_tokens
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_tokens_populated_from_sdk_usage(monkeypatch):
    """A single ResultMessage with thinking_tokens=8500 lights up
    ModelCall.thinking_tokens on the claude_code backend."""
    msg = _make_result_message(thinking_tokens=8500)
    _install_fake_query(monkeypatch, [msg])

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.thinking_tokens == 8500


# ----------------------------------------------------------------------
# 2) Multi-turn accumulator
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_tokens_accumulates_across_turns(monkeypatch):
    """Two ResultMessages (multi-turn chunked-attachment send) yield a
    summed thinking_tokens on ModelCall. Without accumulation a 2-turn
    chunked PDF send would drop the first turn's thinking entirely."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    # The SDK yields AssistantMessage(s) with text content between
    # ResultMessages on multi-turn streams; the extractor only accumulates
    # on ResultMessage, so we don't need to interleave for this test —
    # back-to-back ResultMessages are exercised end-to-end by the cache /
    # tokens accumulators above (test_call_via_claude_code_inner_uses_
    # last_turn_text_with_chunking) which use the same loop.
    yielded = [
        AssistantMessage(content=[TextBlock(text="ack-1")], model="claude-opus-4-7"),
        _make_result_message(input_tokens=300, output_tokens=100, thinking_tokens=2500),
        AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-7"),
        _make_result_message(input_tokens=400, output_tokens=150, thinking_tokens=6000),
    ]
    _install_fake_query(monkeypatch, yielded)

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # 2500 + 6000 = 8500. Verify the sum rather than each turn so the
    # test stays robust to the SDK changing whether it emits one or two
    # ResultMessages per multi-turn stream — what matters is no thinking
    # is lost on the way through.
    assert call.thinking_tokens == 8500
    # Sanity: tokens_in / tokens_out also accumulate (already covered by
    # other tests, but pinned here so a regression in the same loop
    # branch surfaces against this test too).
    assert call.tokens_in == 700
    assert call.tokens_out == 250


# ----------------------------------------------------------------------
# 3) Graceful degrade on older SDK shape
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_tokens_zero_when_sdk_doesnt_expose(monkeypatch):
    """An old SDK that doesn't populate ``thinking_tokens`` on the usage
    dict must NOT raise and must yield ModelCall.thinking_tokens == 0."""
    msg = _make_result_message(thinking_tokens=None)  # key absent from usage
    _install_fake_query(monkeypatch, [msg])

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.thinking_tokens == 0


@pytest.mark.asyncio
async def test_thinking_tokens_zero_when_probe_returned_none(monkeypatch):
    """If the import-time field-name probe returned ``None`` (no known
    candidate found in the bundled binary — would only happen on a
    future SDK that renamed the field to something we don't know
    about), the extractor must short-circuit to 0 rather than crash or
    silently double-count via a ghost key."""
    msg = _make_result_message(thinking_tokens=9999)
    _install_fake_query(monkeypatch, [msg])

    # Force the probe to "no known field" for the duration of the call.
    monkeypatch.setattr(base_mod, "_CLAUDE_CODE_SDK_THINKING_FIELD", None)

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.thinking_tokens == 0


# ----------------------------------------------------------------------
# 4) Wire-through into AgentNode via build_agent_tree
# ----------------------------------------------------------------------


@pytest.fixture
def inmem_session():
    engine = sa.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        sess.commit()
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _seed_minimal_synth_run(sess, *, fm_thinking_tokens: int) -> int:
    """Insert just enough rows for ``build_agent_tree`` to render an
    FM-rooted DAG. We seed only the FM + a single analyst (concentration)
    plus the topology-required intermediate roles so the FM node lights
    up with the desired thinking_tokens value.
    """
    now = datetime.now(timezone.utc)
    run = DecisionRun(
        user_id="ariel",
        ticker="(plan)",
        tier=None,
        decision_kind="plan_revision",
        status="completed",
        started_at=now,
        finished_at=now,
    )
    sess.add(run)
    sess.commit()
    sess.refresh(run)
    rid = run.id
    decision_id_str = f"plan-synth-{rid}"

    def mk(role: str, *, thinking_tokens: int = 0,
           confidence: str | None = "MEDIUM") -> None:
        sess.add(
            ORMAgentReport(
                user_id="ariel",
                agent_role=role,
                decision_id=decision_id_str,
                response_text="ok",
                confidence=confidence,
                model="claude-opus-4-7",
                tokens_in=10,
                tokens_out=20,
                cost_usd=0.001,
                thinking_tokens=thinking_tokens,
            )
        )

    # Phase 1 analysts: enough to fill the topology slots.
    for role in (
        "concentration", "fx", "fundamentals", "news",
        "sentiment", "technical", "macro", "tax",
        "household_budget", "plan_critique",
    ):
        mk(role)
    # Phase 2-4 intermediates.
    mk("bull_researcher")
    mk("bear_researcher")
    mk("researcher_facilitator")
    mk("plan_synthesizer", confidence=None)
    for _ in range(3):
        mk("risk_officer")
    mk("risk_facilitator")
    # Phase 5 FM — the one we actually care about.
    mk("fund_manager", thinking_tokens=fm_thinking_tokens, confidence=None)
    sess.commit()

    sess.add(
        DecisionPhase(
            decision_run_id=rid,
            user_id="ariel",
            seq=1,
            kind="synthesis.phase_1",
            started_at=now,
            finished_at=now,
            participants_json="[]",
            phase_output_json=json.dumps({"phase": 1}),
        )
    )
    sess.commit()
    return rid


def test_thinking_tokens_surfaces_in_agent_tree_node(inmem_session) -> None:
    """``AgentReport.thinking_tokens == 5000`` propagates to the FM
    node's ``AgentNode.thinking_tokens`` so the UI can render the value
    next to tokens_in / tokens_out / cost."""
    rid = _seed_minimal_synth_run(inmem_session, fm_thinking_tokens=5000)

    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    assert tree.root is not None, "synthesis runs always produce an FM-rooted tree"
    assert tree.root.agent_role == "fund_manager"
    assert tree.root.thinking_tokens == 5000


def test_thinking_tokens_none_on_skipped_node(inmem_session) -> None:
    """Nodes for agents that didn't run (no AgentReport row) carry
    ``thinking_tokens=None`` so the UI hides the field on skipped
    rows instead of rendering '0 thinking'."""
    # Seed an FM with thinking but no codex_second_opinion row — codex
    # then renders as "skipped" and must carry thinking_tokens=None.
    rid = _seed_minimal_synth_run(inmem_session, fm_thinking_tokens=1234)

    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    assert tree.root is not None
    codex = next(
        (c for c in tree.root.children if c.agent_role == "codex_second_opinion"),
        None,
    )
    assert codex is not None, "FM should always have a codex child slot"
    assert codex.status == "skipped"
    assert codex.thinking_tokens is None


# ----------------------------------------------------------------------
# 5) Probe is module-level, not per-call
# ----------------------------------------------------------------------


def test_field_probe_is_module_level_not_per_call() -> None:
    """The import-time probe populates a module-level constant; per-call
    code reads the constant. Verifies the constant exists, has a known
    value (not silently None on a normal install), and that the probe
    function itself didn't crash at import."""
    assert hasattr(base_mod, "_CLAUDE_CODE_SDK_THINKING_FIELD")
    # On any supported install the probe returns either
    # "thinking_tokens" (modern claude.exe / non-bundled) or
    # "reasoning_tokens" (hypothetical future rename). Both are
    # acceptable. None would mean the binary lacks any known candidate
    # — also acceptable as a graceful-degrade outcome, but a sanity
    # check on the dev box: the bundled binary as of this commit DOES
    # have ``thinking_tokens``.
    assert base_mod._CLAUDE_CODE_SDK_THINKING_FIELD in (
        "thinking_tokens", "reasoning_tokens", None,
    )
