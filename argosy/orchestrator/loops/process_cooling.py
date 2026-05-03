"""Process-cooling loop (SDD §10.4, Phase 3).

Runs every minute (cheap query). For each `proposals` row in `cooling`
state whose `cooling_off_until` has elapsed:

  - T2/T3 main account → transition to `awaiting_human` (queue)
  - Limited account, paper mode → auto-promote `cooling → approved →
    executed_paper` (the limited-acct paper path is autonomous)

For Phase 3 we don't actually run the next-day re-check LLM call (that
arrives in Phase 4 with the broker preflight); we just log a marker
event so the user sees the proposal advance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.api.events import publish_event
from argosy.decisions.proposals import IllegalTransitionError, ProposalStatus
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import Proposal as ProposalRow, ProposalHistory

_log = get_logger("argosy.loops.process_cooling")


class ProcessCoolingLoop(CadenceLoop):
    """Advance ripe `cooling` proposals.

    Cheap: SELECT WHERE status='cooling' AND cooling_off_until <= NOW.
    """

    name = "process_cooling"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        moment = (now or _utcnow)()
        # Make moment naive-tz-friendly for SQLite comparison.
        async with db_mod.get_session() as session:
            ripe = (
                await session.execute(
                    select(ProposalRow).where(
                        ProposalRow.status == ProposalStatus.COOLING.value,
                        ProposalRow.cooling_off_until <= moment,
                    )
                )
            ).scalars().all()

            advanced = 0
            for row in ripe:
                src = ProposalStatus(row.status)
                if (
                    row.account_class == "limited"
                    and self.settings.execution.default_mode == "paper"
                ):
                    # Limited acct paper: auto-promote through approved → executed_paper
                    try:
                        _safe_advance(
                            row,
                            ProposalStatus.APPROVED,
                            "process_cooling:limited_paper",
                            "auto-approved (limited account, paper mode)",
                            session=session,
                            now=moment,
                        )
                        _safe_advance(
                            row,
                            ProposalStatus.EXECUTED_PAPER,
                            "process_cooling:paper_fill",
                            "PaperFill log entry",
                            session=session,
                            now=moment,
                        )
                    except IllegalTransitionError:
                        _log.warning(
                            "process_cooling.illegal_transition",
                            proposal_id=row.id,
                            src=src.value,
                        )
                        continue
                else:
                    # Main account or live mode: surface to human queue.
                    try:
                        _safe_advance(
                            row,
                            ProposalStatus.AWAITING_HUMAN,
                            "process_cooling:cooling_elapsed",
                            "Cooling-off elapsed; queued for human review",
                            session=session,
                            now=moment,
                        )
                    except IllegalTransitionError:
                        _log.warning(
                            "process_cooling.illegal_transition",
                            proposal_id=row.id,
                            src=src.value,
                        )
                        continue
                advanced += 1
            if advanced:
                await session.commit()
                for row in ripe:
                    try:
                        await publish_event(
                            "proposal.updated",
                            {
                                "proposal_id": row.id,
                                "user_id": row.user_id,
                                "status": row.status,
                            },
                        )
                    except Exception:  # pragma: no cover - defensive
                        _log.exception("process_cooling.publish_failed")


def _safe_advance(
    row: ProposalRow,
    dst: ProposalStatus,
    actor: str,
    note: str,
    *,
    session,
    now: datetime,
) -> None:
    """In-DB advance: validate transition, update row, append history."""
    from argosy.decisions.proposals import assert_legal

    src = ProposalStatus(row.status)
    assert_legal(src, dst)
    row.status = dst.value
    row.updated_at = now
    session.add(
        ProposalHistory(
            proposal_id=row.id,
            status=dst.value,
            transitioned_at=now,
            transitioned_by=actor,
            note=note,
        )
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ProcessCoolingLoop"]
