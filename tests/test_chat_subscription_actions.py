import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.chat_advisor.analysis_dispatch import AnalysisDispatcher
from argosy.services.chat_advisor.conversation import ConversationService, RouteDecision
from argosy.services.chat_advisor.read_team import ReadExecutor, ReadRequest, RecordReference
from argosy.services.chat_advisor.research_actions import (
    ResearchChatTask,
    _claim_video,
    canonical_reference,
)
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.research_catalog import enqueue, upsert_source
from argosy.state.models import Base, User, YouTubeChannel, YouTubeVideo
from argosy.state.research_models import ResearchItem, ResearchSource
from tests.test_chat_analysis_dispatch import Store, principal
from tests.test_chat_conversation import _Agent, _Retrieval


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "research-chat.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id="ariel"), User(id="other")])
        db.commit()

    @contextmanager
    def sessions(*args, **kwargs):
        with Session(engine, expire_on_commit=False) as db:
            yield db

    monkeypatch.setattr("argosy.services.research_catalog.research_session", sessions)
    monkeypatch.setattr("argosy.services.research_worker.research_session", sessions)
    monkeypatch.setattr(
        "argosy.services.youtube_intelligence._factory",
        lambda: sessionmaker(engine, expire_on_commit=False),
    )
    from argosy.services.youtube_intelligence import YouTubeMetadata

    monkeypatch.setattr(
        "argosy.services.youtube_intelligence.resolve_youtube_metadata",
        lambda ref: YouTubeMetadata(
            "bytxhgSZTWI",
            "Example video",
            "UC" + "A" * 22,
            "Meet Kevin",
            "https://www.youtube.com/@MeetKevin",
        ),
    )
    yield engine, path, sessions
    engine.dispose()


@pytest.mark.parametrize(
    "reference",
    [
        "https://youtube.com.evil.test/@a",
        "http://youtube.com/@a",
        "https://youtube.com:444/@a",
        "https://me@youtube.com/@a",
        "file:///tmp/x",
        "https://127.0.0.1/a",
        "https://youtube.com/redirect?q=elsewhere",
        "https://youtube.com/@a?redirect=bad",
    ],
)
def test_follow_accepts_only_public_youtube_channel_targets(reference):
    with pytest.raises(ValueError):
        canonical_reference("youtube_follow", reference)


@pytest.mark.asyncio
async def test_subscriptions_are_scoped_dated_and_drillable(database):
    engine, path, sessions = database
    when = datetime(2026, 9, 24, 21, 30, tzinfo=UTC)  # Sep25 in Israel.
    with sessions() as db:
        channel = YouTubeChannel(
            user_id="ariel",
            channel_name="Meet Kevin",
            youtube_channel_id="chan",
            channel_url="https://www.youtube.com/@MeetKevin",
            last_polled_at=when,
        )
        db.add(channel)
        db.flush()
        source = upsert_source(
            db, user_id="ariel", name="Meet Kevin", kind="youtube", reference="chan"
        )
        item, _ = enqueue(
            db,
            source,
            external_id="bytxhgSZTWI",
            title="Completed video",
            url="https://youtu.be/bytxhgSZTWI",
            body="",
            now=when,
        )
        item.status = "analyzed"
        item.analyzed_at = when
        item.analysis_json = json.dumps({"synthesis": {"executive_summary": "Rates claim"}})
        pending, _ = enqueue(
            db,
            source,
            external_id="abcdefghijk",
            title="Older queued video",
            url="https://youtu.be/abcdefghijk",
            body="",
            now=when - timedelta(days=5),
        )
        video = YouTubeVideo(
            user_id="ariel",
            channel_id=channel.id,
            youtube_video_id="bytxhgSZTWI",
            url=item.url,
            title=item.title,
            analyzed_at=when,
        )
        db.add(video)
        other = upsert_source(
            db, user_id="other", name="Meet Kevin", kind="youtube", reference="private"
        )
        enqueue(db, other, external_id="secret", title="PRIVATE", url="", body="", now=when)
        db.commit()
        video_id = video.id
        item_id = item.id
    reader = RetrievalService(db_path=path)
    try:
        result = reader.read(
            principal(), "subscriptions", channel="Meet Kevin", day="2026-09-25", limit=1
        )
        assert result.data["completed_videos_total"] == 1
        assert result.data["activity_total"] == 1
        assert result.data["backlog"][0]["title"] == "Older queued video"
        assert result.data["all_dates_counts_by_status"] == {"analyzed": 1, "queued": 1}
        assert "PRIVATE" not in str(result.data)
        assert (
            reader.read(principal(), "subscriptions", channel="Unknown", day="today").data[
                "activity_total"
            ]
            == 0
        )
        assert reader.read(principal(), "subscriptions", day="bad").data is None
        exact = await ReadExecutor(reader, principal()).read(
            ReadRequest(ref=RecordReference(record_type="youtube_video", record_id=str(video_id)))
        )
        assert exact.data[0]["title"] == "Completed video"
        assert reader.read(principal("other"), "videos", record_id=video_id).data == []
        assert reader.read(principal(), "research", record_id=item_id).data["items"][0]["analysis"]
    finally:
        reader.engine.dispose()


