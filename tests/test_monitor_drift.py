"""Tests for the monitor agent's allocation-drift trigger (spec §5.1.1).

Sprint commit #11. Covers the fire rules, severity bands, and the
hysteresis state (consecutive-snapshot counting via monitor_flags
history). The detector reads the latest persisted PortfolioSnapshot
row and writes a monitor_flags row per fired drift; we drive it by
seeding rows directly through the ORM.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings
from argosy.services.plan_monitor import (
    DEFAULT_REL_DRIFT_PERSISTENT,
    DEFAULT_REL_DRIFT_SINGLE_SHOT,
    AllocationDriftFlag,
    check_allocation_drift,
    get_active_drift_flags,
)
from argosy.state.models import MonitorFlag, PortfolioSnapshotRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_factory():
    """Sessionmaker pointed at the active ARGOSY_HOME DB (set by fixture)."""
    engine = sa.create_engine(f"sqlite:///{get_settings().db_file}")
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_snapshot(
    session,
    *,
    user_id: str = "ariel",
    snapshot_date: date | None = None,
    allocations: list[dict] | None = None,
) -> PortfolioSnapshotRow:
    """Insert one PortfolioSnapshotRow with the given allocation block."""
    if snapshot_date is None:
        snapshot_date = date(2026, 5, 29)
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date,
        imported_at=datetime.now(timezone.utc),
        source_path="test://",
        positions_json="[]",
        allocations_json=json.dumps(allocations or []),
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json="{}",
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    return row


def _alloc(
    category: str,
    *,
    pct: float,
    target_pct: float,
    usd_value_k: float,
    target_k: float,
) -> dict:
    """One AllocationRow dict matching the JSON shape persisted by the snapshot store."""
    return {
        "category": category,
        "pct": pct,
        "usd_value_k": usd_value_k,
        "target_pct": target_pct,
        "target_k": target_k,
        "delta_k": target_k - usd_value_k,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_snapshot_returns_no_flags(argosy_home_db):
    """No PortfolioSnapshotRow for the user -> no flags, rows_evaluated=0."""
    SF = _session_factory()
    sess = SF()
    try:
        result = check_allocation_drift(sess, "ariel")
        assert result.flags_fired == []
        assert result.rows_evaluated == 0
        assert result.snapshot_date is None
    finally:
        sess.close()


def test_on_target_rows_do_not_fire(argosy_home_db):
    """Snapshot whose rows match targets -> no flags."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_snapshot(sess, allocations=[
            _alloc("Cash", pct=10.0, target_pct=10.0,
                   usd_value_k=100, target_k=100),
            _alloc("Growth", pct=60.0, target_pct=60.0,
                   usd_value_k=600, target_k=600),
            _alloc("Defensive", pct=30.0, target_pct=30.0,
                   usd_value_k=300, target_k=300),
        ])
        result = check_allocation_drift(sess, "ariel")
        assert result.flags_fired == []
        assert result.rows_evaluated == 3
    finally:
        sess.close()


