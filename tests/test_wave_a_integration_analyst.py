"""Live integration: NewsAnalystAgent end-to-end with caching + citations.

Marked ``@pytest.mark.llm_eval`` — opt-in via ``-m llm_eval``. Hits the
real Anthropic backend (api_key path). Expected cost ~$0.05-0.20 per
run (one Sonnet call with a small news payload + cached boilerplate).

Wave A Task 20: verifies that the analyst family produces the new
telemetry shape end-to-end:

  - ``citations_json`` non-NULL on first call (model cites the news
    document blocks attached via the Citations API).
  - ``cache_creation_tokens > 0`` on first call (BaseAgent.BOILERPLATE_SYSTEM
    is marked ``cache_control: ephemeral`` and written into the cache).
  - The in-memory ``AgentReport`` dataclass values match the persisted
    ``agent_reports`` row when we route the dataclass through the same
    persistence helper used by intake.

Backend: requires ``api_key`` because:
  - The Citations API is only exposed by the direct Anthropic SDK path
    (``_call_via_api_key``); the ``claude_code`` backend silently
    ignores ``sources`` (see ``BaseAgent._call_via_claude_code_inner``).
  - Cache telemetry (``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``) is only populated when the
    Anthropic Messages API responds, which again is the ``api_key``
    backend. The ``claude_code`` ResultMessage usage exposes only
    ``input_tokens`` and ``output_tokens``.

If the configured backend is ``claude_code`` OR no API key is reachable,
the test is SKIPPED — not silently passed.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import _llm_backend_available
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.config import get_settings
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import User


def _api_key_backend_available() -> bool:
    """True iff the api_key backend is configured AND a key is reachable.

    The api_key path is the only one that exercises the Citations API
    and cache telemetry — both required by this test.
    """
    try:
        if get_settings().anthropic.backend != "api_key":
            return False
    except Exception:
        return False
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    # Fall back to the keychain lookup BaseAgent itself uses.
    try:
        from argosy.secrets import get_secret
        return bool(get_secret(get_settings().anthropic.keychain_key_name))
    except Exception:
        return False


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)."
    ),
)
@pytest.mark.skipif(
    not _api_key_backend_available(),
    reason=(
        "This test requires the api_key backend — the claude_code SDK does "
        "not expose Citations or cache telemetry. Set "
        "ARGOSY_ANTHROPIC__BACKEND=api_key and provide ANTHROPIC_API_KEY "
        "(or store the key under the configured keychain entry)."
    ),
)
def test_news_analyst_emits_citations_and_caches(
    alembic_engine_at_head,
) -> None:
    """End-to-end live: NewsAnalystAgent → citations_json + cache_creation_tokens.

    Steps:
      1. Construct the agent for user "ariel" (the dev fixture user).
      2. Fire it on a 3-headline NVDA payload — small enough to keep cost
         in the $0.05-0.20 band, large enough that the model has
         something concrete to cite back to the document block.
      3. Assert in-memory report carries citations + cache_creation > 0.
      4. Persist the report via the same path the rest of the codebase
         uses (the intake CLI helper) and verify the DB row mirrors the
         dataclass.
    """
    # Repoint the async engine at the alembic-fixture DB (same trick the
    # decision-flow live test uses). The fixture set ARGOSY_HOME and
    # reloaded settings, so init_engine() picks up the right URL.
    db_mod.init_engine()

    user_id = "ariel"

    # Tip-of-spear NVDA news payload. Three headlines with distinct
    # angles (chip demand, China export rules, valuation) so the model
    # has multiple anchor points to cite. Bodies are non-trivial so the
    # Citations API has actual prose offsets to attach.
    news_payload = {
        "NVDA": [
            {
                "headline": "Nvidia tops earnings on record AI-chip demand",
                "source": "Reuters",
                "url": "https://reuters.com/2026-05-22-nvda-earnings",
                "summary": (
                    "Nvidia reported Q1 revenue of $44 billion, up 73% "
                    "year-over-year, driven by hyperscaler orders for "
                    "Blackwell-generation GPUs. Data-center segment grew "
                    "84% YoY to $36 billion. Forward guidance was $48 "
                    "billion for Q2, above the Street's $46 billion."
                ),
            },
            {
                "headline": "US tightens China export rules on advanced AI chips",
                "source": "Bloomberg",
                "url": "https://bloomberg.com/2026-05-22-export-rules",
                "summary": (
                    "The Commerce Department issued an updated rule "
                    "broadening the licensing requirement for advanced "
                    "AI accelerators sold into China. Analysts estimate "
                    "the affected revenue at ~$2-3 billion annually "
                    "for Nvidia, primarily in the H-series. Nvidia "
                    "said it is reviewing the rule and expects to "
                    "comply with all applicable export controls."
                ),
            },
            {
                "headline": "Nvidia trades at 38x forward earnings — bull and bear takes",
                "source": "WSJ",
                "url": "https://wsj.com/2026-05-22-nvda-valuation",
                "summary": (
                    "Two sell-side desks reiterated Buy with $180 PTs; "
                    "one downgraded to Hold citing the China rule "
                    "tightening and execution risk on the next-gen "
                    "Rubin platform. Forward P/E of 38x is above the "
                    "5-year average of 32x but below the AI-leaders "
                    "peer median of 42x."
                ),
            },
        ],
    }

    async def _setup_and_run():
        # Seed the user row (FK target for agent_reports).
        async with db_mod.get_session() as sess:
            sess.add(User(id=user_id, plan="free"))
            await sess.commit()

        agent = NewsAnalystAgent(user_id=user_id)
        # Sanity: the citations path must be active for this test to mean
        # anything. If the per-user override flipped it off, fail loudly
        # rather than silently producing citations_json=None.
        assert agent.citations_enabled, (
            "NewsAnalystAgent.citations_enabled is False — the test "
            "expects the document-blocks + Citations API path to fire."
        )

        report = await agent.run(
            tickers=["NVDA"],
            news_payload=news_payload,
            time_window_label="overnight",
        )

        # Persist the in-memory report using the same helper the rest
        # of the codebase uses (cli/intake._persist_agent_report). That
        # keeps this test honest about whether the DB shape matches the
        # dataclass shape for the Wave A telemetry columns.
        from argosy.cli.intake import _persist_agent_report
        await _persist_agent_report(report=report)

        return report

    report = asyncio.run(_setup_and_run())

    # ------------------------------------------------------------------
    # In-memory assertions
    # ------------------------------------------------------------------

    # 1. Citations populated. The Anthropic Citations API attaches
    #    CitationCharLocation entries to text blocks when the model cites
    #    a document block. BaseAgent._call_via_api_key flattens these
    #    into a JSON list with source_id + cited_quote + claim_text.
    assert report.citations_json is not None, (
        "report.citations_json is None — the model did not cite any "
        "document blocks. Check that sources were threaded through "
        "_call_via_api_key (document blocks emitted in messages_payload)."
    )
    citations = json.loads(report.citations_json)
    assert isinstance(citations, list) and len(citations) > 0, (
        f"citations_json parsed but empty: {report.citations_json!r}"
    )
    # Each entry must carry source_id + cited_quote so downstream
    # auditors can render claim->source spans.
    for c in citations:
        assert "source_id" in c, f"citation missing source_id: {c}"
        assert "cited_quote" in c, f"citation missing cited_quote: {c}"
    # At least one citation should reference our NVDA news document.
    source_ids = {c.get("source_id") for c in citations}
    assert any(sid and "news/NVDA" in sid for sid in source_ids), (
        f"None of the citations point at the news/NVDA document block. "
        f"source_ids observed: {source_ids}"
    )

    # 2. Cache telemetry. The boilerplate system block is marked
    #    cache_control: ephemeral in _build_system_blocks. On the first
    #    call against a fresh prefix the SDK reports the boilerplate
    #    tokens under cache_creation_input_tokens.
    assert report.cache_creation_tokens > 0, (
        f"cache_creation_tokens is {report.cache_creation_tokens} — "
        "expected > 0 on the first call (boilerplate write to cache)."
    )

    # Cost sanity — should be small but non-zero on the api_key backend.
    assert report.cost_usd > 0, (
        f"cost_usd is {report.cost_usd} — expected > 0 on api_key backend."
    )

    print(
        f"\nIn-memory report: role={report.agent_role} model={report.model} "
        f"tokens_in={report.tokens_in} tokens_out={report.tokens_out} "
        f"cache_creation={report.cache_creation_tokens} "
        f"cache_read={report.cache_input_tokens} "
        f"citations={len(citations)} cost_usd=${report.cost_usd:.4f}"
    )

    # ------------------------------------------------------------------
    # DB-row mirror check
    # ------------------------------------------------------------------

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    with SessionLocal() as s:
        latest = s.execute(
            sa.select(AgentReportRow)
            .where(AgentReportRow.agent_role == "news")
            .where(AgentReportRow.user_id == user_id)
            .order_by(sa.desc(AgentReportRow.id))
            .limit(1)
        ).scalar_one()

        assert latest.citations_json == report.citations_json, (
            "DB citations_json drifted from the in-memory dataclass."
        )
        assert latest.cache_creation_tokens == report.cache_creation_tokens, (
            f"DB cache_creation_tokens={latest.cache_creation_tokens} != "
            f"dataclass.cache_creation_tokens={report.cache_creation_tokens}"
        )
        assert latest.cache_input_tokens == report.cache_input_tokens, (
            f"DB cache_input_tokens={latest.cache_input_tokens} != "
            f"dataclass.cache_input_tokens={report.cache_input_tokens}"
        )
        assert latest.thinking_tokens == report.thinking_tokens
        assert latest.tokens_in == report.tokens_in
        assert latest.tokens_out == report.tokens_out
        # cost_usd is stored as Numeric(12,6); compare loosely.
        assert abs(float(latest.cost_usd) - report.cost_usd) < 1e-4
        print(
            f"DB row id={latest.id}: citations_json len="
            f"{len(latest.citations_json or '')} cache_creation="
            f"{latest.cache_creation_tokens} model={latest.model}"
        )
