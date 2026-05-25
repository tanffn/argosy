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


# ----------------------------------------------------------------------
# Change 4 — multi-PDF auto-batching (claude.exe stdin-line cap)
# ----------------------------------------------------------------------
#
# Background: claude.exe's stdin JSONL parser dies silently when a
# single line exceeds ~760 KB (commit e863fc9). Argosy splits binary
# attachments (PDFs + images) into batches of at most
# `CLAUDE_CODE_MAX_BINARY_BLOCKS_PER_BATCH` when total > threshold.
# Each batch becomes its own user→assistant turn in one streaming-mode
# query; only the final turn's text is used as the model output.


def _make_pdf_att(path, original_name="x.pdf"):
    """Fake Attachment-like object with `.path` and `.original_name`."""
    class _Att:
        pass
    a = _Att()
    a.path = str(path)
    a.original_name = original_name
    return a


def _make_image_att(path, mime_type="image/png"):
    class _Att:
        pass
    a = _Att()
    a.path = str(path)
    a.mime_type = mime_type
    return a


def _write_fake_pdf(tmp_path, name, size_bytes=1024):
    """Write a fake PDF file. Base64-encodable; content doesn't need to
    be a real PDF since we never feed it to a real parser in tests."""
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4\n" + b"x" * (size_bytes - 9))
    return p


def test_build_claude_code_messages_single_batch_below_threshold(tmp_path):
    """Up to MAX_BLOCKS_PER_BATCH PDFs that also fit byte-cap collapse into
    one user message — verbatim pre-batching behavior. Text block carries
    the original prompt unchanged."""
    from argosy.agents.base import (
        CLAUDE_CODE_MAX_BINARY_BLOCKS_PER_BATCH,
        _build_claude_code_messages,
    )

    # Small PDFs so the byte-cap doesn't trip — the block-count cap is
    # what we're testing here.
    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf", size_bytes=1024))
        for i in range(CLAUDE_CODE_MAX_BINARY_BLOCKS_PER_BATCH)
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="original prompt",
        image_attachments=[],
        pdf_attachments=pdfs,
    )

    assert len(msgs) == 1
    content = msgs[0]["message"]["content"]
    pdf_blocks = [b for b in content if b["type"] == "document"]
    text_blocks = [b for b in content if b["type"] == "text"]
    assert len(pdf_blocks) == CLAUDE_CODE_MAX_BINARY_BLOCKS_PER_BATCH
    assert len(text_blocks) == 1
    # Text is unchanged — no batch markers prepended.
    assert text_blocks[0]["text"] == "original prompt"


def test_build_claude_code_messages_chunks_above_block_cap(tmp_path):
    """4 small PDFs (under byte-cap) → 2 batches of 3+1 via block-count cap."""
    from argosy.agents.base import _build_claude_code_messages

    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf", size_bytes=1024))
        for i in range(4)
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="ingest these payslips",
        image_attachments=[],
        pdf_attachments=pdfs,
        max_blocks_per_batch=3,
    )

    assert len(msgs) == 2
    # Batch 1: 3 PDFs + opening text with original prompt + batch-1 marker.
    b1_content = msgs[0]["message"]["content"]
    assert sum(1 for b in b1_content if b["type"] == "document") == 3
    b1_text = next(b["text"] for b in b1_content if b["type"] == "text")
    assert "ingest these payslips" in b1_text
    assert "Batch 1 of 2" in b1_text
    assert "final batch" in b1_text  # tells model to wait for final
    # Batch 2: 1 PDF + final-batch marker.
    b2_content = msgs[1]["message"]["content"]
    assert sum(1 for b in b2_content if b["type"] == "document") == 1
    b2_text = next(b["text"] for b in b2_content if b["type"] == "text")
    assert "Batch 2 of 2" in b2_text
    assert "final" in b2_text.lower()
    assert "complete structured response" in b2_text


def test_build_claude_code_messages_chunks_above_byte_cap(tmp_path):
    """3 medium PDFs (under block-cap, OVER byte-cap) → split by size.

    Uses an explicit `max_bytes_per_batch` so the test stays deterministic
    even as the live default constant is tuned (it moved from 130 KB to
    500 KB once the encryption gate handled the original failure mode).
    """
    from argosy.agents.base import _build_claude_code_messages

    # 3 × 85 KB PDFs. Pairs (170 KB) exceed 130 KB → 1 PDF per batch.
    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf", size_bytes=85 * 1024))
        for i in range(3)
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="ingest payslips",
        image_attachments=[],
        pdf_attachments=pdfs,
        max_bytes_per_batch=130_000,
    )

    assert len(msgs) == 3
    for msg in msgs:
        pdf_count = sum(1 for b in msg["message"]["content"] if b["type"] == "document")
        assert pdf_count == 1


