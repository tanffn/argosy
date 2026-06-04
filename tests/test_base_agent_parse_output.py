"""Regression tests for BaseAgent._parse_output.

Covers the defensive fallback added 2026-06-01 after synth #58 hit
a truncation failure where the model emitted only a markdown fence
opener and ran out of output tokens. The fallback scans for the
first ``{`` or ``[`` in the (cleaned or original) text and tries
``raw_decode`` again from that offset, recovering from preamble /
prose / partial-fence outputs while still raising on truly
unrecoverable inputs.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from argosy.agents.base import BaseAgent


class _Out(BaseModel):
    name: str
    count: int = 0


class _ParseTestAgent(BaseAgent):
    agent_role = "trader"  # any role in the role table; not load-bearing
    output_model = _Out
    require_citations = False

    def build_prompt(self, **_):  # pragma: no cover - unused
        return ("", "")


def _agent() -> _ParseTestAgent:
    return _ParseTestAgent(user_id="ariel")


def test_parse_output_happy_path_strict_json():
    a = _agent()
    out = a._parse_output('{"name": "alpha", "count": 3}')
    assert out.name == "alpha"
    assert out.count == 3


def test_parse_output_strips_markdown_fence():
    a = _agent()
    out = a._parse_output('```json\n{"name": "x", "count": 1}\n```')
    assert out.name == "x"


def test_parse_output_recovers_from_prose_preamble():
    """Codex audit case: model emits 'Sure, here it is: {...}'."""
    a = _agent()
    out = a._parse_output(
        'Sure, here is the plan: {"name": "y", "count": 5} done.'
    )
    assert out.name == "y"
    assert out.count == 5


def test_parse_output_recovers_from_partial_fence_when_body_present_after():
    """Inner content has a junk-prefix line before the JSON. After
    fence-strip the cleaned text starts with prose; the
    scan-from-first-`{` fallback recovers."""
    a = _agent()
    out = a._parse_output(
        '```json\n'
        'leading garbage line\n'
        '{"name": "z", "count": 7}\n'
        '```'
    )
    assert out.name == "z"


def test_parse_output_object_after_array_preamble():
    """If the prose preamble has a literal `[` followed by `{...}`,
    the scan tries `{` first (in the loop) and finds the object."""
    class _W(BaseModel):
        items: list[int]

    class _WAgent(BaseAgent):
        agent_role = "trader"
        output_model = _W
        require_citations = False

        def build_prompt(self, **_):
            return ("", "")

    a = _WAgent(user_id="ariel")
    out = a._parse_output('preamble\n{"items": [1,2,3]} trailing')
    assert out.items == [1, 2, 3]


def test_parse_output_raises_on_truly_unparseable():
    """No `{` or `[` anywhere — original error must surface."""
    a = _agent()
    with pytest.raises(json.JSONDecodeError):
        a._parse_output("just plain prose, no JSON at all")


def test_parse_output_empty_string_raises():
    """Fully empty output is still a hard error — the wrapper's
    empty_output_retry path handles this BEFORE _parse_output fires;
    if we reach here the agent run dies and surfaces the truncation."""
    a = _agent()
    with pytest.raises(json.JSONDecodeError):
        a._parse_output("")


def test_parse_output_fence_only_with_no_body_raises():
    """Synth #58's exact failure mode: model emitted only the fence
    opener and ran out. After fence-strip there's nothing to parse
    AND no `{` to scan to. Surfaces as JSONDecodeError — the
    background_failed log entry in plan_synthesis is the audit trail.
    """
    a = _agent()
    with pytest.raises(json.JSONDecodeError):
        a._parse_output("```json\n```")


# ---------------------------------------------------------------------------
# Schema-validation retry (opt-in via schema_retry_attempts).
# ---------------------------------------------------------------------------
from types import SimpleNamespace


def _fake_call(text: str):
    return SimpleNamespace(
        text=text, tokens_in=1, tokens_out=1, cache_input_tokens=0,
        cache_creation_tokens=0, thinking_tokens=0, model="stub",
        citations_json=None,
    )


class _RetryAgent(BaseAgent):
    agent_role = "trader"
    output_model = _Out
    require_citations = False
    schema_retry_attempts = 2

    def build_prompt(self, **_):
        return ("sys", "usr")


class _NoRetryAgent(_RetryAgent):
    schema_retry_attempts = 0


async def test_schema_retry_self_heals_then_succeeds(monkeypatch):
    """A critical agent (schema_retry_attempts>0) re-calls the model with the
    validation error fed back, and succeeds when a later attempt is valid."""
    a = _RetryAgent(user_id="ariel")
    calls = {"n": 0, "users": []}

    async def _mock(**kw):
        calls["n"] += 1
        calls["users"].append(kw.get("user", ""))
        # 1st attempt: invalid (count is a string); 2nd: valid.
        return _fake_call('{"name":"x","count":"NaN"}') if calls["n"] == 1 \
            else _fake_call('{"name":"x","count":2}')

    monkeypatch.setattr(a, "_call_model", _mock)
    report = await a.run()
    assert report.output.count == 2
    assert calls["n"] == 2  # retried once
    assert "validation_feedback" in calls["users"][1]  # error fed back


async def test_no_retry_by_default_raises_on_first(monkeypatch):
    """Default schema_retry_attempts==0 → one attempt, raises as before."""
    from argosy.agents.errors import AgentRunError
    a = _NoRetryAgent(user_id="ariel")
    calls = {"n": 0}

    async def _mock(**kw):
        calls["n"] += 1
        return _fake_call('{"name":"x","count":"NaN"}')

    monkeypatch.setattr(a, "_call_model", _mock)
    with pytest.raises(AgentRunError):
        await a.run()
    assert calls["n"] == 1  # no retry
