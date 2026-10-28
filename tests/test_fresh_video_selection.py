# ruff: noqa: F811 -- imported pytest fixture is intentionally used as a test parameter
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from argosy.services.research_catalog import complete_item, enqueue, upsert_source
from argosy.services.research_selection import SelectedVideo, VideoSelection, _batch, _save
from argosy.state.models import YouTubeChannel, YouTubeVideo
from argosy.state.research_models import ResearchItem, ResearchSource
from tests.test_chat_subscription_actions import database  # noqa: F401


@pytest.mark.asyncio
async def test_concurrent_first_refresh_has_one_registry_and_one_fetch(database, monkeypatch):
    import asyncio

    from argosy.services import youtube_refresh as refresh

    _, _, sessions = database
    monkeypatch.setattr(refresh, "research_session", sessions)
    with sessions() as db:
        db.add(
            YouTubeChannel(
                user_id="ariel",
                youtube_channel_id="UC1",
                channel_name="Channel",
                channel_url="",
                enabled=1,
            )
        )
        db.commit()
    calls = []

    def fetch(channel):
        calls.append(channel)
        return []

    monkeypatch.setattr("argosy.services.youtube_intelligence._fetch_feed", fetch)
    results = await asyncio.gather(
        *[refresh.refresh_channels(user_id="ariel", channels=["*"]) for _ in range(2)]
    )
    assert sorted(result[0]["status"] for result in results) == [
        "checked_online",
        "recent_attempt_or_in_progress",
    ]
    assert calls == ["UC1"]
    with sessions() as db:
        assert len(db.scalars(select(ResearchSource)).all()) == 1