def test_build_claude_code_messages_2_form_106s_fit_one_batch(tmp_path):
    """The empirically-confirmed working case (id=93 in dev): 2 Form 106
    PDFs totaling ~111 KB stay in ONE batch under the 130 KB byte cap."""
    from argosy.agents.base import _build_claude_code_messages

    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, "form106_a.pdf", size_bytes=43 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "form106_b.pdf", size_bytes=68 * 1024)),
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="review these",
        image_attachments=[],
        pdf_attachments=pdfs,
    )

    # 43 + 68 = 111 KB ≤ 130 KB cap → single batch, no batch markers.
    assert len(msgs) == 1
    text = next(b["text"] for b in msgs[0]["message"]["content"] if b["type"] == "text")
    assert text == "review these"


def test_build_claude_code_messages_nine_pdf_user_case(tmp_path):
    """The user's actual failing case: 9 PDFs at real sizes (Form 106s,
    payslips, statements). Uses explicit caps that match the historical
    130 KB / 3-block defaults so the test pins the packing math
    independently of the live constants (which moved to 500 KB / 9 once
    the encryption gate handled the original failure mode and chunking
    became defense-in-depth rather than load-bearing)."""
    from argosy.agents.base import _build_claude_code_messages

    # Mirror the user's actual file sizes from their failing 9-PDF batch.
    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, "payslip_02.pdf", size_bytes=85 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "payslip_03.pdf", size_bytes=85 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "payslip_04.pdf", size_bytes=85 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "stmt_01.pdf", size_bytes=51 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "stmt_02.pdf", size_bytes=52 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "stmt_03.pdf", size_bytes=53 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "stmt_04.pdf", size_bytes=51 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "form106.pdf", size_bytes=43 * 1024)),
        _make_pdf_att(_write_fake_pdf(tmp_path, "noga_b.pdf", size_bytes=68 * 1024)),
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="ingest all 9, summarize",
        image_attachments=[],
        pdf_attachments=pdfs,
        max_blocks_per_batch=3,
        max_bytes_per_batch=130_000,
    )

    # Greedy packing under 130 KB byte cap + 3 block cap:
    # [85] [85] [85] [51, 52] [53, 51] [43, 68] = 6 batches.
    assert len(msgs) == 6
    # First batch carries the user prompt verbatim + batch markers.
    b1_text = next(b["text"] for b in msgs[0]["message"]["content"] if b["type"] == "text")
    assert "ingest all 9, summarize" in b1_text
    assert "Batch 1 of 6" in b1_text
    # Final batch tells the model to produce the structured response.
    last_text = next(b["text"] for b in msgs[-1]["message"]["content"] if b["type"] == "text")
    assert "Batch 6 of 6 (final)" in last_text
    assert "complete structured response" in last_text
    for i, msg in enumerate(msgs):
        pdf_blocks = [b for b in msg["message"]["content"] if b["type"] == "document"]
        assert len(pdf_blocks) <= 3, f"batch {i+1} has {len(pdf_blocks)} PDFs (> 3 cap)"


def test_build_claude_code_messages_3_decrypted_payslips_fit_single_batch_at_500kb_cap(tmp_path):
    """Regression for the post-encryption-fix failure: 3 decrypted payslips
    at ~94 KB each (282 KB total) should pack into ONE batch under the
    new 500 KB default cap, NOT 3 separate turns (which previously
    caused the SDK to die mid-stream with 'expected 3 turns, got 2')."""
    from argosy.agents.base import _build_claude_code_messages

    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"payslip_decrypted_{i}.pdf", size_bytes=94 * 1024))
        for i in range(3)
    ]

    msgs = _build_claude_code_messages(
        user_with_sources="ingest payslips",
        image_attachments=[],
        pdf_attachments=pdfs,
    )

    # Single batch under the live 500 KB default cap. Critical: text is
    # the verbatim user prompt — NO batch markers, no multi-turn fragility.
    assert len(msgs) == 1
    pdf_blocks = [b for b in msgs[0]["message"]["content"] if b["type"] == "document"]
    text_blocks = [b for b in msgs[0]["message"]["content"] if b["type"] == "text"]
    assert len(pdf_blocks) == 3
    assert text_blocks[0]["text"] == "ingest payslips"


