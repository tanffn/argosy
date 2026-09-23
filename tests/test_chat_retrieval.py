from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.retrieval import RetrievalService, create_read_only_engine
from argosy.state.models import (
    Base,
    NewsSignal,
    PortfolioSnapshotRow,
    User,
    Verdict,
    YouTubeChannel,
)
from argosy.state.research_models import ResearchSource


def _principal(user: str) -> Principal:
    return Principal(user, "g", "c", "u")


def test_exact_ticker_lookup_never_becomes_unrelated_page(tmp_path):
    path = tmp_path / "verdict-selector.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")])
        db.flush()
        db.add_all([Verdict(user_id="a", subject="CSPX", verdict="HOLD", conviction="HIGH"),
                    Verdict(user_id="b", subject="SPCX", verdict="BUY", conviction="HIGH")])
        db.commit()
    reader = RetrievalService(db_path=path)
    try:
        exact = reader.read(_principal("a"), "verdicts", ticker="SPCX")
        assert exact.data == [] and not exact.citations
        assert "exact ticker SPCX" in exact.warnings[0]
        assert reader.read(_principal("a"), "verdicts", symbol="CSPX").data[0]["subject"] == "CSPX"
        rejected = reader.read(_principal("a"), "verdicts", query="SPCX")
        assert rejected.data is None and not rejected.citations
    finally:
        reader.engine.dispose()
        engine.dispose()


def test_news_reads_public_signals_not_private_discord_or_other_households(tmp_path):
    path = tmp_path / "news.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="a"))
        for source, excerpt in (("rss", "Public earnings headline"), ("discord", "Private channel content")):
            db.add(NewsSignal(source=source, source_ref=f"https://example.test/{source}",
                              received_at=datetime(2026, 9, 19), parsed_tickers='["AAA"]',
                              event_keywords="[]", sentiment="neutral", source_trust="medium",
                              evidence_excerpt=excerpt, raw_text="Raw untrusted text"))
        db.commit()
    reader = RetrievalService(db_path=path)
    try:
        result = reader.read(_principal("a"), "news")
        assert len(result.data["news"]) == 1
        assert result.data["news"][0]["excerpt"] == "Public earnings headline"
        assert result.data["news"][0]["publication_date"] is None
        assert "raw_text" not in result.data["news"][0]
        assert "Ingestion time is not publication time" in result.warnings[0]
    finally:
        reader.engine.dispose()
        engine.dispose()


def test_physical_read_only_and_tenant_scope(tmp_path: Path):
    path = tmp_path / "tenant #1.db"
    rw = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(rw)
    with Session(rw) as db:
        db.add_all([User(id="a"), User(id="b")])
        db.add_all(
            [
                PortfolioSnapshotRow(
                    user_id="a",
                    imported_at=datetime(2026, 1, 1),
                    positions_json='[{"ticker":"AAA"}]',
                ),
                PortfolioSnapshotRow(
                    user_id="b",
                    imported_at=datetime(2026, 1, 2),
                    positions_json='[{"ticker":"BBB"}]',
                ),
            ]
        )
        db.commit()
    ro = create_read_only_engine(path)
    service = RetrievalService(engine=ro)
    result = service.read(_principal("a"), "holdings")
    assert result.data["positions"] == [{"ticker": "AAA"}]
    with ro.connect() as connection, pytest.raises(SQLAlchemyError):
        connection.execute(
            text("INSERT INTO users(id, plan, created_at) VALUES ('x','free',CURRENT_TIMESTAMP)")
        )


def test_limit_filter_is_not_forwarded_twice(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    rw = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(rw)
    with Session(rw) as db:
        db.add(User(id="a"))
        db.commit()
    result = RetrievalService(engine=create_read_only_engine(path)).read(
        _principal("a"), "documents", limit=1, offset=0
    )
    assert result.data == []


def test_source_totals_and_page_walk_reconcile_youtube_mirror(tmp_path: Path):
    path = tmp_path / "sources.sqlite"
    rw = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(rw)
    with Session(rw) as db:
        db.add(User(id="a"))
        db.add_all(
            [
                ResearchSource(
                    user_id="a", name="Alpha", kind="rss", reference="https://a", enabled=True
                ),
                ResearchSource(
                    user_id="a", name="Beta", kind="rss", reference="https://b", enabled=True
                ),
                ResearchSource(
                    user_id="a", name="YT mirror", kind="youtube", reference="UC1", enabled=True
                ),
                YouTubeChannel(
                    user_id="a",
                    youtube_channel_id="UC1",
                    channel_name="Channel One",
                    channel_url="https://youtube/1",
                    enabled=1,
                ),
                YouTubeChannel(
                    user_id="a",
                    youtube_channel_id="UC2",
                    channel_name="Channel Two",
                    channel_url="https://youtube/2",
                    enabled=1,
                ),
            ]
        )
        db.commit()
    service = RetrievalService(engine=create_read_only_engine(path))
    pages = [service.read(_principal("a"), "sources", limit=1, offset=i) for i in range(4)]
    assert all(page.data["totals"]["registered"] == 4 for page in pages)
    assert all(page.data["totals"]["enabled"] == 4 for page in pages)
    keys = [(page.data["page"][0]["registry"], page.data["page"][0]["id"]) for page in pages]
    assert len(set(keys)) == 4


def test_per_tenant_mode_missing_db_fails_closed(monkeypatch):
    monkeypatch.setenv("ARGOSY_TENANCY", "per-tenant")
    service = RetrievalService()
    result = service.read(_principal("definitely-missing-chat-tenant"), "holdings")
    assert result.data is None
    assert "FileNotFoundError" in result.warnings[0]
