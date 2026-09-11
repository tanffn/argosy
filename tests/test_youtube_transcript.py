from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import argosy.services.youtube_analysis as analysis_mod
from argosy.agents.base import ModelCall
from argosy.agents.youtube_analysis import (
    YouTubeClaimsAgent,
    YouTubePortfolioAgent,
    YouTubeSkepticAgent,
    YouTubeSynthesisAgent,
)
from argosy.services.youtube_transcript import (
    TranscriptSegment,
    YouTubeTranscript,
    YouTubeTranscriptError,
    extract_video_id,
    fetch_transcript,
    parse_vtt,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, AuditLog, User, UserFile


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("G6xFyw49OgA", "G6xFyw49OgA"),
        ("https://www.youtube.com/watch?v=G6xFyw49OgA&t=10", "G6xFyw49OgA"),
        ("https://youtu.be/G6xFyw49OgA?si=abc", "G6xFyw49OgA"),
        ("https://youtube.com/shorts/G6xFyw49OgA", "G6xFyw49OgA"),
        ("https://www.youtube-nocookie.com/embed/G6xFyw49OgA", "G6xFyw49OgA"),
    ],
)
def test_extract_video_id(reference: str, expected: str) -> None:
    assert extract_video_id(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "https://youtube.com.evil.example/watch?v=G6xFyw49OgA",
        "https://youtube.com/watch?v=too-short",
        "not a video",
    ],
)
def test_extract_video_id_rejects_invalid_references(reference: str) -> None:
    with pytest.raises(YouTubeTranscriptError):
        extract_video_id(reference)


def test_auto_backend_falls_back_to_ytdlp() -> None:
    calls: list[str] = []

    def failed_api(video_id: str, languages: tuple[str, ...]) -> YouTubeTranscript:
        calls.append(f"api:{video_id}:{','.join(languages)}")
        raise RuntimeError("captions endpoint changed")

    def working_ytdlp(video_id: str, languages: tuple[str, ...]) -> YouTubeTranscript:
        calls.append(f"yt-dlp:{video_id}:{','.join(languages)}")
        return _transcript()

    result = fetch_transcript(
        "G6xFyw49OgA",
        languages=("en", "he"),
        api_fetcher=failed_api,
        ytdlp_fetcher=working_ytdlp,
    )

    assert result.backend == "test"
    assert calls == ["api:G6xFyw49OgA:en,he", "yt-dlp:G6xFyw49OgA:en,he"]


def test_parse_vtt_preserves_cue_timestamps_and_cleans_markup() -> None:
    raw = """WEBVTT

00:00:01.000 --> 00:00:03.500 align:start position:0%
<c>Revenue grew &amp; margins expanded.</c>

00:01:02.250 --> 00:01:04.000
Second cue
continues here.
"""
    segments = parse_vtt(raw)

    assert segments == [
        TranscriptSegment(
            text="Revenue grew & margins expanded.", start=1.0, duration=2.5
        ),
        TranscriptSegment(text="Second cue continues here.", start=62.25, duration=1.75),
    ]


def test_ytdlp_rolling_caption_deduplication() -> None:
    from argosy.services.youtube_transcript import _dedupe_rolling_segments

    raw = [
        TranscriptSegment(text="welcome back", start=0.0, duration=1.0),
        TranscriptSegment(text="welcome back to the show", start=1.0, duration=1.0),
        TranscriptSegment(text="to the show today", start=2.0, duration=1.0),
    ]
    assert [segment.text for segment in _dedupe_rolling_segments(raw)] == [
        "welcome back",
        "to the show",
        "today",
    ]


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_fetch_and_catalog_uses_real_catalog_and_audit_seam(
    client_with_db, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_session = client_with_db.app.state.session_factory()
    try:
        sync_session.add(User(id="youtube-user", plan="free"))
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()
    monkeypatch.setattr(analysis_mod, "fetch_transcript", lambda *args, **kwargs: _transcript())

    transcript, cataloged = await analysis_mod.fetch_and_catalog_transcript(
        "G6xFyw49OgA", user_id="youtube-user"
    )

    assert transcript.video_id == "G6xFyw49OgA"
    assert cataloged is not None
    assert cataloged.source == "youtube_transcript"
    async with db_mod.get_session() as session:
        file_row = await session.get(UserFile, cataloged.id)
        audit_rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.user_id == "youtube-user",
                    AuditLog.event_type == "provenance.upload.cataloged",
                )
            )
        ).scalars().all()
    assert file_row is not None
    assert len(audit_rows) == 1


class _StubCallMixin:
    payload: dict

    async def _call_model(self, **_: object) -> ModelCall:
        return ModelCall(
            text=json.dumps(self.payload),
            tokens_in=100,
            tokens_out=50,
            model="test-model",
        )


class _ClaimsStub(_StubCallMixin, YouTubeClaimsAgent):
    payload = {
        "overview": "The speaker presents a bullish semiconductor thesis.",
        "author_thesis": "AI demand will support NVDA growth.",
        "claims": [
            {
                "claim_id": "C1",
                "timestamp": "00:00:00",
                "claim_type": "prediction",
                "statement": "NVDA demand will grow.",
                "evidence_excerpt": "NVDA demand will keep growing",
                "named_entities": ["NVDA"],
                "verification_priority": "high",
            }
        ],
        "market_outlook_claims": [
            {
                "timestamp": "00:00:00",
                "topic": "sector_or_factor",
                "statement": "AI demand will support the semiconductor sector.",
                "evidence_excerpt": "AI demand will keep growing",
                "potential_portfolio_relevance": "Shared factor across current holdings.",
                "verification_priority": "high",
            }
        ],
        "named_tickers": ["NVDA"],
        "themes": ["AI demand"],
        "author_uncertainties": [],
        "cited_sources": ["youtube:G6xFyw49OgA:transcript"],
        "confidence": "LOW",
    }


