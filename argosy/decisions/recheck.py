"""T3 cooling-off next-day re-check (SDD §10.4, Phase 5).

Runs on each ripe T3 proposal in `cooling` state. Steps:

  1. Load the original `decision_runs` row + its analyst reports.
  2. Re-run the relevant analysts (delta detection); compare key fields
     against the original. We don't re-run the full debate by default —
     the SDD calls this "abbreviated re-check".
  3. If any analyst output materially differs (recommendation changed,
     confidence dropped, RED flag added, etc.) → pause the proposal,
     audit_log it, and stop. The user is alerted via WS event.
  4. If outputs are stable, run the rule-based risk preflight against
     latest data. PASS → transition cooling → AWAITING_HUMAN. FAIL →
     pause and audit.

Phase 5 keeps this LLM-free by default: tests inject a `delta_detector`
callable that returns the materiality verdict. Production wiring can
swap in a real analyst-rerun routine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from sqlalchemy import select

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.decisions.proposals import (
    IllegalTransitionError,
    ProposalStatus,
    assert_legal,
)
from argosy.decisions.risk_preflight import (
    PreflightInputs,
    PreflightReport,
    run_preflight,
)
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    Proposal as ProposalRow,
    ProposalHistory,
)


_log = get_logger("argosy.decisions.recheck")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DeltaCheck:
    """Outcome of comparing a re-run analyst output against the original.

    `material` flips the proposal to BLOCKED. `summary` carries a
    human-readable note for the audit log entry.
    """

    material: bool
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# A delta-detector is a sync or async callable that takes the proposal +
# original analyst reports (raw text dicts) and returns a `DeltaCheck`.
# Tests inject mocks; production wiring can hook a real analyst-runner.
DeltaDetector = Callable[
    [ProposalRow, list[dict[str, Any]]],
    DeltaCheck,
]


@dataclass
class RecheckOutcome:
    decision: Literal["passed", "paused", "preflight_failed"]
    delta: DeltaCheck | None
    preflight: PreflightReport | None
    note: str


class T3RecheckRunner:
    """Encapsulates the cooling-off next-day re-check flow."""

    def __init__(
        self,
        *,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
        delta_detector: DeltaDetector | None = None,
        preflight_runner: Callable[[PreflightInputs], PreflightReport] | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        self.delta_detector = delta_detector or _default_no_delta
        self.preflight_runner = preflight_runner or run_preflight

    async def run(
        self,
        proposal_id: int,
        *,
        cash_available_usd: float = 0.0,
        max_position_usd: float | None = None,
        snapshot_pct: dict[str, float] | None = None,
        plan_targets: dict[str, float] | None = None,
        day_pnl_usd: float = 0.0,
        daily_loss_limit_usd: float | None = None,
        now: datetime | None = None,
    ) -> RecheckOutcome:
        moment = now or _utcnow()
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if row.user_id != self.user_id:
                raise PermissionError(
                    f"proposal {proposal_id} belongs to {row.user_id}"
                )
            if row.status != ProposalStatus.COOLING.value:
                raise IllegalTransitionError(
                    ProposalStatus(row.status), ProposalStatus.AWAITING_HUMAN
                )

            # Gather the original analyst reports keyed off decision_run_id.
            analyst_reports: list[dict[str, Any]] = []
            if row.decision_run_id is not None:
                rows = (
                    await session.execute(
                        select(AgentReportRow)
                        .where(AgentReportRow.decision_id == str(row.decision_run_id))
                        .order_by(AgentReportRow.created_at.asc())
                    )
                ).scalars().all()
                for rep in rows:
                    analyst_reports.append(
                        {
                            "agent_role": rep.agent_role,
                            "model": rep.model,
                            "confidence": rep.confidence,
                            "response_text": rep.response_text,
                        }
                    )

            # Step 1: delta detection.
            delta = self.delta_detector(row, analyst_reports)
            if delta.material:
                # Pause — flip COOLING → BLOCKED + audit + history.
                await self._transition(
                    session,
                    row,
                    ProposalStatus.BLOCKED,
                    actor="t3_recheck:material_change",
                    note=f"Pause: {delta.summary}",
                    moment=moment,
                )
                await record_audit_event(
                    user_id=row.user_id,
                    event_type="recheck.paused",
                    entity_type="proposal",
                    entity_id=str(row.id),
                    payload={"summary": delta.summary, "detail": delta.detail},
                    session=session,
                )
                await session.commit()
                return RecheckOutcome(
                    decision="paused",
                    delta=delta,
                    preflight=None,
                    note=delta.summary or "material change detected",
                )

            # Step 2: risk re-preflight.
            inputs = PreflightInputs(
                proposal=row,
                settings=self.settings,
                now=moment,
                cash_available_usd=cash_available_usd,
                max_position_usd=max_position_usd,
                snapshot_pct=snapshot_pct or {},
                plan_targets=plan_targets or {},
                day_pnl_usd=day_pnl_usd,
                daily_loss_limit_usd=daily_loss_limit_usd,
                tier=row.tier,
                account_class=row.account_class,  # type: ignore[arg-type]
            )
            preflight = self.preflight_runner(inputs)
            await record_audit_event(
                user_id=row.user_id,
                event_type="recheck.preflight",
                entity_type="proposal",
                entity_id=str(row.id),
                payload={
                    "passed": preflight.passed,
                    "summary": preflight.summary(),
                    "results": [
                        {"check": r.check, "status": r.status.value, "message": r.message}
                        for r in preflight.results
                    ],
                },
                session=session,
            )

            if not preflight.passed:
                await self._transition(
                    session,
                    row,
                    ProposalStatus.BLOCKED,
                    actor="t3_recheck:preflight_failed",
                    note=f"Preflight failed: {preflight.summary()}",
                    moment=moment,
                )
                await session.commit()
                return RecheckOutcome(
                    decision="preflight_failed",
                    delta=delta,
                    preflight=preflight,
                    note=preflight.summary(),
                )

            # Step 3: pass — advance to AWAITING_HUMAN per SDD §4.3 (T3 always
            # requires human review, even in the limited account).
            await self._transition(
                session,
                row,
                ProposalStatus.AWAITING_HUMAN,
                actor="t3_recheck:passed",
                note="Cooling-off re-check passed; queued for human review",
                moment=moment,
            )
            await record_audit_event(
                user_id=row.user_id,
                event_type="recheck.passed",
                entity_type="proposal",
                entity_id=str(row.id),
                payload={"preflight_summary": preflight.summary()},
                session=session,
            )
            await session.commit()
            return RecheckOutcome(
                decision="passed",
                delta=delta,
                preflight=preflight,
                note="re-check passed; awaiting human approval",
            )

    async def _transition(
        self,
        session: Any,
        row: ProposalRow,
        dst: ProposalStatus,
        *,
        actor: str,
        note: str,
        moment: datetime,
    ) -> None:
        src = ProposalStatus(row.status)
        assert_legal(src, dst)
        row.status = dst.value
        row.updated_at = moment
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status=dst.value,
                transitioned_at=moment,
                transitioned_by=actor,
                note=note,
            )
        )


# ----------------------------------------------------------------------
# Default no-delta detector
# ----------------------------------------------------------------------


def _default_no_delta(
    proposal: ProposalRow, original_reports: list[dict[str, Any]]
) -> DeltaCheck:
    """Conservative default: no LLM available → assume no material change.

    Safe because the preflight still runs after this; tests inject a
    real detector to exercise the material-change branch.
    """
    return DeltaCheck(
        material=False,
        summary="no analyst delta-detector configured; treating as stable",
        detail={"original_reports": len(original_reports)},
    )


__all__ = [
    "DeltaCheck",
    "DeltaDetector",
    "RecheckOutcome",
    "T3RecheckRunner",
]


# A tiny helper for fixtures: dump arbitrary mappings to JSON safely.
def _safe_json(obj: Any) -> str:  # pragma: no cover
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return "{}"
