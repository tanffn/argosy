from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest

from argosy.services.chat_advisor.contracts import OutboxEvent, Principal
from argosy.services.chat_advisor.conversation import ChatIntent, ConversationService, RouteDecision
from argosy.services.chat_advisor.notification_policy import delivery_events
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.status_board import critical_job_events
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
from tests.test_discord_advisor_transport import _config, _Conversation, _Dispatcher, _worker_db


def test_daily_overview_is_short_and_uses_local_date_not_utc():
    rows = [OutboxEvent(str(i), "v1", "inbox.plan_task", "Very long task " * 50) for i in range(20)]
    result = delivery_events(rows, now=datetime(2026, 9, 19, 21, 30, tzinfo=UTC),
                             timezone="Asia/Jerusalem", overview_time=time(0))
    assert len(result) == 1
    assert result[0].semantic_key == "overview:2026-09-20"
    assert "unavailable" in result[0].body
    assert "follow-ups" not in result[0].body
    assert len(result[0].body) < 300


def test_before_daily_time_only_critical_flags_pass():
    critical = OutboxEvent("c", "v1", "alert.critical", "Critical")
    rows = [OutboxEvent("r", "v1", "inbox.plan_task", "Overdue paperwork"), critical]
    assert delivery_events(rows, now=datetime(2026, 9, 19, 5, tzinfo=UTC),
                           timezone="Asia/Jerusalem", overview_time=time(9)) == [critical]


