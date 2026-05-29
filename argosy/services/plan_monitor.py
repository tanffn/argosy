"""Monitor agent — allocation-drift + MC-regression triggers (spec §5.1).

Sprint commits #11 + #12 of the plan/execute/monitor reorg. The monitor
agent's job is to fire red-flag rows on /home when one of three
conditions changes since the last check:

* allocation_drift (spec §5.1.1; check_allocation_drift)
* mc_regression    (spec §5.1.2; check_mc_regression — commit #12)
* macro_shift      (news pipeline classifier — commit #15)

Drift contract (spec §5.1.1):

    fire iff
        ( (rel_drift >= persistent_threshold AND
           consecutive snapshots over threshold >= N)
          OR
          (rel_drift >= single_shot_threshold) )
        AND  abs_drift_usd >= abs_drift_min_usd

where ``rel_drift = |current_pct - target_pct| / target_pct`` per row
and ``abs_drift_usd = |current_k - target_k| * 1000``. Both gates must
pass — a tiny over-allocated sleeve doesn't surface even at 100%
relative drift, and a giant on-target sleeve doesn't surface even if
the dollar wobble is huge.

Severity bands (also §5.1.1):

* info     -- rel_drift in [persistent_threshold, single_shot)
* warning  -- rel_drift in [single_shot, 1.5 * single_shot)
* critical -- rel_drift >= 1.5 * single_shot

Hysteresis state lives in ``monitor_flags`` itself; we don't keep a
separate per-row drift-history table. To count "consecutive snapshots
in drift" we scan recent ``allocation_drift`` rows for the same
(user, row_category) — if there's already a flag from the prior
snapshot AND this snapshot is also in drift, that's 2 consecutive.

Suggested buy-side proposals are produced by reusing
``windfall_allocator._allocate_long_term`` with the snapshot's current
allocation table and a budget equal to the absolute drift in USD. The
proposals show up in the flag payload; acceptance is the user's job via
``/proposals#allocation`` (sprint commit #6b machinery) which writes
``allocation_actions`` rows with ``action_source='monitor_drift'``.
The detector itself does NOT write ``allocation_actions``.

Reuses the same architecture as
``argosy.services.unallocated_cash_detector`` -- read snapshot, compute
condition, fire if threshold. Different shape because drift is per-row
where unallocated-cash is one global cash-row condition.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.ingest.tsv import AllocationRow, PortfolioSnapshot
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
)
from argosy.services.retirement.windfall_allocator import (
    AllocationProposal,
    _allocate_long_term,
)
from argosy.services.retirement.windfall_detector import AllocationLine
from argosy.state.models import MonitorFlag


# --- Defaults ---------------------------------------------------------------

# Spec §5.1.1. Tunable via function arguments; not env-driven to keep the
# trigger contract explicit at the call site (same convention as the
# unallocated-cash detector).
DEFAULT_REL_DRIFT_PERSISTENT = 0.10
DEFAULT_REL_DRIFT_SINGLE_SHOT = 0.20
DEFAULT_ABS_DRIFT_MIN_USD = 5_000.0
DEFAULT_CONSECUTIVE_REQUIRED = 2

# How far back we look when counting consecutive in-drift snapshots.
# 90 days is roughly a quarter — long enough for monthly TSV uploads to
# accumulate a 2-3 sample history; short enough that a flag raised once
# six months ago doesn't latch a "consecutive" verdict today.
_HYSTERESIS_LOOKBACK_DAYS = 90

# Default flag TTL — after this, the flag auto-expires from
# get_active_drift_flags. Re-firing the same condition on a later
# snapshot writes a fresh row (new flag id, new surfaced_at).
DEFAULT_FLAG_TTL_DAYS = 30


Severity = Literal["info", "warning", "critical"]


# --- Public dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class AllocationDriftFlag:
    """One drift flag for one allocation row.

    Shape per spec §5.1.1. Field meanings:

    * ``rel_drift`` -- |current_pct - target_pct| / target_pct as a
      unit-less fraction (0.14 = 14% relative drift).
    * ``abs_drift_usd`` -- |current_k - target_k| * 1000, in dollars.
    * ``severity`` -- info/warning/critical per the band rules.
    * ``suggested_proposals`` -- the buy/sell suggestions that would
      close this row's gap if the user accepts via /proposals#allocation.
      Pulled from ``_allocate_long_term`` with budget=abs_drift_usd.
    """

    user_id: str
    snapshot_date: date
    row_category: str
    rel_drift: float
    abs_drift_usd: float
    severity: Severity
    suggested_proposals: list[AllocationProposal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "row_category": self.row_category,
            "rel_drift": round(self.rel_drift, 4),
            "abs_drift_usd": round(self.abs_drift_usd, 2),
            "severity": self.severity,
            "suggested_proposals": [p.to_dict() for p in self.suggested_proposals],
        }


@dataclass(frozen=True)
class DriftCheckResult:
    """Result of one check_allocation_drift run."""

    flags_fired: list[AllocationDriftFlag]
    rows_evaluated: int
    snapshot_date: date | None

    def to_dict(self) -> dict:
        return {
            "flags_fired": [f.to_dict() for f in self.flags_fired],
            "rows_evaluated": self.rows_evaluated,
            "snapshot_date": (
                self.snapshot_date.isoformat() if self.snapshot_date else None
            ),
        }


# --- Public entry points ----------------------------------------------------


def check_allocation_drift(
    session: Session,
    user_id: str,
    *,
    rel_drift_persistent_threshold: float = DEFAULT_REL_DRIFT_PERSISTENT,
    rel_drift_single_shot_threshold: float = DEFAULT_REL_DRIFT_SINGLE_SHOT,
    abs_drift_min_usd: float = DEFAULT_ABS_DRIFT_MIN_USD,
    consecutive_snapshots_required: int = DEFAULT_CONSECUTIVE_REQUIRED,
    flag_ttl_days: int = DEFAULT_FLAG_TTL_DAYS,
    now: datetime | None = None,
) -> DriftCheckResult:
    """Check the latest snapshot's allocation against targets and fire flags.

    Fire rule (spec §5.1.1):

        ( rel_drift >= persistent_threshold
          AND  consecutive snapshots over threshold >= consecutive_required )
        OR
        ( rel_drift >= single_shot_threshold )

      AND abs_drift_usd >= abs_drift_min_usd  (both branches must pass)

    Severity:

      * info     -- rel_drift in [persistent, single_shot)
      * warning  -- rel_drift in [single_shot, 1.5 * single_shot)
      * critical -- rel_drift >= 1.5 * single_shot

    Writes one ``monitor_flags`` row per fired flag. Does NOT write
    ``allocation_actions`` — those are user-decision records (commit
    #6b machinery), not detector output.
    """
    row = get_latest_snapshot_row(session, user_id=user_id)
    if row is None:
        return DriftCheckResult(flags_fired=[], rows_evaluated=0, snapshot_date=None)

    snapshot = row_to_snapshot(row)
    snapshot_date = snapshot.snapshot_date

    if not snapshot.allocations:
        return DriftCheckResult(
            flags_fired=[], rows_evaluated=0, snapshot_date=snapshot_date,
        )

    if now is None:
        now = datetime.now(timezone.utc)

    # Convert allocation rows to AllocationLine once so suggested-proposal
    # generation can reuse the table without redoing the conversion per row.
    allocation_table = [
        _row_to_line(r) for r in snapshot.allocations if r.target_pct is not None
    ]

    flags_fired: list[AllocationDriftFlag] = []
    rows_evaluated = 0

    for alloc_row in snapshot.allocations:
        if alloc_row.target_pct is None or alloc_row.target_pct <= 0:
            continue
        if alloc_row.pct is None:
            continue
        rows_evaluated += 1

        rel_drift = abs(alloc_row.pct - alloc_row.target_pct) / alloc_row.target_pct

        current_k = alloc_row.usd_value_k or 0.0
        target_k = alloc_row.target_k or 0.0
        abs_drift_usd = abs(current_k - target_k) * 1000.0

        # Both gates: abs threshold AND (single-shot or persistent + hysteresis).
        if abs_drift_usd < abs_drift_min_usd:
            continue

        below_persistent = rel_drift < rel_drift_persistent_threshold
        if below_persistent:
            continue

        is_single_shot = rel_drift >= rel_drift_single_shot_threshold
        fires = is_single_shot

        if not fires:
            # Persistent path: need ≥N distinct prior snapshot_dates
            # in drift for this row, plus the current snapshot. Codex
            # BLOCKER (commit #11 review) — must dedupe by
            # snapshot_date to avoid over-counting correction-reruns
            # of the same snapshot.
            prior_distinct_snapshots = (
                _count_prior_distinct_drift_snapshots(
                    session,
                    user_id=user_id,
                    row_category=alloc_row.category,
                    now=now,
                    lookback_days=_HYSTERESIS_LOOKBACK_DAYS,
                    exclude_snapshot_date=snapshot_date,
                )
            )
            # +1 for the current snapshot we're about to fire on.
            if (prior_distinct_snapshots + 1) >= consecutive_snapshots_required:
                fires = True

        if not fires:
            continue

        severity = _classify_severity(
            rel_drift,
            persistent=rel_drift_persistent_threshold,
            single_shot=rel_drift_single_shot_threshold,
        )

        # Generate suggested buys equal to the row's gap. Budget = abs
        # drift; _allocate_long_term with fraction=1.0 routes 100% to
        # long-term proposals (no medium/short split for a drift flag).
        proposals, _remaining = _allocate_long_term(
            abs_drift_usd,
            allocation_table,
            long_term_budget_fraction=1.0,
        )

        # snapshot_date can be None on synthetic / partial-parse uploads;
        # if so, fall back to today's UTC date for the flag's stamp so
        # the payload is always serializable.
        stamp_date = snapshot_date or now.date()

        flag = AllocationDriftFlag(
            user_id=user_id,
            snapshot_date=stamp_date,
            row_category=alloc_row.category,
            rel_drift=rel_drift,
            abs_drift_usd=abs_drift_usd,
            severity=severity,
            suggested_proposals=proposals,
        )
        flags_fired.append(flag)

        # Persist as a monitor_flags row.
        _write_monitor_flag(
            session,
            flag=flag,
            now=now,
            ttl_days=flag_ttl_days,
        )

    if flags_fired:
        session.commit()

    return DriftCheckResult(
        flags_fired=flags_fired,
        rows_evaluated=rows_evaluated,
        snapshot_date=snapshot_date,
    )


def get_active_drift_flags(
    session: Session,
    user_id: str,
    *,
    now: datetime | None = None,
) -> list[AllocationDriftFlag]:
    """Return active (unacknowledged, unexpired) drift flags for ``user_id``.

    Feeds the Home Red-Flag Strip (sprint commit #17). "Active" =
    ``acknowledged_at IS NULL`` AND (``expires_at IS NULL`` OR
    ``expires_at > now``).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = (
        select(MonitorFlag)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.kind == "allocation_drift")
        .where(MonitorFlag.acknowledged_at.is_(None))
        .order_by(MonitorFlag.surfaced_at.desc())
    )
    rows = list(session.execute(stmt).scalars())

    out: list[AllocationDriftFlag] = []
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
        out.append(_flag_from_payload(payload, user_id=user_id, severity=r.severity))
    return out


# --- Internals --------------------------------------------------------------


def _classify_severity(
    rel_drift: float,
    *,
    persistent: float,
    single_shot: float,
) -> Severity:
    """Map rel_drift to severity band per spec §5.1.1."""
    if rel_drift >= 1.5 * single_shot:
        return "critical"
    if rel_drift >= single_shot:
        return "warning"
    # Caller has already gated rel_drift >= persistent; if it's between
    # persistent and single_shot, it's the "info" band.
    return "info"


def _count_prior_distinct_drift_snapshots(
    session: Session,
    *,
    user_id: str,
    row_category: str,
    now: datetime,
    lookback_days: int,
    exclude_snapshot_date: date | None = None,
) -> int:
    """Count DISTINCT prior snapshot_dates on which (user, row_category)
    fired a drift flag, within `lookback_days`.

    Codex BLOCKER (commit #11 review): the prior implementation counted
    raw monitor_flags rows in the window — that over-counts on snapshot
    correction-reruns (user uploads, then re-uploads to fix a typo;
    both runs write a flag row even though the user perceives them as
    one snapshot). Fix: dedupe by `payload['snapshot_date']` and
    optionally exclude the current snapshot's date so the caller can
    count "prior consecutive" cleanly.
    """
    cutoff = now - timedelta(days=lookback_days)
    stmt = (
        select(MonitorFlag)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.kind == "allocation_drift")
        .where(MonitorFlag.surfaced_at >= cutoff)
    )
    rows = session.execute(stmt).scalars()
    seen_dates: set[str] = set()
    excluded_iso = (
        exclude_snapshot_date.isoformat() if exclude_snapshot_date else None
    )
    for r in rows:
        try:
            payload = json.loads(r.payload)
        except (TypeError, ValueError):
            continue
        if payload.get("row_category") != row_category:
            continue
        snap = payload.get("snapshot_date")
        if not isinstance(snap, str):
            continue
        if snap == excluded_iso:
            continue
        seen_dates.add(snap)
    return len(seen_dates)


