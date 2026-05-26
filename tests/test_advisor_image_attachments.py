"""Tests for Wave 5 image-attachment threading through AdvisorAgent + BaseAgent."""

from __future__ import annotations

import pytest

from argosy.agents.errors import AgentRunError


def _att(kind="image", path="/tmp/x.png", mime="image/png"):
    """Light Attachment-shaped dict for tests that don't need a real file."""

    class _A:
        pass

    a = _A()
    a.kind = kind
    a.path = path
    a.mime_type = mime
    a.original_name = path.split("/")[-1]
    a.size_bytes = 1
    return a


def test_advisor_prompt_adds_image_handling_block_when_images_present():
    from argosy.agents.advisor import AdvisorAgent

    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="stage_1",
        mode="user_driven",
        last_user_message="What does this say?",
        image_attachments=[_att(), _att()],
    )
    assert "IMAGE ATTACHMENT HANDLING" in sys
    assert "2 image(s)" in sys
    assert "brokerage statement" in sys.lower()


def test_advisor_prompt_omits_image_block_without_images():
    from argosy.agents.advisor import AdvisorAgent

    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="stage_1",
        mode="user_driven",
        last_user_message="hello",
    )
    assert "IMAGE ATTACHMENT HANDLING" not in sys


def test_advisor_image_block_coexists_with_amendment_block():
    """Image + has_current_plan both add their own system-prompt sections."""
    from argosy.agents.advisor import AdvisorAgent

    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="stage_1",
        mode="user_driven",
        has_current_plan=True,
        image_attachments=[_att()],
    )
    assert "IMAGE ATTACHMENT HANDLING" in sys
    assert "AMENDMENT INTENT DETECTION" in sys


