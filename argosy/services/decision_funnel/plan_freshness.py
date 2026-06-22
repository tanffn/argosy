"""Plan freshness on material change (P3).

The plan's near-term actions derive from the monthly synthesis; a material
mid-month change (a thesis break, a big drift, a concentration-cap breach)
means the to-do may no longer reflect today. This service surfaces that:

- the plan's age (shown regardless), and
- whether a MATERIAL change has been detected since the plan was accepted —
  drawn from the latest funnel run's hard-trigger routes + active critical
  monitor flags.

It is ADVISORY only: it recommends a refresh and names what changed; it does
NOT auto-regenerate the plan. Regeneration is a goal-affecting, not-yet-proven
action — it stays an explicit user/operator decision
(feedback_proven_reversible_means_do_not_wait applies only to PROVEN+reversible
paths; a from-material-change auto-resynth is neither yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import desc, select

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.decision_funnel.plan_freshness")

# A plan older than this (days) is flagged stale regardless of changes — it
# predates more than a monthly cycle.
STALE_AGE_DAYS = 35

# Routing signals that count as a MATERIAL change to the near-term plan.
_MATERIAL_SIGNALS = {
    "thesis_broken",
    "thesis_weakened",
    "concentration_cap_breach",
    "concentration_unverified",
    "drift_band_breach",
}


@dataclass(frozen=True)
class MaterialChange:
    subject: str
    signal: str
    reason: str


@dataclass(frozen=True)
class PlanFreshness:
    has_plan: bool
    plan_version_id: int | None
    as_of: str | None
    age_days: int | None
    stale: bool
    material_changes: list[MaterialChange] = field(default_factory=list)
    refresh_recommended: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "has_plan": self.has_plan,
            "plan_version_id": self.plan_version_id,
            "as_of": self.as_of,
            "age_days": self.age_days,
            "stale": self.stale,
            "refresh_recommended": self.refresh_recommended,
            "material_changes": [
                {"subject": m.subject, "signal": m.signal, "reason": m.reason}
                for m in self.material_changes
            ],
            "note": self.note,
        }


def _plan_as_of(plan) -> datetime | None:
    for attr in ("accepted_at", "imported_at", "created_at"):
        v = getattr(plan, attr, None)
        if v is not None:
            return v if v.tzinfo else v.replace(tzinfo=UTC)
    return None


def detect_plan_freshness(
    session: Session, *, user_id: str, now: datetime | None = None
) -> PlanFreshness:
    """Compute plan age + material-change detection. Never raises."""
    now = now or datetime.now(UTC)
    from argosy.state.models import FunnelRun, FunnelStageRow
    from argosy.state.queries import get_current_plan

    plan = None
    try:
        plan = get_current_plan(session, user_id)
    except Exception:  # noqa: BLE001
        plan = None
    if plan is None:
        return PlanFreshness(
            has_plan=False, plan_version_id=None, as_of=None, age_days=None,
            stale=False, note="No current plan.",
        )

    as_of_dt = _plan_as_of(plan)
    age_days = (now - as_of_dt).days if as_of_dt else None
    stale = age_days is not None and age_days >= STALE_AGE_DAYS

    # Material changes from the latest funnel run's hard-trigger routes.
    changes: list[MaterialChange] = []
    try:
        latest = session.execute(
            select(FunnelRun)
            .where(FunnelRun.user_id == user_id)
            .order_by(desc(FunnelRun.started_at))
            .limit(1)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        latest = None
    if latest is not None:
        # Only count routes from a run AFTER the plan was accepted.
        run_after_plan = (
            as_of_dt is None
            or latest.started_at is None
            or (
                latest.started_at if latest.started_at.tzinfo
                else latest.started_at.replace(tzinfo=UTC)
            ) >= as_of_dt
        )
        if run_after_plan:
            try:
                rows = session.execute(
                    select(FunnelStageRow).where(
                        FunnelStageRow.run_id == latest.id,
                        FunnelStageRow.stage == "stage1",
                        FunnelStageRow.decision == "routed",
                    )
                ).scalars().all()
            except Exception:  # noqa: BLE001
                rows = []
            seen: set[tuple[str, str]] = set()
            for r in rows:
                sig = r.signal_or_rule or ""
                if sig in _MATERIAL_SIGNALS and (r.subject, sig) not in seen:
                    seen.add((r.subject, sig))
                    changes.append(
                        MaterialChange(subject=r.subject, signal=sig, reason=r.reason)
                    )

    refresh_recommended = bool(changes) or stale
    if changes:
        note = (
            f"{len(changes)} material change(s) since the plan was accepted — "
            "the near-term actions may be out of date; consider refreshing."
        )
    elif stale:
        note = f"Plan is {age_days}d old (> {STALE_AGE_DAYS}d) — consider refreshing."
    else:
        note = "Plan is current; no material change detected."

    _log.info(
        "plan_freshness.detected", user_id=user_id, age_days=age_days,
        stale=stale, material_changes=len(changes),
    )
    return PlanFreshness(
        has_plan=True,
        plan_version_id=getattr(plan, "id", None),
        as_of=as_of_dt.isoformat() if as_of_dt else None,
        age_days=age_days,
        stale=stale,
        material_changes=changes,
        refresh_recommended=refresh_recommended,
        note=note,
    )


__all__ = ["PlanFreshness", "MaterialChange", "detect_plan_freshness", "STALE_AGE_DAYS"]