@pytest.mark.asyncio
async def test_call_via_claude_code_inner_max_turns_scales_with_chunking(
    tmp_path, monkeypatch,
):
    """When chunking yields N user messages, max_turns is bumped to N+1
    so the SDK's agent loop doesn't cap mid-stream. Default of 1 stays
    for the single-message path.

    Uses 6 × 200 KB PDFs to reliably produce 3 batches under the live
    500 KB byte cap: greedy packing yields [200+200][200+200][200+200].
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf", size_bytes=200 * 1024))
        for i in range(6)
    ]
    captured = _install_fake_query(monkeypatch, yielded=[
        AssistantMessage(content=[TextBlock(text="ack-1")], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=10, output_tokens=2),
        AssistantMessage(content=[TextBlock(text="ack-2")], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=10, output_tokens=2),
        AssistantMessage(content=[TextBlock(text="final")], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=10, output_tokens=2),
    ])

    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys", user="ingest", pdf_attachments=pdfs,
    )
    # 3 chunks → max_turns should be 4 (chunks + 1 headroom).
    assert captured["options"].max_turns == 4


@pytest.mark.asyncio
async def test_call_via_claude_code_inner_max_turns_unchanged_for_single_batch(
    tmp_path, monkeypatch,
):
    """Single-message path keeps the default max_turns=1 — we only bump
    when chunking is actually firing."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    pdfs = [_make_pdf_att(_write_fake_pdf(tmp_path, "p.pdf", size_bytes=10 * 1024))]
    captured = _install_fake_query(monkeypatch, yielded=[
        AssistantMessage(content=[TextBlock(text="x")], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=5, output_tokens=1),
    ])
    agent = _make_agent()
    await agent._call_via_claude_code_inner(
        system="sys", user="ingest", pdf_attachments=pdfs,
    )
    assert captured["options"].max_turns == 1


def test_build_claude_code_messages_oversize_single_attachment_stays_one_batch(tmp_path):
    """A single attachment larger than the byte cap still gets its own
    one-element batch (we don't reject; we let claude.exe try)."""
    from argosy.agents.base import _build_claude_code_messages

    big_pdf = _make_pdf_att(_write_fake_pdf(tmp_path, "big.pdf", size_bytes=300 * 1024))

    msgs = _build_claude_code_messages(
        user_with_sources="one big PDF",
        image_attachments=[],
        pdf_attachments=[big_pdf],
    )

    # 300 KB > 130 KB cap but `combined` has only 1 item, so the fast-path
    # check `total_bytes > max_bytes_per_batch` triggers the multi-batch
    # path; greedy packing still produces a single bin holding the one PDF.
    assert len(msgs) == 1
    pdf_count = sum(1 for b in msgs[0]["message"]["content"] if b["type"] == "document")
    assert pdf_count == 1


def test_build_claude_code_messages_mixed_pdf_image_ordering(tmp_path):
    """PDFs come before images in each batch — matches api_key cache-prefix order."""
    from argosy.agents.base import _build_claude_code_messages

    p = _write_fake_pdf(tmp_path, "doc.pdf")
    # Tiny valid PNG so base64 encoding succeeds.
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    ))

    msgs = _build_claude_code_messages(
        user_with_sources="mixed test",
        image_attachments=[_make_image_att(img_path)],
        pdf_attachments=[_make_pdf_att(p)],
    )

    assert len(msgs) == 1
    content = msgs[0]["message"]["content"]
    # PDF block must come before image block.
    block_kinds = [b["type"] for b in content if b["type"] in ("document", "image")]
    assert block_kinds == ["document", "image"]


