"""Stage 1 pipeline orchestrator tests.

Sprint commit #13 of the plan/execute/monitor reorg. Exercises
``run_news_ingest`` end-to-end against a file-backed SQLite DB, with a
mock HTTP fetcher (because ``responses`` is not in the project deps).
Validates persistence, idempotency on (source, source_ref), and the
raw_text / evidence_excerpt round-trip.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.news_ingest import run_news_ingest
from argosy.state.models import Base, NewsSignal

_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


# A minimal RSS 2.0 fixture — title + description + pubDate per item.
# Two distinct items so we can verify dedup behavior.
_FAKE_RSS_TWO_ITEMS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>NVDA News</title>
    <item>
      <title>Nvidia $NVDA beat earnings expectations</title>
      <link>https://example.com/n/1</link>
      <description>Record revenue and strong guidance.</description>
      <pubDate>Mon, 25 May 2026 14:00:00 GMT</pubDate>
    </item>
    <item>
      <title>FOMC decision looms for NVDA holders</title>
      <link>https://example.com/n/2</link>
      <description>Rate path uncertain ahead of FOMC.</description>
      <pubDate>Tue, 26 May 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def session_factory(tmp_path):
    """File-backed SQLite session factory with Base.metadata.create_all."""
    db_path = tmp_path / "news_ingest.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    # Enable FK enforcement to mimic prod.
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield SF
    finally:
        engine.dispose()


def _fake_fetcher(_url: str) -> bytes:
    """Return the same two-item RSS payload for every URL — simulates
    Yahoo Finance returning items for the requested ticker."""
    return _FAKE_RSS_TWO_ITEMS


def test_run_news_ingest_persists_rss_items(session_factory) -> None:
    """Mock RSS returning 2 items → 2 rows in news_signals."""
    with session_factory() as s:
        result = run_news_ingest(
            s,
            tickers=["NVDA"],
            rss_fetcher=_fake_fetcher,
            now=_NOW,
            # Empty horizon so the macro feed contributes 0 rows and
            # the assertion focuses on the RSS path.
            macro_within_days=0,
        )
        s.commit()

        rows = s.query(NewsSignal).filter_by(source="rss").all()
        assert len(rows) == 2
        assert result.by_source["rss"] == 2
        assert result.persisted == 2
        assert result.duplicates == 0


def test_run_news_ingest_idempotent_no_duplicates(session_factory) -> None:
    """Re-run with same items → no duplicate rows; duplicates counted."""
    with session_factory() as s:
        run_news_ingest(
            s, tickers=["NVDA"], rss_fetcher=_fake_fetcher,
            now=_NOW, macro_within_days=0,
        )
        s.commit()

        # Second run — should see both items as duplicates.
        second = run_news_ingest(
            s, tickers=["NVDA"], rss_fetcher=_fake_fetcher,
            now=_NOW, macro_within_days=0,
        )
        s.commit()

        rows = s.query(NewsSignal).filter_by(source="rss").all()
        assert len(rows) == 2  # still 2, not 4
        assert second.persisted == 0
        assert second.duplicates == 2


def test_run_news_ingest_persists_evidence_excerpt_and_raw_text(
    session_factory,
) -> None:
    """Persisted row carries raw_text verbatim and evidence_excerpt
    is <=280 chars derived from title+description."""
    with session_factory() as s:
        run_news_ingest(
            s, tickers=["NVDA"], rss_fetcher=_fake_fetcher,
            now=_NOW, macro_within_days=0,
        )
        s.commit()

        row = (
            s.query(NewsSignal)
            .filter_by(source_ref="https://example.com/n/1")
            .one()
        )
        # raw_text contains BOTH title and description
        assert "Nvidia $NVDA beat earnings expectations" in row.raw_text
        assert "Record revenue and strong guidance." in row.raw_text
        # evidence_excerpt is the cleaned version, capped at 280
        assert 1 <= len(row.evidence_excerpt) <= 280
        # Ticker extraction worked
        assert "NVDA" in json.loads(row.parsed_tickers)
        # Sentiment derived from "beat" / "strong" / "record" / "guidance"
        # — at minimum "beat" + "strong" + "record" outweigh anything
        # negative.
        assert row.sentiment == "positive"


def test_run_news_ingest_macro_feed_persists_high_trust(session_factory) -> None:
    """Macro feed contributes high-trust rows in the forward window."""
    with session_factory() as s:
        result = run_news_ingest(
            s,
            tickers=None,  # skip RSS
            now=_NOW,
            macro_within_days=60,  # captures the 2026-06-17 FOMC
        )
        s.commit()

        rows = s.query(NewsSignal).filter_by(source="macro_feed").all()
        assert len(rows) >= 1
        assert all(r.source_trust == "high" for r in rows)
        assert result.by_source["macro_feed"] >= 1
        # Discord is dormant — 0 rows expected.
        assert result.by_source["discord"] == 0


def test_run_news_ingest_dedupe_uses_source_ref_unique_index(
    session_factory,
) -> None:
    """The dedup key is (source, source_ref). Same source_ref under a
    different source must NOT collide (it's a tuple index)."""
    with session_factory() as s:
        # First insert an RSS row with a particular source_ref.
        run_news_ingest(
            s, tickers=["NVDA"], rss_fetcher=_fake_fetcher,
            now=_NOW, macro_within_days=0,
        )
        s.commit()
        rss_count_before = s.query(NewsSignal).filter_by(source="rss").count()

        # Now add a macro_feed row with a deliberately-overlapping
        # source_ref string — should NOT be rejected because the source
        # differs.
        from argosy.state.models import NewsSignal as NS
        s.add(NS(
            source="macro_feed",
            source_ref="https://example.com/n/1",
            received_at=_NOW,
            parsed_tickers="[]",
            event_keywords="[]",
            sentiment="neutral",
            source_trust="high",
            evidence_excerpt="manual macro row",
            raw_text="manual macro row",
        ))
        s.commit()

        assert s.query(NS).filter_by(source="rss").count() == rss_count_before
        assert s.query(NS).filter_by(source="macro_feed").count() == 1
