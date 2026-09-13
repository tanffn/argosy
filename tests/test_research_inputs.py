from datetime import UTC, datetime, timedelta
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.agents.stock_decision import bundle_has_sufficient_evidence, _bundle_lines
from argosy.services.research_inputs import collect_research_inputs, render_research_inputs
from argosy.state.models import Base, User, UserFile, YouTubeChannel, YouTubeVideo, YouTubeClaim, InvestorEvent


def test_shared_inputs_are_scoped_dated_and_exactly_matched():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (
        User, UserFile, YouTubeChannel, YouTubeVideo, YouTubeClaim, InvestorEvent)])
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")])
        db.flush()
        for i, (user, ticker, age, macro) in enumerate([
            ("a", "AI", 1, False), ("a", "AIG", 1, False),
            ("b", "AI", 1, False), ("a", "AI", 100, False),
            ("a", "AI", -1, False), ("a", "", 1, True),
        ]):
            video = YouTubeVideo(user_id=user, youtube_video_id=str(i), url=f"https://youtu.be/{i}",
                                 published_at=now - timedelta(days=age), analyzed_at=now)
            db.add(video)
            db.flush()
            db.add(YouTubeClaim(video_id=video.id, claim_key="C1", claim_type="prediction",
                                statement=f"claim {i}", tickers_json=json.dumps([ticker]),
                                is_market_outlook=int(macro)))
        db.add(InvestorEvent(user_id="a", ticker="AI", source="sec_form4",
                             unique_key="event", headline="Insider purchase", occurred_at=now,
                             ingested_at=now, payload_json='{"url":"https://sec.gov/example"}'))
        db.flush()
        items = collect_research_inputs(db, user_id="a", ticker="ai", now=now)
        assert {i["statement"] for i in items} == {"claim 0", "claim 5", "Insider purchase"}
        assert next(i for i in items if i["statement"] == "claim 5")["scope"] == "market"
        rendered = render_research_inputs(db, user_id="a", ticker="AI")
        assert "unverified claim" in rendered
        assert "claim 0" in _bundle_lines({"research_inputs": rendered})
        assert not bundle_has_sufficient_evidence({"research_inputs": rendered})


def test_news_analyst_exposes_claims_as_source_documents(monkeypatch):
    from argosy.agents.news_analyst import NewsAnalystAgent
    from argosy.services import research_inputs
    calls = []
    def load(**kwargs):
        calls.append(kwargs)
        return "UNTRUSTED_CLAIM_CANARY"
    monkeypatch.setattr(research_inputs, "load_research_inputs", load)
    agent = object.__new__(NewsAnalystAgent)
    agent.user_id = "test-user"
    system, user, sources = agent.build_prompt(tickers=["AI"], news_payload={})
    assert calls == [{"user_id": "test-user", "ticker": "AI"}]
    assert "UNTRUSTED_CLAIM_CANARY" not in system + user
    assert ("research_inputs/AI", "UNTRUSTED_CLAIM_CANARY") in sources
    assert "Corroborate" in system