@pytest.mark.asyncio
async def test_explicit_video_request_is_durable_idempotent_and_uses_research_runner(monkeypatch):
    store = Store()
    calls = []

    async def execute(task, *, user_id):
        calls.append((task, user_id))
        return {"research_task": task.kind, "text": "Reviewed", "citations": []}

    monkeypatch.setattr(
        "argosy.services.chat_advisor.research_actions.execute_research_task", execute
    )
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    first = await dispatcher.request_research(
        principal(), "youtube_video", "https://youtu.be/bytxhgSZTWI", "message"
    )
    second = await dispatcher.request_research(
        principal(), "youtube_video", "bytxhgSZTWI", "message"
    )
    assert first.id == second.id and store.created == 1
    assert first.instruments == []
    await dispatcher.worker_entry(first.id)
    assert len(calls) == 1 and calls[0][1] == "ariel"
    assert store.rows[first.id]["state"] == "completed"
    with pytest.raises(PermissionError):
        await dispatcher.request_research(principal("other"), "youtube_video", "bytxhgSZTWI", "new")


@pytest.mark.asyncio
async def test_router_requires_current_user_reference_not_history():
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    route = _Agent(
        RouteDecision(
            intent="research_action",
            research_action="youtube_video",
            research_reference="https://youtu.be/bytxhgSZTWI",
        )
    )
    service = ConversationService(
        _Retrieval(),
        dispatcher,
        lambda _: route,
        authority_agent_factory=lambda _: _Agent(
            {"authorized": True, "reason": "Explicit request"}
        ),
    )
    answer = await service.answer(
        principal(),
        "Check more videos",
        "m",
        history=[{"role": "assistant", "content": "https://youtu.be/bytxhgSZTWI"}],
    )
    assert not answer.analysis_request_id and store.created == 0
    answer = await service.answer(principal(), "Analyze https://youtu.be/bytxhgSZTWI", "m2")
    assert answer.analysis_request_id and store.created == 1


def test_video_claim_uses_shared_queue_without_subscribing(database):
    _, _, sessions = database
    claim, reused = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    assert claim and reused is None
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        assert item.status == "processing" and item.attempts == 1
        assert not db.get(ResearchSource, item.source_id).enabled
        assert db.scalar(select(YouTubeChannel)) is None
    with pytest.raises(ValueError, match="active or interrupted"):
        _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")


