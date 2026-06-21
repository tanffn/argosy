"""Tests for monitor-flag lifecycle: producer-scope supersession, within-run
dedup, created_at/status population, and the active-only query contract.

These exercise the root fix for the Home Red-Flag Strip accumulating stale /
duplicate flags (migration 0072 ``status`` column + producer-scope supersede
in ``write_observer_flags`` + the plan-promotion supersede helper).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_monitor_flag_supersession.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import ConfidenceBand
from argosy.agents.state_observer import FlagCandidate
from argosy.services.state_observer_flag_writer import (
    supersede_plan_assumption_flags,
    write_observer_flags,
)
from argosy.state.models import Base, MonitorFlag, User

USER = "ariel"
SNAPSHOT_ID = 17


@pytest.fixture(autouse=True)
def _disable_proposer_hook(monkeypatch):
    monkeypatch.setattr(
        "argosy.services.state_observer_flag_writer."
        "INVOKE_ACTION_PROPOSER_ON_FLAG",
        False,
    )


@pytest.fixture
def sync_session(tmp_path):
    db_path = tmp_path / "supersede.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ix_monitor_flags_observer_dedup "
            "ON monitor_flags (user_id, dedup_key) "
            "WHERE dedup_key IS NOT NULL AND acknowledged_at IS NULL"
        ))
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _cand(
    *,
    primary_field: str,
    inferred_kind: str,
    severity: str = "warning",
    deviation_bucket: str = "large",
) -> FlagCandidate:
    return FlagCandidate(
        severity=severity,  # type: ignore[arg-type]
        primary_field=primary_field,
        related_fields=[],
        rationale_md="r",
        inferred_kind=inferred_kind,
        deviation_bucket=deviation_bucket,  # type: ignore[arg-type]
        mitigation_hint=None,
        confidence=ConfidenceBand.HIGH,
        validator_actions=[],
    )


def _now(day: int = 1) -> datetime:
    return datetime(2026, 6, day, 17, 0, 0, tzinfo=timezone.utc)


def _active_rows(session) -> list[MonitorFlag]:
    return list(
        session.execute(
            sa.select(MonitorFlag)
            .where(MonitorFlag.status == "active")
            .where(MonitorFlag.acknowledged_at.is_(None))
            .order_by(MonitorFlag.surfaced_at.desc())
        ).scalars()
    )


# ---------------------------------------------------------------------------
# created_at + status populated at write time
# ---------------------------------------------------------------------------


def test_created_at_and_status_populated(sync_session):
    write_observer_flags(
        sync_session,
        USER,
        [_cand(primary_field="macro.fx_usd_nis_spot", inferred_kind="fx_observation")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(),
    )
    row = sync_session.execute(sa.select(MonitorFlag)).scalars().one()
    assert row.created_at is not None
    assert row.status == "active"


# ---------------------------------------------------------------------------
# Within-run dedup by (kind, primary_field)
# ---------------------------------------------------------------------------


def test_dedup_within_run_same_topic_different_bucket(sync_session):
    """Two candidates for the same field at different buckets in ONE pass →
    one row written, the second deduplicated."""
    cands = [
        _cand(
            primary_field="macro.fx_usd_nis_spot",
            inferred_kind="fx_observation",
            deviation_bucket="large",
        ),
        _cand(
            primary_field="macro.fx_usd_nis_spot",
            inferred_kind="fx_observation",
            deviation_bucket="extreme",
        ),
    ]
    summary = write_observer_flags(
        sync_session, USER, cands, snapshot_id=SNAPSHOT_ID, now=_now(),
    )
    assert summary.written_count == 1
    assert summary.deduplicated_count == 1
    assert len(_active_rows(sync_session)) == 1


# ---------------------------------------------------------------------------
# Producer-scope supersession across runs
# ---------------------------------------------------------------------------


def test_second_run_supersedes_first_runs_stale_flags(sync_session):
    """A 2nd observer run supersedes the 1st run's flags that it did NOT
    re-observe (different field / bucket-jittered key)."""
    # Run 1 — two observations.
    write_observer_flags(
        sync_session,
        USER,
        [
            _cand(primary_field="macro.fx_usd_nis_spot", inferred_kind="fx_observation",
                  deviation_bucket="large"),
            _cand(primary_field="portfolio.allocations[1].current_pct",
                  inferred_kind="allocation_observation", deviation_bucket="extreme"),
        ],
        snapshot_id=SNAPSHOT_ID,
        now=_now(1),
    )
    assert len(_active_rows(sync_session)) == 2

    # Run 2 — fx still flagged (SAME dedup_key), but allocation jittered to a
    # different bucket key and a NEW cashflow observation appears.
    summary = write_observer_flags(
        sync_session,
        USER,
        [
            _cand(primary_field="macro.fx_usd_nis_spot", inferred_kind="fx_observation",
                  deviation_bucket="large"),
            _cand(primary_field="cashflow_recent.last_3_months[1].realized_income_nis",
                  inferred_kind="cashflow_observation", deviation_bucket="large"),
        ],
        snapshot_id=SNAPSHOT_ID,
        now=_now(2),
    )
    active = _active_rows(sync_session)
    # The stale allocation flag from run 1 must be superseded.
    assert summary.superseded_count == 1
    kinds = sorted(r.kind for r in active)
    assert kinds == [
        "state_observer_cashflow_observation",
        "state_observer_fx_observation",
    ]
    # The fx flag is the SAME row (dedup hit), not a duplicate.
    fx_rows = [r for r in active if r.kind == "state_observer_fx_observation"]
    assert len(fx_rows) == 1


def test_supersession_does_not_touch_other_producers(sync_session):
    """An observer run must not supersede a thesis_monitor / alpha flag."""
    # Seed a non-observer producer flag.
    sync_session.add(MonitorFlag(
        user_id=USER, kind="thesis_monitor_weakened", severity="warning",
        payload='{"ticker": "NVDA"}', surfaced_at=_now(1),
        dedup_key="v1|thesis_monitor|ariel|NVDA|weakened", status="active",
    ))
    sync_session.commit()

    write_observer_flags(
        sync_session,
        USER,
        [_cand(primary_field="macro.fx_usd_nis_spot", inferred_kind="fx_observation")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(2),
    )
    thesis = sync_session.execute(
        sa.select(MonitorFlag).where(MonitorFlag.kind == "thesis_monitor_weakened")
    ).scalars().one()
    assert thesis.status == "active"  # untouched


def test_empty_run_does_not_wipe_active_flags(sync_session):
    """An observer pass with NO candidates must not supersede live flags
    (absence of new observations is not evidence the old resolved)."""
    write_observer_flags(
        sync_session,
        USER,
        [_cand(primary_field="macro.fx_usd_nis_spot", inferred_kind="fx_observation")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(1),
    )
    assert len(_active_rows(sync_session)) == 1
    summary = write_observer_flags(
        sync_session, USER, [], snapshot_id=SNAPSHOT_ID, now=_now(2),
    )
    assert summary.superseded_count == 0
    assert len(_active_rows(sync_session)) == 1


# ---------------------------------------------------------------------------
# Plan-promotion supersedes stale plan-assumption flags
# ---------------------------------------------------------------------------


def test_plan_promotion_supersedes_stale_plan_assumption(sync_session):
    """A plan-assumption flag about a NON-current plan label is superseded
    on promotion; one about the current label survives."""
    # Stale flag (references the old rejected draft).
    sync_session.add(MonitorFlag(
        user_id=USER, kind="state_observer_plan_assumption_observation",
        severity="critical",
        payload='{"primary_field": "plan_inputs.plan_version_label", '
                '"rationale_md": "plan synth-2026-06-17-0356-fm-rejected ..."}',
        surfaced_at=_now(1),
        dedup_key="v1|state_observer|ariel|plan_assumption_observation|x|large",
        status="active",
    ))
    # A current-plan observation that should survive.
    sync_session.add(MonitorFlag(
        user_id=USER, kind="state_observer_plan_assumption_observation",
        severity="info",
        payload='{"primary_field": "plan_inputs.plan_version_label", '
                '"rationale_md": "current plan synth-2026-06-20-1852 ok"}',
        surfaced_at=_now(1),
        dedup_key="v1|state_observer|ariel|plan_assumption_observation|y|small",
        status="active",
    ))
    sync_session.commit()

    n = supersede_plan_assumption_flags(
        sync_session, USER, current_plan_label="synth-2026-06-20-1852",
    )
    assert n == 1
    active = _active_rows(sync_session)
    assert len(active) == 1
    assert "synth-2026-06-20-1852" in active[0].payload
