"""Observer→replan dispatcher (Spec E commit #4).

Closes the loop between Spec B's state-observer flag writer and Spec
A's JobRegistry: when a high-severity observer flag arrives that maps
to a known replan trigger kind, queue a plan-synthesis job — gated by
severity, per-(user, trigger_kind) cooldown, and a global 72h cap.

The dispatcher is the highest-oscillation-risk surface in the whole
sprint per the spec; this module is the codex-single-dispatch-review
focus.  Four gates are evaluated inside ONE ``BEGIN IMMEDIATE``
SQLite transaction so two concurrent flag arrivals cannot race past
the cooldown check (codex BLOCKER #2 from spec §4.3).

Architectural contract
======================

Public entry point: ``maybe_dispatch_replan(session, monitor_flag,
*, now=None)``.  Called from
``argosy/services/state_observer_flag_writer.py`` after every flag
write whose severity is ``warning`` or ``critical``.  Returns a
``ReplanDispatchLog`` row capturing the gate decision.

Behavior matrix (spec §4.2 + §4.3):

  +-------------------+--------------+--------------------------------+
  | flag.kind         | flag.        | dispatcher decision            |
  |                   | severity     |                                |
  +-------------------+--------------+--------------------------------+
  | not in mapping    | (any)        | status='skipped_severity',     |
  |                   |              | trigger_kind=                  |
  |                   |              | observer_emergent_warning_     |
  |                   |              | dry_run (synthetic — the kind  |
  |                   |              | is unmapped so no classical    |
  |                   |              | trigger applies).              |
  +-------------------+--------------+--------------------------------+
  | mapped            | info         | status='skipped_severity'.     |
  +-------------------+--------------+--------------------------------+
  | mapped            | warning      | status='dry_run_logged',       |
  |                   |              | trigger_kind=                  |
  |                   |              | observer_emergent_warning_     |
  |                   |              | dry_run. Spec §4.2 — warning   |
  |                   |              | flags don't auto-fire.         |
  +-------------------+--------------+--------------------------------+
  | mapped            | critical     | classical trigger_kind from    |
  |                   |              | OBSERVER_FLAG_TO_TRIGGER_KIND. |
  |                   |              | Cooldown gate -> global cap    |
  |                   |              | gate -> fire JobRegistry.      |
  +-------------------+--------------+--------------------------------+

The synthetic ``observer_emergent_warning_dry_run`` trigger_kind on
dry-run rows lets the audit log distinguish "warning observer fire,
mapping known, not fired" from "the kind was unmapped" (which uses
the same synthetic kind because the spec defers the unmapped case
to the action_proposer, not to the replan dispatcher).

Concurrency contract (codex BLOCKER from spec §4.3 / §4.4)
==========================================================

The four gates (mapping/severity, cooldown, global cap, fire-or-not)
are evaluated inside ONE ``BEGIN IMMEDIATE`` SQLite transaction.
``BEGIN IMMEDIATE`` acquires a RESERVED lock on the database
immediately, blocking any other writer until commit.  This guarantees:

  * Two concurrent ``maybe_dispatch_replan`` calls cannot both see
    "no prior fired row" and both fire — the second blocks until the
    first commits, then re-reads the cooldown table and observes the
    fresh row.
  * The global cap cannot be bypassed by a flood of identical
    arrivals — the count query runs inside the same transaction as
    the insert, so a concurrent fire cannot land between the count
    and the insert.

Codex single-dispatch review BLOCKER fix #1 (2026-05-30): the
dispatcher opens its OWN fresh Session against the caller's engine
for the BEGIN IMMEDIATE block.  Issuing ``session.rollback()`` on
the caller's session would silently destroy pending uncommitted
work (e.g. the flag-writer's per-candidate state, downstream
savepoints) — the rollback is constrained to the dispatcher-owned
session, which has no pre-existing state.

Idempotency-on-retry (codex single-dispatch focus)
==================================================

When all gates pass, ``JobRegistry.fire_now`` is called AFTER the
dispatch log row is inserted with ``status='fired'`` (so the cooldown
gate sees it instantly for the next caller).  If ``fire_now`` raises:

  * The row's ``status`` is flipped to ``'error'`` AND ``notes`` is
    populated with the exception class + message.
  * ``job_run_id`` stays NULL.
  * The cooldown gate WILL still see this row as "fired-attempt within
    cooldown window" via the kind-specific lookup — we use ``status
    IN ('fired', 'error')`` for the cooldown gate so a transient
    JobRegistry failure does NOT immediately re-fire on the next
    observer pass.  Spec ambiguity, resolved per codex IMPORTANT.

A subsequent retry will see the existing ``error`` row inside the
cooldown window and skip with ``status='skipped_cooldown'`` — the
operator must either bump the cooldown or fix the underlying
JobRegistry issue before the next valid attempt.

Cooldown matrix
===============

Per-trigger-kind cooldown windows (spec §4.2):

  * fx_shock_10pct           — 72h (default)
  * market_drawdown_15pct    — 72h
  * tax_law_change           — 168h (7d — rate cycles move slowly)
  * life_event               — 168h
  * user_request             — 24h
  * health_event             — 72h (default)
  * job_change               — 72h (default)
  * observer_emergent_*      — 72h (default)

Global cap: 4 ``fired`` rows per (user_id) within any 72h window,
regardless of trigger_kind.  Catches multi-flag-kind storms.

Cross-references
================

  * Migration: ``alembic/versions/0056_replan_dispatch_log.py``.
  * Trigger enum: ``argosy/services/retirement/replan_triggers.py``.
  * Producer: ``argosy/services/state_observer_flag_writer.py`` calls
    ``maybe_dispatch_replan`` after every flag write whose severity is
    ``warning`` or ``critical``.
  * Consumer: ``argosy.jobs.JobRegistry.fire_now("plan_synthesis", ...)``.
  * Spec: §4 (architecture), §4.2 (mapping), §4.3 (gates), §4.4 (job),
    §9 commit #4 row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text as _sa_text, update
from sqlalchemy.orm import Session, sessionmaker

from argosy.state.models import MonitorFlag, ReplanDispatchLog

if TYPE_CHECKING:  # pragma: no cover — typing only
    from argosy.services.jobs.registry import JobRegistry


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — mapping table, cooldown matrix, cap, severity ranks.
# ---------------------------------------------------------------------------

#: Spec §4.2 — observer flag kind → replan trigger kind.  Only the
#: kinds for which a "the plan's inputs are stale enough to warrant a
#: full replan" call applies are present.  Unmapped kinds (e.g.
#: ``state_observer_concentration_observation`` which spec says goes
#: through the action_proposer rebalance path) deliberately fall
#: through to ``skipped_severity`` here.
#:
#: Spec §4.2 mapping table (the 7 entries per the prompt):
#:
#:   1. fx_observation              → fx_shock_10pct           (critical, 72h)
#:   2. equity_observation          → market_drawdown_15pct    (critical, 72h)
#:   3. rates_observation           → tax_law_change           (critical, 168h)
#:   4. cashflow_observation        → life_event               (warning, 168h)
#:   5. plan_assumption_observation → user_request             (warning, 24h)
#:   6. tax_observation             → tax_law_change           (warning, 168h)
#:   7. (inferred_life_event accept → life_event)              — handled
#:      downstream by the proposer Accept handler, NOT by this map.
#:
#: The five unmapped kinds (concentration / allocation / volatility /
#: position / cash) intentionally have no entry here per spec §4.2 —
#: they go through the action_proposer's other proposal kinds.  The
#: ``other_observation`` fallback is also unmapped (no clear trigger;
#: action_proposer handles it).
OBSERVER_FLAG_TO_TRIGGER_KIND: dict[str, str] = {
    "state_observer_fx_observation":              "fx_shock_10pct",
    "state_observer_equity_observation":          "market_drawdown_15pct",
    "state_observer_rates_observation":           "tax_law_change",
    "state_observer_cashflow_observation":        "life_event",
    "state_observer_plan_assumption_observation": "user_request",
    "state_observer_tax_observation":             "tax_law_change",
}


#: Spec §4.2 — minimum severity that triggers a CLASSICAL replan fire
#: per trigger_kind.  Below this band, the dispatcher writes
#: ``dry_run_logged`` (when severity == warning) or ``skipped_severity``
#: (when severity == info).
MIN_SEVERITY_FOR_TRIGGER: dict[str, str] = {
    "fx_shock_10pct":        "critical",
    "market_drawdown_15pct": "critical",
    "tax_law_change":        "warning",
    # ^ tax_law_change is shared by rates_observation (critical) and
    # tax_observation (warning).  Floor here is "warning" per spec
    # §4.2's tax_observation row.  The rates_observation path goes
    # through the same trigger kind but its severity is always
    # critical when written by the observer, so the floor doesn't
    # degrade the rates signal.
    "life_event":            "warning",
    "user_request":          "warning",
    # Defaults for the remaining classical TriggerKinds, mostly
    # unused at the dispatcher (they're fired by accept handlers or
    # manual paths).
    "health_event":          "critical",
    "job_change":            "warning",
}


#: Spec §4.2 — per-trigger-kind cooldown window in hours.  Default 72h
#: matches the spec's "72 h default" plus the per-kind overrides.
COOLDOWN_HOURS_BY_KIND: dict[str, int] = {
    "fx_shock_10pct":        72,
    "market_drawdown_15pct": 72,
    "tax_law_change":        168,  # 7d — rate/tax cycles move slowly
    "life_event":            168,
    "user_request":          24,
    "health_event":          72,
    "job_change":            72,
}


#: Default cooldown when a trigger_kind is missing from the matrix
#: above (defensive — every kind in ALL_DISPATCH_TRIGGER_KINDS should
#: have an explicit entry).
DEFAULT_COOLDOWN_HOURS: int = 72


#: Spec §4.3 — global cap.  Max ``fired`` rows per user across all
#: trigger_kinds within ``GLOBAL_CAP_WINDOW_HOURS``.
GLOBAL_CAP_FIRED_COUNT: int = 4
GLOBAL_CAP_WINDOW_HOURS: int = 72


#: Severity rank for the "min severity met" gate.  Higher number is
#: more severe.  Matches the convention in the state_observer + the
#: action_proposer.
_SEVERITY_RANK: dict[str, int] = {
    "info":     1,
    "warning":  2,
    "critical": 3,
}


#: Synthetic trigger_kind for dispatch decisions that didn't fire a
#: classical replan but DID get logged — either because the kind was
#: unmapped or because the severity didn't meet the per-trigger floor.
#: The two synthetic kinds let the operator distinguish "we saw a
#: warning, it mapped to a real trigger, we logged it but didn't
#: fire" from "we saw a critical, fired the trigger."
_DRY_RUN_TRIGGER_KIND: str = "observer_emergent_warning_dry_run"


#: Plan-synthesis job name fired via ``JobRegistry.fire_now``.  Spec
#: §4.4 calls this ``plan_synthesis``.  The dispatcher takes the job
#: name as an injectable constant so tests can mock the registry
#: without requiring the job to be registered in test setups.
PLAN_SYNTHESIS_JOB_NAME: str = "plan_synthesis"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def maybe_dispatch_replan(
    session: "Session",
    monitor_flag: MonitorFlag,
    *,
    now: datetime | None = None,
    job_registry: "JobRegistry | None" = None,
    job_name: str = PLAN_SYNTHESIS_JOB_NAME,
) -> ReplanDispatchLog:
    """Evaluate the four gates and (maybe) fire a plan-synthesis job.

    Spec §4.3 + §4.4 — the central dispatcher entry point called from
    the state-observer flag writer after every flag write whose
    severity is ``warning`` or ``critical``.  Returns the
    ``ReplanDispatchLog`` row that was written so the caller can
    introspect the outcome.

    The function ALWAYS writes a row (even for unmapped / skipped
    outcomes) so the admin UI's "why did the system not replan?"
    audit is complete.

    Args:
      session: live SQLAlchemy Session bound to a sync engine.  Used
        ONLY as a source of the underlying engine — the dispatcher
        opens its OWN fresh Session for the gate transaction so a
        ``rollback()`` inside the dispatcher cannot destroy pending
        caller work (codex BLOCKER fix #1).  The caller's session is
        not mutated at all by this function.
      monitor_flag: the just-written ``MonitorFlag`` row.  Must have
        ``id``, ``user_id``, ``kind``, ``severity`` populated.  The
        attributes are READ in the caller's session before the inner
        session opens, so detached-instance issues across the session
        boundary don't bite us.
      now: override for tests + deterministic backfills.  Defaults
        to ``datetime.now(timezone.utc)``.
      job_registry: the ``JobRegistry`` instance whose ``fire_now``
        gets called on a ``fired`` outcome.  When ``None`` (typical
        in tests of the gate logic itself), the dispatcher SKIPS the
        actual fire and still writes the ``fired`` log row with
        ``job_run_id=NULL`` + ``notes='no_job_registry_bound'``.
      job_name: the registered job name to fire.  Defaults to
        ``PLAN_SYNTHESIS_JOB_NAME``; tests override to verify the
        fire call without requiring plan_synthesis to be registered.

    Returns:
      The ``ReplanDispatchLog`` row that captures the dispatch
      decision.  The returned instance is bound to a SHORT-LIVED
      session that the dispatcher closes before returning — callers
      should treat it as a read-only snapshot (attribute reads work;
      ``session.refresh`` does not).

    Raises:
      RuntimeError when invoked from inside a running event loop AND
      a ``job_registry`` is provided AND the gates clear — the async
      bridge to ``fire_now`` cannot safely block-on-future from the
      loop's own thread (codex BLOCKER fix #2).
      Database errors are NOT caught (a failed insert means the audit
      row is missing entirely — the correct loud-failure signal for
      the operator).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Read flag attributes BEFORE we open the inner session so we
    # don't rely on the original ORM instance being attached to a
    # particular session at any later point.
    flag_kind = str(getattr(monitor_flag, "kind", "") or "")
    flag_severity = str(getattr(monitor_flag, "severity", "info") or "info")
    flag_id = getattr(monitor_flag, "id", None)
    user_id = str(getattr(monitor_flag, "user_id", "") or "")

    # --- Open a fresh inner session for the atomic gate block --------
    # Codex BLOCKER fix #1 (single-dispatch review 2026-05-30): the
    # dispatcher MUST NOT mutate the caller's session state.  The
    # caller may have pending uncommitted work — the flag writer's
    # per-candidate try/except, a downstream predictions writer's
    # savepoint, etc.  Any ``session.rollback()`` we issue would
    # silently destroy that work.  Open our own session against the
    # caller's engine; close it before returning.
    inner_session = _new_session_on_same_engine(session)
    try:
        log_row_id = _run_gate_block_atomically(
            inner_session,
            now=now,
            flag_kind=flag_kind,
            flag_severity=flag_severity,
            flag_id=flag_id,
            user_id=user_id,
        )
        # The gate block has committed.  Look up the row's final
        # status to decide whether to call fire_now.
        gate_row_status = inner_session.execute(
            select(ReplanDispatchLog.status).where(
                ReplanDispatchLog.id == log_row_id
            )
        ).scalar_one()

        if gate_row_status != "fired":
            # Skipped / dry-run — return without firing.
            return _detach_and_return(inner_session, log_row_id)

        # ---- Fire the JobRegistry OUTSIDE the BEGIN IMMEDIATE ----
        # transaction so the job's own DB writes don't deadlock on
        # our serialization lock.
        if job_registry is None:
            _stamp_notes(inner_session, log_row_id, "no_job_registry_bound")
            return _detach_and_return(inner_session, log_row_id)

        triggered_by = (
            f"replan_dispatch:flag_id={flag_id}"
            if flag_id is not None
            else "replan_dispatch"
        )
        try:
            job_run_id = _call_fire_now(
                job_registry, job_name, triggered_by=triggered_by
            )
        except Exception as exc:  # noqa: BLE001
            # Codex single-dispatch review focus — idempotency on
            # retry.  Flip status='fired' → 'error' but keep
            # job_run_id NULL.  The cooldown gate's ``status IN
            # ('fired','error')`` filter blocks immediate re-fire.
            _log.warning(
                "replan_dispatcher.fire_now_failed",
                extra={
                    "user_id": user_id,
                    "flag_id": flag_id,
                    "job_name": job_name,
                    "error": _short_exc(exc),
                },
            )
            _flip_to_error(inner_session, log_row_id, _short_exc(exc))
            return _detach_and_return(inner_session, log_row_id)

        _stamp_job_run_id(inner_session, log_row_id, job_run_id)
        return _detach_and_return(inner_session, log_row_id)
    finally:
        try:
            inner_session.close()
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# Gate block (BEGIN IMMEDIATE) — runs entirely against the inner session
# ---------------------------------------------------------------------------


def _run_gate_block_atomically(
    inner_session: "Session",
    *,
    now: datetime,
    flag_kind: str,
    flag_severity: str,
    flag_id: int | None,
    user_id: str,
) -> int:
    """Open BEGIN IMMEDIATE, run all four gates, insert + commit.

    Returns the ``replan_dispatch_log.id`` of the row that was
    inserted.  The row's final ``status`` tells the caller whether
    to proceed with ``fire_now`` or short-circuit.
    """
    # The inner session is freshly created — no autobegin state to
    # rollback.  Issue BEGIN IMMEDIATE directly.
    _begin_immediate_on_fresh_session(inner_session)
    try:
        # ----- Gate 1: kind mapping + severity floor -----
        trigger_kind = OBSERVER_FLAG_TO_TRIGGER_KIND.get(flag_kind)
        if trigger_kind is None:
            row = _insert_log_row(
                inner_session,
                user_id=user_id,
                source_flag_id=flag_id,
                trigger_kind=_DRY_RUN_TRIGGER_KIND,
                severity=flag_severity,
                status="skipped_severity",
                job_run_id=None,
                dispatched_at=now,
                notes=(
                    f"flag_kind {flag_kind!r} not in "
                    f"OBSERVER_FLAG_TO_TRIGGER_KIND"
                ),
            )
            inner_session.commit()
            return row.id

        min_sev = MIN_SEVERITY_FOR_TRIGGER.get(trigger_kind, "critical")
        flag_rank = _SEVERITY_RANK.get(flag_severity, 0)
        min_rank = _SEVERITY_RANK.get(min_sev, _SEVERITY_RANK["critical"])

        if flag_rank < min_rank:
            row = _insert_log_row(
                inner_session,
                user_id=user_id,
                source_flag_id=flag_id,
                trigger_kind=_DRY_RUN_TRIGGER_KIND,
                severity=flag_severity,
                status="skipped_severity",
                job_run_id=None,
                dispatched_at=now,
                notes=(
                    f"severity {flag_severity!r} below floor "
                    f"{min_sev!r} for trigger_kind {trigger_kind!r}"
                ),
            )
            inner_session.commit()
            return row.id

        # Spec §4.2: critical is the ONLY severity that AUTO-FIRES a
        # plan_synthesis job.  Warning at-or-above the per-kind floor
        # writes ``dry_run_logged`` (audit-only).
        if flag_severity != "critical":
            row = _insert_log_row(
                inner_session,
                user_id=user_id,
                source_flag_id=flag_id,
                trigger_kind=_DRY_RUN_TRIGGER_KIND,
                severity=flag_severity,
                status="dry_run_logged",
                job_run_id=None,
                dispatched_at=now,
                notes=(
                    f"warning-band auto-fire suppressed; "
                    f"would-be trigger_kind={trigger_kind!r}"
                ),
            )
            inner_session.commit()
            return row.id

        # ----- Gate 2: per-(user, trigger_kind) cooldown -----
        cooldown_hours = COOLDOWN_HOURS_BY_KIND.get(
            trigger_kind, DEFAULT_COOLDOWN_HOURS
        )
        cooldown_window_start = _to_naive_utc(
            now - timedelta(hours=cooldown_hours)
        )
        # Cooldown gate considers BOTH 'fired' AND 'error' rows so a
        # transient JobRegistry failure doesn't immediately re-fire
        # on the next observer pass (codex review focus —
        # idempotency-on-retry: see module docstring).
        cooldown_recent = inner_session.execute(
            select(
                ReplanDispatchLog.id,
                ReplanDispatchLog.dispatched_at,
            )
            .where(ReplanDispatchLog.user_id == user_id)
            .where(ReplanDispatchLog.trigger_kind == trigger_kind)
            .where(ReplanDispatchLog.status.in_(("fired", "error")))
            .where(
                ReplanDispatchLog.dispatched_at > cooldown_window_start
            )
            .order_by(ReplanDispatchLog.dispatched_at.desc())
            .limit(1)
        ).first()

        if cooldown_recent is not None:
            recent_at = _ensure_utc(cooldown_recent[1])
            remaining = (
                (recent_at + timedelta(hours=cooldown_hours)) - now
            ).total_seconds() / 60.0
            row = _insert_log_row(
                inner_session,
                user_id=user_id,
                source_flag_id=flag_id,
                trigger_kind=trigger_kind,
                severity=flag_severity,
                status="skipped_cooldown",
                job_run_id=None,
                dispatched_at=now,
                notes=(
                    f"cooldown active for trigger_kind="
                    f"{trigger_kind!r}: ~{max(0, int(remaining))} "
                    f"minutes remaining"
                ),
            )
            inner_session.commit()
            return row.id

        # ----- Gate 3: global 72h cap across all trigger_kinds -----
        global_window_start = _to_naive_utc(
            now - timedelta(hours=GLOBAL_CAP_WINDOW_HOURS)
        )
        n_fired_global = inner_session.scalar(
            select(func.count(ReplanDispatchLog.id))
            .where(ReplanDispatchLog.user_id == user_id)
            .where(ReplanDispatchLog.status == "fired")
            .where(
                ReplanDispatchLog.dispatched_at > global_window_start
            )
        )

        if (n_fired_global or 0) >= GLOBAL_CAP_FIRED_COUNT:
            row = _insert_log_row(
                inner_session,
                user_id=user_id,
                source_flag_id=flag_id,
                trigger_kind=trigger_kind,
                severity=flag_severity,
                status="skipped_global_cap",
                job_run_id=None,
                dispatched_at=now,
                notes=(
                    f"global cap reached: "
                    f"{n_fired_global}/{GLOBAL_CAP_FIRED_COUNT} "
                    f"fired in the last {GLOBAL_CAP_WINDOW_HOURS}h"
                ),
            )
            inner_session.commit()
            return row.id

        # ----- All gates clear: insert 'fired' row + commit so the
        # next concurrent caller sees it in their cooldown check. ----
        row = _insert_log_row(
            inner_session,
            user_id=user_id,
            source_flag_id=flag_id,
            trigger_kind=trigger_kind,
            severity=flag_severity,
            status="fired",
            job_run_id=None,
            dispatched_at=now,
            notes=None,
        )
        inner_session.commit()
        return row.id
    except Exception:
        # Any DB error during the gate block: roll back the
        # BEGIN IMMEDIATE transaction on the INNER session (caller
        # session is untouched per codex BLOCKER fix #1) and re-raise.
        try:
            inner_session.rollback()
        except Exception:  # pragma: no cover - defensive double-fault
            pass
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_session_on_same_engine(caller_session: "Session") -> "Session":
    """Open a fresh ``Session`` bound to the caller's engine.

    The dispatcher's BEGIN IMMEDIATE block runs against this fresh
    session so a rollback inside the dispatcher cannot destroy
    pending caller-session work (codex BLOCKER fix #1).  We use
    ``sessionmaker`` so the new session inherits any class-level
    configuration; the bind comes from the caller's session.
    """
    bind = caller_session.get_bind()
    if bind is None:  # pragma: no cover - defensive
        raise RuntimeError(
            "replan_dispatcher: caller session has no bound engine"
        )
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    return factory()


def _begin_immediate_on_fresh_session(session: "Session") -> None:
    """Issue ``BEGIN IMMEDIATE`` on a freshly-opened session.

    The session is assumed to have NO pending transaction (just
    instantiated via ``sessionmaker``).  We deliberately do NOT call
    ``session.rollback()`` here — the inner session has no state to
    roll back and any pre-existing autobegin would have come from
    code we didn't write.  Codex BLOCKER fix #1: the caller's session
    is untouched.

    On non-SQLite backends the same call falls back to a regular
    ``BEGIN`` plus row-level locking via ``SELECT ... FOR UPDATE`` in
    the gate queries — but v1 is SQLite-only so the Postgres branch
    is deferred (codex review NICE — out of scope for the v1
    dispatcher).
    """
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "sqlite"
    if dialect_name == "sqlite":
        session.execute(_sa_text("BEGIN IMMEDIATE"))
    else:  # pragma: no cover - non-SQLite path not exercised in v1
        # Postgres / etc.: default BEGIN; the gate queries rely on
        # SELECT ... FOR UPDATE for serialization (TODO when we move
        # off SQLite).
        session.execute(_sa_text("BEGIN"))


def _insert_log_row(
    session: "Session",
    *,
    user_id: str,
    source_flag_id: int | None,
    trigger_kind: str,
    severity: str,
    status: str,
    job_run_id: int | None,
    dispatched_at: datetime,
    notes: str | None,
) -> ReplanDispatchLog:
    """Insert a ``replan_dispatch_log`` row + flush so ``.id`` is bound.

    Caller is responsible for committing the surrounding transaction.
    """
    row = ReplanDispatchLog(
        user_id=user_id,
        source_flag_id=source_flag_id,
        trigger_kind=trigger_kind,
        severity=severity,
        status=status,
        job_run_id=job_run_id,
        dispatched_at=_to_naive_utc(dispatched_at),
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row


def _stamp_job_run_id(
    session: "Session", log_id: int, job_run_id: int
) -> None:
    """Patch the just-inserted log row with the JobRegistry's run_id."""
    session.execute(
        update(ReplanDispatchLog)
        .where(ReplanDispatchLog.id == log_id)
        .values(job_run_id=job_run_id)
    )
    session.commit()


def _stamp_notes(session: "Session", log_id: int, notes: str) -> None:
    """Patch the log row's notes column (audit-only side-channel)."""
    session.execute(
        update(ReplanDispatchLog)
        .where(ReplanDispatchLog.id == log_id)
        .values(notes=notes)
    )
    session.commit()


def _flip_to_error(
    session: "Session", log_id: int, error_message: str
) -> None:
    """Flip ``fired`` → ``error`` after JobRegistry.fire_now raised.

    job_run_id stays NULL — the job_runs row may or may not have been
    written by fire_now depending on WHERE it raised, but we don't
    have a reliable way to recover the id from this side.  The
    cooldown gate's ``status IN ('fired','error')`` filter is the
    protection against immediate re-fire.
    """
    session.execute(
        update(ReplanDispatchLog)
        .where(ReplanDispatchLog.id == log_id)
        .values(
            status="error", notes=f"fire_now_failed: {error_message}"
        )
    )
    session.commit()


def _detach_and_return(
    session: "Session", log_id: int
) -> ReplanDispatchLog:
    """Read the row, detach from session, and return a snapshot.

    The dispatcher closes its inner session in the outer ``finally``
    after this returns — so we ``expunge`` the instance so callers
    can read attributes without DetachedInstanceError.
    ``expire_on_commit=False`` on the sessionmaker keeps attributes
    loaded.
    """
    row = session.execute(
        select(ReplanDispatchLog).where(ReplanDispatchLog.id == log_id)
    ).scalar_one()
    session.expunge(row)
    return row


def _call_fire_now(
    job_registry: "JobRegistry",
    job_name: str,
    *,
    triggered_by: str,
) -> int:
    """Call ``job_registry.fire_now`` from a sync context.

    ``JobRegistry.fire_now`` is async; the dispatcher entry point is
    sync (called from inside the state_observer flag writer's sync
    loop, which itself runs under ``asyncio.to_thread`` in production
    so no event loop is running in the calling thread).

    Codex BLOCKER fix #2 (single-dispatch review 2026-05-30): when an
    event loop IS running in the current thread, we CANNOT safely
    bridge to ``fire_now``.  The previous implementation used
    ``asyncio.run_coroutine_threadsafe(..., loop).result()`` which
    deadlocks because we'd be the loop's own thread blocking on a
    future scheduled on that loop — the loop can never run the
    coroutine that would complete the future.  Instead we raise
    ``RuntimeError`` so the caller can re-design (e.g. await the
    dispatcher from an async context, or call it from a worker
    thread via ``asyncio.to_thread``).

    Production callers (the state_observer flag writer running under
    StateObserverLoop's ``asyncio.to_thread`` bridge) hit the
    no-running-loop path and bridge with ``asyncio.run``.
    """
    try:
        # If a loop is already running in this thread, we can't
        # safely bridge.
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to bridge with asyncio.run.
        return int(
            asyncio.run(
                job_registry.fire_now(
                    job_name, triggered_by=triggered_by
                )
            )
        )
    # A loop IS running in this thread — deadlock-prone.  Raise.
    raise RuntimeError(
        "replan_dispatcher._call_fire_now invoked from a thread with "
        "a running event loop; the async bridge to JobRegistry.fire_now "
        "cannot be safely awaited from the loop's own thread (deadlock). "
        "Run the dispatcher under asyncio.to_thread or refactor the "
        "caller to await the dispatcher from async context."
    )


def _to_naive_utc(dt: datetime) -> datetime:
    """Re-anchor a tz-aware datetime to UTC + strip tzinfo.

    SQLite stores DATETIME as text and rehydrates as naive — mixing
    aware + naive in comparisons raises.  Mirror the convention from
    ``argosy/services/state_observer_flag_writer.py::_to_naive_utc``.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _ensure_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (project convention)."""
    if dt is None:  # type: ignore[unreachable]
        return dt  # type: ignore[unreachable]
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _short_exc(exc: BaseException) -> str:
    """Trim an exception's message to fit the ``notes`` audit column."""
    s = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return s[:400]


__all__ = [
    "COOLDOWN_HOURS_BY_KIND",
    "DEFAULT_COOLDOWN_HOURS",
    "GLOBAL_CAP_FIRED_COUNT",
    "GLOBAL_CAP_WINDOW_HOURS",
    "MIN_SEVERITY_FOR_TRIGGER",
    "OBSERVER_FLAG_TO_TRIGGER_KIND",
    "PLAN_SYNTHESIS_JOB_NAME",
    "maybe_dispatch_replan",
]