@pytest.mark.asyncio
async def test_call_via_claude_code_inner_uses_last_turn_text_with_chunking(
    tmp_path, monkeypatch
):
    """Multi-batch send: ModelCall.text == last turn's text only."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    pdfs = [_make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf")) for i in range(4)]

    # Yield: batch1 assistant ack + result, batch2 assistant final + result.
    yielded = [
        AssistantMessage(
            content=[TextBlock(text="Got batch 1, awaiting more.")],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(
            input_tokens=100, output_tokens=10, cache_creation_input_tokens=200,
            total_cost_usd=0.01,
        ),
        AssistantMessage(
            content=[TextBlock(text='{"final": "structured response"}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(
            input_tokens=20, output_tokens=50, cache_read_input_tokens=300,
            total_cost_usd=0.02,
        ),
    ]
    _install_fake_query(monkeypatch, yielded=yielded)

    agent = _make_agent()
    call = await agent._call_via_claude_code_inner(
        system="sys", user="ingest", pdf_attachments=pdfs,
    )

    # Only the LAST turn's text survives — batch-1 ack is dropped.
    assert call.text == '{"final": "structured response"}'
    # Tokens summed across turns: in 100+20=120, out 10+50=60.
    assert call.tokens_in == 120
    assert call.tokens_out == 60
    # cache_creation from batch 1 + cache_read from batch 2, both retained.
    assert call.cache_creation_tokens == 200
    assert call.cache_input_tokens == 300


@pytest.mark.asyncio
async def test_call_via_claude_code_inner_raises_on_incomplete_chunked_turns(
    tmp_path, monkeypatch
):
    """If the SDK yields fewer ResultMessages than user messages (claude.exe
    crashed mid-stream), raise AgentRunError with a turn-count mismatch.

    Uses 4 × 200 KB PDFs to reliably trigger chunking under the live
    500 KB byte cap regardless of future tuning: greedy packing yields
    [200+200][200+200] = 2 batches, so expected_turns=2.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    from argosy.agents.errors import AgentRunError

    pdfs = [
        _make_pdf_att(_write_fake_pdf(tmp_path, f"p{i}.pdf", size_bytes=200 * 1024))
        for i in range(4)
    ]

    # 4 × 200 KB at 500 KB cap → 2 batches. Yield only 1 turn worth.
    yielded = [
        AssistantMessage(
            content=[TextBlock(text="batch1")], model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=10, output_tokens=5),
        # ... no batch 2 — simulate crash
    ]
    _install_fake_query(monkeypatch, yielded=yielded)

    agent = _make_agent()
    with pytest.raises(AgentRunError) as exc_info:
        await agent._call_via_claude_code_inner(
            system="sys", user="ingest", pdf_attachments=pdfs,
        )
    msg = str(exc_info.value)
    assert "expected 2 turn(s), got 1" in msg


# ----------------------------------------------------------------------
# Transient claude.exe exit-1 retry (SDD open-gap #4 / W2.A)
# ----------------------------------------------------------------------
#
# Live synthesis run #6 surfaced a flake where claude.exe exits 1 with
# an empty stderr after the subprocess has been alive a while. We retry
# the SDK `query()` call exactly once with a fresh session; retries that
# look deterministic (exit code != 1, or non-empty stderr) bypass the
# retry and surface the original error immediately.


