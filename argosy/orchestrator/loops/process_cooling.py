"""Process-cooling loop (SDD §10.4, Phase 3 + Phase 5).

Runs every minute (cheap query). For each `proposals` row in `cooling`
state whose `cooling_off_until` has elapsed:

  - Limited account, paper mode → auto-promote `cooling → approved →
    executed_paper` (the limited-acct paper path is autonomous)
  - T3 (any account) → run the `T3RecheckRunner` (SDD §10.4 abbreviated
    re-check: analyst-delta + risk-preflight); on pass, advance to
    `awaiting_human`. On material change or preflight failure, the
    runner pauses the proposal (BLOCKED) with an audit entry.
  - T2 main account → transition to `awaiting_human` (queue)

Phase 5 wires the actual T3 re-check via injectable `recheck_factory`;
tests and production set this to a callable returning a configured
`T3RecheckRunner`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.api.events import publish_event
from argosy.decisions.proposals import IllegalTransitionError, ProposalStatus
from argosy.decisions.recheck import T3RecheckRunner
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
        recheck_factory: Callable[[], T3RecheckRunner] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        # Phase 5: optional factory so tests can inject a runner with a
        # mocked delta_detector / preflight runner. When None, the loop
        # falls back to the Phase 3 behavior (T3 → AWAITING_HUMAN) so
        # existing tests continue to pass without supplying preflight
        # inputs. Production callers wire this in when they have access
        # to live cash / price data to feed preflight.
        self.recheck_factory: Callable[[], T3RecheckRunner] | None = recheck_factory

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        moment = (now or _utcnow)()
        ripe_ids: list[int] = []
        # Phase 5: process each ripe proposal in its own transaction so a
        # T3 re-check (which may pause/preflight-fail) does not roll back
        # the limited-account short-circuits.
        async with db_mod.get_session() as session:
            ripe = (
                await session.execute(
                    select(ProposalRow).where(
                        ProposalRow.status == ProposalStatus.COOLING.value,
                        ProposalRow.cooling_off_until <= moment,
                    )
                )
            ).scalars().all()
            ripe_ids = [r.id for r in ripe]

        for proposal_id in ripe_ids:
            await self._process_one(proposal_id, moment=moment)

        for proposal_id in ripe_ids:
            try:
                async with db_mod.get_session() as session:
                    row = await session.get(ProposalRow, proposal_id)
                if row is not None:
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

    async def _process_one(self, proposal_id: int, *, moment: datetime) -> None:
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None or row.status != ProposalStatus.COOLING.value:
                return

            src = ProposalStatus(row.status)
            global_mode = self.settings.execution.default_mode
            limited_mode = self.settings.limited_account.execution_mode

            # Limited paper: short-circuit through approved → executed_paper.
            # T0/T1/T2 all auto-fill in paper mode per SDD §10.1 routing matrix
            # ("PaperFill log" / "PaperFill + review record"). Only T3 must
            # run the next-day re-check first ("PaperFill + cooling-off +
            # next-day paper re-check"), so T3 falls through to the recheck
            # branch below.
            if (
                row.account_class == "limited"
                and (global_mode == "paper" or limited_mode == "paper")
                and row.tier != "T3"
            ):
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
                    await session.commit()
                except IllegalTransitionError:
                    _log.warning(
                        "process_cooling.illegal_transition",
                        proposal_id=row.id,
                        src=src.value,
                    )
                return

            # Limited LIVE T0/T1 with kill-switch guard: auto-promote to
            # APPROVED so the execution router (next loop tick / caller)
            # picks it up. Phase 4 ensures the kill switch blocks placement.
            if (
                row.account_class == "limited"
                and limited_mode == "live"
                and global_mode != "queue_only"
                and row.tier in ("T0", "T1")
            ):
                import os

                if os.environ.get("ARGOSY_KILL") == "1":
                    # Halt: leave in cooling; next tick will re-evaluate.
                    return
                try:
                    _safe_advance(
                        row,
                        ProposalStatus.APPROVED,
                        "process_cooling:limited_live_t0t1",
                        "auto-approved (limited account, live mode, T0/T1)",
                        session=session,
                        now=moment,
                    )
                    await session.commit()
                except IllegalTransitionError:
                    _log.warning(
                        "process_cooling.illegal_transition",
                        proposal_id=row.id,
                        src=src.value,
                    )
                return

            # T3: run the abbreviated re-check.
            if row.tier == "T3":
                # The runner takes its own session; release this one first.
                pass

        if row.tier == "T3" and self.recheck_factory is not None:
            try:
                runner = self.recheck_factory()
                await runner.run(proposal_id, now=moment)
            except IllegalTransitionError:
                _log.warning(
                    "process_cooling.recheck_illegal_transition",
                    proposal_id=proposal_id,
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("process_cooling.recheck_failed", proposal_id=proposal_id)
            return

        # Default fall-through: T2 main / non-T3 → AWAITING_HUMAN.
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None or row.status != ProposalStatus.COOLING.value:
                return
            try:
                _safe_advance(
                    row,
                    ProposalStatus.AWAITING_HUMAN,
                    "process_cooling:cooling_elapsed",
                    "Cooling-off elapsed; queued for human review",
                    session=session,
                    now=moment,
                )
                await session.commit()
            except IllegalTransitionError:
                _log.warning(
                    "process_cooling.illegal_transition",
                    proposal_id=row.id,
                )


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
