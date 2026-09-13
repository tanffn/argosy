from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.state.models import Base, User, AgentReport, UserFile, YouTubeChannel, YouTubeVideo, YouTubeClaim, InvestorEvent
from argosy.state.research_models import ResearchSource, ResearchItem, ResearchClaim, ResearchReviewRequest, ResearchEvidenceUse
from argosy.services.research_catalog import upsert_source, enqueue, complete_item, pending_candidates, record_triage
from argosy.services.research_connectors import parse_feed, holdings_changes, validate_public_url

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    Base.metadata.create_all(engine, tables=[m.__table__ for m in [User, AgentReport, UserFile,
        YouTubeChannel, YouTubeVideo, YouTubeClaim, InvestorEvent, ResearchSource, ResearchItem,
        ResearchClaim, ResearchReviewRequest, ResearchEvidenceUse]])
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")]); db.commit()
    @contextmanager
    def sessions():
        with Session(engine, expire_on_commit=False) as db:
            yield db
    yield sessions
    engine.dispose()


def payload(ticker="XYZ"):
    return {"claims": {"named_tickers": [ticker], "claims": [{"statement": "Revenue will grow", "named_entities": [ticker]}],
                       "speaker_calls": [{"statement": "Revenue will grow", "ticker": ticker, "direction": "bullish", "horizon_days": 180}]},
            "synthesis": {"ticker_recommendations": [{"ticker": ticker, "disposition": "watch", "rationale": "Investigate a concrete catalyst"}]}}


def test_versions_claim_clocks_and_general_review_lane(store):
    with store() as db:
        source = upsert_source(db, user_id="a", name="Analyst", kind="manual", reference="analyst")
        item, created = enqueue(db, source, external_id="one", title="Thesis", url="https://example.com/one", body="original", published_at=NOW, now=NOW)
        assert created
        same, created = enqueue(db, source, external_id="one", title="Thesis", url=item.url, body="original", now=NOW)
        assert not created and same.id == item.id
        complete_item(db, item, payload(), now=NOW)
        revised, created = enqueue(db, source, external_id="one", title="Thesis", url=item.url, body="changed", now=NOW)
        assert created and revised.id != item.id
        db.flush()
        claim = db.scalar(select(ResearchClaim))
        assert claim.due_at.replace(tzinfo=UTC) == NOW + timedelta(days=180)
        candidates = pending_candidates(db, user_id="a", held_tickers=set(), now=NOW)
        assert candidates[0].subject_type == "research"
        assert pending_candidates(db, user_id="b", held_tickers=set(), now=NOW) == []
        record_triage(db, candidates[0], SimpleNamespace(warrants_decision=False, rationale="Not material"), user_id="a", now=NOW)
        db.flush()
        assert pending_candidates(db, user_id="a", held_tickers=set(), now=NOW) == []
        assert item.analysis_json != "{}" and revised.analysis_json == "{}"


def test_shared_daily_budget_and_source_fairness(store, monkeypatch):
    from argosy.services import research_worker as worker
    monkeypatch.setattr(worker, "research_session", store)
    with store() as db:
        for n in range(5):
            source = upsert_source(db, user_id="a", name=str(n), kind="manual", reference=str(n))
            for version in range(2):
                enqueue(db, source, external_id=str(version), title="Stock thesis", url="", body=str(version), now=NOW)
        db.commit()
    claims = [worker.claim_next(user_id="a", now=NOW) for _ in range(4)]
    assert all(claims[:3]) and claims[3] is None
    with store() as db:
        rows = db.scalars(select(ResearchItem).where(ResearchItem.status == "processing")).all()
        assert len({row.source_id for row in rows}) == 3
        assert len(db.scalars(select(ResearchItem).where(ResearchItem.status == "queued")).all()) == 7