class _RecordingLogger:
    """Minimal stub mimicking structlog's BoundLogger surface used by
    `_call_via_claude_code_inner`. Records every warning call so a test
    can assert both the event name and the structured kwargs.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, *args: Any, **kwargs: Any) -> None:
        # `_capture_stderr` calls with kwargs; the thinking-fallback path
        # uses positional %-formatting. We only need the event name +
        # kwargs for the retry assertion.
        self.warnings.append((event, dict(kwargs)))

    # Defensive — claude_code path only emits warning() today but other
    # code paths in BaseAgent may call info/error during construction.
    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None


def _install_fake_query_with_call_counter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    side_effects: list[Any],
) -> dict[str, Any]:
    """Patch `claude_agent_sdk.query` so each call pops the next item
    from `side_effects`:

      * A `BaseException` instance → raised when `query()` body runs.
      * A `list` of messages → yielded as the stream.

    Returns a `captured` dict with `n_calls` so the test can verify the
    exact number of `query()` invocations (must be 2 for retry-once).
    """
    captured: dict[str, Any] = {"n_calls": 0}
    pending = list(side_effects)

    async def _fake_query(*, prompt, options):
        captured["n_calls"] += 1
        if not pending:
            raise AssertionError(
                "fake query() called more times than side_effects allows"
            )
        effect = pending.pop(0)
        # Drain streaming-mode prompts to mimic the real SDK's behavior.
        if hasattr(prompt, "__aiter__"):
            async for _ in prompt:
                pass
        if isinstance(effect, BaseException):
            raise effect
        for message in effect:
            yield message

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)
    return captured


@pytest.mark.asyncio
async def test_claude_code_retry_on_transient_exit1_flake(monkeypatch):
    """ProcessError(exit_code=1, empty stderr) → retry once, succeed.

    Replicates the live-run #6 fingerprint: claude.exe exits 1 without
    writing to stderr. The retry must:
      - Re-call `query()` exactly once more (n_calls == 2 total).
      - Emit a `claude_code.transient_exit1_retry` warning with
        structured fields (agent_role, model, error).
      - Return the SECOND attempt's output text in the ModelCall.
    """
    from claude_agent_sdk import AssistantMessage, ProcessError, TextBlock

    flake = ProcessError(
        "Command failed with exit code 1",
        exit_code=1,
        stderr="Check stderr output for details",
    )
    # Second attempt: success path — one assistant message + one result.
    success_stream = [
        AssistantMessage(
            content=[TextBlock(text="retry-success-payload")],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[flake, success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # 1. Exactly one retry (initial + one retry == 2 total).
    assert captured["n_calls"] == 2

    # 2. ModelCall text comes from the SECOND (successful) attempt.
    assert call.text == "retry-success-payload"
    # Token counters reflect ONLY the successful attempt — the first-
    # attempt accumulators must have been reset by the retry loop, not
    # double-counted.
    assert call.tokens_in == 11
    assert call.tokens_out == 22

    # 3. Retry warning fired exactly once with structured fields.
    retry_events = [
        kw for ev, kw in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert len(retry_events) == 1, (
        f"expected exactly 1 retry warning, got {len(retry_events)}: "
        f"{recorder.warnings}"
    )
    fields = retry_events[0]
    assert fields["agent_role"] == agent.agent_role
    assert fields["model"] == agent.model
    assert "exit code 1" in fields["error"].lower()


@pytest.mark.asyncio
async def test_claude_code_no_retry_when_stderr_non_empty(monkeypatch):
    """ProcessError(exit_code=1) but with claude.exe stderr output → do
    NOT retry. Non-empty stderr means the failure is deterministic (e.g.
    a model error message) and retrying would just double cost/latency.
    """
    from claude_agent_sdk import ProcessError

    from argosy.agents.errors import AgentRunError

    flake_with_stderr = ProcessError(
        "Command failed with exit code 1", exit_code=1, stderr="ignored",
    )

    pending = [flake_with_stderr]

    async def _fake_query(*, prompt, options):
        # Simulate claude.exe writing to stderr before exiting — the SDK
        # invokes the user-supplied `stderr` callback as lines arrive.
        if hasattr(options, "stderr") and options.stderr is not None:
            options.stderr("deterministic error line\n")
        if not pending:
            raise AssertionError("fake query() called too many times")
        raise pending.pop(0)
        yield  # pragma: no cover — make this an async generator

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    with pytest.raises(AgentRunError) as exc_info:
        await agent._call_via_claude_code_inner(system="sys", user="hi")

    # No retry should have fired.
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert retry_events == []
    # Error message should include the captured stderr tail.
    assert "deterministic error line" in str(exc_info.value)


@pytest.mark.asyncio
async def test_claude_code_no_retry_when_exit_code_not_1(monkeypatch):
    """ProcessError with exit_code != 1 → do NOT retry. The transient-
    flake fingerprint is specifically exit-1; other codes (e.g. 137 OOM,
    2 SIGINT, 130 user-cancel) have different root causes that retrying
    won't fix.
    """
    from claude_agent_sdk import ProcessError

    from argosy.agents.errors import AgentRunError

    flake_wrong_code = ProcessError(
        "Command failed with exit code 137", exit_code=137, stderr="",
    )
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[flake_wrong_code],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    with pytest.raises(AgentRunError):
        await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Only one call — no retry on non-1 exit codes.
    assert captured["n_calls"] == 1
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert retry_events == []


@pytest.mark.asyncio
async def test_claude_code_no_retry_on_non_process_error(monkeypatch):
    """Non-ProcessError exceptions (e.g. CLIJSONDecodeError, generic
    RuntimeError) bypass the retry path — they signal a different class
    of failure that retrying with a fresh session won't help.
    """
    from argosy.agents.errors import AgentRunError

    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[RuntimeError("parser exploded")],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    with pytest.raises(AgentRunError):
        await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert captured["n_calls"] == 1
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert retry_events == []


@pytest.mark.asyncio
async def test_claude_code_retry_caps_at_one_on_repeated_flake(monkeypatch):
    """If the transient flake hits twice in a row, the second occurrence
    must surface as an AgentRunError — we never retry more than once per
    `_call_via_claude_code_inner` invocation.
    """
    from claude_agent_sdk import ProcessError

    from argosy.agents.errors import AgentRunError

    flake_a = ProcessError(
        "Command failed with exit code 1", exit_code=1, stderr="",
    )
    flake_b = ProcessError(
        "Command failed with exit code 1", exit_code=1, stderr="",
    )
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[flake_a, flake_b],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    with pytest.raises(AgentRunError):
        await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly two calls: original + one retry. No third attempt.
    assert captured["n_calls"] == 2
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    # Exactly one retry warning — the retry happened once, then the
    # second occurrence surfaced rather than triggering retry #2.
    assert len(retry_events) == 1


# ----------------------------------------------------------------------
# Empty-output retry (W2.A-v2)
# ----------------------------------------------------------------------
#
# Live synthesis runs #6, #9, #10 hit a second flake fingerprint:
# `claude_agent_sdk.query()` completes cleanly (no exception, no
# non-zero exit) but the model emitted zero text. The downstream
# `_parse_output("")` then raises a JSONDecodeError. The recovery is
# the same as the exit-1 path — restart the SDK session once with a
# fresh `query()` call — and the SHARED `_retried` guard ensures both
# triggers together do at most one retry per invocation.


@pytest.mark.asyncio
async def test_claude_code_retry_on_empty_model_output(monkeypatch):
    """SDK returns successfully but model output is empty → retry once,
    succeed on second call.

    Replicates the live-run #10 fingerprint: the streaming session ends
    cleanly with a `ResultMessage` but every `AssistantMessage` had no
    `TextBlock` content (or whitespace-only). The retry must:
      - Re-call `query()` exactly once more (n_calls == 2 total).
      - Emit a `claude_code.empty_output_retry` warning with
        structured fields (agent_role, model).
      - Return the SECOND attempt's text in the ModelCall.
      - Surface tokens from ONLY the second attempt (the first-
        attempt accumulators must be reset by the retry loop).
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    # First attempt: stream completes but no text is emitted. The
    # `ResultMessage` still arrives (so `turns_seen == expected_turns`
    # below), but `turn_buffers` ends up empty.
    empty_stream = [
        AssistantMessage(content=[], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=7, output_tokens=0),
    ]
    # Second attempt: success — one assistant message + one result.
    success_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"confidence":"HIGH"}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[empty_stream, success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # 1. Exactly one retry (initial + one retry == 2 total).
    assert captured["n_calls"] == 2

    # 2. ModelCall text comes from the SECOND (successful) attempt.
    assert call.text == '{"confidence":"HIGH"}'
    # Token counters reflect ONLY the second attempt — first-attempt
    # accumulators must have been reset by the retry loop, not double-
    # counted with the empty-attempt's 7/0.
    assert call.tokens_in == 11
    assert call.tokens_out == 22

    # 3. Retry warning fired exactly once with structured fields.
    retry_events = [
        kw for ev, kw in recorder.warnings
        if ev == "claude_code.empty_output_retry"
    ]
    assert len(retry_events) == 1, (
        f"expected exactly 1 empty-output retry warning, got "
        f"{len(retry_events)}: {recorder.warnings}"
    )
    fields = retry_events[0]
    assert fields["agent_role"] == agent.agent_role
    assert fields["model"] == agent.model

    # 4. The W2.A exit-1 retry warning must NOT have fired — this is a
    # different fingerprint and we shouldn't double-log.
    exit1_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert exit1_events == []


@pytest.mark.asyncio
async def test_claude_code_retry_on_whitespace_only_model_output(monkeypatch):
    """Whitespace-only text counts as empty: the model emitted only
    spaces / newlines, which would still kill `_parse_output`. Same
    retry path as fully-empty.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    whitespace_stream = [
        AssistantMessage(
            content=[TextBlock(text="   \n\t  \n")],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=3, output_tokens=1),
    ]
    success_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"ok":true}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[whitespace_stream, success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    assert captured["n_calls"] == 2
    assert call.text == '{"ok":true}'
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.empty_output_retry"
    ]
    assert len(retry_events) == 1


@pytest.mark.asyncio
async def test_claude_code_empty_output_retry_caps_at_one(monkeypatch):
    """If the empty-output flake hits twice in a row, the second
    occurrence does NOT trigger a third call — `_retried` is shared
    across both retry signatures and bounds the function at one retry
    total per invocation. The ModelCall returns with empty text (the
    downstream `_parse_output` will then surface the JSONDecodeError
    that motivated this work).
    """
    from claude_agent_sdk import AssistantMessage

    empty_a = [
        AssistantMessage(content=[], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=1, output_tokens=0),
    ]
    empty_b = [
        AssistantMessage(content=[], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=2, output_tokens=0),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[empty_a, empty_b],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly two calls — original + one retry, no third attempt.
    assert captured["n_calls"] == 2
    # Returned ModelCall carries empty text; downstream parse will fail
    # (that's the existing JSONDecodeError surface, not this loop's
    # concern).
    assert call.text == ""
    # Only one retry warning fired.
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.empty_output_retry"
    ]
    assert len(retry_events) == 1


@pytest.mark.asyncio
async def test_claude_code_no_retry_on_empty_after_exit1_retry(monkeypatch):
    """`_retried` is SHARED across the W2.A exit-1 path and the W2.A-v2
    empty-output path: at most one retry per invocation, regardless of
    which signature fires first.

    Scenario: first call hits the exit-1 flake → retry consumed. The
    retry returns successfully but with empty model output. The
    function MUST NOT retry again (would be the 3rd call) — it returns
    a ModelCall with empty text, and the downstream `_parse_output`
    raises the JSONDecodeError that surfaces the underlying issue.
    """
    from claude_agent_sdk import AssistantMessage, ProcessError

    flake = ProcessError(
        "Command failed with exit code 1",
        exit_code=1,
        stderr="Check stderr output for details",
    )
    empty_stream = [
        AssistantMessage(content=[], model="claude-sonnet-4-6"),
        _make_result_message(input_tokens=5, output_tokens=0),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[flake, empty_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly two calls — the exit-1 retry consumed the single retry
    # budget, so the subsequent empty-text result does NOT trigger
    # another call.
    assert captured["n_calls"] == 2
    # ModelCall text is empty; downstream parse will surface the
    # original empty-output failure mode as a JSONDecodeError.
    assert call.text == ""

    # The exit-1 retry warning fired (the first flake).
    exit1_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert len(exit1_events) == 1
    # The empty-output retry warning did NOT fire — `_retried` was
    # already True by the time the empty-text check ran on attempt #2.
    empty_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.empty_output_retry"
    ]
    assert empty_events == []


@pytest.mark.asyncio
async def test_claude_code_no_empty_retry_when_text_non_empty(monkeypatch):
    """Sanity check: non-empty text on the first call must NOT trigger
    the empty-output retry path. Guards against a too-broad signature
    that would silently double cost on every call.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    success_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"confidence":"HIGH"}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly one call — no retry on non-empty output.
    assert captured["n_calls"] == 1
    assert call.text == '{"confidence":"HIGH"}'
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.empty_output_retry"
    ]
    assert retry_events == []


