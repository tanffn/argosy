"""Tests for ``argosy/services/replan_dispatcher.py`` (Spec E commit #4).

The observer→replan dispatcher is the highest-oscillation-risk surface
in the Spec E sprint per the spec text.  Coverage focuses on:

  * **Mapping table comprehensiveness** — every entry in
    ``OBSERVER_FLAG_TO_TRIGGER_KIND`` resolves to a known trigger_kind
    and fires correctly for a critical-severity flag.
  * **Severity gate** — warning maps to ``dry_run_logged`` (no
    JobRegistry.fire_now call); critical maps to ``fired``.
  * **Cooldown** — a 2nd critical fire within 72h for the same
    (user, trigger_kind) skips with ``status='skipped_cooldown'``.
  * **Global cap** — the 5th ``fired`` row across any trigger_kind
    within 72h skips with ``status='skipped_global_cap'``.
  * **Atomic gate (BEGIN IMMEDIATE)** — two concurrent calls on the
    same user race only one through; the other gets ``skipped_cooldown``.
  * **Dry-run does NOT call JobRegistry.fire_now** — mock asserted
    not_called for warning-band fires.
  * **Unmapped flag.kind** — concentration / allocation / cash etc.
    return ``status='skipped_severity'`` with a synthetic dispatch
    trigger_kind.
  * **Idempotency-on-retry** — when JobRegistry.fire_now raises, the
    row flips to ``status='error'`` AND the cooldown gate sees that
    error row, suppressing immediate re-fire.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_replan_dispatcher.py -v
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.replan_dispatcher import (
    COOLDOWN_HOURS_BY_KIND,
    GLOBAL_CAP_FIRED_COUNT,
    OBSERVER_FLAG_TO_TRIGGER_KIND,
    maybe_dispatch_replan,
)
from argosy.services.retirement.replan_triggers import (
    ALL_DISPATCH_TRIGGER_KINDS,
)
from argosy.state.models import (
    Base,
    JobRun,
    MonitorFlag,
    ReplanDispatchLog,
    User,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB.

    Mirrors the pattern in tests/test_state_observer_flag_writer.py.
    The dispatcher's BEGIN IMMEDIATE relies on a real SQLite engine
    (not the in-memory ``sqlite://`` which doesn't honor file-level
    locks across connections); the file-backed DB exercises the
    same lock primitives that production hits.
    """
    db_path = tmp_path / "replan_dispatcher.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        # isolation_level=None puts SQLAlchemy in autocommit so we
        # manage transactions explicitly via BEGIN IMMEDIATE in the
        # dispatcher.  Without this, SQLAlchemy's autobegin would
        # start a DEFERRED transaction before our BEGIN IMMEDIATE.
        isolation_level=None,
    )
    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _now() -> datetime:
    return datetime(2026, 5, 30, 17, 0, 0, tzinfo=timezone.utc)


def _make_flag(
    session,
    *,
    kind: str = "state_observer_fx_observation",
    severity: str = "critical",
    user_id: str = USER,
    surfaced_at: datetime | None = None,
) -> MonitorFlag:
    """Insert + return a MonitorFlag row mimicking a real observer write."""
    if surfaced_at is None:
        surfaced_at = _now()
    row = MonitorFlag(
        user_id=user_id,
        kind=kind,
        severity=severity,
        payload=json.dumps(
            {
                "snapshot_id": 1,
                "primary_field": "macro.fx_usd_nis_spot",
                "deviation_bucket": "extreme",
            }
        ),
        surfaced_at=surfaced_at.replace(tzinfo=None),
        expires_at=(surfaced_at + timedelta(days=7)).replace(tzinfo=None),
        dedup_key=None,
    )
    session.add(row)
    session.commit()
    return row


def _make_mock_registry(run_id: int = 999, raises: Exception | None = None):
    """Build a mock JobRegistry whose fire_now returns ``run_id``.

    The dispatcher's ``_call_fire_now`` bridges to async via
    ``asyncio.run`` (when no loop is running) — mock with
    AsyncMock so the awaitable shape matches.
    """
    registry = MagicMock()
    if raises is not None:
        registry.fire_now = AsyncMock(side_effect=raises)
    else:
        registry.fire_now = AsyncMock(return_value=run_id)
    return registry


