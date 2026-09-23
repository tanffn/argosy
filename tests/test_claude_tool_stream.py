import asyncio
from contextlib import aclosing

import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, ResultMessage

from argosy.agents.claude_tool_stream import query_with_tool_permissions


def result():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="test", stop_reason="end_turn")


@pytest.mark.asyncio
async def test_control_stream_stays_open_and_only_grants_existing_allowlist():
    closed = asyncio.Event()
    async def fake(*, prompt, options):
        async def consume():
            async for item in prompt:
                assert item["message"]["content"] == "test"
            closed.set()
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        assert not closed.is_set()
        assert isinstance(await options.can_use_tool("WebFetch", {"url": "https://example.com"}, None), PermissionResultAllow)
        assert isinstance(await options.can_use_tool("Bash", {"command": "anything"}, None), PermissionResultDeny)
        yield result()
        await asyncio.wait_for(task, 1)
    output = [m async for m in query_with_tool_permissions(query_fn=fake, prompt="test",
        options=ClaudeAgentOptions(), allowed_tools=("WebFetch",))]
    assert len(output) == 1 and closed.is_set()


@pytest.mark.asyncio
async def test_multi_result_input_stays_open_until_final_result():
    closed = asyncio.Event()
    async def fake(*, prompt, options):
        async def consume():
            async for item in prompt:
                pass
            closed.set()
        task = asyncio.create_task(consume())
        yield result()
        await asyncio.sleep(0)
        assert not closed.is_set()
        yield result()
        await asyncio.wait_for(task, 1)
    assert len([m async for m in query_with_tool_permissions(query_fn=fake, prompt="test",
        options=ClaudeAgentOptions(), allowed_tools=("WebFetch",), expected_results=2)]) == 2


@pytest.mark.asyncio
async def test_early_stream_failure_releases_input_waiter():
    tasks = []
    async def fake(*, prompt, options):
        async def consume():
            async for item in prompt:
                pass
        tasks.append(asyncio.create_task(consume()))
        await asyncio.sleep(0)
        raise RuntimeError("provider failed")
        yield  # async generator
    with pytest.raises(RuntimeError):
        async with aclosing(query_with_tool_permissions(query_fn=fake, prompt="test",
            options=ClaudeAgentOptions(), allowed_tools=("WebFetch",))) as stream:
            async for item in stream:
                pass
    await asyncio.wait_for(tasks[0], 1)