# ----------------------------------------------------------------------
# Malformed-JSON retry (W3b.F)
# ----------------------------------------------------------------------
#
# Live synthesis runs #6, #9, #10, #11, #12, #13 surfaced a third flake
# fingerprint, mostly in `PlanCritiqueAgent` but occasionally in other
# long-output agents: the SDK stream completes cleanly with non-empty
# text, but the model emitted STRUCTURALLY invalid JSON (missing comma,
# unclosed bracket, etc.). Recovery is the same as W2.A and W2.A-v2 —
# fresh `query()` call. The SHARED `_retried` guard ensures all three
# triggers together do at most one retry per invocation.


@pytest.mark.asyncio
async def test_claude_code_retry_on_malformed_json(monkeypatch):
    """Model emits structurally invalid JSON on first call (missing
    delimiter) but valid JSON on second → retry once, succeed.

    Replicates the live-run #9/#12/#13 fingerprint: `_parse_output`
    raises `json.JSONDecodeError("Expecting ',' delimiter: ...")`. The
    retry must:
      - Re-call `query()` exactly once more (n_calls == 2 total).
      - Emit a `claude_code.malformed_json_retry` warning with
        structured fields (agent_role, model, error).
      - Return the SECOND attempt's text in the ModelCall.
      - Surface tokens from ONLY the second attempt.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    # First attempt: stream completes with non-empty text, but the JSON
    # is missing a comma between the two fields — a real-world live-run
    # fingerprint that neither strict=False nor raw_decode can recover.
    malformed_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"foo": 1 "bar": 2}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=7, output_tokens=3),
    ]
    success_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"confidence":"HIGH"}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[malformed_stream, success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # 1. Exactly one retry (initial + one retry == 2 total).
    assert captured["n_calls"] == 2

    # 2. ModelCall text comes from the SECOND (successful) attempt.
    assert call.text == '{"confidence":"HIGH"}'
    # Token counters reflect ONLY the second attempt — first-attempt
    # accumulators must have been reset by the retry loop, not double-
    # counted with the malformed-attempt's 7/3.
    assert call.tokens_in == 11
    assert call.tokens_out == 22

    # 3. Retry warning fired exactly once with structured fields.
    retry_events = [
        kw for ev, kw in recorder.warnings
        if ev == "claude_code.malformed_json_retry"
    ]
    assert len(retry_events) == 1, (
        f"expected exactly 1 malformed-json retry warning, got "
        f"{len(retry_events)}: {recorder.warnings}"
    )
    fields = retry_events[0]
    assert fields["agent_role"] == agent.agent_role
    assert fields["model"] == agent.model
    # Error string is truncated at 200 chars; just check it contains
    # the diagnostic the parser produced for missing-delimiter.
    assert "delimiter" in fields["error"].lower() or "expecting" in fields["error"].lower()

    # 4. The W2.A exit-1 + W2.A-v2 empty-output warnings must NOT have
    # fired — this is a distinct fingerprint and we shouldn't double-log.
    other_events = [
        ev for ev, _ in recorder.warnings
        if ev in (
            "claude_code.transient_exit1_retry",
            "claude_code.empty_output_retry",
        )
    ]
    assert other_events == []


@pytest.mark.asyncio
async def test_claude_code_no_retry_on_valid_json(monkeypatch):
    """Sanity check: well-formed JSON on the first call must NOT trigger
    the malformed-JSON retry path. Guards against a too-broad signature
    that would silently double cost on every call.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    success_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"text":"ok"}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=11, output_tokens=22),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[success_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly one call — no retry on valid JSON.
    assert captured["n_calls"] == 1
    assert call.text == '{"text":"ok"}'
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.malformed_json_retry"
    ]
    assert retry_events == []