def test_single_shot_severe_drift_fires_immediately(argosy_home_db):
    """rel_drift = 0.25, abs_drift = $10K -> fires on first snapshot."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_snapshot(sess, allocations=[
            # Growth: current 75pct vs target 60pct => rel_drift = 0.25
            # current 750k, target 600k => abs_drift = $150K  (well > $5K)
            _alloc("Growth", pct=75.0, target_pct=60.0,
                   usd_value_k=750, target_k=600),
            _alloc("Cash", pct=25.0, target_pct=40.0,
                   usd_value_k=250, target_k=400),
        ])
        result = check_allocation_drift(sess, "ariel")
        # Both rows are in drift -- rel_drift Growth=0.25, Cash=0.375
        assert len(result.flags_fired) == 2
        cats = {f.row_category for f in result.flags_fired}
        assert cats == {"Growth", "Cash"}
        # Persisted to monitor_flags
        n_rows = sess.query(MonitorFlag).count()
        assert n_rows == 2
    finally:
        sess.close()


def test_persistent_moderate_drift_does_not_fire_on_first_snapshot(argosy_home_db):
    """rel_drift = 0.12 (between persistent and single-shot), no prior history -> no fire."""
    SF = _session_factory()
    sess = SF()
    try:
        # current 67.2 vs target 60 => rel_drift = 0.12; abs_drift = $72K (≫ $5K)
        _seed_snapshot(sess, allocations=[
            _alloc("Growth", pct=67.2, target_pct=60.0,
                   usd_value_k=672, target_k=600),
            _alloc("Cash", pct=32.8, target_pct=40.0,
                   usd_value_k=328, target_k=400),
        ])
        result = check_allocation_drift(sess, "ariel")
        # rel_drift 0.12 < single_shot (0.20). No prior flags -> only this snapshot
        # in window (count=1), threshold is 2 -> no fire.
        assert result.flags_fired == []
        # rows_evaluated still counts both
        assert result.rows_evaluated == 2
        # Nothing was persisted
        assert sess.query(MonitorFlag).count() == 0
    finally:
        sess.close()


def test_persistent_moderate_drift_fires_on_second_snapshot(argosy_home_db):
    """Same Growth row in drift on snapshot N and N+1 -> fires on N+1."""
    SF = _session_factory()
    sess = SF()
    try:
        # Snapshot 1: moderate drift. Seed a prior monitor_flag manually as
        # if a previous detector run had fired or recorded the drift.
        # We instead just seed the snapshot, run the detector twice with
        # different snapshot dates to simulate the timeline.
        _seed_snapshot(sess, snapshot_date=date(2026, 4, 29), allocations=[
            _alloc("Growth", pct=67.2, target_pct=60.0,
                   usd_value_k=672, target_k=600),
        ])
        r1 = check_allocation_drift(sess, "ariel")
        # First snapshot doesn't fire (no prior history).
        assert r1.flags_fired == []

        # Seed a prior flag row directly to represent the previous month's
        # detector having identified the drift (the v1 hysteresis counts
        # raw monitor_flags rows in the window).
        sess.add(MonitorFlag(
            user_id="ariel",
            kind="allocation_drift",
            severity="info",
            payload=json.dumps({
                "snapshot_date": "2026-04-29",
                "row_category": "Growth",
                "rel_drift": 0.12,
                "abs_drift_usd": 72000.0,
                "suggested_proposals": [],
            }),
            surfaced_at=datetime.now(timezone.utc) - timedelta(days=30),
        ))
        sess.commit()

        # Snapshot 2: same drift one month later.
        _seed_snapshot(sess, snapshot_date=date(2026, 5, 29), allocations=[
            _alloc("Growth", pct=67.2, target_pct=60.0,
                   usd_value_k=672, target_k=600),
        ])
        r2 = check_allocation_drift(sess, "ariel")
        # Now the consecutive count (1 prior + 1 current) >= 2 -> fires.
        assert len(r2.flags_fired) == 1
        assert r2.flags_fired[0].row_category == "Growth"
        assert r2.flags_fired[0].severity == "info"
    finally:
        sess.close()


def test_drift_below_abs_min_does_not_fire(argosy_home_db):
    """Tiny sleeve with huge rel_drift but tiny absolute drift -> no fire."""
    SF = _session_factory()
    sess = SF()
    try:
        # Alternative: 1.5pct current vs 1pct target => rel_drift = 0.5
        # current $1.5K, target $1K => abs_drift = $500 (≪ $5K min)
        _seed_snapshot(sess, allocations=[
            _alloc("Alternative", pct=1.5, target_pct=1.0,
                   usd_value_k=1.5, target_k=1.0),
            _alloc("Cash", pct=98.5, target_pct=99.0,
                   usd_value_k=98.5, target_k=99.0),
        ])
        result = check_allocation_drift(sess, "ariel")
        assert result.flags_fired == []
        assert result.rows_evaluated == 2
    finally:
        sess.close()


def test_severity_bands(argosy_home_db):
    """Verify info/warning/critical assignment."""
    SF = _session_factory()
    sess = SF()
    try:
        # Single-shot threshold = 0.20; warning band starts at 0.20;
        # critical at 1.5 * 0.20 = 0.30.
        #   Growth A: rel_drift = 0.22 -> warning
        #   Growth B: rel_drift = 0.35 -> critical
        _seed_snapshot(sess, allocations=[
            _alloc("Growth", pct=73.2, target_pct=60.0,
                   usd_value_k=732, target_k=600),
            _alloc("Cash", pct=27.0, target_pct=40.0,
                   usd_value_k=270, target_k=400),
        ])
        result = check_allocation_drift(sess, "ariel")
        by_cat = {f.row_category: f for f in result.flags_fired}
        # Growth rel_drift = |73.2-60|/60 = 0.22 -> warning
        assert by_cat["Growth"].severity == "warning"
        # Cash rel_drift = |27-40|/40 = 0.325 -> critical (>= 0.30)
        assert by_cat["Cash"].severity == "critical"
    finally:
        sess.close()


def test_acknowledged_flags_filtered_from_active(argosy_home_db):
    """Acknowledged flags do not appear in get_active_drift_flags."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_snapshot(sess, allocations=[
            _alloc("Growth", pct=75.0, target_pct=60.0,
                   usd_value_k=750, target_k=600),
        ])
        check_allocation_drift(sess, "ariel")
        active = get_active_drift_flags(sess, "ariel")
        assert len(active) == 1

        # Acknowledge the only flag.
        flag_row = sess.query(MonitorFlag).first()
        assert flag_row is not None
        flag_row.acknowledged_at = datetime.now(timezone.utc)
        sess.commit()

        active = get_active_drift_flags(sess, "ariel")
        assert active == []
    finally:
        sess.close()