class _SkepticStub(_StubCallMixin, YouTubeSkepticAgent):
    payload = {
        "strongest_version_of_thesis": "Demand may remain strong.",
        "source_quality": "weak",
        "findings": [
            {
                "severity": "high",
                "timestamp": "00:00:00",
                "issue": "No primary evidence is supplied.",
                "evidence_excerpt": "will keep growing",
                "why_it_matters": "The forecast is unverified.",
                "verification_test": "Check filings and customer capex.",
            }
        ],
        "missing_counterevidence": ["valuation"],
        "persuasion_or_bias_signals": [],
        "claims_worth_external_verification": ["AI demand growth"],
        "cited_sources": ["youtube:G6xFyw49OgA:transcript"],
        "confidence": "LOW",
    }


class _PortfolioStub(_StubCallMixin, YouTubePortfolioAgent):
    payload = {
        "relevance": "high",
        "implications": [
            {
                "subject": "NVDA",
                "relationship": "held",
                "implication": "The claim touches an existing concentration.",
                "transcript_evidence": "NVDA demand will keep growing",
                "next_step": "Recheck the existing thesis with primary evidence.",
            }
        ],
        "existing_theses_to_recheck": ["NVDA demand"],
        "ignored_as_irrelevant": [],
        "decision_status": "existing_thesis_recheck",
        "cited_sources": [
            "youtube:G6xFyw49OgA:transcript",
            "argosy:ariel:current-portfolio",
        ],
        "confidence": "LOW",
    }


class _SynthesisStub(_StubCallMixin, YouTubeSynthesisAgent):
    payload = {
        "executive_summary": "Relevant idea, thin evidence.",
        "what_the_author_argues": ["AI demand supports NVDA."],
        "credible_takeaways": ["The topic matters to a current holding."],
        "challenged_claims": [
            {
                "claim": "Demand will keep growing.",
                "concern": "No primary evidence in the video.",
                "what_would_resolve_it": "Filings and customer capex data.",
            }
        ],
        "market_outlook": [
            {
                "claim": "AI demand supports semiconductor valuations.",
                "transcript_evidence": "AI demand will keep growing",
                "portfolio_impact": "Could affect several correlated holdings.",
                "external_verification_test": "Check current capex and valuation data.",
            }
        ],
        "portfolio_readthrough": ["Recheck the existing NVDA thesis."],
        "verification_queue": ["Compare with current filings."],
        "bottom_line": "Research further; do not trade from this video alone.",
        "decision_status": "existing_thesis_recheck",
        "cited_sources": [
            "youtube:G6xFyw49OgA:transcript",
            "youtube:G6xFyw49OgA:placeholder:claims",
        ],
        "confidence": "LOW",
    }


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_analysis_runs_real_agent_objects_through_baseagent(
    client_with_db, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_session = client_with_db.app.state.session_factory()
    try:
        if sync_session.get(User, "ariel") is None:
            sync_session.add(User(id="ariel", plan="free"))
            sync_session.commit()
    finally:
        sync_session.close()
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    async def fake_fetch(*_: object, **__: object):
        return _transcript(), None

    monkeypatch.setattr(analysis_mod, "fetch_and_catalog_transcript", fake_fetch)
    monkeypatch.setattr(
        analysis_mod, "_portfolio_context", lambda _: "Current positions:\n- NVDA"
    )

    synthesis = _SynthesisStub(user_id="ariel")
    original_run = synthesis.run

    async def synthesis_run(**kwargs: object):
        claims_source_id = str(kwargs["claims_source_id"])
        synthesis.payload = {
            **synthesis.payload,
            "cited_sources": [
                "youtube:G6xFyw49OgA:transcript",
                claims_source_id,
            ],
        }
        return await original_run(**kwargs)

    synthesis.run = synthesis_run  # type: ignore[method-assign]
    result = await analysis_mod.analyze_youtube(
        "G6xFyw49OgA",
        save=True,
        claims_agent=_ClaimsStub(user_id="ariel"),
        skeptic_agent=_SkepticStub(user_id="ariel"),
        portfolio_agent=_PortfolioStub(user_id="ariel"),
        synthesis_agent=synthesis,
    )

    assert result.synthesis.decision_status == "existing_thesis_recheck"
    assert result.claims.named_tickers == ["NVDA"]
    assert result.claims.market_outlook_claims[0].topic == "sector_or_factor"
    assert result.synthesis.market_outlook[0].portfolio_impact.startswith("Could affect")
    assert len(result.agent_report_ids) == 4
    assert result.cost_usd > 0
    assert result.synthesis.bottom_line.endswith("video alone.")
    assert result.artifact_path is not None and result.artifact_path.is_file()
    assert "## Market outlook" in result.artifact_path.read_text(encoding="utf-8")
    assert result.json_path is not None and result.json_path.is_file()
    async with db_mod.get_session() as session:
        persisted = (
            await session.execute(
                select(AgentReport).where(AgentReport.decision_id == result.run_id)
            )
        ).scalars().all()
    assert {row.agent_role for row in persisted} == {
        "youtube_claims",
        "youtube_skeptic",
        "youtube_portfolio_relevance",
        "youtube_synthesis",
    }


def _transcript() -> YouTubeTranscript:
    return YouTubeTranscript(
        video_id="G6xFyw49OgA",
        url="https://www.youtube.com/watch?v=G6xFyw49OgA",
        language="English",
        language_code="en",
        is_generated=True,
        backend="test",
        segments=(
            TranscriptSegment(
                text="NVDA demand will keep growing", start=0.0, duration=3.0
            ),
        ),
    )