@pytest.mark.asyncio
async def test_claude_code_no_retry_on_pydantic_validation_error(monkeypatch):
    """JSON that PARSES OK but fails pydantic schema validation must NOT
    trigger the malformed-JSON retry path. That's a deterministic schema
    error (wrong shape from the model), not a syntactic flake — retrying
    won't change the schema mismatch, and silently doubling cost on
    every schema error would hide a real bug. The downstream
    `BaseAgent.run` parse will surface the `ValidationError` cleanly.
    """
    from claude_agent_sdk import AssistantMessage, TextBlock

    # Valid JSON but wrong shape for `_Out`: an array, not an object.
    # `JSONDecoder.raw_decode` accepts this (it's valid JSON), but
    # `_Out.model_validate(["not", "a", "dict"])` raises ValidationError.
    bad_shape_stream = [
        AssistantMessage(
            content=[TextBlock(text='["not", "a", "dict"]')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=7, output_tokens=3),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[bad_shape_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    # _call_via_claude_code_inner itself does NOT raise — the
    # validation error surfaces later in BaseAgent.run. So just call
    # and inspect the returned ModelCall + the recorder.
    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly one call — no retry on schema mismatch (deterministic).
    assert captured["n_calls"] == 1
    # The original (bad-shape) text is returned so the downstream
    # parse can surface the ValidationError with its real diagnostic.
    assert call.text == '["not", "a", "dict"]'
    # No malformed-JSON retry fired.
    retry_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.malformed_json_retry"
    ]
    assert retry_events == []


@pytest.mark.asyncio
async def test_claude_code_no_retry_on_malformed_after_first_retry(monkeypatch):
    """`_retried` is SHARED across all three retry signatures (W2.A
    exit-1, W2.A-v2 empty-output, W3b.F malformed-JSON): at most one
    retry per invocation, regardless of which signature fires first.

    Scenario: first call hits the exit-1 flake → retry consumed. The
    retry returns successfully but with malformed JSON. The function
    MUST NOT retry again (would be the 3rd call) — it returns a
    ModelCall with the malformed text, and the downstream
    `_parse_output` surfaces the JSONDecodeError that motivated this
    work (with a clear diagnostic so the operator can see the model
    flaked twice rather than the silently-double-billing alternative).
    """
    from claude_agent_sdk import AssistantMessage, ProcessError, TextBlock

    flake = ProcessError(
        "Command failed with exit code 1",
        exit_code=1,
        stderr="Check stderr output for details",
    )
    # Retry result: parses-as-JSON-fails (missing colon).
    malformed_stream = [
        AssistantMessage(
            content=[TextBlock(text='{"foo": 1 "bar": 2}')],
            model="claude-sonnet-4-6",
        ),
        _make_result_message(input_tokens=5, output_tokens=3),
    ]
    captured = _install_fake_query_with_call_counter(
        monkeypatch, side_effects=[flake, malformed_stream],
    )

    agent = _make_agent()
    recorder = _RecordingLogger()
    agent._log = recorder  # type: ignore[assignment]

    call = await agent._call_via_claude_code_inner(system="sys", user="hi")

    # Exactly two calls — the exit-1 retry consumed the single retry
    # budget, so the subsequent malformed-JSON result does NOT trigger
    # another call.
    assert captured["n_calls"] == 2
    # Malformed text is returned; downstream parse surfaces the
    # JSONDecodeError as the original failure mode.
    assert call.text == '{"foo": 1 "bar": 2}'

    # The exit-1 retry warning fired (the first flake).
    exit1_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.transient_exit1_retry"
    ]
    assert len(exit1_events) == 1
    # The malformed-JSON retry warning did NOT fire — `_retried` was
    # already True by the time the trial parse ran on attempt #2.
    malformed_events = [
        ev for ev, _ in recorder.warnings
        if ev == "claude_code.malformed_json_retry"
    ]
    assert malformed_events == []
