"""Tests for ``argosy/orchestrator/loops/state_observer.py`` (Spec B commit #7).

Coverage:

  * **Defaults** — cron is ``"0 17 * * *"``, timezone is
    ``"Asia/Jerusalem"``, ``cadences.state_observer.enabled`` defaults
    True. Pinning the schedule prevents a silent drift from the
    17:00 IDT contract advertised in the SDD.
  * **Metadata** — :func:`state_observer_metadata` returns
    ``source_kind='monitor'`` (the observer surfaces flags on the
    Red-Flag Strip).
  * **Happy path** — ``tick`` exercises the full pipeline against a
    fake StateObserverAgent that returns 2 candidates; one snapshot
    row lands + two ``monitor_flags`` rows are written + the summary
    dict carries the expected counts.
  * **Cool-off** — back-to-back ``tick`` calls inside
    ``min_run_interval_minutes`` cause the second one to return early
    with ``skipped_reason='cool_off'`` and no new snapshot.
  * **force=True bypasses cool-off** — the backfill / manual-override
    path forces a second run within the cool-off window.
  * **On-demand trigger entry point** —
    :func:`run_state_observer_now` forwards through to
    ``StateObserverLoop.tick`` exactly once with the trigger_reason
    plumbed through. Pre-built loop injection works.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_state_observer_loop.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agent_settings import AgentSettings
from argosy.agents.base import ConfidenceBand
from argosy.agents.state_observer import FlagCandidate, StateObserverOutput
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.state_observer import (
    MIN_RUN_INTERVAL_MINUTES,
    StateObserverLoop,
    run_state_observer_now,
    state_observer_metadata,
)
from argosy.state.models import Base, MonitorFlag, StateSnapshot, User


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_proposer_hook(monkeypatch):
    """Disable the observer→action_proposer wiring for loop tests.

    The wiring (Spec E commit #2) fires a live ``ActionProposerAgent``
    LLM call on every warning/critical flag write — fine in production
    but it would hang the loop's pipeline tests (whose contract is the
    snapshot→diff→agent→flag chain, not the downstream proposer call).
    See ``tests/test_state_observer_proposer_wired.py`` for the
    end-to-end wiring test with a mocked LLM.
    """
    monkeypatch.setattr(
        "argosy.services.state_observer_flag_writer."
        "INVOKE_ACTION_PROPOSER_ON_FLAG",
        False,
    )


@pytest.fixture
def sync_session_factory(tmp_path):
    """Build a sync sessionmaker over a tmp_path sqlite DB."""
    db_path = tmp_path / "state_observer_loop.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    # Mirror migration 0049's partial-unique index — same setup the
    # flag-writer tests use so the dedup contract is exercised.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ix_monitor_flags_observer_dedup "
            "ON monitor_flags (user_id, dedup_key) "
            "WHERE dedup_key IS NOT NULL AND acknowledged_at IS NULL"
        ))

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    # Seed the user row (FK target).
    db = SessionLocal()
    try:
        db.add(User(id=USER, plan="free"))
        db.commit()
    finally:
        db.close()
    yield SessionLocal
    engine.dispose()


def _now() -> datetime:
    return datetime(2026, 5, 29, 17, 0, 0, tzinfo=timezone.utc)


def _make_state_collected() -> dict[str, Any]:
    """Return the dict ``collect_state_snapshot`` would have produced.

    Shape: ``{"state": {...six sections...}, "source_versions": {...}}``.
    """
    return {
        "state": {
            "plan_inputs": {
                "assumed_fx_usd_nis": 3.6,
                "assumed_mu_nominal_annual": 0.08,
                "assumed_retirement_age": 62.0,
                "assumed_monthly_expenses_nis": 25000.0,
            },
            "portfolio": {
                "total_value_usd": 1_500_000.0,
                "allocations": [
                    {
                        "category": "Growth",
                        "current_pct": 0.52,
                        "target_pct": 0.40,
                        "current_k_usd": 780.0,
                        "target_k_usd": 600.0,
                    },
                ],
                "top_concentration_pct": 0.34,
            },
            "macro": {
                "fx_usd_nis_spot": 2.81,
                "fx_usd_nis_30d_avg": 2.85,
                "recent_high_materiality_news": [],
                "recent_news_summary": {},
            },
            "cashflow_recent": {"last_3_months": []},
            "tax_assumptions": {},
            "metadata": {
                "snapshot_id": None,
                "user_id": USER,
                "snapshot_date": "2026-05-29",
                "plan_version_id": 1,
            },
        },
        "source_versions": {
            "schema_migration_head": "0049",
            "historical_replay_gaps": [],
            "trigger_reason": "daily_cron",
        },
    }


@dataclass
class _FakeReport:
    """Stand-in for ``AgentReport`` — only ``output`` is read."""
    output: StateObserverOutput


@dataclass
class _FakeAgent:
    """Records ``run`` invocations + emits a canned list of candidates.

    Used in place of the real ``StateObserverAgent`` so tests don't hit
    Anthropic.
    """
    candidates: list[FlagCandidate] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, **kwargs: Any) -> _FakeReport:
        self.calls.append(kwargs)
        return _FakeReport(output=StateObserverOutput(
            flag_candidates=list(self.candidates),
            overall_assessment="(fake agent canned output)",
            confidence=ConfidenceBand.HIGH,
            cited_sources=[
                c.primary_field for c in self.candidates
            ],
        ))


def _make_candidate(
    *,
    primary_field: str,
    severity: str = "warning",
    inferred_kind: str = "fx_observation",
    deviation_bucket: str = "large",
    rationale_md: str = "Plan baseline drift.",
) -> FlagCandidate:
    return FlagCandidate(
        severity=severity,  # type: ignore[arg-type]
        primary_field=primary_field,
        related_fields=[],
        rationale_md=rationale_md,
        inferred_kind=inferred_kind,
        deviation_bucket=deviation_bucket,  # type: ignore[arg-type]
        mitigation_hint=None,
        confidence=ConfidenceBand.HIGH,
        validator_actions=[],
    )


def _make_loop(
    *,
    session_factory: Any,
    agent: _FakeAgent,
    now_fn: Any = None,
    min_run_interval_minutes: int = MIN_RUN_INTERVAL_MINUTES,
) -> StateObserverLoop:
    """Construct a loop whose external services are all stubbed.

    - collect_fn returns a fixed state dict.
    - diff_fn returns a tiny FullDiff-like dict (the flag writer doesn't
      need the diff; the agent is stubbed too).
    - persist_fn delegates to the real ``persist_state_snapshot`` (so
      ``state_snapshots`` rows actually land — cool-off needs them).
    - write_fn delegates to the real ``write_observer_flags`` (so
      ``monitor_flags`` rows actually land).
    """
    from argosy.services.state_snapshot import persist_state_snapshot
    from argosy.services.state_observer_flag_writer import (
        write_observer_flags,
    )

    def _collect(session, user_id, *, as_of=None, trigger_reason="manual"):
        # Return a fresh dict each call so persist_state_snapshot's
        # JSON round-trip succeeds.
        return _make_state_collected()

    def _diff(current, plan_baseline, prior_snapshot, **_kwargs):
        # A minimal dict-shape FullDiff that satisfies the agent stub.
        return {
            "vs_plan": [{"path": "macro.fx_usd_nis_spot"}],
            "vs_prior": [],
        }

    return StateObserverLoop(
        schedule=LoopSchedule(cron="0 17 * * *", timezone="Asia/Jerusalem"),
        enabled=True,
        user_id=USER,
        session_factory=session_factory,
        collect_fn=_collect,
        persist_fn=persist_state_snapshot,
        diff_fn=_diff,
        agent_factory=lambda: agent,
        write_fn=write_observer_flags,
        now_fn=now_fn or _now,
        min_run_interval_minutes=min_run_interval_minutes,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_cron_is_17_idt() -> None:
    """The agent_settings default cron MUST be 17:00 Asia/Jerusalem.

    The SDD § Sprint B advertises that the observer fires "alongside
    news_daily" at 17:00 IDT. A change here is a behavioral break the
    SDD must follow.
    """
    cfg = AgentSettings().cadences.state_observer
    assert cfg.enabled is True
    assert cfg.cron == "0 17 * * *"
    assert cfg.timezone == "Asia/Jerusalem"


def test_metadata_source_kind_is_monitor() -> None:
    """Observer flags surface on the Red-Flag Strip; source_kind='monitor'."""
    meta = state_observer_metadata()
    assert meta.name == "state_observer_daily"
    assert meta.source_kind == "monitor"
    assert meta.schedule_cron == "0 17 * * *"
    assert meta.long_running is False


def test_loop_default_schedule_matches_metadata() -> None:
    """A loop built with defaults uses the same cron + tz the metadata
    advertises."""
    loop = StateObserverLoop()
    assert loop.schedule.cron == "0 17 * * *"
    assert loop.schedule.timezone == "Asia/Jerusalem"
    assert loop.enabled is True


def test_loop_rejects_negative_cool_off() -> None:
    """The constructor must validate that the cool-off window is non-
    negative — a negative value would invert the comparison and silently
    skip every run."""
    with pytest.raises(ValueError):
        StateObserverLoop(min_run_interval_minutes=-1)


# ---------------------------------------------------------------------------
# Happy path — tick() runs the full pipeline.
# ---------------------------------------------------------------------------


def test_tick_happy_path_two_candidates(sync_session_factory) -> None:
    """tick() collects → persists → diffs → runs agent → writes flags.

    With 2 candidates the summary reports:
      candidates_emitted=2 / flags_written=2 / deduplicated=0.
    """
    agent = _FakeAgent(candidates=[
        _make_candidate(
            primary_field="macro.fx_usd_nis_spot",
            inferred_kind="fx_observation",
            severity="critical",
        ),
        _make_candidate(
            primary_field="portfolio.top_concentration_pct",
            inferred_kind="concentration_observation",
            severity="warning",
            deviation_bucket="moderate",
        ),
    ])
    loop = _make_loop(session_factory=sync_session_factory, agent=agent)

    summary = asyncio.run(loop.tick(trigger_reason="manual"))

    assert summary is not None
    assert summary["skipped_reason"] is None
    assert summary["candidates_emitted"] == 2
    assert summary["flags_written"] == 2
    assert summary["flags_deduplicated"] == 0
    assert summary["snapshot_id"] > 0
    assert summary["trigger_reason"] == "manual"

    # Snapshot row landed.
    db = sync_session_factory()
    try:
        snapshots = db.execute(sa.select(StateSnapshot)).scalars().all()
        assert len(snapshots) == 1
        # Flags landed with the expected kinds.
        flags = db.execute(
            sa.select(MonitorFlag).order_by(MonitorFlag.id)
        ).scalars().all()
        assert len(flags) == 2
        kinds = {f.kind for f in flags}
        assert kinds == {
            "state_observer_fx_observation",
            "state_observer_concentration_observation",
        }
    finally:
        db.close()

    # And the agent was driven exactly once with the expected fields.
    assert len(agent.calls) == 1
    call = agent.calls[0]
    assert call["user_id"] == USER
    assert call["trigger_reason"] == "manual"
    assert "current_state" in call
    assert "full_diff" in call


# ---------------------------------------------------------------------------
# Cool-off (spec §4.4)
# ---------------------------------------------------------------------------


def test_cool_off_skips_second_tick_within_window(sync_session_factory) -> None:
    """A second tick() inside the cool-off window returns
    ``skipped_reason='cool_off'`` and writes no new rows."""
    first_call_at = datetime(2026, 5, 29, 17, 0, 0, tzinfo=timezone.utc)
    # Second call is 10 minutes later — well inside the 6h default.
    second_call_at = first_call_at + timedelta(minutes=10)

    times = iter([first_call_at, second_call_at])

    def _stepping_now() -> datetime:
        return next(times)

    agent = _FakeAgent(candidates=[
        _make_candidate(primary_field="macro.fx_usd_nis_spot"),
    ])
    loop = _make_loop(
        session_factory=sync_session_factory,
        agent=agent,
        now_fn=_stepping_now,
    )

    first = asyncio.run(loop.tick(trigger_reason="daily_cron"))
    second = asyncio.run(loop.tick(trigger_reason="snapshot_upload"))

    assert first["skipped_reason"] is None
    assert first["flags_written"] == 1

    assert second["skipped_reason"] == "cool_off"
    assert second["flags_written"] == 0
    assert second["candidates_emitted"] == 0
    # The snapshot_id surfaced on the skip points at the prior run's
    # row so the caller can audit which snapshot blocked the re-fire.
    assert second["snapshot_id"] == first["snapshot_id"]

    # Confirm no second snapshot row was persisted.
    db = sync_session_factory()
    try:
        snapshots = db.execute(sa.select(StateSnapshot)).scalars().all()
        assert len(snapshots) == 1
    finally:
        db.close()

    # Agent was driven exactly once (cool-off skipped before the agent
    # call).
    assert len(agent.calls) == 1


def test_force_bypasses_cool_off(sync_session_factory) -> None:
    """``force=True`` runs the pipeline even inside the cool-off window."""
    first_call_at = datetime(2026, 5, 29, 17, 0, 0, tzinfo=timezone.utc)
    # Second call is 5 minutes later — clearly inside any reasonable
    # cool-off — and uses a DIFFERENT snapshot_date so persistence
    # doesn't violate the (user_id, snapshot_date) UNIQUE constraint.
    second_call_at = first_call_at + timedelta(minutes=5)
    times = iter([first_call_at, second_call_at])

    def _stepping_now() -> datetime:
        return next(times)

    agent = _FakeAgent(candidates=[
        _make_candidate(primary_field="macro.fx_usd_nis_spot"),
    ])

    # We need the second collect to produce a different snapshot_date,
    # otherwise the persist step raises IntegrityError on the UNIQUE
    # (user_id, snapshot_date) constraint. We rebuild the loop with a
    # collect_fn that varies the date by call count.
    call_count = {"n": 0}

    def _collect(session, user_id, *, as_of=None, trigger_reason="manual"):
        call_count["n"] += 1
        data = _make_state_collected()
        # Increment the snapshot date on the second call.
        data["state"]["metadata"]["snapshot_date"] = (
            "2026-05-29" if call_count["n"] == 1 else "2026-05-30"
        )
        return data

    from argosy.services.state_snapshot import persist_state_snapshot
    from argosy.services.state_observer_flag_writer import (
        write_observer_flags,
    )

    def _diff(current, plan_baseline, prior_snapshot, **_kwargs):
        return {"vs_plan": [{"path": "macro.fx_usd_nis_spot"}], "vs_prior": []}

    loop = StateObserverLoop(
        user_id=USER,
        session_factory=sync_session_factory,
        collect_fn=_collect,
        persist_fn=persist_state_snapshot,
        diff_fn=_diff,
        agent_factory=lambda: agent,
        write_fn=write_observer_flags,
        now_fn=_stepping_now,
        min_run_interval_minutes=MIN_RUN_INTERVAL_MINUTES,
    )

    first = asyncio.run(loop.tick(trigger_reason="daily_cron"))
    second = asyncio.run(
        loop.tick(trigger_reason="backfill", force=True)
    )

    assert first["skipped_reason"] is None
    assert first["flags_written"] == 1
    assert second["skipped_reason"] is None
    assert second["snapshot_id"] != first["snapshot_id"]
    # Same FX-observation flag — dedup_key collides, so the second write
    # is deduplicated (the cool-off bypass exercises the run path; flag-
    # level dedup is its own contract).
    assert second["candidates_emitted"] == 1
    assert second["flags_written"] + second["flags_deduplicated"] == 1

    # Two snapshot rows now exist.
    db = sync_session_factory()
    try:
        snapshots = db.execute(sa.select(StateSnapshot)).scalars().all()
        assert len(snapshots) == 2
    finally:
        db.close()
    assert len(agent.calls) == 2


# ---------------------------------------------------------------------------
# On-demand trigger entry point
# ---------------------------------------------------------------------------


def test_run_state_observer_now_uses_injected_loop(
    sync_session_factory,
) -> None:
    """``run_state_observer_now`` forwards to ``tick`` with the requested
    trigger_reason and respects the cool-off contract."""
    agent = _FakeAgent(candidates=[
        _make_candidate(primary_field="macro.fx_usd_nis_spot"),
    ])
    loop = _make_loop(session_factory=sync_session_factory, agent=agent)

    summary = asyncio.run(run_state_observer_now(
        USER, trigger_reason="snapshot_upload", loop=loop,
    ))
    assert summary is not None
    assert summary["skipped_reason"] is None
    assert summary["flags_written"] == 1
    assert summary["trigger_reason"] == "snapshot_upload"


def test_run_state_observer_now_force_flag(sync_session_factory) -> None:
    """``force=True`` propagates through the convenience wrapper."""
    times = iter([
        datetime(2026, 5, 29, 17, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 29, 17, 5, 0, tzinfo=timezone.utc),
    ])
    agent = _FakeAgent(candidates=[
        _make_candidate(primary_field="macro.fx_usd_nis_spot"),
    ])
    call_count = {"n": 0}

    def _collect(session, user_id, *, as_of=None, trigger_reason="manual"):
        call_count["n"] += 1
        data = _make_state_collected()
        data["state"]["metadata"]["snapshot_date"] = (
            "2026-05-29" if call_count["n"] == 1 else "2026-05-30"
        )
        return data

    from argosy.services.state_snapshot import persist_state_snapshot
    from argosy.services.state_observer_flag_writer import (
        write_observer_flags,
    )

    def _diff(current, plan_baseline, prior_snapshot, **_kwargs):
        return {"vs_plan": [{"path": "macro.fx_usd_nis_spot"}], "vs_prior": []}

    loop = StateObserverLoop(
        user_id=USER,
        session_factory=sync_session_factory,
        collect_fn=_collect,
        persist_fn=persist_state_snapshot,
        diff_fn=_diff,
        agent_factory=lambda: agent,
        write_fn=write_observer_flags,
        now_fn=lambda: next(times),
        min_run_interval_minutes=MIN_RUN_INTERVAL_MINUTES,
    )

    first = asyncio.run(run_state_observer_now(USER, loop=loop))
    second = asyncio.run(run_state_observer_now(
        USER, trigger_reason="backfill", force=True, loop=loop,
    ))
    assert first["skipped_reason"] is None
    assert second["skipped_reason"] is None
    assert second["trigger_reason"] == "backfill"