# ---------------------------------------------------------------------------
# Mapping table coverage
# ---------------------------------------------------------------------------


def test_mapping_table_aligned_with_replan_triggers_enum():
    """Every trigger_kind in the mapping is in ALL_DISPATCH_TRIGGER_KINDS.

    Pins the invariant that the dispatcher's mapping table cannot
    drift from the CHECK enum in migration 0056 — if a future spec
    adds a mapping with a new trigger_kind, this test fails until
    the enum is extended too.
    """
    for flag_kind, trigger_kind in OBSERVER_FLAG_TO_TRIGGER_KIND.items():
        assert trigger_kind in ALL_DISPATCH_TRIGGER_KINDS, (
            f"trigger_kind {trigger_kind!r} for flag_kind {flag_kind!r} "
            f"is not in ALL_DISPATCH_TRIGGER_KINDS"
        )


@pytest.mark.parametrize(
    "flag_kind,expected_trigger_kind",
    list(OBSERVER_FLAG_TO_TRIGGER_KIND.items()),
)
def test_mapping_coverage_critical_fires(
    sync_session, flag_kind, expected_trigger_kind
):
    """Each mapped flag_kind at critical severity FIRES with the right kind.

    The spec lists 7 mappings; this parametrized test asserts every
    entry resolves to the expected trigger_kind on a critical-severity
    flag arrival.
    """
    flag = _make_flag(sync_session, kind=flag_kind, severity="critical")
    registry = _make_mock_registry(run_id=42)

    log = maybe_dispatch_replan(
        sync_session,
        flag,
        now=_now(),
        job_registry=registry,
    )

    assert log.status == "fired", (
        f"flag_kind={flag_kind!r} expected fired, got {log.status!r}: "
        f"{log.notes!r}"
    )
    assert log.trigger_kind == expected_trigger_kind
    assert log.severity == "critical"
    assert log.source_flag_id == flag.id
    assert log.job_run_id == 42
    registry.fire_now.assert_called_once()


# ---------------------------------------------------------------------------
# Severity gate
# ---------------------------------------------------------------------------


def test_warning_severity_dry_run_logged(sync_session):
    """A warning-severity flag whose trigger_kind floor IS 'warning'
    writes dry_run_logged + does NOT fire.

    Spec §4.2 — only ``critical`` AUTO-FIRES a plan_synthesis job;
    ``warning`` is dry-run-logged for audit visibility even when the
    per-trigger-kind floor admits warnings.  Uses
    ``state_observer_cashflow_observation`` whose trigger_kind
    (``life_event``) has a ``warning`` floor in
    MIN_SEVERITY_FOR_TRIGGER — so we exercise the "mapped, severity-
    floor-met, but still NOT critical so dry-run-logged" path.
    """
    flag = _make_flag(
        sync_session,
        kind="state_observer_cashflow_observation",
        severity="warning",
    )
    registry = _make_mock_registry()

    log = maybe_dispatch_replan(
        sync_session,
        flag,
        now=_now(),
        job_registry=registry,
    )

    assert log.status == "dry_run_logged"
    assert log.job_run_id is None
    assert log.trigger_kind == "observer_emergent_warning_dry_run"
    assert log.notes is not None
    assert "life_event" in log.notes  # records the would-be trigger
    registry.fire_now.assert_not_called()


def test_warning_severity_below_per_kind_floor_skipped(sync_session):
    """A warning fx_observation flag (critical floor) → skipped_severity.

    Counterpart to test_warning_severity_dry_run_logged: when the
    per-kind floor is ``critical``, a warning flag doesn't even reach
    the dry-run-logged path — it's skipped at the severity gate.
    """
    flag = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="warning",
    )
    registry = _make_mock_registry()

    log = maybe_dispatch_replan(
        sync_session,
        flag,
        now=_now(),
        job_registry=registry,
    )

    assert log.status == "skipped_severity"
    assert log.job_run_id is None
    registry.fire_now.assert_not_called()


def test_info_severity_skipped(sync_session):
    """An info-severity flag is skipped (below floor) and does NOT fire."""
    flag = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="info",
    )
    registry = _make_mock_registry()

    log = maybe_dispatch_replan(
        sync_session,
        flag,
        now=_now(),
        job_registry=registry,
    )

    assert log.status == "skipped_severity"
    registry.fire_now.assert_not_called()