def test_all_report_writers_get_supplied_vs_cited_receipts(store):
    with store() as db:
        source = upsert_source(db, user_id="a", name="Source", kind="manual", reference="source")
        item, _ = enqueue(db, source, external_id="x", title="X", url="https://example.com/x", body="thesis", now=NOW)
        common = dict(user_id="a", agent_role="news", model="test", confidence="LOW", created_at=NOW,
                      sources_json=json.dumps([{"source_id": "research_inputs/XYZ", "content": "research:" + item.id}]))
        db.add(AgentReport(**common, response_text="Considered; no material change"))
        db.add(AgentReport(**common, response_text="Cited https://example.com/x"))
        db.flush()
        uses = db.scalars(select(ResearchEvidenceUse).order_by(ResearchEvidenceUse.report_id)).all()
        assert [u.cited for u in uses] == [False, True]


def test_rss_atom_dates_and_13f_share_changes():
    feed = b'<rss><channel><item><title>Stock thesis</title><link>https://example.com/a</link><guid>1</guid><pubDate>Sun, 13 Sep 2026 08:00:00 GMT</pubDate><description>&lt;p&gt;Specific thesis&lt;/p&gt;</description></item></channel></rss>'
    result = parse_feed(feed, "https://example.com/feed")
    assert result[0]["external_id"] == "1"
    assert result[0]["published_at"].year == 2026
    assert "Specific thesis" in result[0]["body"]
    previous = [{"cusip": "001", "name": "One", "shares": 100, "value_usd": 10}]
    current = [{"cusip": "001", "name": "One", "shares": 100, "value_usd": 99},
               {"cusip": "002", "name": "Two", "shares": 20},
               {"cusip": "003", "name": "Option", "shares": 100, "put_call": "PUT"}]
    assert [c["cusip"] for c in holdings_changes(current, previous)] == ["002"]


def test_private_sources_and_xml_entities_rejected():
    with pytest.raises(ValueError):
        validate_public_url("http://127.0.0.1/private")
    with pytest.raises(ValueError):
        validate_public_url("file:///secret")
    with pytest.raises(ValueError):
        parse_feed(b'<!DOCTYPE rss><rss/>', "https://example.com")


def test_due_claim_evaluation_respects_horizon_and_preserves_missing_data(store, monkeypatch):
    from argosy.services import research_impact as impact
    from argosy.services.predictions.evaluator import Bar
    monkeypatch.setattr(impact, "research_session", store)
    with store() as db:
        source = upsert_source(db, user_id="a", name="Source", kind="manual", reference="source")
        item, _ = enqueue(db, source, external_id="x", title="X", url="", body="old thesis", published_at=NOW-timedelta(days=200), now=NOW-timedelta(days=199))
        complete_item(db, item, payload(), now=NOW-timedelta(days=199))
        db.commit()
    assert impact.evaluate_due_claims(user_id="a", now=NOW-timedelta(days=100), price_fetcher=lambda *args:[]) == 0
    assert impact.evaluate_due_claims(user_id="a", now=NOW, price_fetcher=lambda *args:[]) == 0
    def prices(ticker, start, end):
        gain = 120 if ticker == "XYZ" else 110
        return [Bar(start, 100,100,100,100), Bar(end,gain,gain,gain,gain)]
    assert impact.evaluate_due_claims(user_id="a", now=NOW, price_fetcher=prices) == 1
    with store() as db:
        outcome = json.loads(db.scalar(select(ResearchClaim)).outcome_json)
        assert outcome["verdict"] == "correct"
        assert outcome["excess_return_pct"] == pytest.approx(10)

