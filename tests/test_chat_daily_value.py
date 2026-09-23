import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.chat_advisor.contracts import ChatAnswer, Citation, OutboxEvent
from argosy.services.chat_advisor.conversation import GroundedAnswer
from argosy.services.chat_advisor.daily_overview import build_daily_overview
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.services.chat_advisor.read_team import ReadExecutor, ReadRequest, RecordReference
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.state.models import Base, ScanState, User
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
from tests.test_chat_read_team import PRINCIPAL, Agent, Reader, review
from tests.test_discord_advisor_transport import _config, _Conversation, _Dispatcher, _worker_db


@pytest.mark.asyncio
async def test_daily_uses_news_discovery_holdings_actions_and_independent_review():
    reader = Reader()
    author = Agent(GroundedAnswer(text="Nothing worth highlighting in the latest reviewed news or discovery today."))
    reviewer = Agent(review())
    answer = await build_daily_overview(reader, PRINCIPAL, now=datetime.now(UTC),
                                       answerer=author, reviewer=reviewer)
    assert {call[1] for call in reader.calls} == {"news", "discovery", "actions", "holdings"}
    assert len(reviewer.inputs) == 1 and answer.text.startswith("Nothing worth")
    assert next(call[2] for call in reader.calls if call[1] == "actions")["record_id"] == "current_trade_plan"
    assert "publication, ingestion, and review dates" in author.inputs[0]["question"]
    assert "stale" in reviewer.inputs[0]["question"]


@pytest.mark.asyncio
async def test_discovery_is_tenant_scoped_dated_and_exact_reference_readable(tmp_path):
    path = tmp_path / "discovery.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    when = datetime(2026, 9, 20, 10, tzinfo=UTC)
    with Session(engine) as db:
        db.add_all([User(id="owner"), User(id="other")])
        db.flush()
        db.add_all([ScanState(user_id="owner", ticker="XYZ", rank=1, last_fleet_at=when,
                              fleet_json=json.dumps({"thesis": "Watch, not buy"})),
                    ScanState(user_id="owner", ticker="OLD", status="dropped"),
                    ScanState(user_id="other", ticker="PRIVATE", rank=1)])
        db.commit()
    reader = RetrievalService(db_path=path)
    try:
        result = reader.read(PRINCIPAL, "discovery")
        assert [r["ticker"] for r in result.data["candidates"]] == ["XYZ"]
        assert result.data["candidates"][0]["reviewed_at"].startswith("2026-09-20")
        exact = await ReadExecutor(reader, PRINCIPAL).read(ReadRequest(
            ref=RecordReference(record_type="discovery", record_id="XYZ")))
        assert exact.data["candidates"][0]["research"]["thesis"] == "Watch, not buy"
    finally:
        reader.engine.dispose()
        engine.dispose()


def test_chat_keeps_internal_provenance_out_of_reply_but_preserves_public_link():
    citations = [Citation("verdict", "123", "today"),
                 Citation("news", "2", "today", url="https://example.org/news", label="News")]
    text = "\n".join(OutboundFilter.chunk("A concise answer.", citations))
    assert "verdict:123" not in text and "as of" not in text
    assert "https://example.org/news" in text
    assert len(citations) == 2


@pytest.mark.asyncio
async def test_quiet_hours_do_not_launch_draft_and_failed_summary_is_not_no_news(monkeypatch):
    class Store:
        async def latest_sent_outbox(self, key):
            return None
        async def latest_unsent_outbox(self, key):
            return None
    class Producer:
        calls = 0
        async def daily_overview(self, principal, *, now):
            self.calls += 1
            raise RuntimeError("upstream unavailable")
    producer = Producer()
    worker = DiscordAdvisorWorker(_config(), "test", FakeGateway(),
        session_factory=object(), notification_producer=producer)
    event = OutboxEvent("overview:2026-09-22", "version", "overview.daily", "**Argosy**\nplaceholder")
    monkeypatch.setattr(worker, "in_quiet_hours", lambda: True)
    assert await worker._daily_event(event, PRINCIPAL, Store(), ("1", "2", "3")) is None
    assert producer.calls == 0
    monkeypatch.setattr(worker, "in_quiet_hours", lambda: False)
    assert await worker._daily_event(event, PRINCIPAL, Store(), ("1", "2", "3")) is None
    await asyncio.gather(*worker._daily_drafts.values(), return_exceptions=True)
    result = await worker._daily_event(event, PRINCIPAL, Store(), ("1", "2", "3"))
    assert "unavailable" in result.body and "Nothing worth" not in result.body


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_slow_daily_draft_never_blocks_alert_and_is_reused_after_send_failure(tmp_path, monkeypatch, changed):
    engine, sessions = await _worker_db(tmp_path)
    release = asyncio.Event()
    class Producer:
        calls = 0
        version = "1"
        def current_events(self, principal):
            return [OutboxEvent("risk", "1", "alert.critical", "Important portfolio risk"),
                    OutboxEvent("plan", self.version, "inbox.order_sheet", "Current plan")]
        async def daily_overview(self, principal, *, now):
            self.calls += 1
            await release.wait()
            text = "Updated plan; earlier recommendation withdrawn." if self.version == "2" else "Useful reviewed discovery highlight."
            return ChatAnswer(text, [Citation("discovery", "XYZ", "today")])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 22, 12, tzinfo=UTC)
    monkeypatch.setattr("argosy.transport.discord_advisor.worker.datetime", Clock)
    monkeypatch.setattr("argosy.services.chat_advisor.store._now", lambda: Clock.now(UTC))
    producer, gateway = Producer(), FakeGateway()
    async def make_worker():
        worker = DiscordAdvisorWorker(_config(), "test", gateway, session_factory=sessions,
            retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
            dispatcher_factory=_Dispatcher, notification_producer=producer)
        await worker.initialize()
        return worker
    try:
        worker = await make_worker()
        await worker.pump_notifications()
        assert len(gateway.sent) == 1 and "Important portfolio risk" in gateway.sent[0]["text"]
        release.set()
        await asyncio.gather(*worker._daily_drafts.values())
        original_send = gateway.send
        async def fail(*args, **kwargs):
            raise RuntimeError("temporary network failure")
        monkeypatch.setattr(gateway, "send", fail)
        await worker.pump_notifications()
        store = next(iter(worker._stores.values()))
        saved = await store.latest_unsent_outbox("overview:2026-09-22")
        assert saved.status == "pending" and saved.attempts == 1 and "Useful reviewed" in saved.body
        monkeypatch.setattr(gateway, "send", original_send)
        if changed:
            producer.version = "2"  # Current plan superseded while the message was undelivered.
        restarted = await make_worker()
        await restarted.pump_notifications()
        if changed:
            await asyncio.gather(*restarted._daily_drafts.values())
            await restarted.pump_notifications()
            assert producer.calls == 2 and len(gateway.sent) == 2
            assert "earlier recommendation withdrawn" in gateway.sent[-1]["text"]
            assert not any("Useful reviewed" in row["text"] for row in gateway.sent)
            return
        assert producer.calls == 1  # No repeat LLM charge, respects retry_after.
        assert len(gateway.sent) == 1
        await store.mark_outbox_failed(saved.id, "retry", datetime(2020, 1, 1, tzinfo=UTC))
        await restarted.pump_notifications()
        assert len(gateway.sent) == 2 and "Useful reviewed" in gateway.sent[-1]["text"]
        await restarted.pump_notifications()
        assert len(gateway.sent) == 2 and producer.calls == 1
    finally:
        await engine.dispose()
