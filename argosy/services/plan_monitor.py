"""Monitor agent — MC-regression trigger (spec §5.1.2).

T5.5 refactor: ``check_allocation_drift`` (per-symptom) and
``check_macro_shift`` (per-symptom, already deprecated) have been removed.
Anomaly detection for allocation drift and macro events flows exclusively
through the emergent ``StateObserverAgent`` (daily 17:00 IDT cron).

Allocation drift is covered emergently because ``state_diff.py``'s
``PLAN_BASELINE_COMPARATOR_MAP`` pairs
``portfolio.allocations[].current_pct`` against
``portfolio.allocations[].target_pct`` — the observer surfaces deviations
from that diff without any hardcoded symptom check.

``check_mc_regression`` is retained because the observer does NOT run
Monte-Carlo simulation; it reads only the state snapshot, which does not
include P(solvent) figures. Until the MC output is persisted into the
snapshot, this detector has no emergent equivalent (T5.5 BLOCKER: see
docs/superpowers/plans/2026-06-09-argosy-realignment-roadmap.md G48).

Severity bands (spec §5.1.2):

* info     -- delta_pp in [-threshold*1.5, -threshold)
* warning  -- delta_pp in [-threshold*2.5, -threshold*1.5)
* critical -- delta_pp < -threshold*2.5
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import MonitorFlag


# --- Defaults ---------------------------------------------------------------

# Default flag TTL used by mc_regression rows.
DEFAULT_FLAG_TTL_DAYS = 30

Severity = Literal["info", "warning", "critical"]


# ---------------------------------------------------------------------------
# Predictions-ledger writer (Spec C commit #3) — used by mc_regression
# ---------------------------------------------------------------------------


def _maybe_write_monitor_flag_prediction(
    session: Session,
    *,
    user_id: str,
    flag_row: MonitorFlag,
    kind: str,
    severity: str,
    now: datetime,
) -> None:
    """Spec C commit #3 — emit a meta-prediction for a fired MonitorFlag.

    Called from ``check_mc_regression`` after the underlying
    ``monitor_flags`` row commits. Best-effort: any failure logs + swallows
    so a writer issue never breaks the monitor's primary flag-writing path.
    The id-must-be-non-None preflight catches the (rare) case where the
    caller hasn't yet committed the row.

    Per spec §2.4 / §2.3 the prediction is a meta-prediction (ticker
    None, direction neutral, ``fixed_lookahead_30d``); evaluator scores
    against a portfolio-level proxy in commit #4.
    """
    if flag_row.id is None:
        return
    if severity not in ("info", "warning", "critical"):
        return
    try:
        # Import inline so plan_monitor's tests don't pay the predictions
        # package import cost (it's only meaningful when the writer is
        # actually exercised).
        from argosy.services.predictions.writers import (
            write_monitor_flag_prediction,
        )
        # SAVEPOINT — writer FK / CHECK failure must not roll back the
        # monitor_flags row the caller just committed.
        with session.begin_nested():
            write_monitor_flag_prediction(
                session,
                user_id,
                monitor_flag_id=int(flag_row.id),
                kind=kind,
                severity=severity,  # type: ignore[arg-type]
                event_at=now,
            )
        session.commit()
    except Exception:  # noqa: BLE001 — never break monitor on writer failure
        import logging
        logging.getLogger(__name__).warning(
            "plan_monitor: write_monitor_flag_prediction failed for "
            "monitor_flag_id=%s kind=%s",
            flag_row.id, kind,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# MC regression trigger (spec §5.1.2) — sprint commit #12
# ---------------------------------------------------------------------------
#
# Monthly Monte-Carlo run against the user's current portfolio + pension
# state. Fire a flag when P(solvent at age 95) drops by >= 5 percentage
# points month-over-month. Cadence is enforced by the caller (cron on
# the 1st of each month hits POST /retirement/monitor/mc-check); this
# function just runs once and compares to the most recent persisted
# 'mc_regression' monitor_flags row.
#
# Baseline contract: the first run for a user has no prior point to
# diff against, so it persists a BASELINE row with payload['baseline']=
# True and severity='info', and returns flag_fired=None. Subsequent
# runs compare against the most recent run's curr_p_solvent regardless
# of whether that prior row was a baseline or a fired flag (both carry
# curr_p_solvent in the payload, which is the comparison anchor).
#
# Severity bands (delta_pp is signed; -10pp means P(solvent) dropped 10
# percentage points e.g. 82% → 72%):
#   info     -- delta_pp in [-threshold*1.5, -threshold)   small drop
#   warning  -- delta_pp in [-threshold*2.5, -threshold*1.5)
#   critical -- delta_pp < -threshold*2.5                  very large
#
# The MC run uses ``effective_retire_ready_age('base', ...)`` to pin the
# retirement_age parameter so this trigger never contradicts the
# Holistic Timeline / ExpectedRetirementAgeCard / RuinProbabilityHero
# (codex BLOCKER #3 resolution; see effective_retire_ready_age docstring).


DEFAULT_MC_DELTA_PP_THRESHOLD = 5.0  # percentage points
DEFAULT_MC_N_PATHS = 1000
DEFAULT_MC_TARGET_AGE = 95
DEFAULT_MC_PROJECTION_YEARS = 60  # cap so age 95 is always reachable
DEFAULT_MC_SEED = 42  # deterministic by default; cron can override


@dataclass(frozen=True)
class MCRegressionFlag:
    """One MC regression flag — fires when P(solvent) drops ≥5pp
    month-over-month (spec §5.1.2)."""

    user_id: str
    snapshot_date: date  # the date this check was run
    prev_p_solvent: float  # last month's P(solvent at age 95)
    curr_p_solvent: float
    delta_pp: float  # curr - prev, signed (negative = regression)
    severity: Severity

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "prev_p_solvent": round(self.prev_p_solvent, 4),
            "curr_p_solvent": round(self.curr_p_solvent, 4),
            "delta_pp": round(self.delta_pp, 4),
            "severity": self.severity,
        }


@dataclass(frozen=True)
class MCRegressionCheckResult:
    """Result of one ``check_mc_regression`` run."""

    flag_fired: MCRegressionFlag | None
    prev_run_date: date | None  # the date of the run we compared against
    curr_p_solvent: float
    rows_evaluated: int  # always 1 for MC regression, kept for symmetry

    def to_dict(self) -> dict:
        return {
            "flag_fired": self.flag_fired.to_dict() if self.flag_fired else None,
            "prev_run_date": (
                self.prev_run_date.isoformat() if self.prev_run_date else None
            ),
            "curr_p_solvent": round(self.curr_p_solvent, 4),
            "rows_evaluated": self.rows_evaluated,
        }


def check_mc_regression(
    session: Session,
    user_id: str,
    *,
    delta_pp_threshold: float = DEFAULT_MC_DELTA_PP_THRESHOLD,
    n_paths: int = DEFAULT_MC_N_PATHS,
    target_age: int = DEFAULT_MC_TARGET_AGE,
    as_of: date | None = None,
    seed: int | None = DEFAULT_MC_SEED,
    now: datetime | None = None,
) -> MCRegressionCheckResult:
    """Run Monte Carlo against current portfolio state, compare to the
    most recent prior ``mc_regression`` row's ``curr_p_solvent``. Fire a
    new ``MCRegressionFlag`` if P(solvent at ``target_age``) dropped by
    >= ``delta_pp_threshold`` percentage points (spec §5.1.2).

    Writes one ``monitor_flags`` row per call:

      - First run for the user: ``payload['baseline']=True``,
        ``severity='info'``, ``flag_fired=None`` returned. This row is
        a comparison anchor for next month; it is NOT a fired flag.
      - Subsequent runs: ``payload['baseline']=False`` always.
        ``severity`` matches the fired flag's band when a flag fires;
        when no flag fires (drop below threshold or P(solvent) flat /
        improved), the row still records ``curr_p_solvent`` so next
        month has a fresh comparison anchor, with severity='info' and
        ``payload['fired']=False``.

    The 'monthly' cadence is enforced by the caller (cron on day 1 of
    each month, see ``POST /retirement/monitor/mc-check``).
    """
    # Lazy imports — cashflow_projection pulls numpy, and importing at
    # module level would slow every import of plan_monitor (which the
    # allocation-drift path doesn't need).
    from argosy.services.cashflow_projection import (
        effective_retire_ready_age,
        extract_household_state,
        extract_pension_state,
        project_monte_carlo,
    )

    if now is None:
        now = datetime.now(timezone.utc)
    snapshot_date = as_of or now.date()

    # Pick retirement_age via the canonical clamp so all consumers
    # agree on the projection's retirement assumption. Fall back to a
    # safe heuristic (current_age + 1.0) when no crossing exists yet —
    # the MC still needs SOME retirement_age and we'd rather run with
    # an "early" assumption than skip the check entirely.
    household = extract_household_state(session, user_id, today=snapshot_date)
    pensions = extract_pension_state(session, user_id)
    canonical = effective_retire_ready_age(
        "base", user_id, session, as_of=snapshot_date,
    )
    if canonical.age_years is not None:
        retirement_age = canonical.age_years
    else:
        retirement_age = household.current_age_years + 1.0

    # Project enough years out to reach target_age from the household's
    # current age. Add a 5-yr safety margin so the percentile/aggregate
    # math at target_age has a populated tick even when the household
    # is older than expected (e.g. seeded user near 60).
    years = max(1, int(target_age - household.current_age_years) + 5)

    mc = project_monte_carlo(
        household=household,
        pensions=pensions,
        retirement_age=retirement_age,
        years=years,
        n_paths=n_paths,
        seed=seed,
        today=snapshot_date,
    )

    # P(solvent at target_age). The MonteCarloProjection exposes
    # ``p_failure_before_age_95`` (and 75/85); P(solvent) is the
    # complement. We only ever read the age-95 figure for the
    # regression test — the spec pins target_age=95 — but the
    # ``target_age`` kwarg stays in the signature for future-
    # proofing once the projection grows symmetric accessors.
    if target_age == 95:
        p_failure = mc.p_failure_before_age_95
    elif target_age == 85:
        p_failure = mc.p_failure_before_age_85
    elif target_age == 75:
        p_failure = mc.p_failure_before_age_75
    else:
        # No persisted attribute for other ages; conservatively look
        # up the closest series tick and use 1 - fraction_solvent.
        # The series is monthly so a tick within ~half a month of
        # the target age is the right read.
        closest = min(mc.series, key=lambda p: abs(p.age_years - target_age))
        p_failure = 1.0 - closest.fraction_solvent
    curr_p_solvent = max(0.0, min(1.0, 1.0 - p_failure))

    # Find the most recent prior mc_regression row for this user — it's
    # the comparison anchor regardless of whether it was a baseline or
    # a fired flag.
    prior = _latest_mc_regression_row(session, user_id=user_id)

    if prior is None:
        # First run — persist a baseline anchor, do NOT fire a flag.
        _write_mc_regression_row(
            session,
            user_id=user_id,
            curr_p_solvent=curr_p_solvent,
            prev_p_solvent=None,
            delta_pp=None,
            severity="info",
            baseline=True,
            fired=False,
            snapshot_date=snapshot_date,
            retirement_age=retirement_age,
            target_age=target_age,
            n_paths=n_paths,
            now=now,
        )
        session.commit()
        return MCRegressionCheckResult(
            flag_fired=None,
            prev_run_date=None,
            curr_p_solvent=curr_p_solvent,
            rows_evaluated=1,
        )

    # Diff against prior. Compute delta_pp in PERCENTAGE POINTS:
    # P(solvent) is a fraction in [0, 1] so the pp scale is *100.
    try:
        prior_payload = json.loads(prior.payload)
    except (TypeError, ValueError):
        prior_payload = {}
    prev_p_solvent = float(prior_payload.get("curr_p_solvent") or 0.0)
    prev_run_iso = prior_payload.get("snapshot_date")
    prev_run_date: date | None = None
    if isinstance(prev_run_iso, str):
        try:
            prev_run_date = date.fromisoformat(prev_run_iso)
        except ValueError:
            prev_run_date = None

    delta_pp = (curr_p_solvent - prev_p_solvent) * 100.0

    # Severity classification — only matters if a flag actually fires.
    fires = delta_pp <= -delta_pp_threshold
    if fires:
        severity = _classify_mc_severity(delta_pp, threshold=delta_pp_threshold)
        flag = MCRegressionFlag(
            user_id=user_id,
            snapshot_date=snapshot_date,
            prev_p_solvent=prev_p_solvent,
            curr_p_solvent=curr_p_solvent,
            delta_pp=delta_pp,
            severity=severity,
        )
        mc_row = _write_mc_regression_row(
            session,
            user_id=user_id,
            curr_p_solvent=curr_p_solvent,
            prev_p_solvent=prev_p_solvent,
            delta_pp=delta_pp,
            severity=severity,
            baseline=False,
            fired=True,
            snapshot_date=snapshot_date,
            retirement_age=retirement_age,
            target_age=target_age,
            n_paths=n_paths,
            now=now,
        )
        session.commit()
        # Spec C commit #3 — emit a prediction row for the FIRED mc_regression.
        # GATE: only fired rows reach this branch; baseline + no-fire anchor
        # rows are accounting and skip the ledger (spec §2.4).
        _maybe_write_monitor_flag_prediction(
            session,
            user_id=user_id,
            flag_row=mc_row,
            kind="mc_regression",
            severity=severity,
            now=now,
        )
        return MCRegressionCheckResult(
            flag_fired=flag,
            prev_run_date=prev_run_date,
            curr_p_solvent=curr_p_solvent,
            rows_evaluated=1,
        )

    # No-fire path: persist a fresh anchor (not a baseline; the user has
    # had a prior run) so next month diffs against THIS run, not the
    # original baseline. Without this, a slow multi-month slide where
    # each step is below the threshold would never fire.
    _write_mc_regression_row(
        session,
        user_id=user_id,
        curr_p_solvent=curr_p_solvent,
        prev_p_solvent=prev_p_solvent,
        delta_pp=delta_pp,
        severity="info",
        baseline=False,
        fired=False,
        snapshot_date=snapshot_date,
        retirement_age=retirement_age,
        target_age=target_age,
        n_paths=n_paths,
        now=now,
    )
    session.commit()
    return MCRegressionCheckResult(
        flag_fired=None,
        prev_run_date=prev_run_date,
        curr_p_solvent=curr_p_solvent,
        rows_evaluated=1,
    )


def get_active_mc_regression_flags(
    session: Session,
    user_id: str,
    *,
    now: datetime | None = None,
) -> list[MCRegressionFlag]:
    """Active (unacknowledged, unexpired) fired MC-regression flags.

    Baseline + no-fire anchor rows are filtered out — only rows with
    ``payload['fired']==True`` surface here. Feeds the Home Red-Flag
    Strip alongside state-observer flags.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = (
        select(MonitorFlag)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.kind == "mc_regression")
        .where(MonitorFlag.acknowledged_at.is_(None))
        .order_by(MonitorFlag.surfaced_at.desc())
    )
    rows = list(session.execute(stmt).scalars())
    out: list[MCRegressionFlag] = []
    for r in rows:
        if r.expires_at is not None:
            exp = r.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue
        try:
            payload = json.loads(r.payload)
        except (TypeError, ValueError):
            continue
        if not payload.get("fired"):
            continue  # skip baseline + no-fire anchor rows
        out.append(_mc_flag_from_payload(payload, user_id=user_id, severity=r.severity))
    return out