@pytest.mark.asyncio
async def test_youtube_cursor_only_advances_after_all_uploads_queued(store, monkeypatch):
    from argosy.services import research_worker as worker, youtube_intelligence as youtube
    monkeypatch.setattr(worker, 'research_session', store)
    source = dict(id=1, channel_name='Channel', youtube_channel_id='UCtest', enabled=True, last_seen_video_id='old')
    monkeypatch.setattr(youtube, 'list_youtube_sources', lambda **kw: {'sources': [source]})
    feed = [dict(video_id=str(n), title='Stock thesis', published_at=NOW.isoformat()) for n in range(5)]
    monkeypatch.setattr(youtube, '_fetch_feed', lambda _: feed)
    monkeypatch.setattr(youtube, '_video_ingested', lambda *args: False)
    def cursor(*args):
        with store() as db:
            assert len(db.scalars(select(ResearchItem)).all()) == 5
    monkeypatch.setattr(youtube, '_update_poll', cursor)
    assert (await worker.queue_youtube(user_id='a'))['queued'] == 5
    assert (await worker.queue_youtube(user_id='a'))['queued'] == 0


def test_youtube_finish_does_not_deliver_duplicate_recommendations(store, monkeypatch):
    from argosy.services import research_worker as worker, ingest_recommendation_router as router
    monkeypatch.setattr(worker, 'research_session', store)
    with store() as db:
        source = upsert_source(db, user_id='a', name='YT', kind='youtube', reference='UCtest')
        item, _ = enqueue(db, source, external_id='one', title='One', url='', body='claims', now=NOW)
        db.commit()
    def unexpected(*args, **kwargs):
        raise AssertionError('YouTube analysis has already routed this action')
    monkeypatch.setattr(router, 'route_ingest_recommendations', unexpected)
    worker._finish(item.id, payload(), user_id='a')
    with store() as db:
        assert db.get(ResearchItem, item.id).status == 'analyzed'


def test_general_research_bypasses_x10_grader(monkeypatch):
    from argosy.services import high_potential_funnel as funnel
    monkeypatch.setattr(funnel, '_external_rows', lambda *args: [SimpleNamespace(ticker='XYZ',
        nomination_evidence_json=json.dumps({'stream': 'ingest_youtube', 'research_mandate': 'general'}))])
    assert funnel._load_external_candidates('a') == []


def test_company_names_are_retained_without_invented_tickers(store):
    with store() as db:
        source = upsert_source(db, user_id='a', name='Source', kind='manual', reference='names')
        item, _ = enqueue(db, source, external_id='names', title='Names', url='', body='body', now=NOW)
        complete_item(db, item, {'claims': {'named_tickers': ['Microsoft', 'NVIDIA', 'NVDA'],
            'claims': [{'statement': 'Microsoft outlook', 'named_entities': ['Microsoft']}],
            'speaker_calls': [{'statement': 'NVIDIA outlook', 'ticker': 'NVIDIA', 'direction': 'bullish', 'horizon_days': 90}]}}, now=NOW)
        db.flush()
        claims = db.scalars(select(ResearchClaim)).all()
        assert len(claims) == 2 and all(c.ticker is None for c in claims)


def test_replaying_queued_youtube_artifact_reuses_original_item(store, monkeypatch):
    from argosy.services import research_catalog as catalog
    monkeypatch.setattr(catalog, 'research_session', lambda *_: store())
    with store() as db:
        channel = YouTubeChannel(user_id='a', youtube_channel_id='UCtest', channel_name='Channel', channel_url='https://youtube.com/channel/UCtest', enabled=1)
        db.add(channel); db.flush()
        db.add(YouTubeVideo(user_id='a', channel_id=channel.id, youtube_video_id='abcdefghijk', url='https://youtu.be/abcdefghijk', analyzed_at=NOW))
        source = upsert_source(db, user_id='a', name='Channel', kind='youtube', reference='UCtest')
        item, _ = enqueue(db, source, external_id='abcdefghijk', title='Queued', url='', body='', now=NOW)
        original_id = item.id
        db.commit()
    analysis = {**payload(), 'run_id': 'youtube:stable-run', 'video': {'video_id': 'abcdefghijk', 'url': 'https://youtu.be/abcdefghijk'}}
    catalog.index_youtube(analysis, user_id='a')
    catalog.index_youtube(analysis, user_id='a')
    with store() as db:
        items = db.scalars(select(ResearchItem)).all()
        assert len(items) == 1 and items[0].id == original_id
