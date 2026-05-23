"""Wave A.5 — claude_code backend telemetry + sources backport.

Verifies that `_call_via_claude_code_inner` (the agent-sdk path used by
Argosy's default backend) now mirrors the Wave A treatment the api_key
backend gained:

1. Extended-thinking config is forwarded to ``ClaudeAgentOptions`` when
   the agent's ``thinking_budget`` is positive.
2. Cache + thinking telemetry on ``ResultMessage.usage`` is extracted
   into ``ModelCall.cache_input_tokens`` / ``cache_creation_tokens`` /
   ``thinking_tokens``.
3. ``sources`` are inlined into the user prompt as an ``<sources>`` XML
   block (claude_code has no equivalent of Anthropic's document blocks,
   so without this the Wave A 11-agent refactor would have left the
   model with source IDs but no source content — a regression).
"""

from __future__ import annotations

from typing import Any

import pytest

from argosy.agents.base import BaseAgent, ModelCall
from pydantic import BaseModel


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _Out(BaseModel):
    text: str = "ok"


class _Subagent(BaseAgent[_Out]):
    """Concrete BaseAgent for direct `_call_via_claude_code_inner` exercise."""

    agent_role = "test_subagent_wave_a5"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


def _make_agent(*, thinking_budget: int = 0) -> _Subagent:
    agent = _Subagent(user_id="test")
    # `agent_role` is unknown to the role defaults table, so the budget
    # came in as 0. Inject manually so individual tests can control it.
    agent.thinking_budget = thinking_budget
    return agent


