from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.base import LongRunningJob
from argosy.services.chat_advisor.store import ChatStore
from argosy.services.jobs.registry import JobMetadata, JobRegistry, JobView
from argosy.state import db as db_mod
from argosy.state.chat_models import NotificationOutbox
from argosy.state.models import Base, CadenceState, JobRun, User
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.status_board import (
    StatusBoardRefreshError,
    publish_status_board,
)


class _Registry:
    def __init__(self, views, jobs):
        self.views = views
        self.jobs = jobs
        self.error = None

    async def list(self):
        if self.error:
            raise self.error
        return self.views

    def last_successes(self, names):
        return {
            view.metadata.name: view.last_success_at
            for view in self.views
            if view.metadata.name in names and getattr(view, "last_success_at", None)
        }

    def get_job(self, name):
        return self.jobs[name]


class _Store:
    def __init__(self):
        self.rows = {}
        self.counter = 0

    async def active_sent_outbox(self):
        return [row for row in self.rows.values() if row.status in {"sent", "board_sent"}]

    async def status_board_pages(self, semantic_prefix):
        return [
            row
            for row in self.rows.values()
            if row.semantic_key.startswith(semantic_prefix)
            and row.status in {"board_pending", "board_sent"}
        ]

    async def reserve_status_board_page(self, event, *, nonce):
        row = self.rows.get(event.semantic_key)
        if row:
            return row
        self.counter += 1
        row = SimpleNamespace(
            id=f"outbox-{self.counter}",
            semantic_key=event.semantic_key,
            material_version=event.material_version,
            category=event.category,
            body=event.body,
            nonce=nonce,
            status="board_pending",
            sent_message_id=None,
        )
        self.rows[event.semantic_key] = row
        return row

    async def mark_status_board_sent(self, outbox_id, *, message_id, body):
        row = next(row for row in self.rows.values() if row.id == outbox_id)
        row.status = "board_sent"
        row.sent_message_id = message_id
        row.body = body

    async def update_status_board_body(self, outbox_id, *, body):
        row = next(row for row in self.rows.values() if row.id == outbox_id)
        row.body = body

    async def latest_sent_outbox(self, semantic_key):
        row = self.rows.get(semantic_key)
        return row if row and row.status == "sent" else None

    async def enqueue_outbox(self, event):
        row = self.rows.get(event.semantic_key)
        if row:
            return row
        self.counter += 1
        row = SimpleNamespace(
            id=f"outbox-{self.counter}",
            semantic_key=event.semantic_key,
            material_version=event.material_version,
            category=event.category,
            body=event.body,
            nonce=f"status-board-{self.counter}",
            status="pending",
            sent_message_id=None,
        )
        self.rows[event.semantic_key] = row
        return row

    async def mark_outbox_sent(self, outbox_id, message_id):
        row = next(row for row in self.rows.values() if row.id == outbox_id)
        row.status = "sent"
        row.sent_message_id = message_id


class _ConnectedJob(LongRunningJob):
    name = "live_feed"

    def __init__(self):
        super().__init__()
        self.status = "connected"

    async def run(self):
        return None

    def connection_status(self):
        return self.status


def _view(name, *, status, health, last, next_run, error=None, success=None):
    view = JobView(
        metadata=JobMetadata(
            name=name,
            schedule_cron="0 * * * *",
            schedule_human="hourly",
            source_kind="maintenance",
            description="test job",
        ),
        last_run_at=last,
        last_run_status=status,
        last_run_error=error,
        next_run_at=next_run,
        health=health,
    )
    view.last_success_at = success
    return view


@pytest.mark.asyncio
async def test_status_board_sends_once_then_edits_stable_private_pages(monkeypatch):
    refreshed = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    monkeypatch.setattr("argosy.transport.discord_advisor.status_board._now", lambda: refreshed)
    views = [
        _view(
            "backup",
            status="error",
            health="red",
            last=datetime(2026, 9, 19, 8, 0, tzinfo=UTC),
            success=datetime(2026, 9, 18, 8, 0, tzinfo=UTC),
            next_run=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
            error="account_id: 123456 failed @everyone",
        ),
        _view(
            "disabled_job",
            status=None,
            health="unknown",
            last=None,
            success=None,
            next_run=None,
        ),
    ]
    registry = _Registry(
        views,
        {
            "backup": SimpleNamespace(enabled=True),
            "disabled_job": SimpleNamespace(enabled=False),
        },
    )
    gateway = FakeGateway()
    store = _Store()

    await publish_status_board(
        registry=registry,
        gateway=gateway,
        store=store,
        channel_id="private-status",
        timezone="Asia/Jerusalem",
    )
    assert len(gateway.sent) == 1
    body = gateway.sent[0]["text"]
    assert "DISABLED" in body
    assert "Last success: 2026-09-18 11:00 IDT" in body
    assert "not proof the PC stayed online" in body
    assert "123456" not in body and "@everyone" not in body
    assert len(body) <= 2000
    row = next(iter(store.rows.values()))
    assert row.semantic_key == "status-board:private-status:1"
    assert row.material_version == "status-board-v1"
    assert len(row.nonce) == 24

    views[0].health = "green"
    views[0].last_run_status = "ok"
    await publish_status_board(
        registry=registry,
        gateway=gateway,
        store=store,
        channel_id="private-status",
        timezone="Asia/Jerusalem",
    )
    assert len(gateway.sent) == 1
    assert len(gateway.edits) == 1
    assert gateway.edits[0]["message_id"] == gateway.sent[0]["id"]
    assert row.body == gateway.edits[0]["text"]