# --- MC-regression internals -----------------------------------------------


def _classify_mc_severity(
    delta_pp: float,
    *,
    threshold: float,
) -> Severity:
    """Map a signed ``delta_pp`` to an MC-regression severity band.

    Caller has already gated ``delta_pp <= -threshold``; this maps the
    magnitude into info/warning/critical per spec §5.1.2."""
    drop = -delta_pp  # positive magnitude in pp
    if drop >= threshold * 2.5:
        return "critical"
    if drop >= threshold * 1.5:
        return "warning"
    return "info"


def _latest_mc_regression_row(
    session: Session,
    *,
    user_id: str,
) -> MonitorFlag | None:
    """Return the most-recent ``mc_regression`` row (any payload kind:
    baseline / fired / no-fire anchor) for ``user_id``. ``None`` when
    the user has never run the check.

    Acknowledgement does NOT exclude — an acknowledged baseline still
    serves as the comparison anchor for the next run (the spec calls
    this out: 'Acknowledged baseline does NOT prevent future flags
    from firing'). The user can dismiss the red-flag UI surface
    without losing the time-series comparison.
    """
    stmt = (
        select(MonitorFlag)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.kind == "mc_regression")
        .order_by(MonitorFlag.surfaced_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _write_mc_regression_row(
    session: Session,
    *,
    user_id: str,
    curr_p_solvent: float,
    prev_p_solvent: float | None,
    delta_pp: float | None,
    severity: Severity,
    baseline: bool,
    fired: bool,
    snapshot_date: date,
    retirement_age: float,
    target_age: int,
    n_paths: int,
    now: datetime,
) -> MonitorFlag:
    """Persist one ``mc_regression`` row to ``monitor_flags``.

    Payload shape (kind='mc_regression'):

        {"baseline": bool,        # true = first-run anchor
         "fired": bool,           # true = surfaced as red flag
         "snapshot_date": "2026-05-29",
         "curr_p_solvent": 0.82,
         "prev_p_solvent": 0.88,  # null on baseline
         "delta_pp": -6.0,        # null on baseline; signed pp
         "run_metadata": {
             "retirement_age": 49.0,
             "target_age": 95,
             "n_paths": 1000,
         }}

    Baseline + no-fire anchor rows still occupy a row so the next
    month's run has something to diff against. Only ``fired=True``
    rows surface on the Red-Flag Strip via
    ``get_active_mc_regression_flags``.
    """
    payload_dict: dict = {
        "baseline": baseline,
        "fired": fired,
        "snapshot_date": snapshot_date.isoformat(),
        "curr_p_solvent": round(curr_p_solvent, 4),
        "prev_p_solvent": (
            round(prev_p_solvent, 4) if prev_p_solvent is not None else None
        ),
        "delta_pp": (round(delta_pp, 4) if delta_pp is not None else None),
        "run_metadata": {
            "retirement_age": round(float(retirement_age), 2),
            "target_age": target_age,
            "n_paths": n_paths,
        },
    }
    # MC regression rows have a longer TTL than allocation_drift — the
    # comparison anchor needs to survive a full month plus slack.
    expires = now + timedelta(days=DEFAULT_FLAG_TTL_DAYS * 2)
    row = MonitorFlag(
        user_id=user_id,
        kind="mc_regression",
        severity=severity,
        payload=json.dumps(payload_dict, default=str),
        surfaced_at=now,
        expires_at=expires,
    )
    session.add(row)
    return row


def _mc_flag_from_payload(
    payload: dict, *, user_id: str, severity: str,
) -> MCRegressionFlag:
    """Reverse of ``_write_mc_regression_row`` for fired rows.

    Only called from ``get_active_mc_regression_flags`` after baseline /
    no-fire anchor rows have been filtered out, so ``prev_p_solvent`` and
    ``delta_pp`` are guaranteed non-null in practice. We still defensively
    coerce to 0.0 in case a hand-edited row escaped the contract."""
    snap_raw = payload.get("snapshot_date")
    if isinstance(snap_raw, str):
        try:
            snap_date = date.fromisoformat(snap_raw)
        except ValueError:
            snap_date = datetime.now(timezone.utc).date()
    else:
        snap_date = datetime.now(timezone.utc).date()

    sev: Severity
    if severity in ("info", "warning", "critical"):
        sev = severity  # type: ignore[assignment]
    else:
        sev = "info"

    return MCRegressionFlag(
        user_id=user_id,
        snapshot_date=snap_date,
        prev_p_solvent=float(payload.get("prev_p_solvent") or 0.0),
        curr_p_solvent=float(payload.get("curr_p_solvent") or 0.0),
        delta_pp=float(payload.get("delta_pp") or 0.0),
        severity=sev,
    )


__all__ = [
    "DEFAULT_FLAG_TTL_DAYS",
    "DEFAULT_MC_DELTA_PP_THRESHOLD",
    "DEFAULT_MC_N_PATHS",
    "DEFAULT_MC_TARGET_AGE",
    "MCRegressionCheckResult",
    "MCRegressionFlag",
    "check_mc_regression",
    "get_active_mc_regression_flags",
]