def test_critical_severity_fires(sync_session):
    """A critical-severity flag (mapped) fires + writes job_run_id."""
    flag = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=77)

    log = maybe_dispatch_replan(
        sync_session,
        flag,
        now=_now(),
        job_registry=registry,
    )

    assert log.status == "fired"
    assert log.job_run_id == 77
    assert log.trigger_kind == "fx_shock_10pct"
    registry.fire_now.assert_called_once_with(
        "plan_synthesis",
        triggered_by=f"replan_dispatch:flag_id={flag.id}",
    )


# ---------------------------------------------------------------------------
# Cooldown gate
# ---------------------------------------------------------------------------


def test_second_critical_within_cooldown_skipped(sync_session):
    """2nd critical fire of same trigger_kind within 72h → skipped_cooldown."""
    flag1 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=1)

    log1 = maybe_dispatch_replan(
        sync_session, flag1, now=_now(), job_registry=registry
    )
    assert log1.status == "fired"

    # Fresh flag, 1 hour later — well within the 72h cooldown for
    # fx_shock_10pct.
    flag2 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
        surfaced_at=_now() + timedelta(hours=1),
    )
    log2 = maybe_dispatch_replan(
        sync_session,
        flag2,
        now=_now() + timedelta(hours=1),
        job_registry=registry,
    )
    assert log2.status == "skipped_cooldown"
    # The cooldown notes should mention remaining minutes.
    assert log2.notes is not None
    assert "remaining" in log2.notes
    # fire_now should have been called exactly once (for flag1 only).
    assert registry.fire_now.call_count == 1


def test_cooldown_expires_after_window(sync_session):
    """A fire AFTER the cooldown window passes is allowed again."""
    cooldown_hours = COOLDOWN_HOURS_BY_KIND["fx_shock_10pct"]

    flag1 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=1)

    log1 = maybe_dispatch_replan(
        sync_session, flag1, now=_now(), job_registry=registry
    )
    assert log1.status == "fired"

    # Past the cooldown — 1 hour beyond the window.
    after = _now() + timedelta(hours=cooldown_hours + 1)
    flag2 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
        surfaced_at=after,
    )
    log2 = maybe_dispatch_replan(
        sync_session, flag2, now=after, job_registry=registry
    )
    assert log2.status == "fired"
    assert registry.fire_now.call_count == 2


def test_cooldown_separated_per_trigger_kind(sync_session):
    """Different trigger_kinds have INDEPENDENT cooldown windows."""
    flag1 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    flag2 = _make_flag(
        sync_session,
        kind="state_observer_equity_observation",
        severity="critical",
        surfaced_at=_now() + timedelta(minutes=5),
    )
    registry = _make_mock_registry(run_id=1)

    log1 = maybe_dispatch_replan(
        sync_session, flag1, now=_now(), job_registry=registry
    )
    log2 = maybe_dispatch_replan(
        sync_session,
        flag2,
        now=_now() + timedelta(minutes=5),
        job_registry=registry,
    )

    # Different trigger_kinds → both fire (no cooldown collision).
    assert log1.status == "fired"
    assert log2.status == "fired"
    assert log1.trigger_kind != log2.trigger_kind
    assert registry.fire_now.call_count == 2


# ---------------------------------------------------------------------------
# Global cap gate
# ---------------------------------------------------------------------------


def test_global_cap_blocks_fifth_fire(sync_session):
    """The 5th 'fired' row across any trigger_kind within 72h → skipped."""
    # Pre-seed the table with GLOBAL_CAP_FIRED_COUNT fired rows
    # across mixed trigger_kinds, all within the last 72h.  The
    # 5th attempt should skip with status='skipped_global_cap'.
    base = _now() - timedelta(hours=1)
    for i, trigger_kind in enumerate(
        # 4 mixed trigger_kinds to fill the cap; they're real
        # values from ALL_DISPATCH_TRIGGER_KINDS.
        [
            "fx_shock_10pct",
            "market_drawdown_15pct",
            "tax_law_change",
            "life_event",
        ][:GLOBAL_CAP_FIRED_COUNT]
    ):
        sync_session.add(
            ReplanDispatchLog(
                user_id=USER,
                source_flag_id=None,
                trigger_kind=trigger_kind,
                severity="critical",
                status="fired",
                job_run_id=None,
                dispatched_at=(base - timedelta(minutes=i)).replace(
                    tzinfo=None
                ),
                notes=None,
            )
        )
    sync_session.commit()

    # 5th attempt — a fresh trigger_kind that has no per-kind cooldown
    # collision, so the global cap is the only gate that can fail.
    flag = _make_flag(
        sync_session,
        kind="state_observer_plan_assumption_observation",
        severity="critical",
    )
    registry = _make_mock_registry()

    log = maybe_dispatch_replan(
        sync_session, flag, now=_now(), job_registry=registry
    )

    assert log.status == "skipped_global_cap"
    assert log.notes is not None
    assert "global cap" in log.notes
    registry.fire_now.assert_not_called()


