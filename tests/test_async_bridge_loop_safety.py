"""Regression: shared aiosqlite pool must survive sync worker + nested-loop use.

Reproduces the production defect logged 1,065 times as
``Queue is bound to a different event loop`` when sync jobs called
``asyncio.run(adapter...)`` → ``cached_call`` → ``get_session`` after the
app main loop had already checked out pooled connections.

The FIXED path uses ``NullPool`` for file SQLite (SQLAlchemy's documented
cross-loop requirement) plus ``run_coro_sync`` (long-lived main/bridge
loop). Tests below:

1. Prove the UNFIXED pattern (default QueuePool + ``asyncio.run`` from a
   worker after warming the pool on another loop) still fails.
2. Prove the FIXED path works from a worker thread AND from inside a
   running event loop, and that cache hits still avoid re-fetch.
3. Prove ``/health`` is recency-scoped (recovers) and ``run_coro_sync``
   failure modes (timeout counts as infra; same-loop refuse; different-loop
   offload; bridge fallback) are defined.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from argosy.adapters.data.async_bridge import (
    capture_main_loop,
    get_main_loop,
    is_bridge_timeout,
    is_event_loop_mismatch,
    is_infra_data_failure,
    mismatch_count,
    note_event_loop_mismatch,
    recent_mismatch_count,
    reset_for_tests,
    run_coro_sync,
    set_mismatch_health_window_for_tests,
)
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.state import db as db_mod
from argosy.state.models import Base


def _file_url(tmp_path: Path) -> str:
    db_path = tmp_path / "loop_safety.db"
    # Forward slashes — SQLAlchemy on Windows accepts this form.
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


async def _create_schema(url: str) -> None:
    eng = create_async_engine(url, poolclass=NullPool, future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()


def test_unfixed_shared_queuepool_fails_across_asyncio_run(tmp_path: Path) -> None:
    """Document the defect: shared QueuePool + asyncio.run from a worker.

    Production shape: the FastAPI main loop has already bound the pool's
    ``AsyncAdaptedQueue`` (and often has parked ``_getters`` under load).
    A worker thread then calls ``asyncio.run`` against the same engine →
    ``Queue is bound to a different event loop``.

    Idle single-checkout warm-ups can pass on Python 3.12; the failure is
    reliable once the main loop has waiters on the pool queue (the shape
    matching today's log lines with ``_getters[N]``).
    """
    from sqlalchemy import text

    url = _file_url(tmp_path)
    asyncio.run(_create_schema(url))

    # Classic UNFIXED engine: default AsyncAdaptedQueuePool (NOT NullPool).
    engine = create_async_engine(url, future=True, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    main_ready = threading.Event()
    stop_main = threading.Event()
    warm_error: list[BaseException] = []

    def _main_loop_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _hold_with_waiters() -> None:
            # Hold the only connection so further checkouts park on the queue.
            session_cm = factory()
            session = await session_cm.__aenter__()
            await session.execute(text("select 1"))

            async def _waiter() -> None:
                try:
                    async with factory() as s2:
                        await s2.execute(text("select 1"))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — expected under teardown
                    pass

            waiters = [asyncio.create_task(_waiter()) for _ in range(5)]
            await asyncio.sleep(0.15)  # park waiters on the pool queue
            main_ready.set()
            while not stop_main.is_set():
                await asyncio.sleep(0.05)
            await session_cm.__aexit__(None, None, None)
            for task in waiters:
                task.cancel()

        try:
            loop.run_until_complete(_hold_with_waiters())
        except BaseException as exc:  # noqa: BLE001
            warm_error.append(exc)
            main_ready.set()
        finally:
            loop.close()

    main_t = threading.Thread(target=_main_loop_thread, name="fake-fastapi")
    main_t.start()
    assert main_ready.wait(timeout=30), f"main loop failed to warm: {warm_error!r}"

    errors: list[BaseException] = []

    async def _use_again() -> str:
        async with factory() as session:
            await session.execute(text("select 1"))
            return "ok"

    def _worker() -> None:
        try:
            asyncio.run(_use_again())
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()

    stop_main.set()
    main_t.join(timeout=30)

    assert errors, (
        "Expected the unfixed QueuePool+asyncio.run pattern to fail; "
        "got success — the regression no longer reproduces on this platform."
    )
    assert any(is_event_loop_mismatch(e) for e in errors), (
        f"Expected event-loop mismatch; got {errors!r}"
    )

    engine.sync_engine.dispose()


def test_fixed_path_survives_contention_via_run_coro_sync(tmp_path: Path) -> None:
    """FIXED: NullPool engine + run_coro_sync under the same contention shape."""
    from sqlalchemy import text

    reset_for_tests()
    url = _file_url(tmp_path)
    asyncio.run(_create_schema(url))
    db_mod.init_engine(url)
    assert db_mod.get_engine().pool.__class__ is NullPool

    main_ready = threading.Event()
    stop_main = threading.Event()

    def _main_loop_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _idle() -> None:
            # Touch the engine on the main loop (captures main for the bridge).
            async with db_mod.get_session() as session:
                await session.execute(text("select 1"))
            capture_main_loop(loop)
            main_ready.set()
            while not stop_main.is_set():
                await asyncio.sleep(0.05)

        try:
            loop.run_until_complete(_idle())
        finally:
            loop.close()

    main_t = threading.Thread(target=_main_loop_thread, name="fake-fastapi")
    main_t.start()
    assert main_ready.wait(timeout=30)

    counter = {"n": 0}

    def _fetch() -> dict:
        counter["n"] += 1
        return {"n": counter["n"]}

    async def _once() -> dict:
        return await cached_call(
            kind=CacheKind.PRICES,
            provider="loop_safety_contention",
            key="k1",
            ttl_seconds=3600,
            fetch=_fetch,
        )

    # First fill via the bridge (lands on main loop).
    first = run_coro_sync(_once())
    assert first["n"] == 1

    errors: list[BaseException] = []
    results: dict[str, object] = {}

    def _worker() -> None:
        try:
            results["worker"] = run_coro_sync(_once())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=_worker) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
        assert not w.is_alive()

    stop_main.set()
    main_t.join(timeout=30)

    assert not errors, f"fixed path failed under contention: {errors!r}"
    assert results["worker"] == first
    assert counter["n"] == 1, "cache must be honored"

    # Nested running-loop call must not deadlock.
    async def _nested() -> dict:
        return run_coro_sync(_once())

    assert asyncio.run(_nested()) == first
    assert counter["n"] == 1

    asyncio.run(db_mod.dispose_engine())


def test_fixed_cached_call_from_worker_thread_and_running_loop(
    tmp_path: Path,
) -> None:
    """FIXED path: worker thread + nested running-loop both succeed; cache hits."""
    reset_for_tests()
    url = _file_url(tmp_path)
    asyncio.run(_create_schema(url))
    db_mod.init_engine(url)

    # Confirm production file engines use NullPool (the shared-layer fix).
    assert db_mod.get_engine().pool.__class__ is NullPool

    counter = {"n": 0}

    def _fetch() -> dict:
        counter["n"] += 1
        return {"n": counter["n"], "at": datetime.now(timezone.utc).isoformat()}

    async def _once() -> dict:
        return await cached_call(
            kind=CacheKind.PRICES,
            provider="loop_safety_test",
            key="k1",
            ttl_seconds=3600,
            fetch=_fetch,
        )

    # Warm on a first loop (simulates app traffic).
    first = asyncio.run(_once())
    assert first["n"] == 1
    assert counter["n"] == 1

    # Worker thread via run_coro_sync (the fixed sync bridge) — must hit cache.
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            results["worker"] = run_coro_sync(_once())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert not errors, f"worker failed: {errors!r}"
    assert results["worker"] == first
    assert counter["n"] == 1, "cache must be honored across the bridge"

    # Inside a running event loop (nested) — run_coro_sync must not deadlock.
    async def _nested() -> dict:
        return run_coro_sync(_once())

    nested = asyncio.run(_nested())
    assert nested == first
    assert counter["n"] == 1

    # Direct await on a fresh loop still works (NullPool).
    again = asyncio.run(_once())
    assert again == first
    assert counter["n"] == 1

    asyncio.run(db_mod.dispose_engine())


def test_note_event_loop_mismatch_is_loud() -> None:
    reset_for_tests()
    n = note_event_loop_mismatch(
        scope="unit.test",
        error="Queue is bound to a different event loop",
        symbol="TEST",
    )
    assert n >= 1
    assert is_event_loop_mismatch(
        RuntimeError("<Queue> is bound to a different event loop")
    )
    assert not is_event_loop_mismatch(RuntimeError("timeout"))


def test_health_recovers_after_mismatch_window_expires() -> None:
    """``/health`` is recency-scoped — must return to ok after the window.

    Would FAIL against the old ever-since-boot latch: once note_* fired,
    status stayed ``degraded`` forever (and poisoned later health tests).
    """
    reset_for_tests()
    # Wide window so create_app() + first /health cannot race past expiry.
    set_mismatch_health_window_for_tests(60.0)

    note_event_loop_mismatch(
        scope="unit.health_recovery",
        error="Queue is bound to a different event loop",
    )
    assert recent_mismatch_count() >= 1
    assert mismatch_count() >= 1

    from argosy.api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)

    async def _get_health() -> dict:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")
            assert resp.status_code == 200
            return resp.json()

    body = asyncio.run(_get_health())
    assert body["status"] == "degraded", body
    assert body["event_loop_mismatches_recent"] >= 1
    assert body["event_loop_mismatches_lifetime"] >= 1

    # Collapse the window → prune all timestamps → health must recover
    # without a process restart (proves recovery is possible).
    set_mismatch_health_window_for_tests(0.0)
    assert recent_mismatch_count() == 0
    body2 = asyncio.run(_get_health())
    assert body2["status"] == "ok", body2
    assert body2["event_loop_mismatches_recent"] == 0
    # Lifetime total remains (informational); does not latch status.
    assert body2["event_loop_mismatches_lifetime"] >= 1


def test_run_coro_sync_times_out_loudly() -> None:
    """Saturated / hung target loop must raise TimeoutError, not hang.

    Timeout is an infra data miss — must increment the degradation counter
    (would FAIL if timeouts were silent: health stays ok, job omits
    infra_degraded, frozen prices look healthy).
    """
    reset_for_tests()
    before = mismatch_count()

    async def _hang() -> None:
        await asyncio.sleep(60)

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out") as ei:
        run_coro_sync(_hang(), timeout=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"timeout took too long: {elapsed:.1f}s"
    assert is_bridge_timeout(ei.value)
    assert is_infra_data_failure(ei.value)
    assert mismatch_count() == before + 1
    assert recent_mismatch_count() >= 1


def test_run_coro_sync_same_loop_as_target_refuses_immediately() -> None:
    """Production capture: calling loop **is** the target → fail fast.

    Must NOT reset capture first (that made the old test hit the bridge
    loop — a different loop — and mask the deadlock). Captures the running
    loop as main (FastAPI-shaped), then calls ``run_coro_sync`` from that
    same loop. Would FAIL against the old offload-and-block-same-thread
    path (120s deadlock then a misleading timeout).
    """
    reset_for_tests()
    before = mismatch_count()

    async def _prod_shaped() -> None:
        capture_main_loop()  # production: this thread owns the target
        assert get_main_loop() is asyncio.get_running_loop()

        async def _work() -> str:
            return "should-not-run"

        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="await the coroutine directly"):
            run_coro_sync(_work(), timeout=120.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"same-loop path hung instead of refusing: {elapsed:.1f}s"

    asyncio.run(_prod_shaped())
    assert mismatch_count() == before + 1
    assert recent_mismatch_count() >= 1


def test_run_coro_sync_from_different_running_loop_offloads() -> None:
    """Caller loop ≠ target loop → offload blocking wait; must complete.

    No captured main → target is the dedicated bridge loop, which is a
    *different* loop from ``asyncio.run``'s loop. Offload is correct here.
    """
    reset_for_tests()
    assert get_main_loop() is None

    async def _work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    async def _nested() -> str:
        return run_coro_sync(_work(), timeout=5.0)

    assert asyncio.run(_nested()) == "ok"


def test_run_coro_sync_falls_back_to_bridge_without_main_loop() -> None:
    """CLI / no captured main → dedicated bridge loop."""
    reset_for_tests()
    assert get_main_loop() is None

    async def _work() -> str:
        return "bridge"

    assert run_coro_sync(_work(), timeout=5.0) == "bridge"


def test_tenant_file_engine_uses_nullpool(tmp_path: Path, monkeypatch) -> None:
    """Per-tenant engines must use NullPool (same rule as global db.py).

    Would FAIL if tenancy/database.py still used default AsyncAdaptedQueuePool.
    """
    from sqlalchemy.pool import NullPool

    from argosy.config import get_settings
    from argosy.tenancy import database as tenant_db

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    get_settings.cache_clear()
    asyncio.run(tenant_db.reset_tenant_engines())

    eng = tenant_db.get_tenant_engine("test-tenant-nullpool")
    assert eng.pool.__class__ is NullPool
    asyncio.run(tenant_db.reset_tenant_engines())
    get_settings.cache_clear()


def test_snapshot_refresh_job_marks_infra_degraded_on_timeout() -> None:
    """Bridge timeout during refresh → job summary is loud (infra_degraded).

    Would FAIL if timeouts were omitted from mismatch_count / job delta.
    """
    reset_for_tests()

    from argosy.services.jobs.snapshot_refresh_job import SnapshotRefreshJob

    class _Res:
        def summary(self) -> dict:
            return {"repriced": 0, "carried": 1}

    def _refresh_that_times_out(session, **_kw):  # noqa: ANN001
        async def _hang() -> None:
            await asyncio.sleep(60)

        try:
            run_coro_sync(_hang(), timeout=0.15)
        except TimeoutError:
            pass
        return _Res()

    job = SnapshotRefreshJob(
        enabled=False,
        session_factory=lambda: type("S", (), {"close": lambda self: None})(),
        refresh_fn=_refresh_that_times_out,
    )

    async def _tick() -> dict:
        return await job.tick()

    summary = asyncio.run(_tick())
    assert summary["infra_degraded"] is True
    assert summary["infra_data_failures"] >= 1
    assert summary["event_loop_mismatches"] >= 1


def test_thesis_monitor_summary_aggregates_infra_degraded() -> None:
    """Bundle infra_degraded must reach the persisted job summary.

    Would FAIL against the pre-fix path that only set flags on bundles
    and reported a healthy completed assessment (HOLD/LOW epidemic).
    """
    from datetime import datetime, timezone

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.orchestrator.loops.thesis_monitor import ThesisMonitorLoop
    from argosy.state.models import Base

    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    class _Assessment:
        ticker = "NVDA"
        severity = "info"
        action = "hold"
        confidence = "low"

    class _Out:
        assessments = [_Assessment()]

    class _Report:
        output = _Out()

    class _Agent:
        async def run(self, *, bundles):  # noqa: ANN001
            assert bundles[0].get("infra_degraded") is True
            return _Report()

    def _holdings(session, user_id):  # noqa: ANN001
        return [{"ticker": "NVDA", "shares": 1.0}]

    def _gather(holding, *, now):  # noqa: ANN001
        return {
            **holding,
            "news": [],
            "insider": [],
            "price": {},
            "infra_degraded": True,
            "infra_data_failures": 2,
            "infra_loop_mismatches": 2,
        }

    loop = ThesisMonitorLoop(
        enabled=False,
        session_factory=factory,
        holdings_fn=_holdings,
        gather_fn=_gather,
        agent_factory=lambda: _Agent(),
        write_fn=lambda *a, **k: None,
    )
    summary = loop._run_sync(run_at=datetime.now(timezone.utc))
    assert summary["infra_degraded"] is True
    assert summary["infra_degraded_holdings"] == 1
    assert summary["infra_data_failures"] == 2
    assert summary["event_loop_mismatches"] == 2
    assert summary["assessed"] == 1


@pytest.mark.asyncio
async def test_cached_call_still_works_on_memory_engine(engine: None) -> None:
    """Existing in-memory StaticPool path (tests) must keep working."""
    hits = {"n": 0}

    def _fetch() -> dict:
        hits["n"] += 1
        return {"ok": True}

    a = await cached_call(
        kind=CacheKind.UI,
        provider="loop_safety_mem",
        key="m1",
        ttl_seconds=60,
        fetch=_fetch,
    )
    b = await cached_call(
        kind=CacheKind.UI,
        provider="loop_safety_mem",
        key="m1",
        ttl_seconds=60,
        fetch=_fetch,
    )
    assert a == b == {"ok": True}
    assert hits["n"] == 1
