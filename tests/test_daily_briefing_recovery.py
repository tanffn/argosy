"""Durable daily review receipts are not notifications, even across restarts."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from argosy.services.chat_advisor.contracts import ChatAnswer, OutboxEvent
from argosy.services.chat_advisor.conversation import GroundedAnswer
from argosy.services.chat_advisor.daily_overview import (
    DailyBriefingAuthor, DailyBriefingReviewer, build_daily_overview,
)
from argosy.services.chat_advisor.store import ChatStore
from argosy.state.chat_models import NotificationOutbox
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
from tests.test_chat_read_team import Agent, PRINCIPAL, Reader, review
from tests.test_discord_advisor_transport import _config, _Conversation, _Dispatcher, _worker_db


@pytest.mark.asyncio
async def test_reviewed_no_action_and_rejected_drafts_have_explicit_receipts():
    good = await build_daily_overview(Reader(), PRINCIPAL, now=datetime.now(UTC),
        answerer=Agent(GroundedAnswer(text="No new action recommended.")), reviewer=Agent(review()))
    assert good.review_status == "accepted"
    assert good.review_trace[0]["review"]["verdict"] == "accept"
    author = Agent(GroundedAnswer(text="unsupported"), GroundedAnswer(text="still unsupported"),
                   GroundedAnswer(text="still unsupported"))
    bad = await build_daily_overview(Reader(), PRINCIPAL, now=datetime.now(UTC),
        answerer=author,
        reviewer=Agent(review("revise", feedback="Wrong facts"), review("revise", feedback="Still wrong"),
                       review("revise", feedback="Still wrong")))
    assert bad.review_status == "failed"
    assert len(bad.review_trace) == 3
    assert bad.review_trace[-1]["review"]["feedback"] == "Still wrong"
    assert '"text":"unsupported"' in author.inputs[1]["review_feedback"]
    assert "Wrong facts" in author.inputs[1]["review_feedback"]


def test_daily_agents_do_not_inherit_exhaustive_chat_editorial_instructions():
    args = dict(question="daily", history="", payload="{}", warnings="", catalog="[]")
    author, *_ = DailyBriefingAuthor(user_id="owner").build_prompt(**args, topic="daily", review_feedback="")
    reviewer, _ = DailyBriefingReviewer(user_id="owner").build_prompt(**args, hints="", draft="", receipts="")
    for prompt in (author, reviewer):
        assert "NO ACTION is a valid outcome" in prompt
        assert "NOT mandatory sections" in prompt
        assert "120 words" in prompt
    assert "not exhaustive coverage" in reviewer


@pytest.mark.asyncio
async def test_draft_budget_lease_restart_and_audit_are_durable(tmp_path, monkeypatch):
    engine, sessions = await _worker_db(tmp_path)
    worker = DiscordAdvisorWorker(_config(), "test", FakeGateway(), session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation, dispatcher_factory=_Dispatcher)
    await worker.initialize()
    store = next(iter(worker._stores.values()))
    now = datetime(2026, 9, 27, 10, tzinfo=UTC)
    monkeypatch.setattr("argosy.services.chat_advisor.store._now", lambda: now)
    try:
        key = "overview:2026-09-27"
        claims = await asyncio.gather(*(store.claim_daily_draft(key, "v1") for _ in range(3)))
        assert sum(c is not None for c in claims) == 1
        first = next(c for c in claims if c)
        restarted = ChatStore(sessions, store.household_user_id, binding_id=store.binding_id)
        assert await restarted.claim_daily_draft(key, "v2") is None
        assert await store.finish_daily_draft(first, ChatAnswer("failed", review_status="failed",
            review_trace=[{"review": {"feedback": "unsupported", "path": "/tmp/source.txt",
                                      "secret": "api_key=redacted"}}]))
        now += timedelta(minutes=29)
        assert await restarted.claim_daily_draft(key, "v2") is None
        now += timedelta(minutes=2)
        second = await restarted.claim_daily_draft(key, "v2")
        now += timedelta(minutes=6)  # crashed worker's lease expired
        third = await restarted.claim_daily_draft(key, "v3")
        assert not await store.finish_daily_draft(second, ChatAnswer("old", review_status="accepted"))
        assert await restarted.finish_daily_draft(third, ChatAnswer("No action.", review_status="accepted"))
        ready = await restarted.claim_daily_draft(key, "v3")
        assert ready["status"] == "draft_ready" and ready["text"] == "No action."
        assert await restarted.claim_daily_draft(key, "v4") is None  # no reset on material churn
        assert await restarted.claim_daily_draft("overview:2026-09-28", "v4") is not None
        assert await store.latest_unsent_outbox(key) is None
        assert await store.latest_sent_outbox(key) is None
        assert not await store.pending_outbox()
        other = ChatStore(sessions, "other", binding_id=store.binding_id)
        assert not await other.finish_daily_draft(third, ChatAnswer("foreign", review_status="accepted"))
        async with sessions() as session:
            rows = list((await session.execute(select(NotificationOutbox))).scalars())
            failed = next(row for row in rows if row.status == "draft_failed")
            assert json.loads(failed.body)["review_trace"][0]["review"]["feedback"] == "unsupported"
            assert json.loads(failed.body)["review_trace"][0]["review"]["path"] == "[LOCAL PATH REDACTED]"
            assert json.loads(failed.body)["review_trace"][0]["review"]["secret"] == "api_key=[REDACTED]"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_daily_projection_exception_does_not_block_critical_alert(tmp_path, monkeypatch):
    engine, sessions = await _worker_db(tmp_path)
    class Producer:
        def current_events(self, principal):
            return [OutboxEvent("risk", "v", "alert.critical", "Portfolio risk")]
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(_config(), "test", gateway, session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=Producer())
    calls = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 27, 10, tzinfo=UTC)
    monkeypatch.setattr("argosy.transport.discord_advisor.worker.datetime", Clock)
    async def broken(*args):
        calls.append(True)
        raise ValueError("broken receipt")
    monkeypatch.setattr(worker, "_daily_event", broken)
    try:
        await worker.initialize()
        await worker.pump_notifications()
        assert calls == [True]
        assert len(gateway.sent) == 1 and "Portfolio risk" in gateway.sent[0]["text"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unreviewed", "failed", "accepted"])
async def test_only_accepted_briefings_reach_delivery_and_legacy_failure_is_suppressed(tmp_path, monkeypatch, status):
    engine, sessions = await _worker_db(tmp_path)
    class Producer:
        async def daily_overview(self, principal, *, now):
            return ChatAnswer("No action recommended.", review_status=status)
    worker = DiscordAdvisorWorker(_config(), "test", FakeGateway(), session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=Producer())
    monkeypatch.setattr(worker, "in_quiet_hours", lambda: False)
    try:
        await worker.initialize()
        store = next(iter(worker._stores.values()))
        event = OutboxEvent("overview:2026-09-27", "v", "overview.daily", "**Argosy**\nplaceholder")
        legacy = await store.enqueue_outbox(OutboxEvent(event.semantic_key, "v:old", event.category, "Internal review failed"))
        assert await worker._daily_event(event, PRINCIPAL, store, ("1", "2", "3")) is None
        await asyncio.gather(*worker._daily_drafts.values())
        result = await worker._daily_event(event, PRINCIPAL, store, ("1", "2", "3"))
        if status == "accepted":
            assert "No action recommended" in result.body
            assert ":brief-v2:" in result.material_version
            assert len(result.material_version) <= 64
        else:
            assert result is None
        async with sessions() as session:
            assert (await session.get(NotificationOutbox, legacy.id)).status == "suppressed"
    finally:
        await engine.dispose()
