"""_call_via_api_key passes thinking param when budget > 0 and extracts thinking_tokens."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _Trader(BaseAgent):
    agent_role = "trader"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def _make_mock_msg(input_toks=100, output_toks=50, thinking_toks=0):
    msg = MagicMock()
    blocks = []
    if thinking_toks:
        thinking_block = MagicMock(spec=["type", "thinking"])
        thinking_block.type = "thinking"
        thinking_block.thinking = "thinking text"
        # Real ThinkingBlock has no `.text`; getattr(..., None) returns None.
        # spec=[...] above ensures MagicMock raises AttributeError for unspecced
        # attrs so getattr defaults kick in correctly.
        blocks.append(thinking_block)
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    blocks.append(text_block)
    msg.content = blocks
    msg.usage.input_tokens = input_toks
    msg.usage.output_tokens = output_toks
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    # Default thinking_tokens to thinking_toks (caller can override). Without
    # explicit set, MagicMock auto-attrs cast to int as 1.
    msg.usage.thinking_tokens = thinking_toks
    # Anthropic puts thinking tokens in a separate counter:
    msg.usage.cache_creation = MagicMock()
    msg.model = "claude-opus-4-7"
    return msg


@pytest.mark.asyncio
async def test_thinking_passed_when_budget_positive(monkeypatch):
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(thinking_toks=500)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" in call_kwargs
    assert call_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8000}


@pytest.mark.asyncio
async def test_thinking_NOT_passed_when_budget_zero(monkeypatch):
    agent = _News(user_id="ariel")  # news_analyst has budget=0
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg()
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" not in call_kwargs


@pytest.mark.asyncio
async def test_thinking_tokens_extracted_from_response(monkeypatch):
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()
    # Anthropic returns thinking token count via the dedicated usage field:
    mock_msg = _make_mock_msg(thinking_toks=500)
    mock_msg.usage.thinking_tokens = 500   # the field the SDK exposes
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")
    assert result.thinking_tokens == 500


@pytest.mark.asyncio
async def test_thinking_unsupported_falls_back(monkeypatch, caplog):
    """When the model rejects the thinking param, retry once without it."""
    import logging
    caplog.set_level(logging.WARNING)

    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call (with thinking) raises a "not supported" error
            raise Exception("400 Bad Request: thinking is not supported on this model")
        # Second call (without thinking) succeeds
        return _make_mock_msg()
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 2  # initial + fallback
    # Second call's kwargs should NOT contain 'thinking'
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    assert "thinking" not in second_call_kwargs
    assert result.thinking_tokens == 0
    assert any("thinking not supported" in rec.message.lower() for rec in caplog.records)


def _make_bad_request(message: str, body: object) -> Exception:
    """Build a real anthropic.BadRequestError (status 400) so the matcher
    sees structured fields the way the live SDK would emit them.

    The SDK constructor requires a `httpx.Response`; we manufacture one.
    """
    import httpx
    from anthropic import BadRequestError

    return BadRequestError(
        message=message,
        response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
        body=body,
    )


@pytest.mark.asyncio
async def test_thinking_fallback_fires_on_structured_thinking_error(monkeypatch):
    """Structured 400 whose body.error.message names thinking → fallback fires."""
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _make_bad_request(
                "thinking.budget_tokens: thinking is not supported on this model",
                {"error": {
                    "type": "invalid_request_error",
                    "message": "thinking.budget_tokens: thinking is not supported on this model",
                }},
            )
        return _make_mock_msg()
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 2  # initial + fallback
    second_call_kwargs = fake_client.messages.create.call_args_list[1].kwargs
    assert "thinking" not in second_call_kwargs
    assert result.thinking_tokens == 0


@pytest.mark.asyncio
async def test_thinking_fallback_fires_on_param_thinking(monkeypatch):
    """Structured 400 whose top-level `param` is 'thinking' → fallback fires.

    Some Anthropic 400s surface the offending parameter at body['param']
    instead of (or in addition to) the error message. The matcher must
    catch that shape too.
    """
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _make_bad_request(
                "validation error",
                {"error": {"type": "invalid_request_error", "message": "validation error"},
                 "param": "thinking"},
            )
        return _make_mock_msg()
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 2
    assert "thinking" not in fake_client.messages.create.call_args_list[1].kwargs
    assert result.thinking_tokens == 0


@pytest.mark.asyncio
async def test_thinking_fallback_does_NOT_fire_on_unrelated_400(monkeypatch):
    """Codex blocker fix: a 400 about max_tokens (no thinking reference) must
    NOT silently retry without thinking. The agent must surface the original
    error instead — otherwise we'd drop a feature the call site asked for
    and produce a degraded answer the caller never authorized.
    """
    from argosy.agents.errors import AgentRunError

    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        # Body talks about max_tokens, NOT thinking. Old loose matcher
        # would have spuriously retried because "400" is in str(exc) and
        # the message could accidentally contain "thinking" elsewhere.
        raise _make_bad_request(
            "max_tokens: cannot exceed 200000 tokens for this model",
            {"error": {
                "type": "invalid_request_error",
                "message": "max_tokens: cannot exceed 200000 tokens for this model",
            }, "param": "max_tokens"},
        )
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    with pytest.raises(AgentRunError):
        await agent._call_via_api_key(system=full_system, user="hello")

    # Critical: only ONE call. The matcher MUST NOT have triggered the
    # fallback retry — that would silently drop the thinking param when
    # the real error was about max_tokens.
    assert call_count["n"] == 1, (
        f"Expected exactly 1 call (no spurious thinking fallback), got "
        f"{call_count['n']}. The matcher is too broad."
    )


@pytest.mark.asyncio
async def test_thinking_fallback_does_NOT_fire_on_400_mentioning_thinking_in_unrelated_field(
    monkeypatch,
):
    """A 400 whose structured body identifies a NON-thinking param must not
    fire the thinking fallback even if the human-readable message happens to
    mention thinking incidentally (e.g. a docs link).

    The structured `param` field is the source of truth when present.
    """
    from argosy.agents.errors import AgentRunError

    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        # Body has param=max_tokens, message about max_tokens — no
        # 'thinking' anywhere in structured fields. Matcher must NOT fire.
        raise _make_bad_request(
            "max_tokens exceeded the per-request cap",
            {"error": {
                "type": "invalid_request_error",
                "message": "max_tokens exceeded the per-request cap",
            }, "param": "max_tokens"},
        )
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    with pytest.raises(AgentRunError):
        await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_thinking_fallback_does_NOT_fire_on_non_400_error(monkeypatch):
    """A 500 / rate-limit error must propagate, NOT trigger the thinking
    fallback. Wave A finalization safety net.
    """
    import httpx
    from anthropic import InternalServerError

    from argosy.agents.errors import AgentRunError

    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()

    call_count = {"n": 0}
    def side_effect(**kwargs):
        call_count["n"] += 1
        raise InternalServerError(
            message="server error mentioning thinking somewhere",
            response=httpx.Response(500, request=httpx.Request("POST", "http://x")),
            body={"error": {"type": "server_error",
                            "message": "server error mentioning thinking somewhere"}},
        )
    fake_client.messages.create.side_effect = side_effect
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    with pytest.raises(AgentRunError):
        await agent._call_via_api_key(system=full_system, user="hello")

    assert call_count["n"] == 1, (
        "Non-400 errors must propagate without triggering the thinking fallback."
    )