def test_global_cap_does_not_count_skipped_rows(sync_session):
    """Skipped rows in the audit log don't count toward the global cap."""
    # Pre-seed 4 'skipped_cooldown' rows — these should NOT count.
    base = _now() - timedelta(hours=1)
    for i in range(GLOBAL_CAP_FIRED_COUNT):
        sync_session.add(
            ReplanDispatchLog(
                user_id=USER,
                source_flag_id=None,
                trigger_kind="fx_shock_10pct",
                severity="critical",
                status="skipped_cooldown",
                job_run_id=None,
                dispatched_at=(base - timedelta(minutes=i)).replace(
                    tzinfo=None
                ),
                notes=None,
            )
        )
    sync_session.commit()

    flag = _make_flag(
        sync_session,
        kind="state_observer_equity_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=1)

    log = maybe_dispatch_replan(
        sync_session, flag, now=_now(), job_registry=registry
    )
    assert log.status == "fired"
    registry.fire_now.assert_called_once()


# ---------------------------------------------------------------------------
# Unmapped flag.kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unmapped_kind",
    [
        "state_observer_concentration_observation",
        "state_observer_allocation_observation",
        "state_observer_volatility_observation",
        "state_observer_position_observation",
        "state_observer_cash_observation",
        "state_observer_other_observation",
    ],
)
def test_unmapped_flag_kind_skipped(sync_session, unmapped_kind):
    """Unmapped flag.kind values write skipped_severity with the synthetic kind."""
    flag = _make_flag(
        sync_session, kind=unmapped_kind, severity="critical"
    )
    registry = _make_mock_registry()

    log = maybe_dispatch_replan(
        sync_session, flag, now=_now(), job_registry=registry
    )
    assert log.status == "skipped_severity"
    # The synthetic trigger_kind covers both "unmapped" and
    # "below-floor" cases.
    assert log.trigger_kind == "observer_emergent_warning_dry_run"
    assert log.notes is not None
    assert "not in OBSERVER_FLAG_TO_TRIGGER_KIND" in log.notes
    registry.fire_now.assert_not_called()


# ---------------------------------------------------------------------------
# Atomic gate / concurrency
# ---------------------------------------------------------------------------


