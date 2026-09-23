from __future__ import annotations

from urllib.error import HTTPError

import pytest
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


@pytest.mark.parametrize("status", [404, 410])
def test_unavailable_rss_uses_verified_public_uploads(monkeypatch, status):
    import yt_dlp

    channel = "UC" + "a" * 22
    observed = {}

    def unavailable(*args, **kwargs):
        raise HTTPError("https://www.youtube.com/feeds/videos.xml", status, "Gone", {}, None)

    class Extractor:
        def __init__(self, options):
            observed["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, *, download):
            observed.update(url=url, download=download)
            return {"channel_id": channel, "entries": [
                {"id": f"{index:011d}", "title": f"Upload {index}"} for index in range(20)
            ]}

    monkeypatch.setattr(service, "urlopen", unavailable)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Extractor)
    rows = service._fetch_feed(channel)
    assert len(rows) == 15
    assert all(row["published_at"] is None for row in rows)
    assert rows[0]["title"] == "Upload 0"
    assert observed["url"] == "https://www.youtube.com/playlist?list=UU" + "a" * 22
    assert observed["download"] is False
    assert observed["options"]["skip_download"] is True
    assert observed["options"]["extract_flat"] is True
    assert observed["options"]["playlistend"] == 15


def test_forbidden_rss_does_not_fall_back(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        raise HTTPError("https://www.youtube.com/feeds/videos.xml", 403, "Forbidden", {}, None)

    monkeypatch.setattr(service, "urlopen", forbidden)
    monkeypatch.setattr(service, "_fetch_public_uploads", lambda channel: calls.append(channel))
    with pytest.raises(HTTPError) as caught:
        service._fetch_feed("UC" + "a" * 22)
    assert caught.value.code == 403
    assert calls == []


@pytest.mark.parametrize("data", [
    {"channel_id": "UC" + "b" * 22, "entries": []},
    {"channel_id": "UC" + "a" * 22, "entries": [{"id": "invalid"}]},
])
def test_public_uploads_rejects_unverified_identity(monkeypatch, data):
    import yt_dlp

    class Extractor:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, *args, **kwargs):
            return data

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Extractor)
    with pytest.raises(ValueError, match="identity"):
        service._fetch_public_uploads("UC" + "a" * 22)
