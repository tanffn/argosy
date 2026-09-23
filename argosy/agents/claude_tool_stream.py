"""Keep the SDK control channel open for explicitly allowed research tools."""
from __future__ import annotations

import asyncio
from dataclasses import replace


async def query_with_tool_permissions(*, query_fn, prompt, options, allowed_tools, expected_results=1):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

    done = asyncio.Event()
    allowed = frozenset(allowed_tools)

    async def permit(tool_name, inputs, context):
        # Do not grant suggested permissions or change persisted/managed policy.
        # Only approve a tool already explicitly enabled for this agent role.
        if tool_name in allowed:
            return PermissionResultAllow(updated_input=inputs)
        return PermissionResultDeny(message="Tool is outside this agent's explicit allowlist")

    async def stream():
        if isinstance(prompt, str):
            yield {"type": "user", "session_id": "", "parent_tool_use_id": None,
                   "message": {"role": "user", "content": prompt}}
        else:
            async for item in prompt:
                yield item
        # Bundled SDK waits for MCP/hooks here, but omits can_use_tool. Ending
        # the iterable closes stdin, making permission callbacks impossible.
        await done.wait()

    results = 0
    try:
        async for message in query_fn(prompt=stream(), options=replace(options, can_use_tool=permit)):
            if isinstance(message, ResultMessage):
                results += 1
                if message.is_error or results >= expected_results:
                    done.set()
            yield message
    finally:
        done.set()