def test_atomic_gate_two_concurrent_dispatchers_only_one_fires(tmp_path):
    """Two concurrent maybe_dispatch_replan calls → only ONE fires.

    The other gets ``skipped_cooldown`` (the race-loser path: it
    sees the just-committed 'fired' row from the winner inside
    its own BEGIN IMMEDIATE transaction).

    Uses two THREADS + a real file-backed SQLite DB so the
    BEGIN IMMEDIATE lock primitives exercise their actual
    serialization behavior — an in-memory DB or a single-thread
    test wouldn't surface a race here.
    """
    db_path = tmp_path / "atomic_race.db"
    # File DB; check_same_thread=False so threads share the engine.
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
        isolation_level=None,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed user + two distinct flags (same kind, same critical
    # severity) so each thread has its own flag row to pass in.
    setup_db = SessionLocal()
    setup_db.add(User(id=USER, plan="free"))
    setup_db.commit()
    flag_a = MonitorFlag(
        user_id=USER,
        kind="state_observer_fx_observation",
        severity="critical",
        payload="{}",
        surfaced_at=_now().replace(tzinfo=None),
        expires_at=(_now() + timedelta(days=7)).replace(tzinfo=None),
        dedup_key=None,
    )
    flag_b = MonitorFlag(
        user_id=USER,
        kind="state_observer_fx_observation",
        severity="critical",
        payload="{}",
        surfaced_at=_now().replace(tzinfo=None),
        expires_at=(_now() + timedelta(days=7)).replace(tzinfo=None),
        dedup_key=None,
    )
    setup_db.add_all([flag_a, flag_b])
    setup_db.commit()
    flag_a_id = flag_a.id
    flag_b_id = flag_b.id
    setup_db.close()

    # Barrier so both threads enter maybe_dispatch_replan at the
    # same instant — without the barrier the test is timing-sensitive
    # and one call would naturally finish before the other starts.
    barrier = threading.Barrier(2)
    results: list = [None, None]
    exceptions: list = [None, None]

    def _worker(idx: int, flag_id: int) -> None:
        try:
            db = SessionLocal()
            try:
                flag = (
                    db.query(MonitorFlag)
                    .filter(MonitorFlag.id == flag_id)
                    .one()
                )
                # A registry mock per thread; both return distinct
                # job_run_ids so we can tell which won.
                registry = _make_mock_registry(run_id=1000 + idx)
                barrier.wait(timeout=5)
                results[idx] = maybe_dispatch_replan(
                    db, flag, now=_now(), job_registry=registry
                )
            finally:
                db.close()
        except Exception as exc:
            exceptions[idx] = exc

    t1 = threading.Thread(target=_worker, args=(0, flag_a_id))
    t2 = threading.Thread(target=_worker, args=(1, flag_b_id))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # Surface any worker exception loudly.
    for i, exc in enumerate(exceptions):
        assert exc is None, f"worker {i} raised: {exc!r}"

    # Assert: exactly ONE thread fires, the other gets a skip.
    statuses = sorted([results[0].status, results[1].status])
    assert statuses == ["fired", "skipped_cooldown"], (
        f"expected one fired + one skipped_cooldown, got {statuses!r}"
    )

    # And only one fired row landed in the table.
    check_db = SessionLocal()
    try:
        fired_count = (
            check_db.query(ReplanDispatchLog)
            .filter(ReplanDispatchLog.status == "fired")
            .count()
        )
        skipped_count = (
            check_db.query(ReplanDispatchLog)
            .filter(ReplanDispatchLog.status == "skipped_cooldown")
            .count()
        )
        assert fired_count == 1
        assert skipped_count == 1
    finally:
        check_db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Idempotency-on-retry — JobRegistry.fire_now raises
# ---------------------------------------------------------------------------


def test_fire_now_raises_flips_to_error_and_blocks_immediate_refire(
    sync_session,
):
    """A JobRegistry.fire_now exception flips status='error'; cooldown holds.

    Codex single-dispatch focus — idempotency on retry: a transient
    JobRegistry failure should NOT immediately re-fire on the next
    observer pass (the operator wants to see the error in the audit
    log + diagnose, not have the system thrash).
    """
    flag1 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    raising_registry = _make_mock_registry(raises=RuntimeError("boom"))

    log1 = maybe_dispatch_replan(
        sync_session, flag1, now=_now(), job_registry=raising_registry
    )
    assert log1.status == "error"
    assert log1.job_run_id is None
    assert log1.notes is not None
    assert "boom" in log1.notes

    # Second attempt within the cooldown — should be suppressed.
    flag2 = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
        surfaced_at=_now() + timedelta(minutes=10),
    )
    working_registry = _make_mock_registry(run_id=99)
    log2 = maybe_dispatch_replan(
        sync_session,
        flag2,
        now=_now() + timedelta(minutes=10),
        job_registry=working_registry,
    )
    assert log2.status == "skipped_cooldown", (
        f"expected error row to hold the cooldown, got {log2.status!r}"
    )
    working_registry.fire_now.assert_not_called()


# ---------------------------------------------------------------------------
# No JobRegistry bound — gate logic still exercised
# ---------------------------------------------------------------------------


def test_no_registry_writes_fired_row_with_no_job_run_id(sync_session):
    """job_registry=None still writes the gate-decision row; notes flag it."""
    flag = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )

    log = maybe_dispatch_replan(
        sync_session, flag, now=_now(), job_registry=None
    )

    assert log.status == "fired"
    assert log.job_run_id is None
    assert log.notes == "no_job_registry_bound"


# ---------------------------------------------------------------------------
# Mapping shape sanity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex BLOCKER fix coverage — caller-session isolation + async-bridge
# deadlock prevention
# ---------------------------------------------------------------------------