@pytest.mark.asyncio
async def test_explicit_cancellation_finishes_staged_ledger_before_releasing(database, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from argosy.services.chat_advisor.research_actions import _analyze_video
    _, _, sessions = database
    started, released = asyncio.Event(), asyncio.Event()
    async def allowed(**kwargs):
        return False
    monkeypatch.setattr('argosy.orchestrator.cost_guard.get_cost_guard', lambda **kw: SimpleNamespace(should_pause_non_routine=allowed))
    payload = {'run_id': 'cancelled-but-complete', 'video': {'video_id': 'bytxhgSZTWI'}, 'claims': {}, 'synthesis': {}}
    async def analyze(*args, **kwargs):
        started.set()
        await released.wait()
        await kwargs['on_completed'](payload)
        return payload
    monkeypatch.setattr('argosy.services.youtube_analysis.analyze_youtube', analyze)
    monkeypatch.setattr('argosy.services.youtube_analysis.result_to_dict', lambda value: value)
    def no_summary_read(*args, **kwargs):
        raise RuntimeError('Summary is outside this isolated cancellation test')
    monkeypatch.setattr('argosy.services.chat_advisor.retrieval.RetrievalService', no_summary_read)
    task = asyncio.create_task(_analyze_video('https://youtu.be/bytxhgSZTWI', user_id='ariel'))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with sessions() as db:
        assert db.scalar(select(ResearchItem)).status == 'analyzed'


def test_dead_explicit_owner_recovers_to_cooldown_not_duplicate_work(database, monkeypatch):
    _, _, sessions = database
    claim, _ = _claim_video('https://youtu.be/bytxhgSZTWI', user_id='ariel')
    monkeypatch.setattr('argosy.services.research_catalog.automatic_owner_alive', lambda _: False)
    with pytest.raises(ValueError, match='cooldown'):
        _claim_video('https://youtu.be/bytxhgSZTWI', user_id='ariel')
    with sessions() as db:
        row = db.get(ResearchItem, claim[0])
        assert row.status == 'failed' and row.attempts == 1 and row.next_attempt_at


def test_existing_source_stays_enabled_and_expired_chat_claim_cannot_be_reclaimed(database):
    from argosy.services.research_worker import claim_next

    _, _, sessions = database
    with sessions() as db:
        upsert_source(
            db,
            user_id="ariel",
            name="Meet Kevin",
            kind="youtube",
            reference="UC" + "A" * 22,
            enabled=True,
        )
        db.commit()
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        item.lease_until = datetime.now(UTC) - timedelta(hours=1)
        assert db.get(ResearchSource, item.source_id).enabled
        db.commit()
    assert claim_next(user_id="ariel", now=datetime.now(UTC) + timedelta(days=1)) is None


def test_old_claim_cannot_overwrite_new_claim_result(database):
    from argosy.services.chat_advisor.research_actions import _failed_video, _finish_video

    _, _, sessions = database
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        item.analysis_json = json.dumps({"_chat_claim": "different-owner"})
        db.commit()
    _failed_video(claim, "ariel", "late error")
    with pytest.raises(ValueError, match="ownership changed"):
        _finish_video(claim, "ariel", {})
    with sessions() as db:
        assert db.get(ResearchItem, claim[0]).status == "processing"
        assert db.get(ResearchItem, claim[0]).error is None


def test_real_video_catalog_mirror_preserves_claim_until_fenced_finish(database):
    from argosy.services.chat_advisor.research_actions import _finish_video
    from argosy.services.youtube_intelligence import persist_youtube_analysis

    _, _, sessions = database
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    payload = {
        "run_id": "real-save-path-test",
        "video": {
            "video_id": "bytxhgSZTWI",
            "url": "https://youtu.be/bytxhgSZTWI",
            "title": "Example video",
            "channel": "Meet Kevin",
        },
        "claims": {},
        "synthesis": {"executive_summary": "Attributed research"},
    }
    persist_youtube_analysis(payload, user_id="ariel")
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        assert (
            item.status == "processing"
            and json.loads(item.analysis_json)["_chat_claim"] == claim[1]
        )
    _finish_video(claim, "ariel", payload)
    with sessions() as db:
        assert db.get(ResearchItem, claim[0]).status == "analyzed"
        assert len(db.scalars(select(ResearchItem)).all()) == 1


def test_active_video_blocks_other_version_and_source_alias(database):
    from argosy.services.research_worker import claim_next

    _, _, sessions = database
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    with sessions() as db:
        source = upsert_source(
            db, user_id="ariel", name="Alias", kind="youtube", reference="alias", enabled=True
        )
        enqueue(
            db, source, external_id="bytxhgSZTWI", title="Another version", url="", body="new body"
        )
        db.commit()
    assert claim_next(user_id="ariel", now=datetime.now(UTC) + timedelta(days=1)) is None
    with pytest.raises(ValueError, match="active or interrupted"):
        _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")


def test_follow_reenables_manual_only_source_and_is_idempotent(database):
    from argosy.services.youtube_intelligence import subscribe_youtube_source

    _, _, sessions = database
    _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    first = subscribe_youtube_source("https://www.youtube.com/@MeetKevin", user_id="ariel")
    second = subscribe_youtube_source("https://www.youtube.com/@MeetKevin", user_id="ariel")
    assert first["id"] == second["id"]
    with sessions() as db:
        assert db.scalar(select(ResearchSource)).enabled
        assert db.scalar(select(YouTubeChannel)).enabled


def test_completed_video_archives_queued_alias_without_reanalysis(database):
    from argosy.services.chat_advisor.research_actions import _finish_video
    from argosy.services.research_worker import claim_next

    _, _, sessions = database
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    with sessions() as db:
        source = upsert_source(
            db, user_id="ariel", name="Alias", kind="youtube", reference="alias", enabled=True
        )
        alias, _ = enqueue(
            db, source, external_id="bytxhgSZTWI", title="Alias", url="", body="new body"
        )
        alias_id = alias.id
        db.commit()
    _finish_video(
        claim,
        "ariel",
        {"run_id": "done", "video": {"video_id": "bytxhgSZTWI"}, "claims": {}, "synthesis": {}},
    )
    assert claim_next(user_id="ariel", now=datetime.now(UTC) + timedelta(days=1)) is None
    with sessions() as db:
        assert db.get(ResearchItem, alias_id).status == "archived"


@pytest.mark.parametrize("blocked_by", ["cooldown", "attempts"])
def test_video_retry_limits_survive_source_alias_and_new_body(database, blocked_by):
    from argosy.services.research_worker import claim_next

    _, _, sessions = database
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(
            db, user_id="ariel", name="Original", kind="youtube", reference="original", enabled=True
        )
        failed, _ = enqueue(
            db, source, external_id="bytxhgSZTWI", title="Failed", url="", body="old"
        )
        failed.status = "failed"
        failed.attempts = 3 if blocked_by == "attempts" else 1
        failed.attempted_at = now - timedelta(days=1)
        failed.next_attempt_at = now + timedelta(hours=12) if blocked_by == "cooldown" else None
        alias = upsert_source(
            db,
            user_id="ariel",
            name="Alias",
            kind="youtube",
            reference="UC" + "A" * 22,
            enabled=True,
        )
        enqueue(db, alias, external_id="bytxhgSZTWI", title="New body", url="", body="new")
        db.commit()
    assert claim_next(user_id="ariel", now=now) is None
    with pytest.raises(ValueError, match="cooldown|exhausted three attempts"):
        _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")


def test_unlinked_direct_import_included_without_channel_filter(database):
    _, path, sessions = database
    when = datetime(2026, 9, 25, 1, tzinfo=UTC)
    with sessions() as db:
        db.add(
            YouTubeVideo(
                user_id="ariel",
                channel_id=None,
                youtube_video_id="abcdefghijk",
                url="https://youtu.be/abcdefghijk",
                analyzed_at=when,
            )
        )
        db.commit()
    reader = RetrievalService(db_path=path)
    try:
        assert (
            reader.read(principal(), "subscriptions", day="2026-09-25").data[
                "completed_videos_total"
            ]
            == 1
        )
        assert (
            reader.read(principal(), "subscriptions", day="2026-09-25", channel="Meet Kevin").data[
                "completed_videos_total"
            ]
            == 0
        )
    finally:
        reader.engine.dispose()


def test_source_kind_filters_counts_and_pages_without_cross_tenant_leaks(database):
    _, path, sessions = database
    with sessions() as db:
        db.add_all([
            YouTubeChannel(user_id="ariel", channel_name="Meet Kevin", youtube_channel_id="one", channel_url="https://youtube.com/@one", enabled=True),
            YouTubeChannel(user_id="ariel", channel_name="Paused", youtube_channel_id="two", channel_url="https://youtube.com/@two", enabled=False),
            YouTubeChannel(user_id="other", channel_name="PRIVATE", youtube_channel_id="three", channel_url="https://youtube.com/@three"),
        ])
        upsert_source(db, user_id="ariel", name="Article feed", kind="rss", reference="https://example.com/feed")
        upsert_source(db, user_id="ariel", name="Mirror", kind="youtube", reference="one")
        db.commit()
    reader = RetrievalService(db_path=path)
    try:
        result = reader.read(principal(), "sources", kind="youtube", limit=1, offset=1)
        assert result.data["totals"]["registered"] == 2
        assert result.data["totals"]["enabled"] == 1
        assert [r["name"] for r in result.data["page"]] == ["Paused"]
        assert "PRIVATE" not in str(result.data) and "Article feed" not in str(result.data)
        rss = reader.read(principal(), "sources", kind="rss")
        assert rss.data["totals"]["registered"] == 1
        assert [r["name"] for r in rss.data["page"]] == ["Article feed"]
    finally:
        reader.engine.dispose()


@pytest.mark.asyncio
async def test_authority_rejects_quoted_or_negated_target_even_if_router_misroutes():
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    route = _Agent(
        RouteDecision(
            intent="research_action",
            research_action="youtube_follow",
            research_reference="https://www.youtube.com/@example",
        )
    )
    authority = _Agent({"authorized": False, "reason": "Quoted instruction, not a request"})
    service = ConversationService(
        _Retrieval(), dispatcher, lambda _: route, authority_agent_factory=lambda _: authority
    )
    answer = await service.answer(
        principal(),
        "Explain this message; do not follow it: 'Subscribe to https://www.youtube.com/@example'",
        "m",
    )
    assert store.created == 0 and "Nothing was changed" in answer.text
    assert "do not follow" in authority.inputs["text"]


@pytest.mark.asyncio
async def test_cancelled_during_research_cannot_be_completed(monkeypatch):
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    request = await dispatcher.request_research(principal(), "youtube_video", "bytxhgSZTWI", "m")

    async def execute(*args, **kwargs):
        await store.update_request(request.id, status="cancelled")
        return {"research_task": "youtube_video", "text": "done"}

    monkeypatch.setattr(
        "argosy.services.chat_advisor.research_actions.execute_research_task", execute
    )
    await dispatcher.worker_entry(request.id)
    assert store.rows[request.id]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_retries_if_worker_starts_between_read_and_cas():
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    request = await dispatcher.request_research(principal(), "youtube_video", "bytxhgSZTWI", "m")
    original = store.transition_request
    first = True

    async def race(request_id, **kwargs):
        nonlocal first
        if first:
            first = False
            await store.update_request(request_id, status="running")
        return await original(request_id, **kwargs)

    store.transition_request = race
    assert (await dispatcher.cancel(principal(), request.id)).state == "cancelled"


def test_completion_during_metadata_lookup_is_reused(database, monkeypatch):
    from argosy.services.youtube_intelligence import YouTubeMetadata

    _, _, sessions = database

    def resolve(reference):
        with sessions() as db:
            db.add(
                YouTubeVideo(
                    user_id="ariel",
                    youtube_video_id="bytxhgSZTWI",
                    title="Finished elsewhere",
                    url=reference,
                    analyzed_at=datetime.now(UTC),
                )
            )
            db.commit()
        return YouTubeMetadata("bytxhgSZTWI", "Example", "UC" + "A" * 22, "Meet Kevin", reference)

    monkeypatch.setattr("argosy.services.youtube_intelligence.resolve_youtube_metadata", resolve)
    claim, reused = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    assert claim is None and "Already analyzed" in reused["text"]
    with sessions() as db:
        assert db.scalar(select(ResearchItem)) is None


def test_staged_success_reconciles_after_failed_finalization_without_new_claim(
    database, monkeypatch
):
    import argosy.services.research_catalog as catalog
    from argosy.services.chat_advisor.research_actions import (
        _failed_video,
        _finish_video,
        _stage_video_result,
    )

    _, _, sessions = database
    claim, _ = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    payload = {
        "run_id": "saved-result",
        "video": {"video_id": "bytxhgSZTWI"},
        "claims": {},
        "synthesis": {},
    }
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        state = json.loads(item.analysis_json)
        state['daily_selection'] = {'day': '2026-09-25', 'selected': True}
        item.analysis_json = json.dumps(state)
        db.commit()
    _stage_video_result(claim, "ariel", payload)
    with sessions() as db:
        assert json.loads(db.get(ResearchItem, claim[0]).analysis_json)['daily_selection']['selected']
    original = catalog.complete_item

    def fail(*args, **kwargs):
        raise RuntimeError("temporary finalization failure")

    monkeypatch.setattr(catalog, "complete_item", fail)
    with pytest.raises(RuntimeError):
        _finish_video(claim, "ariel", payload)
    _failed_video(claim, "ariel", "temporary finalization failure")
    monkeypatch.setattr(catalog, "complete_item", original)
    new_claim, reused = _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    assert new_claim is None and "Already analyzed" in reused["text"]
    with sessions() as db:
        item = db.get(ResearchItem, claim[0])
        assert item.status == "analyzed" and item.attempts == 1


def test_saved_catalog_alone_does_not_hide_interrupted_manual_ledger(database):
    from argosy.services.youtube_intelligence import persist_youtube_analysis

    _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
    persist_youtube_analysis(
        {
            "run_id": "catalog-only",
            "video": {"video_id": "bytxhgSZTWI", "url": "https://youtu.be/bytxhgSZTWI"},
            "claims": {},
            "synthesis": {},
        },
        user_id="ariel",
    )
    with pytest.raises(ValueError, match="catalog data alone"):
        _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")


@pytest.mark.asyncio
async def test_summary_initialization_failure_preserves_successful_video(database, monkeypatch):
    from argosy.services.chat_advisor.research_actions import execute_research_task

    _, _, sessions = database

    class Guard:
        async def should_pause_non_routine(self, **kwargs):
            return False

    monkeypatch.setattr("argosy.orchestrator.cost_guard.get_cost_guard", lambda **kwargs: Guard())

    async def analyze(*args, **kwargs):
        return {
            "run_id": "finished",
            "video": {"video_id": "bytxhgSZTWI"},
            "claims": {},
            "synthesis": {},
        }

    monkeypatch.setattr("argosy.services.youtube_analysis.analyze_youtube", analyze)
    monkeypatch.setattr("argosy.services.youtube_analysis.result_to_dict", lambda value: value)

    def broken_reader():
        raise RuntimeError("reader configuration unavailable")

    monkeypatch.setattr("argosy.services.chat_advisor.retrieval.RetrievalService", broken_reader)
    result = await execute_research_task(
        ResearchChatTask(kind="youtube_video", reference="bytxhgSZTWI"), user_id="ariel"
    )
    assert "analysis saved" in result["text"] and "summary is unavailable" in result["text"]
    with sessions() as db:
        assert db.scalar(select(ResearchItem)).status == "analyzed"


@pytest.mark.asyncio
async def test_video_failure_is_recorded_and_cooldown_prevents_repeat(database, monkeypatch):
    from argosy.services.chat_advisor.research_actions import execute_research_task

    _, _, sessions = database

    class Guard:
        async def should_pause_non_routine(self, **kwargs):
            return False

    monkeypatch.setattr("argosy.orchestrator.cost_guard.get_cost_guard", lambda **kwargs: Guard())

    async def fail(*args, **kwargs):
        raise RuntimeError("YouTube authentication required")

    monkeypatch.setattr("argosy.services.youtube_analysis.analyze_youtube", fail)
    with pytest.raises(RuntimeError, match="authentication"):
        await execute_research_task(
            ResearchChatTask(kind="youtube_video", reference="bytxhgSZTWI"), user_id="ariel"
        )
    with sessions() as db:
        item = db.scalar(select(ResearchItem))
        assert item.status == "failed" and "authentication" in item.error
    with pytest.raises(ValueError, match="cooldown"):
        _claim_video("https://youtu.be/bytxhgSZTWI", user_id="ariel")