@pytest.mark.asyncio
async def test_registry_failure_keeps_prior_board_and_never_claims_health(monkeypatch):
    monkeypatch.setattr(
        "argosy.transport.discord_advisor.status_board._now",
        lambda: datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
    )
    view = _view(
        "job",
        status="ok",
        health="green",
        last=datetime(2026, 9, 19, 8, 0, tzinfo=UTC),
        success=None,
        next_run=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
    )
    registry = _Registry([view], {"job": SimpleNamespace(enabled=True)})
    gateway = FakeGateway()
    store = _Store()
    kwargs = dict(
        registry=registry,
        gateway=gateway,
        store=store,
        channel_id="private-status",
        timezone="UTC",
    )
    await publish_status_board(**kwargs)
    prior = gateway.sent[0]["text"]
    registry.error = RuntimeError("job database unavailable")

    with pytest.raises(StatusBoardRefreshError):
        await publish_status_board(**kwargs)
    assert len(gateway.sent) == 1
    failure = gateway.edits[-1]["text"]
    assert "REFRESH FAILED" in failure
    assert "may be stale" in failure
    assert prior in failure
    assert "all healthy" not in failure.lower()


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_actual_registry_chatstore_sqlite_and_gateway_boundary(tmp_path, monkeypatch):
    """Real registry reads and durable board rows; only Discord is fake."""
    now = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    monkeypatch.setattr("argosy.transport.discord_advisor.status_board._now", lambda: now)
    await db_mod.dispose_engine()
    engine = db_mod.init_engine(f"sqlite+aiosqlite:///{tmp_path / 'status-real.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = db_mod.get_session_factory()
        async with sessions() as session:
            session.add(User(id="owner"))
            session.add(
                CadenceState(
                    loop_name="live_feed",
                    last_tick_at=now.replace(day=18),
                    next_due_at=None,
                    last_status="error",
                    last_error="stale completed receipt",
                )
            )
            session.add_all(
                [
                    JobRun(
                        job_name="live_feed",
                        started_at=now.replace(day=17),
                        finished_at=now.replace(day=17, hour=10),
                        status="ok",
                        manual_trigger=0,
                        idempotency_key="live-feed-ok",
                    ),
                    JobRun(
                        job_name="live_feed",
                        started_at=now.replace(day=18),
                        finished_at=now.replace(day=18, hour=10),
                        status="error",
                        error_message="old failure",
                        manual_trigger=0,
                        idempotency_key="live-feed-error",
                    ),
                ]
            )
            await session.commit()

        store = ChatStore(sessions, "owner")
        await store.ensure_binding(
            provider="discord",
            guild_id="guild",
            channel_id="chat-channel",
            provider_user_id="owner-discord",
        )
        live_job = _ConnectedJob()
        registry = JobRegistry()
        registry.register(
            job=live_job,
            metadata=JobMetadata(
                name="live_feed",
                schedule_cron=None,
                schedule_human="continuous supervisor",
                source_kind="ingest",
                description="live feed",
                long_running=True,
            ),
        )
        gateway = FakeGateway()
        kwargs = dict(
            registry=registry,
            gateway=gateway,
            store=store,
            channel_id="status-channel",
            timezone="UTC",
        )

        await publish_status_board(**kwargs)
        assert len(gateway.sent) == 1
        assert "GREEN" in gateway.sent[0]["text"]
        assert "connected" in gateway.sent[0]["text"]
        assert "Last success: 2026-09-17 10:00 UTC" in gateway.sent[0]["text"]
        async with sessions() as session:
            row = await session.scalar(select(NotificationOutbox))
            assert row is not None
            assert row.status == "board_sent"
            assert row.category == "status_board"
            assert len(row.nonce) == 24
            assert row.body == gateway.sent[0]["text"]

        live_job.status = "reconnecting"
        await publish_status_board(**kwargs)
        assert len(gateway.sent) == 1 and len(gateway.edits) == 1
        async with sessions() as session:
            row = await session.scalar(select(NotificationOutbox))
            assert row is not None
            assert row.body == gateway.edits[-1]["text"]
    finally:
        await db_mod.dispose_engine()