# Legacy alias retained briefly in case any external caller imported the
# old name. Remove in the next monitor-related commit.
_count_prior_drift_flags = _count_prior_distinct_drift_snapshots


def _write_monitor_flag(
    session: Session,
    *,
    flag: AllocationDriftFlag,
    now: datetime,
    ttl_days: int,
) -> MonitorFlag:
    """Persist one drift flag to ``monitor_flags``.

    Payload schema (kind='allocation_drift') matches the example in the
    MonitorFlag ORM docstring:

        {"snapshot_date": "2026-05-29",
         "row_category": "Growth",
         "rel_drift": 0.14,
         "abs_drift_usd": 8200,
         "suggested_proposals": [<AllocationProposal.to_dict>, ...]}
    """
    payload_dict = {
        "snapshot_date": flag.snapshot_date.isoformat(),
        "row_category": flag.row_category,
        "rel_drift": round(flag.rel_drift, 4),
        "abs_drift_usd": round(flag.abs_drift_usd, 2),
        "suggested_proposals": [p.to_dict() for p in flag.suggested_proposals],
    }
    expires = now + timedelta(days=ttl_days) if ttl_days > 0 else None
    row = MonitorFlag(
        user_id=flag.user_id,
        kind="allocation_drift",
        severity=flag.severity,
        payload=json.dumps(payload_dict, default=str),
        surfaced_at=now,
        expires_at=expires,
    )
    session.add(row)
    return row