def test_expired_flags_filtered_from_active(argosy_home_db):
    """Flags past expires_at do not appear in get_active_drift_flags."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_snapshot(sess, allocations=[
            _alloc("Growth", pct=75.0, target_pct=60.0,
                   usd_value_k=750, target_k=600),
        ])
        check_allocation_drift(sess, "ariel")
        # Force the flag's expires_at into the past.
        flag_row = sess.query(MonitorFlag).first()
        assert flag_row is not None
        flag_row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        sess.commit()

        active = get_active_drift_flags(sess, "ariel")
        assert active == []
    finally:
        sess.close()


def test_correction_rerun_on_same_snapshot_does_not_double_count(argosy_home_db):
    """Codex BLOCKER (commit #11 review): hysteresis was counting raw
    monitor_flags rows, so a correction-rerun on the SAME snapshot
    would falsely satisfy the consecutive-snapshots threshold. The
    fix dedupes by payload['snapshot_date'] + excludes the current
    snapshot — so re-running the detector on a single snapshot can
    NEVER fire the persistent path.
    """
    SF = _session_factory()
    sess = SF()
    try:
        snap_date = date(2026, 5, 29)
        # Seed a prior flag row claiming THIS snapshot already drifted
        # (as if a previous correction-rerun had fired before fix).
        _seed_snapshot(sess, snapshot_date=snap_date, allocations=[
            _alloc("Growth", pct=67.2, target_pct=60.0,
                   usd_value_k=672, target_k=600),
        ])
        sess.add(MonitorFlag(
            user_id="ariel",
            kind="allocation_drift",
            severity="info",
            payload=json.dumps({
                "snapshot_date": snap_date.isoformat(),
                "row_category": "Growth",
                "rel_drift": 0.12,
                "abs_drift_usd": 72000.0,
            }),
        ))
        sess.commit()

        # Run detector. Pre-fix: this would have counted 1 prior row +
        # 1 current = 2 ≥ threshold → fire. Post-fix: the prior row's
        # snapshot_date == current snapshot_date so it's excluded;
        # distinct prior count = 0 + 1 current = 1 < 2 → no fire.
        result = check_allocation_drift(sess, "ariel")
        assert result.flags_fired == [], (
            "correction-rerun on the same snapshot must NOT trigger "
            "the persistent hysteresis path (codex BLOCKER)"
        )
    finally:
        sess.close()


def test_get_active_returns_dataclass_with_proposals(argosy_home_db):
    """Active flags carry the full AllocationDriftFlag shape including proposals."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_snapshot(sess, allocations=[
            _alloc("Growth", pct=75.0, target_pct=60.0,
                   usd_value_k=750, target_k=600),
            # Cash overweight by enough to fund the suggested buy.
            _alloc("Cash", pct=25.0, target_pct=40.0,
                   usd_value_k=250, target_k=400),
        ])
        check_allocation_drift(sess, "ariel")
        active = get_active_drift_flags(sess, "ariel")
        assert len(active) >= 1
        # Cash row is under-target so it doesn't get bought; Growth is
        # over-target so its flag's suggested buys target the OTHER
        # under-target classes. We just assert the type is right and
        # proposals serialize cleanly.
        for f in active:
            assert isinstance(f, AllocationDriftFlag)
            # round-trip serializable
            assert f.to_dict()["row_category"] == f.row_category
    finally:
        sess.close()
