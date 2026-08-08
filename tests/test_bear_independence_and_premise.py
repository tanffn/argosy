"""Regression: bear independence + premise check (TRLV scar) — fix iter 1.

These tests must FAIL against the unfixed behaviour:
  (a) self-attested ``independent`` without a tool-use URL is downgraded
  (b) fabricated URL is caught by the hallucination detector (no WebSearch
      capability exemption)
  (c) premise says pending + bear independently retrieves already_happened
      → disagreement reaches the facilitator (not suppressed)
  (d) premise-check failure → explicit unverified state blocks green_light
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.base import (
    AgentReport,
    ConfidenceBand,
    ModelCall,
    collect_tool_retrieved_urls_from_sdk_message,
    normalize_url_for_match,
    urls_match,
)
from argosy.agents.fund_manager import FundManagerAgent
from argosy.agents.premise_check import PremiseCheckAgent
from argosy.agents.researcher import (
    BearResearcherAgent,
    BullResearcherAgent,
    CitedPoint,
    ResearcherTurn,
    claim_has_independent_http_cite,
    detect_premise_disagreements,
    derive_point_independence,
    missing_catalyst_status_claims_reason,
)
from argosy.agents.researcher_facilitator import DebateOutcome, ResearcherFacilitatorAgent
from argosy.agents.risk_facilitator import RiskFacilitatorAgent
from argosy.agents.risk_officer import RiskOfficerAgent
from argosy.agents.trader import TraderAgent
from argosy.decisions.flow import BlockedProposal, DecisionFlow, FlowConfig
from argosy.decisions.tiers import Tier
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow, DecisionRun, User


# ---------------------------------------------------------------------------
# B1 — real SDK extractor (NOT injected tool_retrieved_urls)
# ---------------------------------------------------------------------------
# These call collect_tool_retrieved_urls_from_sdk_message against the installed
# claude_agent_sdk.types shapes. They MUST fail if the extractor again scans
# AssistantMessage text or ToolUseBlock.input, OR if it rejects the SDK's
# real success shape (is_error defaulting to None / unset).


def test_extractor_accepts_sdk_default_success_without_is_error() -> None:
    """SDK success shape: ToolResultBlock WITHOUT is_error → URL retrieved.

    The installed SDK defaults ``is_error`` to ``None`` and only sets True
    on failure. Constructing ``is_error=False`` is NOT how the SDK looks.
    FAILS if unset is_error is treated as non-success (silent empty).
    """
    from claude_agent_sdk.types import ToolResultBlock, UserMessage

    ok = "https://ir.trulieve.com/news-releases/q2-2026"
    # Do NOT pass is_error — mirrors real SDK construction.
    block = ToolResultBlock(
        tool_use_id="tu_ok",
        content=f"Page title… body mentions {ok} and quotes.",
    )
    assert block.is_error is None  # canary: SDK default
    msg = UserMessage(content=[block])
    got = collect_tool_retrieved_urls_from_sdk_message(msg)
    assert ok in got


def test_extractor_canary_realistic_webfetch_user_message_nonempty() -> None:
    """Canary against silent-empty: realistic successful web-fetch UserMessage.

    FAILS if a future SDK shape change blinds the extractor — must go red
    rather than quietly returning [].
    """
    from claude_agent_sdk.types import ToolResultBlock, UserMessage

    url = (
        "https://www.dea.gov/press-releases/2026/04/23/"
        "schedule-iii-marijuana-rescheduling"
    )
    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_web_1",
                content=(
                    f"WebFetch result for {url}\n"
                    "<title>DEA announces Schedule III</title>\n"
                    "Medical marijuana moved to Schedule III effective "
                    "2026-04-23. Full text at the URL above."
                ),
                # is_error omitted on purpose — SDK default None
            )
        ]
    )
    got = collect_tool_retrieved_urls_from_sdk_message(msg)
    assert got, (
        "silent-empty canary: successful web-fetch UserMessage yielded no "
        "retrieved URLs — extractor is blind to the live SDK shape"
    )
    assert any(urls_match(u, url) or url in u for u in got)


def test_extractor_ignores_url_only_in_assistant_text() -> None:
    """URL written only in the model's answer is NOT retrieved.

    FAILS if collect_tool_retrieved_urls_from_sdk_message scans AssistantMessage
    TextBlock (the original laundering path).
    """
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    fake = "https://nowhere.example/fabricated-in-answer"
    msg = AssistantMessage(
        content=[TextBlock(text=f"Revenue down 10% per {fake}.")],
        model="claude-opus-4-8",
    )
    assert collect_tool_retrieved_urls_from_sdk_message(msg) == []


def test_extractor_ignores_url_in_failed_fetch_tool_use_and_error_result() -> None:
    """URL in ToolUseBlock.input + failed ToolResultBlock is NOT retrieved.

    FAILS if ToolUseBlock.input (request) or is_error=True results count as
    retrieved — a model can fabricate a URL, attempt fetch, fail, and laundry.
    """
    from claude_agent_sdk.types import (
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    fake = "https://nowhere.example/fabricated-fetch-target"
    # Request side — never a result.
    ask = AssistantMessage(
        content=[
            ToolUseBlock(
                id="tu_fail",
                name="WebFetch",
                input={"url": fake},
            )
        ],
        model="claude-opus-4-8",
    )
    assert collect_tool_retrieved_urls_from_sdk_message(ask) == []

    # Failed result that still echoes the fabricated URL in error text.
    fail = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="tu_fail",
                content=f"Error: could not fetch {fake}",
                is_error=True,
            )
        ]
    )
    assert collect_tool_retrieved_urls_from_sdk_message(fail) == []


def test_url_normalization_near_misses_still_match() -> None:
    """Trailing slash / utm / http↔https / host case must not silently downgrade.

    FAILS under exact-string equality matching.
    """
    base = "https://ir.example.com/news/release"
    assert urls_match(base, base + "/")
    assert urls_match(base, base + "?utm_source=x&utm_medium=y")
    assert urls_match(base, "http://IR.EXAMPLE.COM/news/release")
    assert urls_match(base, "https://ir.example.com/news/release/")
    assert normalize_url_for_match(base + "/") == normalize_url_for_match(base)
    # Independence derivation must use the same normalisation.
    assert (
        derive_point_independence(
            [base + "?utm_campaign=x"],
            tool_retrieved_urls=[base + "/"],
        )
        == "independent"
    )


def test_url_normalization_preserves_meaningful_query_and_fragment() -> None:
    """Non-tracking params / fragments must NOT collapse distinct resources.

    FAILS if ``ref`` is dropped or fragments are stripped unconditionally —
    a fabricated ``?ref=456`` could then match a retrieved ``?ref=123``.
    """
    a = "https://docs.example.com/article?ref=123"
    b = "https://docs.example.com/article?ref=456"
    assert not urls_match(a, b)
    spa_a = "https://app.example.com/docs#/filings/10-k"
    spa_b = "https://app.example.com/docs#/filings/10-q"
    assert not urls_match(spa_a, spa_b)
    # Tracking params still collapse.
    assert urls_match(
        "https://ir.example.com/x?utm_source=tw",
        "https://ir.example.com/x",
    )


# Shared payload understated the decline (live TRLV shape).
_TRLV_WRONG_PAYLOAD = {
    "agent_role": "fundamentals",
    "per_ticker": {
        "TRLV": {
            "revenue_growth_yoy": -0.0139,
            "notes": "Top-line roughly flat YoY per payload.",
            "cited_sources": ["fundamentals/TRLV"],
        }
    },
    "summary": (
        "TRLV revenue_growth_yoy is -1.39%. Bull thesis: Schedule III "
        "rescheduling will remove 280E — pending regulatory coin-flip."
    ),
    "cited_sources": ["fundamentals/TRLV"],
}

_TRLV_IR_URL = (
    "https://ir.trulieve.com/news-releases/news-release-details/"
    "trulieve-reports-second-quarter-2026-financial-results"
)
_DEA_URL = "https://www.dea.gov/press-releases/2026/04/23/schedule-iii"


def _mock(cls, canned: dict, *, tool_retrieved_urls: list[str] | None = None):
    urls = list(tool_retrieved_urls or [])

    class _M(cls):  # type: ignore[misc, valid-type]
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=120,
                tokens_out=180,
                model=self.model,
                tool_retrieved_urls=urls,
            )

    return _M


# ---------------------------------------------------------------------------
# (a) Independence is derived from tool-use — not self-attested
# ---------------------------------------------------------------------------


def test_self_attested_independent_without_tool_url_is_downgraded() -> None:
    """Claiming independent while citing a URL absent from tool results → shared_payload.

    FAILS if independence is trusted as a model claim (pre-fix behaviour).
    """
    fabricated = "https://nowhere.example/fake-trlv-10pct"
    assert (
        derive_point_independence([fabricated], tool_retrieved_urls=[])
        == "shared_payload"
    )
    assert (
        derive_point_independence(
            [fabricated], tool_retrieved_urls=["https://other.example/x"]
        )
        == "shared_payload"
    )


def test_independent_only_when_cited_url_in_tool_record() -> None:
    assert (
        derive_point_independence(
            [_TRLV_IR_URL], tool_retrieved_urls=[_TRLV_IR_URL]
        )
        == "independent"
    )


@pytest.mark.asyncio
async def test_bear_run_downgrades_fabricated_independent_claim() -> None:
    """End-to-end: canned output claims independent + fake URL; no tool URLs
    → derived independence is shared_payload and sources_json does NOT
    promote the fake URL as retrieved.
    """
    fake = "https://nowhere.example/made-up-10pct"
    canned = {
        "side": "bear",
        "round_index": 1,
        "position_summary": "Revenue down 10%.",
        "points": [
            {
                "claim": "Revenue declined 10% YoY.",
                "evidence": "Invented.",
                "cited_sources": [fake],
                "independence": "independent",  # self-attested — must be ignored
            }
        ],
        "response_to_opposing": "",
        "confidence": "HIGH",
        "cited_sources": [fake],
    }
    # No tool_retrieved_urls — retrieval never happened.
    bear = _mock(BearResearcherAgent, canned, tool_retrieved_urls=[])(user_id="ariel")
    rep = await bear.run(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        prior_rounds=None,
        round_index=1,
        n_max=1,
        ticker="TRLV",
    )
    assert isinstance(rep.output, ResearcherTurn)
    assert rep.output.points[0].independence == "shared_payload"
    # Fabricated URL must not be laundered into sources_json as "retrieved".
    if rep.sources_json:
        ids = {e["source_id"] for e in json.loads(rep.sources_json)}
        assert fake not in ids


@pytest.mark.asyncio
async def test_bear_run_keeps_independent_when_tool_record_has_url() -> None:
    canned = {
        "side": "bear",
        "round_index": 1,
        "position_summary": "Revenue down ~10% YoY per IR — payload's -1.39% is wrong.",
        "points": [
            {
                "claim": "Q2 revenue declined ~10% YoY.",
                "evidence": "Company IR release.",
                "cited_sources": [_TRLV_IR_URL],
                "independence": "shared_payload",  # even wrong claim is overridden
            }
        ],
        "response_to_opposing": "",
        "confidence": "HIGH",
        "cited_sources": [_TRLV_IR_URL],
    }
    bear = _mock(
        BearResearcherAgent, canned, tool_retrieved_urls=[_TRLV_IR_URL]
    )(user_id="ariel")
    rep = await bear.run(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        prior_rounds=None,
        round_index=1,
        n_max=1,
        ticker="TRLV",
    )
    assert rep.output.points[0].independence == "independent"
    assert rep.sources_json is not None
    ids = {e["source_id"] for e in json.loads(rep.sources_json)}
    assert _TRLV_IR_URL in ids


# ---------------------------------------------------------------------------
# (b) Fabricated URL caught by hallucination detector (no capability exemption)
# ---------------------------------------------------------------------------


def test_fabricated_url_flagged_despite_websearch_capability() -> None:
    """FAILS if WebSearch allowlist alone exempts all https URLs."""
    bear = BearResearcherAgent(user_id="ariel")
    assert "WebSearch" in bear.claude_code_allowed_tools
    out = ResearcherTurn(
        side="bear",
        round_index=1,
        position_summary="x",
        points=[
            CitedPoint(
                claim="c",
                evidence="e",
                cited_sources=["https://nowhere.example/fake"],
                independence="independent",
            )
        ],
        cited_sources=["https://nowhere.example/fake"],
    )
    sources = [("fundamentals/TRLV", "revenue_growth_yoy: -0.0139")]
    flagged = bear._detect_hallucinated_sources(
        out, sources, tool_retrieved_urls=[]
    )
    assert "https://nowhere.example/fake" in flagged


def test_tool_retrieved_url_not_flagged() -> None:
    bear = BearResearcherAgent(user_id="ariel")
    out = ResearcherTurn(
        side="bear",
        round_index=1,
        position_summary="x",
        points=[
            CitedPoint(
                claim="c",
                evidence="e",
                cited_sources=[_TRLV_IR_URL],
            )
        ],
        cited_sources=[_TRLV_IR_URL],
    )
    sources = [("fundamentals/TRLV", "revenue_growth_yoy: -0.0139")]
    assert (
        bear._detect_hallucinated_sources(
            out, sources, tool_retrieved_urls=[_TRLV_IR_URL]
        )
        == []
    )


# ---------------------------------------------------------------------------
# (c) Premise pending + bear already_happened disagreement reaches facilitator
# ---------------------------------------------------------------------------


def test_premise_block_is_contestable_not_authoritative() -> None:
    """FAILS if prompt still calls premise check 'authoritative' / ground truth."""
    pending = {
        "status": "ok",
        "ticker": "TRLV",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "Schedule III / 280E relief",
                "status": "pending",
                "as_of": "",
                "evidence": "Still awaiting federal action.",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
        "summary": "Rescheduling still pending.",
        "confidence": "MEDIUM",
        "cited_sources": ["https://example.com/stale"],
    }
    bear = BearResearcherAgent(user_id="ariel")
    result = bear.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        ticker="TRLV",
        premise_status=pending,
    )
    sys, usr, _ = result
    assert "NOT ground truth" in usr or "contestable" in usr.lower()
    assert "authoritative" not in usr.lower()
    assert "challenge" in sys.lower() or "contradict" in sys.lower()

    fac = ResearcherFacilitatorAgent(user_id="ariel")
    fsys, _fusr = fac.build_prompt(
        bull_turns=[], bear_turns=[], rounds_run=1, ticker="TRLV",
        premise_status=pending,
    )
    assert "NOT ground truth" in fsys or "contestable" in fsys.lower()
    assert "disagreement" in fsys.lower()
    # Must not tell facilitator to treat premise as authoritative gospel.
    assert "authoritative" not in fsys.lower()


@pytest.mark.asyncio
async def test_bear_contradiction_of_pending_premise_reaches_facilitator() -> None:
    """Premise check says pending; bear retrieves already_happened evidence.

    Facilitator user prompt must contain the bear's contradiction (not
    suppressed). FAILS if facilitator is told to treat premise as gospel
    and the disagreement is dropped from inputs.
    """
    pending_premise = {
        "status": "ok",
        "ticker": "TRLV",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": (
                    "US federal cannabis rescheduling to Schedule III / "
                    "removal of 280E tax penalty"
                ),
                "status": "pending",
                "as_of": "",
                "evidence": "Still a coin-flip.",
                "cited_sources": ["https://example.com/stale-pending"],
            }
        ],
        "summary": "Schedule III still pending.",
        "confidence": "MEDIUM",
        "cited_sources": ["https://example.com/stale-pending"],
    }
    bear_canned = {
        "side": "bear",
        "round_index": 1,
        "position_summary": (
            "Premise check is wrong: Schedule III already happened 2026-04-23; "
            "280E no longer applies to retained medical."
        ),
        "points": [
            {
                "claim": (
                    "Medical marijuana moved to Schedule III on 2026-04-23 — "
                    "catalyst already_happened, contradicting premise_check pending."
                ),
                "evidence": (
                    "DEA press release 2026-04-23; Trulieve IR applauded and "
                    "deconsolidated adult-use. Premise check said pending."
                ),
                "cited_sources": [_DEA_URL],
                "independence": "independent",
            }
        ],
        "response_to_opposing": "",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [_DEA_URL],
            }
        ],
        "confidence": "HIGH",
        "cited_sources": [_DEA_URL],
    }
    bull_canned = {
        "side": "bull",
        "round_index": 1,
        "position_summary": "Pending Schedule III is the upside.",
        "points": [
            {
                "claim": "280E relief still pending.",
                "evidence": "Premise check status=pending.",
                "cited_sources": ["fundamentals/TRLV"],
                "independence": "shared_payload",
            }
        ],
        "response_to_opposing": "",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "pending",
                "cited_sources": ["fundamentals/TRLV"],
            }
        ],
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/TRLV"],
    }

    bear = _mock(
        BearResearcherAgent, bear_canned, tool_retrieved_urls=[_DEA_URL]
    )(user_id="ariel")
    bull = _mock(BullResearcherAgent, bull_canned)(user_id="ariel")

    bear_rep = await bear.run(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        premise_status=pending_premise,
        ticker="TRLV",
        round_index=1,
        n_max=1,
    )
    bull_rep = await bull.run(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        premise_status=pending_premise,
        ticker="TRLV",
        round_index=1,
        n_max=1,
    )
    assert bear_rep.output.points[0].independence == "independent"
    assert "already_happened" in bear_rep.output.position_summary.lower() or (
        "already" in bear_rep.output.points[0].claim.lower()
    )

    captured: dict[str, str] = {}

    class _CapFac(ResearcherFacilitatorAgent):
        async def _call_model(self, *, system: str, user: str, **_e: Any) -> ModelCall:
            captured["user"] = user
            captured["system"] = system
            return ModelCall(
                text=json.dumps(
                    {
                        "winning_side": "bear",
                        "synthesis": (
                            "Bear independently showed Schedule III already "
                            "happened; premise_check pending is contested."
                        ),
                        "cited_evidence": [bear_rep.output.points[0].claim],
                        "rounds_run": 1,
                        "confidence": "HIGH",
                        "cited_sources": [_DEA_URL],
                    }
                ),
                tokens_in=10,
                tokens_out=10,
                model=self.model,
            )

    fac = _CapFac(user_id="ariel")
    await fac.run(
        bull_turns=[bull_rep.output.model_dump()],
        bear_turns=[bear_rep.output.model_dump()],
        rounds_run=1,
        ticker="TRLV",
        premise_status=pending_premise,
    )
    # Disagreement survives into facilitator input.
    assert "already_happened" in captured["user"].lower() or "2026-04-23" in captured["user"]
    assert _DEA_URL in captured["user"] or "Schedule III" in captured["user"]
    assert "pending" in captured["user"].lower()  # premise still visible
    assert "disagreement" in captured["system"].lower() or "contestable" in captured["system"].lower()


def test_structural_premise_disagreement_detectable_without_facilitator_prose() -> None:
    """Disagreement is keyed by stable premise_id — not label similarity.

    FAILS under fuzzy label matching OR when omitted claims silently yield [].
    """
    pending_premise = {
        "status": "ok",
        "ticker": "TRLV",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "as_of": "",
                "evidence": "Still awaiting federal action.",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
        "summary": "Rescheduling still pending.",
        "confidence": "MEDIUM",
        "cited_sources": ["https://example.com/stale"],
    }
    # Near-miss label in prose; claim keys by premise_id only.
    bear_turns = [
        {
            "side": "bear",
            "round_index": 1,
            "position_summary": (
                "DEA completed rescheduling on 2026-04-23; the pending "
                "framing in the premise check is obsolete."
            ),
            "points": [
                {
                    "claim": "DEA completed rescheduling on 2026-04-23.",
                    "evidence": "DEA press release.",
                    "cited_sources": [_DEA_URL],
                    "independence": "independent",
                }
            ],
            "catalyst_status_claims": [
                {
                    "premise_id": "p0",
                    "status": "already_happened",
                    "cited_sources": [_DEA_URL],
                }
            ],
            "cited_sources": [_DEA_URL],
            "tool_retrieved_urls": [_DEA_URL],
        }
    ]
    found = detect_premise_disagreements(
        pending_premise, bear_turns=bear_turns, bull_turns=[]
    )
    assert found, (
        "canary: populated turn with genuine conflict must yield non-empty "
        "disagreement list"
    )
    assert any("already_happened" in d and "pending" in d for d in found)
    assert any("premise_id=p0" in d for d in found)

    # Omitted field → surfaced as missing (not silent empty disagreements).
    omitted = {"side": "bear", "points": [], "cited_sources": [_DEA_URL]}
    assert "catalyst_status_claims" not in omitted
    reason = missing_catalyst_status_claims_reason(
        pending_premise, bear_turns=[omitted], bull_turns=[]
    )
    assert reason is not None
    assert "omitted" in reason.lower() or "empty" in reason.lower()

    # Empty list → also missing (never answered), not "no disagreement".
    empty_claims = {
        "side": "bear",
        "catalyst_status_claims": [],
        "points": [],
        "cited_sources": [],
    }
    assert missing_catalyst_status_claims_reason(
        pending_premise, bear_turns=[empty_claims], bull_turns=[]
    )

    # Null (parsed omission under default=None) → silence, not agreement.
    null_claims = {
        "side": "bear",
        "catalyst_status_claims": None,
        "points": [],
        "cited_sources": [],
    }
    null_reason = missing_catalyst_status_claims_reason(
        pending_premise, bear_turns=[null_claims], bull_turns=[]
    )
    assert null_reason is not None
    assert "null" in null_reason.lower() or "silence" in null_reason.lower()

    # Two DEA near-miss labels resolve via the SAME premise_id.
    alt_label_turn = [
        {
            "side": "bear",
            "catalyst_status_claims": [
                {
                    "premise_id": "p0",
                    "status": "already_happened",
                    "cited_sources": [_DEA_URL],
                }
            ],
            "points": [],
            "cited_sources": [_DEA_URL],
            "tool_retrieved_urls": [_DEA_URL],
        }
    ]
    # Premise uses "DEA rescheduling"; claim does not restate label at all.
    assert detect_premise_disagreements(
        {
            **pending_premise,
            "premises": [
                {
                    **pending_premise["premises"][0],
                    "catalyst": "DEA reschedule decision",
                }
            ],
        },
        bear_turns=alt_label_turn,
        bull_turns=[],
    )

    # Two distinct drug catalysts stay distinct via different premise_ids.
    two_drugs = {
        "status": "ok",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "FDA approval decision drug A",
                "status": "pending",
                "cited_sources": ["https://example.com/a"],
            },
            {
                "premise_id": "p1",
                "catalyst": "FDA approval decision drug B",
                "status": "pending",
                "cited_sources": ["https://example.com/b"],
            },
        ],
    }
    # Claim only disagrees on drug A (p0) — must not fabricate disagreement on B.
    only_a = detect_premise_disagreements(
        two_drugs,
        bear_turns=[
            {
                "side": "bear",
                "catalyst_status_claims": [
                    {
                        "premise_id": "p0",
                        "status": "already_happened",
                        "cited_sources": ["https://example.com/a"],
                    },
                    {
                        "premise_id": "p1",
                        "status": "pending",
                        "cited_sources": ["https://example.com/b"],
                    },
                ],
                "points": [],
                "cited_sources": [],
                "tool_retrieved_urls": [
                    "https://example.com/a",
                    "https://example.com/b",
                ],
            }
        ],
        bull_turns=[],
    )
    assert len(only_a) == 1
    assert "drug A" in only_a[0]
    assert "drug B" not in only_a[0]

    # Unknown premise_id is a parse/surface failure, not a silent skip.
    unknown = missing_catalyst_status_claims_reason(
        pending_premise,
        bear_turns=[
            {
                "side": "bear",
                "catalyst_status_claims": [
                    {
                        "premise_id": "p99",
                        "status": "already_happened",
                        "cited_sources": [_DEA_URL],
                    }
                ],
                "points": [],
                "cited_sources": [],
            }
        ],
        bull_turns=[],
    )
    assert unknown is not None
    assert "unknown" in unknown.lower() or "p99" in unknown

    # Facilitator omission does not suppress validated disagreements —
    # the validated set is authoritative (replacement, not merge).
    outcome = DebateOutcome(
        winning_side="bull",
        synthesis="Bull carries on shared payload; ignoring the DEA cite.",
        cited_evidence=["shared"],
        premise_disagreements=[],
        rounds_run=1,
        confidence=ConfidenceBand.MEDIUM,
        cited_sources=["fundamentals/TRLV"],
    )
    from argosy.agents.researcher import authoritative_premise_disagreements

    authoritative = authoritative_premise_disagreements(
        found, list(outcome.premise_disagreements or [])
    )
    outcome = outcome.model_copy(update={"premise_disagreements": authoritative})
    assert outcome.premise_disagreements

    trader = TraderAgent(user_id="ariel")
    _sys, usr = trader.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        debate_outcome=outcome.model_dump(),
        positions_snapshot="{}",
        user_constraints="",
        tier="T1",
        ticker="TRLV",
        premise_status=pending_premise,
    )
    assert "PREMISE DISAGREEMENTS" in usr
    assert any(d in usr for d in outcome.premise_disagreements)


def test_uncited_status_claim_not_promoted_to_structural_disagreement() -> None:
    """Uncited already_happened claim must NOT become a structural disagreement.

    FAILS if detect_premise_disagreements promotes claims with empty
    cited_sources — that elevates an unsourced assertion to the trader.
    """
    pending = {
        "status": "ok",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
    }
    turn = {
        "side": "bear",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [],  # uncited
            }
        ],
        "cited_sources": [_DEA_URL],  # top-level cite must NOT count
        "tool_retrieved_urls": [_DEA_URL],
    }
    assert detect_premise_disagreements(
        pending, bear_turns=[turn], bull_turns=[]
    ) == []
    assert not claim_has_independent_http_cite(
        turn["catalyst_status_claims"][0], turn["tool_retrieved_urls"]
    )


def test_unretrieved_url_status_claim_not_promoted() -> None:
    """Claim citing a URL never fetched must NOT be promoted.

    FAILS if claim URLs skip the tool-retrieved ground truth (laundering).
    """
    pending = {
        "status": "ok",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
    }
    fabricated = "https://nowhere.example/never-fetched"
    turn = {
        "side": "bear",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [fabricated],
            }
        ],
        "cited_sources": [fabricated],
        "tool_retrieved_urls": [],  # never retrieved
    }
    assert detect_premise_disagreements(
        pending, bear_turns=[turn], bull_turns=[]
    ) == []
    # Even with unrelated retrievals, fabricated claim URL must not match.
    turn["tool_retrieved_urls"] = ["https://other.example/irrelevant"]
    assert detect_premise_disagreements(
        pending, bear_turns=[turn], bull_turns=[]
    ) == []


def test_independently_retrieved_status_claim_is_promoted() -> None:
    """Claim citing a genuinely retrieved URL IS promoted — mechanism still fires.

    FAILS if the promotion gate becomes a block-everything no-op.
    """
    pending = {
        "status": "ok",
        "premises": [
            {
                "premise_id": "p0",
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
    }
    turn = {
        "side": "bear",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [_DEA_URL],
            }
        ],
        "cited_sources": [_DEA_URL],
        "tool_retrieved_urls": [_DEA_URL],
    }
    found = detect_premise_disagreements(
        pending, bear_turns=[turn], bull_turns=[]
    )
    assert found, "independently retrieved conflict must promote"
    assert any("already_happened" in d and "pending" in d for d in found)
    assert claim_has_independent_http_cite(
        turn["catalyst_status_claims"][0], turn["tool_retrieved_urls"]
    )
    # Near-miss URL normalisation still counts as retrieved.
    turn_norm = {
        **turn,
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [_DEA_URL + "/"],
            }
        ],
        "tool_retrieved_urls": [_DEA_URL + "?utm_source=x"],
    }
    assert detect_premise_disagreements(
        pending, bear_turns=[turn_norm], bull_turns=[]
    )


def test_facilitator_only_disagreement_does_not_reach_trader_as_structural() -> None:
    """Facilitator-authored disagreement with no validated claim is filtered out.

    FAILS if flow merges facilitator premise_disagreements unchecked —
    that reopens the citation-gate bypass.
    """
    from argosy.agents.researcher import authoritative_premise_disagreements

    validated: list[str] = []
    fac = [
        "bear says Schedule III already_happened (uncited) — ignore premise_check"
    ]
    out = authoritative_premise_disagreements(validated, fac)
    assert out == []

    trader = TraderAgent(user_id="ariel")
    _sys, usr = trader.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        debate_outcome={
            "winning_side": "bear",
            "synthesis": "x",
            "cited_evidence": [],
            "premise_disagreements": out,
            "rounds_run": 1,
            "confidence": "HIGH",
            "cited_sources": ["x"],
        },
        positions_snapshot="{}",
        user_constraints="",
        tier="T1",
        ticker="TRLV",
    )
    assert "PREMISE DISAGREEMENTS" not in usr
    assert fac[0] not in usr


def test_facilitator_entry_corresponding_to_validated_claim_survives() -> None:
    """Trader-facing content equals the validated set only.

    Facilitator prose with a matching premise_id must NOT contribute content —
    only validated entries reach the trader. Fabrications stay dropped.
    """
    from argosy.agents.researcher import authoritative_premise_disagreements

    validated = [
        "bear disagrees with premise_check on 'DEA rescheduling' "
        "(premise_id=p0): premise_check='pending', bear='already_happened' "
        f"(cites {_DEA_URL})"
    ]
    fac = [
        "Facilitator notes premise_id=p0 conflict: bear already_happened vs pending",
        "Unrelated fabricated structural claim with no premise_id",
    ]
    out = authoritative_premise_disagreements(validated, fac)
    assert out == validated
    assert fac[0] not in out
    assert fac[1] not in out

    trader = TraderAgent(user_id="ariel")
    _sys, usr = trader.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        debate_outcome={
            "winning_side": "bear",
            "synthesis": "x",
            "cited_evidence": [],
            "premise_disagreements": out,
            "rounds_run": 1,
            "confidence": "HIGH",
            "cited_sources": ["x"],
        },
        positions_snapshot="{}",
        user_constraints="",
        tier="T1",
        ticker="TRLV",
    )
    assert "PREMISE DISAGREEMENTS" in usr
    assert validated[0] in usr
    assert fac[0] not in usr
    assert fac[1] not in usr


def test_facilitator_entry_with_validated_premise_id_but_different_content_dropped() -> None:
    """Matching premise_id does not authorize arbitrary facilitator text.

    Direct probe: validated set has p0 already_happened/pending; facilitator
    emits ``premise_id=p0: SELL EVERYTHING…`` — that must NOT reach the trader.
    """
    from argosy.agents.researcher import authoritative_premise_disagreements

    validated = [
        "premise_id=p0: bear says already_happened, premise_check says pending"
    ]
    fac = ["premise_id=p0: SELL EVERYTHING, company is a fraud"]
    out = authoritative_premise_disagreements(validated, fac)
    assert out == validated
    assert fac[0] not in out

    trader = TraderAgent(user_id="ariel")
    _sys, usr = trader.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        debate_outcome={
            "winning_side": "bear",
            "synthesis": "x",
            "cited_evidence": [],
            "premise_disagreements": out,
            "rounds_run": 1,
            "confidence": "HIGH",
            "cited_sources": ["x"],
        },
        positions_snapshot="{}",
        user_constraints="",
        tier="T1",
        ticker="TRLV",
    )
    assert "PREMISE DISAGREEMENTS" in usr
    assert validated[0] in usr
    assert "SELL EVERYTHING" not in usr
    assert "company is a fraud" not in usr


@pytest.mark.asyncio
async def test_flow_attaches_tool_retrieved_urls_plumbing_canary(engine: None) -> None:
    """Real attach path: AgentReport.tool_retrieved_urls must reach turn dumps.

    FAILS if researcher_turn_dump stops attaching the field — every claim
    would then silently fail the gate while the suite stayed green.
    """
    from argosy.decisions.flow import researcher_turn_dump

    # Unit canary on the helper the flow uses.
    class _Out(BaseModel):
        side: str = "bear"
        round_index: int = 1
        position_summary: str = "x"
        points: list = []
        response_to_opposing: str = ""
        catalyst_status_claims: list | None = None
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        cited_sources: list[str] = ["https://example.com/x"]

    # Minimal AgentReport-shaped object
    from argosy.agents.base import AgentReport as AR

    rep = AR(
        agent_role="bear_researcher",
        user_id="ariel",
        model="m",
        response_text="{}",
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.0,
        prompt_hash="h",
        confidence=ConfidenceBand.MEDIUM,
        output=_Out(),
        tool_retrieved_urls=[_DEA_URL],
    )
    dump = researcher_turn_dump(rep)
    assert "tool_retrieved_urls" in dump
    assert _DEA_URL in dump["tool_retrieved_urls"]

    # End-to-end: ModelCall.tool_retrieved_urls → report → dump → promotion.
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    premise_ok = {
        "ticker": "TRLV",
        "premises": [
            {
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "as_of": "",
                "evidence": "Awaiting.",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
        "summary": "Pending.",
        "confidence": "MEDIUM",
        "cited_sources": ["https://example.com/stale"],
    }
    bear_body = {
        "side": "bear",
        "round_index": 1,
        "position_summary": "DEA completed rescheduling.",
        "points": [
            {
                "claim": "Rescheduling done.",
                "evidence": "DEA.",
                "cited_sources": [_DEA_URL],
            }
        ],
        "response_to_opposing": "",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "already_happened",
                "cited_sources": [_DEA_URL],
            }
        ],
        "confidence": "HIGH",
        "cited_sources": [_DEA_URL],
    }
    bull_body = {
        "side": "bull",
        "round_index": 1,
        "position_summary": "Still pending.",
        "points": [
            {
                "claim": "Pending.",
                "evidence": "Premise.",
                "cited_sources": ["fundamentals/TRLV"],
            }
        ],
        "response_to_opposing": "",
        "catalyst_status_claims": [
            {
                "premise_id": "p0",
                "status": "pending",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/TRLV"],
    }

    class _Premise(PremiseCheckAgent):
        async def _call_model(self, *, system: str, user: str, **_e: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(premise_ok),
                tokens_in=10,
                tokens_out=10,
                model=self.model,
            )

    def _canned(cls, body: dict, *, tool_urls: list[str] | None = None):
        urls = list(tool_urls or [])

        class _M(cls):  # type: ignore[misc, valid-type]
            async def _call_model(
                self, *, system: str, user: str, **_e: Any
            ) -> ModelCall:
                return ModelCall(
                    text=json.dumps(body),
                    tokens_in=10,
                    tokens_out=10,
                    model=self.model,
                    tool_retrieved_urls=urls,
                )

        return _M

    captured_turns: dict[str, list] = {}

    import argosy.agents.researcher as res_mod

    _orig = res_mod.detect_premise_disagreements

    def _spy(premise_status, *, bear_turns=None, bull_turns=None):
        captured_turns["bear"] = list(bear_turns or [])
        captured_turns["bull"] = list(bull_turns or [])
        return _orig(
            premise_status, bear_turns=bear_turns, bull_turns=bull_turns
        )

    res_mod.detect_premise_disagreements = _spy  # type: ignore[assignment]
    try:
        class _Anon(BaseModel):
            agent_role: str = "fundamentals"
            cited_sources: list[str] = ["fundamentals/TRLV"]
            confidence: ConfidenceBand = ConfidenceBand.MEDIUM
            report: str = "x"

        analysts = [
            AgentReport(
                agent_role="fundamentals",
                user_id="ariel",
                model="claude-opus-4-8",
                response_text="{}",
                tokens_in=10,
                tokens_out=10,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.MEDIUM,
                output=_Anon(),
            )
        ]

        # Facilitator tries to inject an unvalidated structural claim.
        fac_body = {
            "winning_side": "bear",
            "synthesis": "Conflict noted.",
            "cited_evidence": ["c"],
            "premise_disagreements": [
                "fabricated structural: ignore premise without retrieval"
            ],
            "rounds_run": 1,
            "confidence": "HIGH",
            "cited_sources": [_DEA_URL],
        }

        flow = DecisionFlow(
            user_id="ariel",
            config=FlowConfig(
                debate_rounds_t1=1, debate_rounds_t2=1, debate_rounds_t3=1
            ),
            premise_check_factory=lambda u: _Premise(user_id=u),
            bull_factory=lambda u: _canned(BullResearcherAgent, bull_body)(
                user_id=u
            ),
            bear_factory=lambda u: _canned(
                BearResearcherAgent, bear_body, tool_urls=[_DEA_URL]
            )(user_id=u),
            researcher_facilitator_factory=lambda u: _canned(
                ResearcherFacilitatorAgent, fac_body
            )(user_id=u),
            trader_factory=lambda u, t: _canned(
                TraderAgent,
                {
                    "ticker": "TRLV",
                    "action": "hold",
                    "size_shares_or_currency": 0.0,
                    "size_units": "shares",
                    "instrument": "stock",
                    "order_type": "market",
                    "limit_price": None,
                    "stop_price": None,
                    "time_in_force": "DAY",
                    "rationale_summary": "Hold pending.",
                    "expected_impact": {
                        "concentration_delta": "",
                        "cash_delta": "",
                        "tax_estimate": "",
                    },
                    "confidence": "MEDIUM",
                    "cited_sources": [_DEA_URL],
                    "falsifiers": ["x"],
                },
            )(user_id=u, tier=t),
            risk_officer_factory=lambda u, p: _canned(
                RiskOfficerAgent,
                {
                    "perspective": "neutral",
                    "round_index": 1,
                    "verdict": "APPROVE",
                    "conditions": [],
                    "concerns": [],
                    "response_to_opposing": "",
                    "confidence": "MEDIUM",
                    "cited_sources": ["x"],
                },
            )(user_id=u, perspective=p),
            risk_facilitator_factory=lambda u: _canned(
                RiskFacilitatorAgent,
                {
                    "consensus_verdict": "APPROVE",
                    "consolidated_conditions": [],
                    "dissent_summary": "",
                    "rounds_run": 1,
                    "confidence": "MEDIUM",
                    "cited_sources": ["x"],
                },
            )(user_id=u),
            fund_manager_factory=lambda u: _canned(
                FundManagerAgent,
                {
                    "decision": "green_light",
                    "reason": "ok",
                    "required_conditions": [],
                    "post_execution_checks": [],
                    "confidence": "HIGH",
                    "cited_sources": ["x"],
                },
            )(user_id=u),
        )
        outcome = await flow.run(
            ticker="TRLV", tier=Tier.T1, analyst_reports=analysts,
        )
    finally:
        res_mod.detect_premise_disagreements = _orig  # type: ignore[assignment]

    # Plumbing: attach must have delivered the retrieved URL into the spy.
    assert captured_turns.get("bear"), "detect was not called with bear turns"
    assert any(
        _DEA_URL in (t.get("tool_retrieved_urls") or [])
        for t in captured_turns["bear"]
    ), "tool_retrieved_urls missing from flow turn dump — attach broken"

    # Facilitator fabrications must not be the sole structural content.
    do = getattr(outcome, "debate_outcome", None)
    if do is not None:
        fac_only = "fabricated structural: ignore premise without retrieval"
        disagreements = list(getattr(do, "premise_disagreements", None) or [])
        assert fac_only not in disagreements
        # Validated bear conflict should have been promoted via attach.
        assert any("premise_id=p0" in d for d in disagreements)


@pytest.mark.asyncio
async def test_omitted_catalyst_status_claims_surface_premise_unverified(
    engine: None,
) -> None:
    """Omitted catalyst_status_claims with non-empty premises → unverified.

    Uses the REAL production researcher validators (no bypass). Agent no
    longer aborts; DecisionFlow's post-debate gate marks premise_unverified.
    FAILS if agent raises AgentRunError (hard abort) or if gate is skipped.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    premise_ok = {
        "ticker": "TRLV",
        "premises": [
            {
                "catalyst": "DEA rescheduling",
                "status": "pending",
                "as_of": "",
                "evidence": "Awaiting.",
                "cited_sources": ["https://example.com/stale"],
            }
        ],
        "summary": "Pending.",
        "confidence": "MEDIUM",
        "cited_sources": ["https://example.com/stale"],
    }

    class _Premise(PremiseCheckAgent):
        async def _call_model(self, *, system: str, user: str, **_e: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(premise_ok),
                tokens_in=10,
                tokens_out=10,
                model=self.model,
            )

    # Canned JSON omits catalyst_status_claims → parses as None under
    # production schema (default=None). Production agent cite gate still runs.
    bull_body = {
        "side": "bull",
        "round_index": 1,
        "position_summary": "Buy.",
        "points": [
            {
                "claim": "c",
                "evidence": "e",
                "cited_sources": ["fundamentals/TRLV"],
            }
        ],
        "response_to_opposing": "",
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/TRLV"],
    }
    assert "catalyst_status_claims" not in bull_body
    bear_body = {
        "side": "bear",
        "round_index": 1,
        "position_summary": "Sell.",
        "points": [
            {
                "claim": "c",
                "evidence": "e",
                "cited_sources": ["fundamentals/TRLV"],
            }
        ],
        "response_to_opposing": "",
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/TRLV"],
    }
    assert "catalyst_status_claims" not in bear_body

    class _Anon(BaseModel):
        agent_role: str = "fundamentals"
        cited_sources: list[str] = ["fundamentals/TRLV"]
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        report: str = "x"

    analysts = [
        AgentReport(
            agent_role="fundamentals",
            user_id="ariel",
            model="claude-opus-4-8",
            response_text="{}",
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
            prompt_hash="h",
            confidence=ConfidenceBand.MEDIUM,
            output=_Anon(),
        )
    ]

    def _canned(cls, body: dict):
        """Production agent subclasses — no _validate_citations bypass."""

        class _M(cls):  # type: ignore[misc, valid-type]
            async def _call_model(
                self, *, system: str, user: str, **_e: Any
            ) -> ModelCall:
                return ModelCall(
                    text=json.dumps(body),
                    tokens_in=10,
                    tokens_out=10,
                    model=self.model,
                )

        return _M

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(debate_rounds_t1=1, debate_rounds_t2=1, debate_rounds_t3=1),
        premise_check_factory=lambda u: _Premise(user_id=u),
        bull_factory=lambda u: _canned(BullResearcherAgent, bull_body)(user_id=u),
        bear_factory=lambda u: _canned(BearResearcherAgent, bear_body)(user_id=u),
        researcher_facilitator_factory=lambda u: _canned(
            ResearcherFacilitatorAgent,
            {
                "winning_side": "bull",
                "synthesis": "Buy.",
                "cited_evidence": ["c"],
                "rounds_run": 1,
                "confidence": "MEDIUM",
                "cited_sources": ["fundamentals/TRLV"],
            },
        )(user_id=u),
        trader_factory=lambda u, t: _canned(
            TraderAgent,
            {
                "ticker": "TRLV",
                "action": "buy",
                "size_shares_or_currency": 10.0,
                "size_units": "shares",
                "instrument": "stock",
                "order_type": "market",
                "limit_price": None,
                "stop_price": None,
                "time_in_force": "DAY",
                "rationale_summary": "Buy.",
                "expected_impact": {
                    "concentration_delta": "",
                    "cash_delta": "",
                    "tax_estimate": "",
                },
                "confidence": "HIGH",
                "cited_sources": ["fundamentals/TRLV"],
                "falsifiers": ["x"],
            },
        )(user_id=u, tier=t),
        risk_officer_factory=lambda u, p: _canned(
            RiskOfficerAgent,
            {
                "perspective": "neutral",
                "round_index": 1,
                "verdict": "APPROVE",
                "conditions": [],
                "concerns": [],
                "response_to_opposing": "",
                "confidence": "MEDIUM",
                "cited_sources": ["x"],
            },
        )(user_id=u, perspective=p),
        risk_facilitator_factory=lambda u: _canned(
            RiskFacilitatorAgent,
            {
                "consensus_verdict": "APPROVE",
                "consolidated_conditions": [],
                "dissent_summary": "",
                "rounds_run": 1,
                "confidence": "MEDIUM",
                "cited_sources": ["x"],
            },
        )(user_id=u),
        fund_manager_factory=lambda u: _canned(
            FundManagerAgent,
            {
                "decision": "green_light",
                "reason": "ok",
                "required_conditions": [],
                "post_execution_checks": [],
                "confidence": "HIGH",
                "cited_sources": ["x"],
            },
        )(user_id=u),
    )

    outcome = await flow.run(
        ticker="TRLV", tier=Tier.T1, analyst_reports=analysts,
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "premise_unverified"
    assert "catalyst_status_claims" in outcome.reason.lower() or (
        "unverified" in outcome.reason.lower()
    )


# ---------------------------------------------------------------------------
# (d) Premise-check failure → unverified blocks green_light (does not abort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premise_check_failure_blocks_green_light_not_abort(
    engine: None,
) -> None:
    """FAILS if premise failure aborts the flow OR silently allows buy."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _BoomPremise(PremiseCheckAgent):
        async def run(self, **inputs: Any) -> AgentReport:  # type: ignore[override]
            raise RuntimeError("simulated claude.exe exit-1 burst")

    class _Anon(BaseModel):
        agent_role: str = "fundamentals"
        cited_sources: list[str] = ["fundamentals/TRLV"]
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        report: str = "pending Schedule III"

    analysts = [
        AgentReport(
            agent_role="fundamentals",
            user_id="ariel",
            model="claude-opus-4-8",
            response_text="{}",
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
            prompt_hash="h",
            confidence=ConfidenceBand.MEDIUM,
            output=_Anon(),
        )
    ]

    def _canned(cls, body: dict):
        class _M(cls):  # type: ignore[misc, valid-type]
            async def _call_model(
                self, *, system: str, user: str, **_e: Any
            ) -> ModelCall:
                return ModelCall(
                    text=json.dumps(body),
                    tokens_in=10,
                    tokens_out=10,
                    model=self.model,
                )

        return _M

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(debate_rounds_t1=1, debate_rounds_t2=1, debate_rounds_t3=1),
        premise_check_factory=lambda u: _BoomPremise(user_id=u),
        bull_factory=lambda u: _canned(
            BullResearcherAgent,
            {
                "side": "bull",
                "round_index": 1,
                "position_summary": "Buy.",
                "points": [
                    {
                        "claim": "c",
                        "evidence": "e",
                        "cited_sources": ["fundamentals/TRLV"],
                        "independence": "shared_payload",
                    }
                ],
                "response_to_opposing": "",
                "confidence": "MEDIUM",
                "cited_sources": ["fundamentals/TRLV"],
            },
        )(user_id=u),
        bear_factory=lambda u: _canned(
            BearResearcherAgent,
            {
                "side": "bear",
                "round_index": 1,
                "position_summary": "Sell.",
                "points": [
                    {
                        "claim": "c",
                        "evidence": "e",
                        "cited_sources": ["fundamentals/TRLV"],
                        "independence": "shared_payload",
                    }
                ],
                "response_to_opposing": "",
                "confidence": "MEDIUM",
                "cited_sources": ["fundamentals/TRLV"],
            },
        )(user_id=u),
        researcher_facilitator_factory=lambda u: _canned(
            ResearcherFacilitatorAgent,
            {
                "winning_side": "bull",
                "synthesis": "Buy.",
                "cited_evidence": ["c"],
                "rounds_run": 1,
                "confidence": "MEDIUM",
                "cited_sources": ["fundamentals/TRLV"],
            },
        )(user_id=u),
        trader_factory=lambda u, t: _canned(
            TraderAgent,
            {
                "ticker": "TRLV",
                "action": "buy",
                "size_shares_or_currency": 10.0,
                "size_units": "shares",
                "instrument": "stock",
                "order_type": "market",
                "limit_price": None,
                "stop_price": None,
                "time_in_force": "DAY",
                "rationale_summary": "Buy TRLV.",
                "expected_impact": {
                    "concentration_delta": "",
                    "cash_delta": "",
                    "tax_estimate": "",
                },
                "confidence": "HIGH",
                "cited_sources": ["fundamentals/TRLV"],
                "falsifiers": [
                    "Thesis still frames Schedule III as pending when already fired."
                ],
            },
        )(user_id=u, tier=t),
        risk_officer_factory=lambda u, p: _canned(
            RiskOfficerAgent,
            {
                "perspective": "neutral",
                "round_index": 1,
                "verdict": "APPROVE",
                "conditions": [],
                "concerns": [],
                "response_to_opposing": "",
                "confidence": "MEDIUM",
                "cited_sources": ["x"],
            },
        )(user_id=u, perspective=p),
        risk_facilitator_factory=lambda u: _canned(
            RiskFacilitatorAgent,
            {
                "consensus_verdict": "APPROVE",
                "consolidated_conditions": [],
                "dissent_summary": "",
                "rounds_run": 1,
                "confidence": "MEDIUM",
                "cited_sources": ["x"],
            },
        )(user_id=u),
        fund_manager_factory=lambda u: _canned(
            FundManagerAgent,
            {
                "decision": "green_light",
                "reason": "ok",
                "required_conditions": [],
                "post_execution_checks": [],
                "confidence": "HIGH",
                "cited_sources": ["x"],
            },
        )(user_id=u),
    )

    outcome = await flow.run(
        ticker="TRLV", tier=Tier.T1, analyst_reports=analysts,
    )
    # Must complete (not raise) and block green_light explicitly.
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "premise_unverified"
    assert "unverified" in outcome.reason.lower()

    async with db_mod.get_session() as session:
        run = await session.get(DecisionRun, outcome.decision_run_id)
        assert run is not None
        assert run.notes_json is not None
        notes = json.loads(run.notes_json)
        assert notes["premise_check"]["status"] == "unverified"
        assert notes["premise_check"]["blocks_green_light"] is True
        # Debate still ran (inspectable) — bull/bear reports present.
        roles = (
            await session.execute(
                select(AgentReportRow.agent_role).where(
                    AgentReportRow.decision_id == str(outcome.decision_run_id)
                )
            )
        ).scalars().all()
    assert "bull_researcher" in roles
    assert "bear_researcher" in roles


def test_bear_prompt_requires_websearch_and_challenge() -> None:
    bear = BearResearcherAgent(user_id="ariel")
    assert "WebSearch" in bear.claude_code_allowed_tools
    sys, usr, sources = bear.build_prompt(
        analyst_reports=[_TRLV_WRONG_PAYLOAD],
        ticker="TRLV",
    )
    assert "INDEPENDENT RETRIEVAL" in sys
    assert "PREMISE CHECK CHALLENGE" in sys
    assert any(s[0].startswith("fundamentals/") for s in sources)