def _flag_from_payload(
    payload: dict, *, user_id: str, severity: str,
) -> AllocationDriftFlag:
    """Reverse of ``_write_monitor_flag``: payload dict -> dataclass."""
    raw_props = payload.get("suggested_proposals") or []
    proposals: list[AllocationProposal] = []
    for p in raw_props:
        try:
            proposals.append(AllocationProposal(
                horizon=p.get("horizon", "long"),
                asset_class=p.get("asset_class", ""),
                instrument=p.get("instrument", ""),
                amount_usd=float(p.get("amount_usd") or 0.0),
                rationale=p.get("rationale", ""),
                closes_delta_usd=float(p.get("closes_delta_usd") or 0.0),
                confidence=p.get("confidence", "medium"),
                source_id=p.get("source_id", "argosy_derived"),
            ))
        except (TypeError, ValueError):
            continue

    snap_raw = payload.get("snapshot_date")
    snap_date: date
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

    return AllocationDriftFlag(
        user_id=user_id,
        snapshot_date=snap_date,
        row_category=str(payload.get("row_category") or ""),
        rel_drift=float(payload.get("rel_drift") or 0.0),
        abs_drift_usd=float(payload.get("abs_drift_usd") or 0.0),
        severity=sev,
        suggested_proposals=proposals,
    )