def _install_fake_query(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yielded: list[Any] | None = None,
    captured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch `claude_agent_sdk.query` to a fake that records call args and
    yields the provided messages. Returns the `captured` dict for asserts.
    """
    captured = captured if captured is not None else {}
    yielded = yielded if yielded is not None else []

    async def _fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        # Drain streaming-mode AsyncIterable prompts so callers can inspect
        # the yielded message-shape dict.
        if hasattr(prompt, "__aiter__"):
            captured["mode"] = "streaming"
            captured["yields"] = []
            async for item in prompt:
                captured["yields"].append(item)
        else:
            captured["mode"] = "string"
            captured["string_prompt"] = prompt
        for message in yielded:
            yield message

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)
    return captured


def _make_result_message(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    thinking_tokens: int = 0,
    total_cost_usd: float = 0.0,
):
    """Construct a real `ResultMessage` with the given usage shape.

    Using the actual dataclass (not a stub) ensures `isinstance` checks in
    the production code accept our fixture.
    """
    from claude_agent_sdk import ResultMessage

    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "thinking_tokens": thinking_tokens,
    }
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        total_cost_usd=total_cost_usd,
        usage=usage,
        result="ok",
    )


# ----------------------------------------------------------------------
# Change 1 — thinking config forwarded to agent-sdk
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_config_forwarded_to_agent_sdk(monkeypatch):
    """Agent with thinking_budget>0 sets `thinking` on ClaudeAgentOptions."""
    captured = _install_fake_query(monkeypatch)

    agent = _make_agent(thinking_budget=4000)
    await agent._call_via_claude_code_inner(system="sys", user="hi")

    opts = captured["options"]
    # Both fields should be populated when budget>0. The SDK accepts the
    # same shape Anthropic's REST API uses for the `thinking` field.
    assert opts.thinking == {"type": "enabled", "budget_tokens": 4000}
    assert opts.max_thinking_tokens == 4000


@pytest.mark.asyncio
async def test_thinking_config_absent_when_budget_zero(monkeypatch):
    """No thinking config on the options when budget is 0 (most agents)."""
    captured = _install_fake_query(monkeypatch)

    agent = _make_agent(thinking_budget=0)
    await agent._call_via_claude_code_inner(system="sys", user="hi")

    opts = captured["options"]
    assert opts.thinking is None
    assert opts.max_thinking_tokens is None


# ----------------------------------------------------------------------
# Change 2 — cache + thinking telemetry from ResultMessage
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_telemetry_extracted_from_result_message(monkeypatch):
    """ResultMessage.usage cache_* fields populate ModelCall.cache_*."""
    msg = _make_result_message(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=700,
        cache_creation_input_tokens=300,
    )
    _install_fake_query(monkeypatch, yielded=[msg])

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.cache_input_tokens == 700
    assert call.cache_creation_tokens == 300
    assert call.tokens_in == 1000
    assert call.tokens_out == 200


@pytest.mark.asyncio
async def test_thinking_tokens_extracted_from_result_message(monkeypatch):
    """ResultMessage.usage.thinking_tokens populates ModelCall.thinking_tokens."""
    msg = _make_result_message(thinking_tokens=500)
    _install_fake_query(monkeypatch, yielded=[msg])

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.thinking_tokens == 500


@pytest.mark.asyncio
async def test_cache_telemetry_defaults_to_zero_when_missing(monkeypatch):
    """Missing cache_* / thinking_tokens keys must not crash; default to 0."""
    from claude_agent_sdk import ResultMessage

    msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage={"input_tokens": 10, "output_tokens": 5},
        result="ok",
    )
    _install_fake_query(monkeypatch, yielded=[msg])

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert call.cache_input_tokens == 0
    assert call.cache_creation_tokens == 0
    assert call.thinking_tokens == 0


# ----------------------------------------------------------------------
# Change 3 — sources inlined into user prompt (regression fix)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_inlined_into_user_prompt_on_claude_code(monkeypatch):
    """sources tuples become <source id="..."> blocks in the user prompt."""
    captured = _install_fake_query(monkeypatch)

    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys",
        user="What does the news say?",
        sources=[
            ("news/NVDA", "Headline: NVDA up 3% on AI demand"),
            ("news/AAPL", "Headline: AAPL ships new chip"),
        ],
    )

    # No images: SDK prompt is the plain string.
    prompt = captured["string_prompt"]
    assert "<sources>" in prompt
    assert '<source id="news/NVDA">' in prompt
    assert "Headline: NVDA up 3% on AI demand" in prompt
    assert '<source id="news/AAPL">' in prompt
    assert "Headline: AAPL ships new chip" in prompt
    assert "</sources>" in prompt
    # Original user prompt body must still be present after the sources block.
    assert "What does the news say?" in prompt
    # And the sources block must come before the original prompt body.
    assert prompt.index("</sources>") < prompt.index("What does the news say?")


@pytest.mark.asyncio
async def test_no_sources_no_wrapper(monkeypatch):
    """Empty/None sources produce a prompt with no <sources> markup."""
    captured = _install_fake_query(monkeypatch)

    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys", user="plain question", sources=None,
    )

    prompt = captured["string_prompt"]
    assert prompt == "plain question"
    assert "<sources>" not in prompt


@pytest.mark.asyncio
async def test_empty_sources_list_no_wrapper(monkeypatch):
    """Explicit empty list is treated the same as None."""
    captured = _install_fake_query(monkeypatch)

    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys", user="plain question", sources=[],
    )

    prompt = captured["string_prompt"]
    assert prompt == "plain question"
    assert "<sources>" not in prompt


@pytest.mark.asyncio
async def test_sources_inlined_with_image_attachments(tmp_path, monkeypatch):
    """When images + sources both present, sources go into the text block."""
    captured = _install_fake_query(monkeypatch)

    # Minimal valid PNG so base64 encode succeeds.
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    ))

    class _Att:
        path = str(img_path)
        mime_type = "image/png"

    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys",
        user="describe",
        image_attachments=[_Att()],
        sources=[("doc/1", "source body")],
    )

    # Streaming-mode prompt: pull text block from the yielded message dict.
    assert captured["mode"] == "streaming"
    msg = captured["yields"][0]
    content = msg["message"]["content"]
    text_blocks = [b for b in content if b["type"] == "text"]
    assert len(text_blocks) == 1
    text = text_blocks[0]["text"]
    assert '<source id="doc/1">' in text
    assert "source body" in text
    assert "describe" in text
