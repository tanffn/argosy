from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.services import youtube_intelligence as service
from argosy.state.models import (
    Base,
    User,
    UserFile,
    YouTubeChannel,
    YouTubeClaim,
    YouTubeClaimEvaluation,
    YouTubeVideo,
)


def _test_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'youtube.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            UserFile.__table__,
            YouTubeChannel.__table__,
            YouTubeVideo.__table__,
            YouTubeClaim.__table__,
            YouTubeClaimEvaluation.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_subscription_is_idempotent_and_exposes_stats(monkeypatch, tmp_path):
    factory = _test_factory(tmp_path)
    monkeypatch.setattr(service, "_factory", lambda: factory)
    monkeypatch.setattr(
        service,
        "resolve_youtube_metadata",
        lambda reference: service.YouTubeMetadata(
            video_id="abcdefghijk",
            title="Example",
            channel_id="UC-example",
            channel_name="Example Finance",
            channel_url="https://youtube.com/channel/UC-example",
        ),
    )

    first = service.subscribe_youtube_source("https://youtu.be/abcdefghijk")
    second = service.subscribe_youtube_source("https://youtu.be/abcdefghijk")

    assert first["id"] == second["id"]
    assert first["enabled"] is True
    assert service.list_youtube_sources()["totals"] == {
        "sources": 1,
        "enabled": 1,
        "videos_ingested": 0,
        "tickers_collected": 0,
        "claims_captured": 0,
        "claims_evaluated": 0,
    }


def test_analysis_persistence_populates_channel_video_claims_and_tickers(monkeypatch, tmp_path):
    factory = _test_factory(tmp_path)
    monkeypatch.setattr(service, "_factory", lambda: factory)
    monkeypatch.setattr(
        service,
        "resolve_youtube_metadata",
        lambda reference: service.YouTubeMetadata(
            video_id="abcdefghijk",
            title="A stock idea",
            channel_id="UC-example",
            channel_name="Example Finance",
            channel_url="https://youtube.com/channel/UC-example",
        ),
    )
    payload = {
        "run_id": "youtube:abcdefghijk:run1",
        "video": {"video_id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk"},
        "claims": {
            "named_tickers": ["NVDA", "not a ticker"],
            "speaker_calls": [{
                "ticker": "NVDA", "direction": "bullish", "horizon_days": 90,
                "statement": "NVDA can outperform over the next quarter.",
            }],
            "claims": [{
                "claim_id": "C1", "timestamp": "00:01:00", "claim_type": "prediction",
                "statement": "NVDA can outperform over the next quarter.",
                "evidence_excerpt": "NVDA can outperform", "named_entities": ["NVDA"],
            }],
            "market_outlook_claims": [{
                "timestamp": "00:02:00", "statement": "Rates will stay high.",
                "evidence_excerpt": "rates stay high",
            }],
        },
        "synthesis": {"ticker_recommendations": [{"ticker": "NVDA", "disposition": "watch"}]},
        "cost_usd": 1.25,
    }

    video_row_id = service.persist_youtube_analysis(payload)
    service.persist_youtube_analysis(payload)
    result = service.list_youtube_sources()

    assert video_row_id > 0
    assert result["totals"]["videos_ingested"] == 1
    assert result["totals"]["tickers_collected"] == 1
    assert result["totals"]["claims_captured"] == 2
    assert result["sources"][0]["stats"]["claims_open"] == 2
    assert result["sources"][0]["stats"]["tickers"] == ["NVDA"]