@pytest.mark.asyncio
async def test_call_via_claude_code_streams_image_content_blocks(tmp_path, monkeypatch):
    """claude_code backend uses streaming-mode prompt when images are present.

    The SDK's prompt API accepts `AsyncIterable[dict]`; we yield a single
    dict in the message-shape claude.exe expects. Verify the shape by
    monkeypatching the SDK's `query()` to capture what we pass.

    The fake `query()` must yield a successful AssistantMessage + ResultMessage
    so the T2.6 empty-output retry envelope (N=3, shared budget) does not
    fire and re-stream the prompt on retries — otherwise the captured yields
    pile up and the shape assertion below sees 4 copies instead of 1.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    from argosy.agents.advisor import AdvisorAgent

    # Real PNG file so base64 encode succeeds
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    ))

    captured: dict = {}

    # Build a valid-JSON Advisor-shaped success payload so the malformed-JSON
    # trial-parse (also under the T2.6 shared budget) does not fire either.
    _success_text = (
        '{"stage":"stage_1","question_for_user":"ok",'
        '"stage_complete":false,"next_stage":null,'
        '"confidence":"MEDIUM","cited_sources":[],'
        '"notes_for_orchestrator":"","context_updates":[],'
        '"intake_session_id":"x","mode":"user_driven"}'
    )

    async def _fake_query(*, prompt, options):
        # Drain the AsyncIterable so we can assert on its yielded shape.
        if hasattr(prompt, "__aiter__"):
            captured["mode"] = "streaming"
            async for item in prompt:
                captured.setdefault("yields", []).append(item)
        else:
            captured["mode"] = "string"
            captured["string_prompt"] = prompt
        # Yield a successful stream so the retry envelope does not fire.
        yield AssistantMessage(
            content=[TextBlock(text=_success_text)],
            model="claude-sonnet-4-6",
        )
        from claude_agent_sdk import ResultMessage
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="test",
            stop_reason="end_turn",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
            result="ok",
        )

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)

    agent = AdvisorAgent(user_id="test")
    att = _att(path=str(img_path), mime="image/png")
    await agent._call_via_claude_code_inner(
        system="sys", user="hello", image_attachments=[att],
    )

    assert captured["mode"] == "streaming"
    assert len(captured["yields"]) == 1
    msg = captured["yields"][0]
    assert msg["type"] == "user"
    content = msg["message"]["content"]
    assert isinstance(content, list)
    types = [b["type"] for b in content]
    assert "image" in types
    assert "text" in types
    img_block = next(b for b in content if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert len(img_block["source"]["data"]) > 0


@pytest.mark.asyncio
async def test_call_via_claude_code_uses_string_prompt_when_no_images(monkeypatch):
    """Text-only turns keep the cheaper string-prompt path on claude_code."""
    from argosy.agents.advisor import AdvisorAgent

    captured: dict = {}

    async def _fake_query(*, prompt, options):
        captured["prompt"] = prompt
        if False:
            yield None

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)

    agent = AdvisorAgent(user_id="test")
    await agent._call_via_claude_code_inner(
        system="sys", user="just text", image_attachments=None,
    )
    assert captured["prompt"] == "just text"


@pytest.mark.asyncio
async def test_call_via_api_key_builds_image_content_blocks(tmp_path, monkeypatch):
    """api_key backend prepends image content blocks to the user message."""
    from argosy.agents.advisor import AdvisorAgent

    # Write a real PNG file for the helper to base64-encode
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    )
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(png_bytes)

    captured: dict = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Msg:
                content = [type("B", (), {"text": '{"stage":"stage_1","question_for_user":"ok","stage_complete":false,"next_stage":null,"confidence":"MEDIUM","cited_sources":[],"notes_for_orchestrator":"","context_updates":[],"intake_session_id":"x","mode":"user_driven"}'})()]
                model = "claude-sonnet-4-6"
                usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()

            return _Msg()

    class _FakeClient:
        messages = _FakeMessages()

    agent = AdvisorAgent(user_id="test")
    monkeypatch.setattr(agent, "_build_client", lambda: _FakeClient())

    att = _att(path=str(img_path), mime="image/png")
    await agent._call_via_api_key(
        system="sys", user="hello", image_attachments=[att],
    )

    msgs = captured.get("messages", [])
    assert msgs and isinstance(msgs[0]["content"], list)
    block_types = [b["type"] for b in msgs[0]["content"]]
    assert "image" in block_types
    assert "text" in block_types
    img_block = next(b for b in msgs[0]["content"] if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert len(img_block["source"]["data"]) > 0  # base64-encoded png


@pytest.mark.asyncio
async def test_call_via_api_key_uses_string_content_when_no_images(monkeypatch):
    """No-image path stays on the cheap string-content shape."""
    from argosy.agents.advisor import AdvisorAgent

    captured: dict = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Msg:
                content = [type("B", (), {"text": '{"stage":"stage_1","question_for_user":"ok","stage_complete":false,"next_stage":null,"confidence":"MEDIUM","cited_sources":[],"notes_for_orchestrator":"","context_updates":[],"intake_session_id":"x","mode":"user_driven"}'})()]
                model = "claude-sonnet-4-6"
                usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()

            return _Msg()

    class _FakeClient:
        messages = _FakeMessages()

    agent = AdvisorAgent(user_id="test")
    monkeypatch.setattr(agent, "_build_client", lambda: _FakeClient())

    await agent._call_via_api_key(system="sys", user="hello", image_attachments=None)
    msgs = captured.get("messages", [])
    assert msgs and msgs[0]["content"] == "hello"


def test_run_drops_image_attachments_when_build_prompt_doesnt_accept_them():
    """A subclass without image_attachments in its signature must not blow up
    when run() is called with image_attachments — they're popped silently."""
    import asyncio

    from argosy.agents.base import BaseAgent, ModelCall
    from pydantic import BaseModel

    class _Out(BaseModel):
        text: str = "ok"

    class _Subagent(BaseAgent[_Out]):
        agent_role = "test_subagent"
        output_model = _Out
        require_citations = False

        def build_prompt(self, *, message: str = "") -> tuple[str, str]:
            return ("system", f"user says {message}")

        async def _call_model(self, *, system, user, image_attachments=None):
            # Should be reachable; image_attachments arrives but isn't used.
            return ModelCall(text='{"text":"ok"}', tokens_in=1, tokens_out=1, model="x", raw=None)

    agent = _Subagent(user_id="test")
    # Would TypeError if run() forwarded image_attachments to build_prompt.
    report = asyncio.run(agent.run(message="hi", image_attachments=["fake"]))
    assert report.output.text == "ok"