@pytest.mark.asyncio
async def test_daily_persists_dedup_restart_changes_and_next_day(monkeypatch, tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    now = datetime(2026, 9, 19, 10, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    class Producer:
        events = [OutboxEvent("a", "v1", "inbox.plan_task", "Routine task")]

        def current_events(self, principal):
            return self.events

    monkeypatch.setattr("argosy.transport.discord_advisor.worker.datetime", Clock)
    producer = Producer()
    gateway = FakeGateway()
    cfg = _config()

    async def worker():
        instance = DiscordAdvisorWorker(
            cfg, "test", gateway, session_factory=sessions,
            retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
            dispatcher_factory=_Dispatcher, notification_producer=producer,
        )
        await instance.initialize()
        return instance

    try:
        first = await worker()
        await first.pump_notifications()
        assert len(gateway.sent) == 1
        assert "unavailable" in gateway.sent[0]["text"]
        producer.events.append(OutboxEvent("b", "v2", "inbox.note", "Another change"))
        restarted = await worker()
        await restarted.pump_notifications()
        assert len(gateway.sent) == 1
        assert not gateway.edits
        # Overnight red flags bypass quiet hours, but ordinary digests don't.
        now = datetime(2026, 9, 19, 20, tzinfo=UTC)
        producer.events.append(OutboxEvent("red", "v1", "alert.critical", "Risk alert"))
        await restarted.pump_notifications()
        assert len(gateway.sent) == 2
        assert gateway.sent[-1]["text"] == "🔴 Risk alert"
        # Waking after several missed days emits today's overview only.
        now = datetime(2026, 9, 23, 10, tzinfo=UTC)
        await restarted.pump_notifications()
        assert len(gateway.sent) == 3
        assert "23 Sep" in gateway.sent[-1]["text"]
        assert not gateway.edits  # Prior daily summaries remain historical snapshots.
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_social_reply_does_not_become_capability_dump():
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.CONVERSATION, social_reply="Hi!")

    service = ConversationService(None, route_agent_factory=lambda _: Router())
    answer = await service.answer(Principal("u", "g", "c", "d"), "Hi", "1")
    assert answer.text == "Hi!"
    assert not answer.citations


@pytest.mark.asyncio
async def test_system_red_ignores_disabled_and_does_not_ping_each_retry():
    view = SimpleNamespace(metadata=SimpleNamespace(name="youtube_subscriptions"), health="red",
                           last_run_error="Database busy", last_run_at=datetime.now(UTC))
    class Registry:
        enabled = True
        success = datetime(2026, 9, 18, tzinfo=UTC)

        async def list(self):
            return [view]

        def get_job(self, name):
            return SimpleNamespace(enabled=self.enabled)

        def last_successes(self, names):
            return {names[0]: self.success}

    registry = Registry()
    first = (await critical_job_events(registry, None))[0]
    view.last_run_error = "Next retry failed: sensitive SQL and parameters"
    second = (await critical_job_events(registry, None))[0]
    assert first.material_version == second.material_version
    assert "SQL" not in second.body
    registry.success = datetime(2026, 9, 19, tzinfo=UTC)
    assert (await critical_job_events(registry, None))[0].material_version != first.material_version
    registry.enabled = False
    assert await critical_job_events(registry, None) == []


@pytest.mark.asyncio
async def test_background_failures_go_to_status_channel_and_risk_stays_in_chat(tmp_path, monkeypatch):
    from argosy.services.chat_advisor.notification_policy import OPERATIONAL
    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    cfg = _config()
    cfg = cfg.model_copy(update={"bindings": (cfg.bindings[0].model_copy(update={"status_channel_id": "4"}),)})
    class Producer:
        def current_events(self, principal):
            return [OutboxEvent("system:job-health", "v1", OPERATIONAL, "Background tasks need attention"),
                    OutboxEvent("risk", "v1", "alert.critical", "Portfolio risk requires action")]
    worker = DiscordAdvisorWorker(cfg, "test", gateway, session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=Producer())
    monkeypatch.setattr(worker, "in_quiet_hours", lambda: False)
    try:
        await worker.initialize()
        await worker.pump_notifications()
        ops = next(message for message in gateway.sent if "Background tasks" in message["text"])
        risk = next(message for message in gateway.sent if "Portfolio risk" in message["text"])
        assert ops["channel_id"] == "4" and risk["channel_id"] == "2"
        assert not any("Background tasks" in message["text"] for message in gateway.sent if message["channel_id"] == "2")
        before = len(gateway.sent)
        await worker.pump_notifications()
        assert len(gateway.sent) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_system_alert_is_relabelled_once_not_declared_resolved(tmp_path, monkeypatch):
    from argosy.state.chat_models import NotificationOutbox

    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()

    class Producer:
        def current_events(self, principal):
            return []

    worker = DiscordAdvisorWorker(_config(), "test", gateway, session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=Producer())
    monkeypatch.setattr(worker, "in_quiet_hours", lambda: True)
    try:
        await worker.initialize()
        store = next(iter(worker._stores.values()))
        row = await store.enqueue_outbox(OutboxEvent(
            "system:critical-jobs", "old", "alert.critical", "System alert: 4 tasks need attention"))
        await store.mark_outbox_sent(row.id, "old-message")
        await worker.pump_notifications()
        assert len(gateway.edits) == 1
        assert gateway.edits[0]["message_id"] == "old-message"
        assert "unrelated to your chat query" in gateway.edits[0]["text"]
        assert "does not mean the task failures are resolved" in gateway.edits[0]["text"]
        async with sessions() as session:
            assert (await session.get(NotificationOutbox, row.id)).status == "superseded"
        await worker.pump_notifications()
        assert len(gateway.edits) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_state,urgent,expected", [
    ("knowledge_incomplete", False, "amber"),
    ("knowledge_recovered", False, "green"),
    ("knowledge_incomplete", True, "red"),
    ("error", False, "red"),
])
async def test_annual_current_evidence_preserves_real_urgency(tmp_path, monkeypatch, current_state, urgent, expected):
    from zoneinfo import ZoneInfo

    from argosy.services.jobs.registry import JobMetadata, JobView
    from argosy.transport.discord_advisor.status_board import _job_block, current_job_views
    engine, sessions = await _worker_db(tmp_path)
    view = JobView(metadata=JobMetadata("annual", None, "annual", "maintenance", "knowledge"),
                   health="red", last_run_status="error", last_run_error="Historical verification gaps")
    class Registry:
        async def list(self):
            return [view]
    async def current(session, *, user_id):
        assert user_id == "owner"
        return {"jobs": [{"name": "annual", "status": current_state}],
                "knowledge": {"verified": 13, "total": 18, "outstanding": 5},
                "urgent_findings": [{"urgency": "critical"}] if urgent else []}
    monkeypatch.setattr("argosy.transport.discord_advisor.status_board.db_mod.get_session", lambda **kw: sessions())
    monkeypatch.setattr("argosy.services.decision_readiness.collect_decision_readiness", current)
    try:
        result = await current_job_views(Registry(), SimpleNamespace(household_user_id="owner"))
        assert result[0].health == expected
        assert result[0].last_run_status == "error"  # Historical receipt is not falsified.
        assert view.health == "red"  # Do not mutate shared registry state.
        rendered = _job_block(result[0], enabled=True, tz=ZoneInfo("UTC"), last_success=None)
        assert f"{expected.upper()} · last run: error" in rendered
        if current_state in {"knowledge_incomplete", "knowledge_recovered"}:
            assert "Details: Current evidence:" in rendered
    finally:
        await engine.dispose()