@pytest.mark.asyncio
async def test_cancellation_during_claim_does_not_abandon_its_result(database, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    from argosy.services import research_worker as worker

    entered, release = threading.Event(), threading.Event()
    finished = []

    async def allowed(**kwargs):
        return False

    monkeypatch.setattr(
        "argosy.orchestrator.cost_guard.get_cost_guard",
        lambda **kw: SimpleNamespace(should_pause_non_routine=allowed),
    )

    def claim(**kwargs):
        entered.set()
        assert release.wait(5)
        return ("item", "youtube", "token")

    async def process(claimed, **kwargs):
        finished.append(claimed)
        return None

    monkeypatch.setattr(worker, "claim_next", claim)
    monkeypatch.setattr(worker, "_process_claim", process)
    task = asyncio.create_task(worker.process_queue(user_id="ariel"))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [("item", "youtube", "token")]


def test_five_per_channel_no_old_backlog_and_receipt_survives_analysis(database, monkeypatch):
    from argosy.services import research_selection as selection
    from argosy.services import research_worker as worker

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        for channel in range(2):
            s = upsert_source(
                db, user_id="ariel", name=str(channel), kind="youtube", reference=str(channel)
            )
            for number in range(7):
                enqueue(
                    db,
                    s,
                    external_id=f"{channel}-{number}",
                    title="Research",
                    url="",
                    body="",
                    published_at=now,
                    now=now,
                )
            enqueue(
                db,
                s,
                external_id=f"{channel}-old",
                title="Old",
                url="",
                body="",
                published_at=now - timedelta(days=8),
                now=now - timedelta(days=8),
            )
        db.commit()
    batches = _batch("ariel", now)
    assert len(batches) == 2
    for source_id, remaining, candidates in batches:
        assert remaining == 5 and len(candidates) == 7
        output = VideoSelection(
            selected=[
                SelectedVideo(item_id=r["item_id"], why="New evidence") for r in candidates[:5]
            ],
            rationale="Other items duplicate coverage",
        )
        _save("ariel", source_id, candidates, output, now)
    claims = [worker.claim_next(user_id="ariel", now=now) for _ in range(11)]
    assert all(claims[:10]) and claims[-1] is None
    with sessions() as db:
        row = db.get(ResearchItem, claims[0][0])
        complete_item(db, row, {"claims": {}, "synthesis": {}}, now=now)
        db.commit()
        assert json.loads(row.analysis_json)["daily_selection"]["selected"]
        assert (
            len(db.scalars(select(ResearchItem).where(ResearchItem.status == "archived")).all())
            == 6
        )
    assert _batch("ariel", now) == []


def test_unknown_selection_ids_do_not_commit(database, monkeypatch):
    from argosy.services import research_selection as selection

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    with pytest.raises(ValueError, match="unknown"):
        _save(
            "ariel",
            1,
            [],
            VideoSelection(selected=[SelectedVideo(item_id="invented", why="x")], rationale="x"),
            datetime.now(UTC),
        )


def test_midnight_cooldown_does_not_spend_new_day_slots(database, monkeypatch):
    from argosy.services import research_selection as selection

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    before = datetime(2026, 9, 24, 20, 45, tzinfo=UTC)  # 23:45 local
    after = before + timedelta(hours=1)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        for number in range(5):
            row, _ = enqueue(
                db,
                source,
                external_id=f"failed{number}",
                title="Yesterday",
                url="",
                body="",
                now=before,
            )
            row.status = "failed"
            row.attempts = 1
            row.next_attempt_at = before + timedelta(hours=24)
            row.analysis_json = json.dumps(
                {"daily_selection": {"selected": True, "day": selection.day_key(before)}}
            )
        enqueue(db, source, external_id="fresh", title="Fresh", url="", body="", now=after)
        db.commit()
    source_id, remaining, candidates = _batch("ariel", after)[0]
    assert remaining == 5 and [c["video_id"] for c in candidates] == ["fresh"]
    # A concurrent explicit attempt can change eligibility while the selector runs.
    with sessions() as db:
        db.get(ResearchItem, candidates[0]["item_id"]).next_attempt_at = after + timedelta(hours=24)
        db.commit()
    output = VideoSelection(
        selected=[SelectedVideo(item_id=candidates[0]["item_id"], why="Relevant")],
        rationale="Relevant",
    )
    assert _save("ariel", source_id, candidates, output, after, committed_at=after) == 0


def test_stale_prior_day_selector_cannot_overwrite_new_day(database, monkeypatch):
    from argosy.services import research_selection as selection
    _, _, sessions = database
    monkeypatch.setattr(selection, 'research_session', sessions)
    before = datetime(2026, 9, 24, 20, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    with sessions() as db:
        source = upsert_source(db, user_id='ariel', name='Channel', kind='youtube', reference='UC1')
        enqueue(db, source, external_id='fresh', title='Fresh', url='', body='', now=before)
        db.commit()
    source_id, _, candidates = _batch('ariel', before)[0]
    choice = VideoSelection(selected=[SelectedVideo(item_id=candidates[0]['item_id'], why='Relevant')], rationale='Relevant')
    assert _save('ariel', source_id, candidates, choice, after, committed_at=after) == 1
    with pytest.raises(ValueError, match='daily boundary'):
        _save('ariel', source_id, candidates, VideoSelection(rationale='Old exclusion'), before, committed_at=after)
    with sessions() as db:
        row = db.get(ResearchItem, candidates[0]['item_id'])
        assert row.status == 'queued'
        assert json.loads(row.analysis_json)['daily_selection']['day'] == selection.day_key(after)


def test_concurrent_selection_reports_only_committed_choices(database, monkeypatch):
    from argosy.services import research_selection as selection

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        for n in range(2):
            enqueue(db, source, external_id=str(n), title="New", url="", body="", now=now)
        db.commit()
    source_id, _, candidates = _batch("ariel", now)[0]
    for n, expected in [(0, 1), (1, 0)]:
        output = VideoSelection(
            selected=[SelectedVideo(item_id=candidates[n]["item_id"], why="New")],
            rationale="Other redundant",
        )
        assert _save("ariel", source_id, candidates, output, now) == expected


def test_explicit_attempts_do_not_consume_auto_quota_and_expired_claims_archive(
    database, monkeypatch
):
    from argosy.services import research_selection as selection
    from argosy.services import research_worker as worker

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        for n in range(9):
            item, _ = enqueue(
                db, source, external_id=f"manual{n}", title="Manual", url="", body="", now=now
            )
            item.status = "analyzed"
            item.analyzed_at = item.attempted_at = now
        expired, _ = enqueue(
            db,
            source,
            external_id="expired",
            title="Old",
            url="",
            body="",
            now=now - timedelta(days=3),
        )
        expired.status = "processing"
        expired.lease_until = now - timedelta(days=1)
        old_id = expired.id
        for n in range(5):
            enqueue(db, source, external_id=f"auto{n}", title="Fresh", url="", body="", now=now)
        db.commit()
    source_id, remaining, candidates = _batch("ariel", now)[0]
    assert remaining == 5 and len(candidates) == 5
    _save(
        "ariel",
        source_id,
        candidates,
        VideoSelection(
            selected=[SelectedVideo(item_id=c["item_id"], why="New") for c in candidates],
            rationale="Relevant",
        ),
        now,
    )
    assert all(worker.claim_next(user_id="ariel", now=now) for _ in range(5))
    assert worker.claim_next(user_id="ariel", now=now) is None
    with sessions() as db:
        assert db.get(ResearchItem, old_id).status == "archived"


@pytest.mark.asyncio
async def test_live_refresh_is_scoped_debounced_and_failures_keep_last_success(
    database, monkeypatch
):
    from argosy.services import youtube_refresh as refresh

    _, _, sessions = database
    monkeypatch.setattr(refresh, "research_session", sessions)
    with sessions() as db:
        upsert_source(
            db,
            user_id="ariel",
            name="Meet Kevin",
            kind="youtube",
            reference="configured",
            enabled=False,
        )
        db.add(
            YouTubeChannel(
                user_id="ariel",
                youtube_channel_id="configured",
                channel_name="Meet Kevin",
                channel_url="https://youtube.com/@MeetKevin",
                enabled=1,
            )
        )
        db.commit()
    calls = []

    def fetch(channel):
        calls.append(channel)
        return [
            {
                "video_id": "abcdefghijk",
                "title": "New today",
                "published_at": datetime.now(UTC).isoformat(),
            }
        ]

    monkeypatch.setattr("argosy.services.youtube_intelligence._fetch_feed", fetch)
    result = await refresh.refresh_channels(user_id="ariel", channels=["Meet Kevin"])
    assert result[0]["status"] == "checked_online"
    assert (await refresh.refresh_channels(user_id="ariel", channels=["Meet Kevin"]))[0][
        "status"
    ] == "recent_attempt_or_in_progress"
    assert (await refresh.refresh_channels(user_id="other", channels=["Meet Kevin"]))[0][
        "status"
    ] == "not_found"
    assert calls == ["configured"]
    with sessions() as db:
        assert db.scalar(select(ResearchItem)).title == "New today"
        assert db.scalar(select(ResearchSource)).enabled


@pytest.mark.asyncio
async def test_refresh_failure_is_not_reported_as_no_uploads(database, monkeypatch):
    from argosy.services import youtube_refresh as refresh

    _, _, sessions = database
    monkeypatch.setattr(refresh, "research_session", sessions)
    previous = datetime.now(UTC) - timedelta(days=1)
    with sessions() as db:
        db.add(
            YouTubeChannel(
                user_id="ariel",
                youtube_channel_id="configured",
                channel_name="Meet Kevin",
                channel_url="https://youtube.com/@MeetKevin",
                enabled=1,
                last_polled_at=previous,
            )
        )
        db.commit()

    def fail(_):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr("argosy.services.youtube_intelligence._fetch_feed", fail)
    result = await refresh.refresh_channels(user_id="ariel", channels=["Meet Kevin"])
    assert result[0]["status"] == "failed" and "429" in result[0]["error"]
    with sessions() as db:
        channel = db.scalar(select(YouTubeChannel))
        assert channel.last_polled_at.replace(tzinfo=UTC) == previous


def test_live_owner_not_reclaimed_after_lease_and_old_completion_fenced(database, monkeypatch):
    from argosy.services import research_selection as selection
    from argosy.services import research_worker as worker

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        enqueue(db, source, external_id="one", title="Fresh", url="", body="", now=now)
        db.commit()
    source_id, _, candidates = _batch("ariel", now)[0]
    _save(
        "ariel",
        source_id,
        candidates,
        VideoSelection(
            selected=[SelectedVideo(item_id=candidates[0]["item_id"], why="Relevant")],
            rationale="Selected",
        ),
        now,
    )
    item_id, _, token = worker.claim_next(user_id="ariel", now=now)
    assert _batch("ariel", now + timedelta(hours=4)) == []
    assert worker.claim_next(user_id="ariel", now=now + timedelta(hours=4)) is None
    with pytest.raises(ValueError, match="ownership"):
        worker._finish(item_id, {}, user_id="ariel", token="old-owner")
    worker._finish(item_id, {"claims": {}, "synthesis": {}}, user_id="ariel", token=token)


@pytest.mark.asyncio
async def test_refresh_does_not_present_historical_uploads_as_backlog(database, monkeypatch):
    from argosy.services import youtube_refresh as refresh

    _, _, sessions = database
    monkeypatch.setattr(refresh, "research_session", sessions)
    with sessions() as db:
        db.add(
            YouTubeChannel(
                user_id="ariel",
                youtube_channel_id="UC1",
                channel_name="Channel",
                channel_url="",
                enabled=1,
            )
        )
        db.commit()
    monkeypatch.setattr(
        "argosy.services.youtube_intelligence._fetch_feed",
        lambda _: [
            {
                "video_id": "old",
                "title": "Historical",
                "published_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
            }
        ],
    )
    receipt = (await refresh.refresh_channels(user_id="ariel", channels=["*"]))[0]
    assert receipt["queued"] == 0 and receipt["uploads_returned"] == 1
    with sessions() as db:
        assert db.scalar(select(ResearchItem)).status == "archived"


def test_saved_video_duplicates_do_not_consume_selection(database, monkeypatch):
    from argosy.services import research_selection as selection

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        enqueue(db, source, external_id="alreadydone", title="Seen", url="", body="", now=now)
        enqueue(db, source, external_id="fresh", title="Fresh", url="", body="", now=now)
        db.add(
            YouTubeVideo(user_id="ariel", youtube_video_id="alreadydone", url="", analyzed_at=now)
        )
        db.commit()
    _, remaining, candidates = _batch("ariel", now)[0]
    assert remaining == 5 and [r["video_id"] for r in candidates] == ["fresh"]


def test_interrupted_automatic_result_reconciles_without_reanalysis(database, monkeypatch):
    from argosy.services import research_selection as selection
    from argosy.services import research_worker as worker

    _, _, sessions = database
    monkeypatch.setattr(selection, "research_session", sessions)
    now = datetime.now(UTC)
    with sessions() as db:
        source = upsert_source(db, user_id="ariel", name="Channel", kind="youtube", reference="UC1")
        enqueue(db, source, external_id="one", title="Fresh", url="", body="", now=now)
        db.commit()
    source_id, _, candidates = _batch("ariel", now)[0]
    _save(
        "ariel",
        source_id,
        candidates,
        VideoSelection(
            selected=[SelectedVideo(item_id=candidates[0]["item_id"], why="Relevant")],
            rationale="Selected",
        ),
        now,
    )
    item_id, _, token = worker.claim_next(user_id="ariel", now=now)
    payload = {"run_id": "saved-automatic", "claims": {}, "synthesis": {}}
    worker._stage_result(item_id, token, payload, "ariel")
    assert worker.reconcile_completed(user_id="ariel") == 0  # Live owner still finishes normally.
    with sessions() as db:
        db.get(
            ResearchItem, item_id
        ).status = "archived"  # A later age sweep must not lose a staged success.
        db.commit()
    monkeypatch.setattr("argosy.services.research_catalog.automatic_owner_alive", lambda _: False)
    assert worker.reconcile_completed(user_id="ariel") == 1
    with sessions() as db:
        row = db.get(ResearchItem, item_id)
        assert row.analyzed_at and json.loads(row.analysis_json)["daily_selection"]["selected"]


@pytest.mark.asyncio
async def test_cancelled_batch_waits_for_owned_work(database, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from argosy.services import research_worker as worker

    async def allowed(**kwargs):
        return False

    monkeypatch.setattr(
        "argosy.orchestrator.cost_guard.get_cost_guard",
        lambda **kw: SimpleNamespace(should_pause_non_routine=allowed),
    )
    started, finished = asyncio.Event(), asyncio.Event()

    async def process(*args, **kwargs):
        started.set()
        await finished.wait()
        return None

    monkeypatch.setattr(worker, "_process_claim", process)
    monkeypatch.setattr(worker, "claim_next", lambda **kw: ("item", "youtube", "token"))
    task = asyncio.create_task(worker.process_queue(user_id="ariel"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finished.set()
    with pytest.raises(asyncio.CancelledError):
        await task