def test_caller_session_state_not_mutated_by_dispatcher(sync_session):
    """Codex BLOCKER fix #1 — caller session pending work survives dispatch.

    Stage uncommitted work in the caller's session BEFORE invoking
    the dispatcher; assert the work is STILL pending after the
    dispatcher returns.  The dispatcher must not call
    ``rollback()`` on the caller session.
    """
    # Stage a pending MonitorFlag on the caller session — not
    # committed.
    pending = MonitorFlag(
        user_id=USER,
        kind="state_observer_other_observation",
        severity="info",
        payload="{}",
        surfaced_at=_now().replace(tzinfo=None),
        expires_at=(_now() + timedelta(days=7)).replace(tzinfo=None),
        dedup_key=None,
    )
    sync_session.add(pending)
    sync_session.flush()  # gets the id without committing
    pending_id = pending.id

    # Now invoke the dispatcher on a separately-committed flag.
    fired_flag = _make_flag(
        sync_session,  # this commits the flag
        kind="state_observer_fx_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=1)
    log = maybe_dispatch_replan(
        sync_session, fired_flag, now=_now(), job_registry=registry
    )
    assert log.status == "fired"

    # Assert: the pending in-flight transaction object on the caller
    # session is still there (it would be gone if the dispatcher
    # rolled back the caller session).
    sync_session.commit()  # commit the pending work
    still_there = (
        sync_session.query(MonitorFlag)
        .filter(MonitorFlag.id == pending_id)
        .one_or_none()
    )
    assert still_there is not None, (
        "Caller-session pending work was destroyed by the dispatcher "
        "— codex BLOCKER #1 (rollback on caller session) regressed."
    )


def test_call_fire_now_in_running_loop_flips_to_error(sync_session):
    """Codex BLOCKER fix #2 — refuse to bridge when an event loop is live.

    The previous implementation used
    ``asyncio.run_coroutine_threadsafe(..., loop).result()`` from
    the loop's own thread — a deadlock because the loop can't run
    the scheduled coroutine while the thread is blocked on the
    future.  The new behavior raises RuntimeError from
    ``_call_fire_now``; the dispatcher's outer fire-now try/except
    catches it, flips the row to ``status='error'``, and the
    cooldown gate then blocks immediate re-fire (same path as a
    JobRegistry.fire_now that raised any other RuntimeError).

    Inspect the resulting log row to confirm the failure mode is
    well-defined (not a hang, not a silent swallow).
    """
    flag = _make_flag(
        sync_session,
        kind="state_observer_fx_observation",
        severity="critical",
    )
    registry = _make_mock_registry(run_id=1)

    async def _exercise() -> "ReplanDispatchLog":
        # Inside an async context: a loop IS running in this thread.
        # The dispatcher's _call_fire_now should raise RuntimeError;
        # the dispatcher's fire_now-failure path converts that into
        # a status='error' log row.
        return maybe_dispatch_replan(
            sync_session,
            flag,
            now=_now(),
            job_registry=registry,
        )

    import asyncio as _asyncio

    log = _asyncio.run(_exercise())

    # The row is flipped to 'error' — no hang, no silent swallow.
    assert log.status == "error"
    assert log.notes is not None
    assert "event loop" in log.notes
    assert log.job_run_id is None


def test_mapping_table_has_expected_seven_entries():
    """Spec §4.2 — the mapping table covers exactly the spec's 7 entries.

    If the mapping shrinks or grows, this test fails so the operator
    is forced to update both the dispatcher AND this test
    intentionally (no silent drift).  The 7th spec entry (inferred
    life-event accept → life_event) is NOT in the dispatcher map
    because that path is fired from the proposer Accept handler,
    not from the observer flag writer.  So the writer-facing
    mapping has 6 entries plus a 7th covered downstream.
    """
    assert len(OBSERVER_FLAG_TO_TRIGGER_KIND) == 6, (
        f"OBSERVER_FLAG_TO_TRIGGER_KIND has "
        f"{len(OBSERVER_FLAG_TO_TRIGGER_KIND)} entries; expected 6 "
        f"(the spec's 7th entry — inferred life-event accept — is "
        f"fired from the proposer Accept handler, not the writer)."
    )