def _row_to_line(row: AllocationRow) -> AllocationLine:
    """Convert AllocationRow (TSV parser) -> AllocationLine (allocator input).

    Mirrors the helper in unallocated_cash_detector; kept private here
    to avoid coupling the two detectors via a shared utility module
    while the contract is still settling.
    """
    current_k = row.usd_value_k or 0.0
    target_k = row.target_k or 0.0
    delta_k = row.delta_k if row.delta_k is not None else (target_k - current_k)
    return AllocationLine(
        asset_class=row.category,
        current_pct=row.pct or 0.0,
        current_k_usd=current_k,
        target_pct=row.target_pct or 0.0,
        target_k_usd=target_k,
        delta_k_usd=delta_k,
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
        _write_mc_regression_row(
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
    Strip alongside ``get_active_drift_flags``.
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
    "AllocationDriftFlag",
    "DEFAULT_ABS_DRIFT_MIN_USD",
    "DEFAULT_CONSECUTIVE_REQUIRED",
    "DEFAULT_FLAG_TTL_DAYS",
    "DEFAULT_MC_DELTA_PP_THRESHOLD",
    "DEFAULT_MC_N_PATHS",
    "DEFAULT_MC_TARGET_AGE",
    "DEFAULT_REL_DRIFT_PERSISTENT",
    "DEFAULT_REL_DRIFT_SINGLE_SHOT",
    "DriftCheckResult",
    "MCRegressionCheckResult",
    "MCRegressionFlag",
    "check_allocation_drift",
    "check_mc_regression",
    "get_active_drift_flags",
    "get_active_mc_regression_flags",
]
