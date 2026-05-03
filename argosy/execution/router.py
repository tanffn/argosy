"""Execution router (SDD §10.3, Phase 4).

Given an APPROVED `Proposal` ORM row, the router:

  1. Resolves the broker adapter for the proposal's account
  2. Re-runs the rule-based risk preflight (SDD §9.3) against latest data
  3. If `execution_mode == "paper"`:
       - calls `adapter.place_order(order, paper=True)` (writes PaperFill)
       - transitions proposal to EXECUTED_PAPER
  4. If `execution_mode == "live"`:
       - calls `adapter.place_order(order, paper=False)`
       - records a `pending_orders` row for the reconcile loop
       - transitions proposal to EXECUTED_LIVE
  5. Records audit_log entries throughout

Phase 4 wires only the IBKR + Schwab + Leumi adapters. Selection is by
account_class:

  - "limited"        → IBKR (live in Phase 5, paper in Phase 4)
  - "main"           → Schwab read-only or Leumi read-only (manual_required)
                       OR IBKR if the account_id starts with "ibkr-"

Phase 4 limits live execution to T0/T1 main accounts via the queue+
approve flow; T2/T3 still requires human approval via the API. Limited-
account autonomy is Phase 5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from argosy.adapters.brokers.base import BrokerAdapter
from argosy.adapters.brokers.ibkr import IBKRAdapter
from argosy.adapters.brokers.leumi_tsv import LeumiTSVAdapter
from argosy.adapters.brokers.schwab_csv import SchwabCSVAdapter
from argosy.adapters.brokers.types import ExecutionResult, ProposedOrder
from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.api.events import publish_event
from argosy.decisions.proposals import (
    IllegalTransitionError,
    ProposalStatus,
    assert_legal,
)
from argosy.decisions.risk_preflight import (
    PreflightInputs,
    run_preflight,
)
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import (
    PendingOrder,
    Proposal as ProposalRow,
    ProposalHistory,
)

_log = get_logger("argosy.execution.router")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionRouter:
    """Drives APPROVED proposals through preflight → broker → state machine."""

    def __init__(
        self,
        *,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
        adapter_factories: dict[str, Any] | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        # Tests inject mock adapters via this dict; keys are broker names.
        self.adapter_factories: dict[str, Any] = adapter_factories or {}

    # ------------------------------------------------------------------
    # Adapter resolution
    # ------------------------------------------------------------------

    def resolve_broker(self, proposal: ProposalRow) -> str:
        """Pick the broker name for a proposal.

        Routing rules (Phase 4):
          - account_class == "limited"            → "ibkr" (write-capable)
          - account_class == "main"  + IBKR hint  → "ibkr"
          - account_class == "main"  + Schwab     → "schwab_csv" (read-only;
            place_order returns manual_required so the dashboard surfaces
            a manual-entry instruction)
          - account_class == "main"  + Leumi      → "leumi_tsv" (read-only)

        The hint is read from `proposal.account_id` if present (e.g.
        "schwab_main", "leumi_main", "ibkr_main", "ibkr_argonaut") or
        falls back to "ibkr" for backwards compatibility with proposals
        created before account_id was always set.
        """
        if proposal.account_class == "limited":
            return "ibkr"
        # Main accounts: route by account_id prefix when available.
        account_id = (getattr(proposal, "account_id", None) or "").lower()
        if account_id.startswith("schwab"):
            return "schwab_csv"
        if account_id.startswith("leumi"):
            return "leumi_tsv"
        return "ibkr"

    def get_adapter(self, broker: str) -> BrokerAdapter:
        if broker in self.adapter_factories:
            adapter = self.adapter_factories[broker]
            return adapter() if callable(adapter) else adapter
        if broker == "ibkr":
            return IBKRAdapter(user_id=self.user_id)
        if broker == "schwab_csv":
            return SchwabCSVAdapter(user_id=self.user_id)
        if broker == "leumi_tsv":
            return LeumiTSVAdapter(user_id=self.user_id)
        raise ValueError(f"unknown broker {broker!r}")

    # ------------------------------------------------------------------
    # Preflight (Phase 4 wires latest-data inputs)
    # ------------------------------------------------------------------

    def build_preflight_inputs(
        self,
        proposal: ProposalRow,
        *,
        cash_available_usd: float = 0.0,
        max_position_usd: float | None = None,
        snapshot_pct: dict[str, float] | None = None,
        plan_targets: dict[str, float] | None = None,
        day_pnl_usd: float = 0.0,
        daily_loss_limit_usd: float | None = None,
        now: datetime | None = None,
    ) -> PreflightInputs:
        return PreflightInputs(
            proposal=proposal,
            settings=self.settings,
            now=now or _utcnow(),
            cash_available_usd=cash_available_usd,
            max_position_usd=max_position_usd,
            snapshot_pct=snapshot_pct or {},
            plan_targets=plan_targets or {},
            day_pnl_usd=day_pnl_usd,
            daily_loss_limit_usd=daily_loss_limit_usd,
            tier=proposal.tier,
            account_class=proposal.account_class,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # Drive
    # ------------------------------------------------------------------

    async def execute(
        self,
        proposal_id: int,
        *,
        cash_available_usd: float = 0.0,
        max_position_usd: float | None = None,
        snapshot_pct: dict[str, float] | None = None,
        plan_targets: dict[str, float] | None = None,
        day_pnl_usd: float = 0.0,
        daily_loss_limit_usd: float | None = None,
    ) -> ExecutionResult:
        # ----- Kill switch (SDD §14.5) --------------------------------------
        # Hard halt: when ARGOSY_KILL=1 is set in the environment, refuse
        # to place any new orders. In-flight orders complete; new ones are
        # rejected with an audit-log entry and a clear ExecutionResult.
        import os

        if os.environ.get("ARGOSY_KILL") == "1":
            await record_audit_event(
                user_id=self.user_id,
                event_type="execution.kill_switch_blocked",
                entity_type="proposal",
                entity_id=str(proposal_id),
                payload={"reason": "ARGOSY_KILL=1 in environment"},
            )
            return ExecutionResult(
                status="rejected",
                reason="ARGOSY_KILL=1 — new orders halted by kill switch",
            )

        async with db_mod.get_session() as session:
            proposal = await session.get(ProposalRow, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if proposal.user_id != self.user_id:
                raise PermissionError(
                    f"proposal {proposal_id} belongs to {proposal.user_id}, "
                    f"not {self.user_id}"
                )

            current = ProposalStatus(proposal.status)
            if current is not ProposalStatus.APPROVED:
                raise IllegalTransitionError(current, ProposalStatus.EXECUTED_LIVE)

            # ----- Risk preflight ------------------------------------------------
            inputs = self.build_preflight_inputs(
                proposal,
                cash_available_usd=cash_available_usd,
                max_position_usd=max_position_usd,
                snapshot_pct=snapshot_pct,
                plan_targets=plan_targets,
                day_pnl_usd=day_pnl_usd,
                daily_loss_limit_usd=daily_loss_limit_usd,
            )
            report = run_preflight(inputs)
            await record_audit_event(
                user_id=self.user_id,
                event_type="preflight.completed",
                entity_type="proposal",
                entity_id=str(proposal.id),
                payload={
                    "passed": report.passed,
                    "summary": report.summary(),
                    "results": [
                        {
                            "check": r.check,
                            "status": r.status.value,
                            "message": r.message,
                        }
                        for r in report.results
                    ],
                },
                session=session,
            )

            if not report.passed:
                # Hard-fail: cancel the proposal (per SDD §10.3 "Rejected + alert").
                await self._transition(
                    session,
                    proposal,
                    ProposalStatus.CANCELLED,
                    actor="execution_router",
                    note=f"preflight blocked: {report.summary()}",
                )
                await session.commit()
                return ExecutionResult(
                    status="rejected",
                    broker="(preflight)",
                    reason=report.summary(),
                )

            # ----- Mode selection -----------------------------------------------
            mode = self.settings.execution.default_mode
            broker_name = self.resolve_broker(proposal)
            adapter = self.get_adapter(broker_name)

            order = ProposedOrder(
                account_id=proposal.account_class,
                ticker=proposal.ticker,
                action=proposal.action,  # type: ignore[arg-type]
                order_type=proposal.order_type,  # type: ignore[arg-type]
                quantity=float(proposal.size_shares_or_currency),
                limit_price=(
                    float(proposal.limit_price)
                    if proposal.limit_price is not None
                    else None
                ),
                stop_price=(
                    float(proposal.stop_price)
                    if proposal.stop_price is not None
                    else None
                ),
                time_in_force=proposal.time_in_force,  # type: ignore[arg-type]
                instrument=proposal.instrument,  # type: ignore[arg-type]
                client_order_id=uuid4().hex,
                proposal_id=proposal.id,
                user_id=self.user_id,
            )

            paper = mode != "live"
            result = await adapter.place_order(order, paper=paper)

            # ----- Post-place state-machine + bookkeeping -----------------------
            if result.status == "paper":
                await self._transition(
                    session,
                    proposal,
                    ProposalStatus.EXECUTED_PAPER,
                    actor="execution_router",
                    note="PaperFill via execution router",
                )
            elif result.status == "submitted" or result.status == "filled":
                # Live: register pending_orders, advance proposal.
                pending = PendingOrder(
                    user_id=self.user_id,
                    proposal_id=proposal.id,
                    broker=result.broker,
                    broker_order_id=result.broker_order_id,
                    status=result.status,
                )
                session.add(pending)
                await self._transition(
                    session,
                    proposal,
                    ProposalStatus.EXECUTED_LIVE,
                    actor="execution_router",
                    note=f"Live placement at {result.broker}",
                )
            elif result.status == "manual_required":
                await record_audit_event(
                    user_id=self.user_id,
                    event_type="order.manual_required",
                    entity_type="proposal",
                    entity_id=str(proposal.id),
                    payload={"broker": result.broker, "reason": result.reason},
                    session=session,
                )
            else:
                # rejected → cancel the proposal
                await self._transition(
                    session,
                    proposal,
                    ProposalStatus.CANCELLED,
                    actor="execution_router",
                    note=f"broker rejected: {result.reason}",
                )

            await session.commit()

        # Publish events outside the DB transaction.
        try:
            await publish_event(
                "proposal.executed",
                {
                    "proposal_id": proposal_id,
                    "user_id": self.user_id,
                    "status": result.status,
                    "broker": result.broker,
                    "paper": result.paper,
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("execution_router.publish_failed")

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _transition(
        self,
        session: Any,
        proposal: ProposalRow,
        dst: ProposalStatus,
        *,
        actor: str,
        note: str,
    ) -> None:
        src = ProposalStatus(proposal.status)
        assert_legal(src, dst)
        moment = _utcnow()
        proposal.status = dst.value
        proposal.updated_at = moment
        session.add(
            ProposalHistory(
                proposal_id=proposal.id,
                status=dst.value,
                transitioned_at=moment,
                transitioned_by=actor,
                note=note,
            )
        )
        await record_audit_event(
            user_id=proposal.user_id,
            event_type="proposal.transition",
            entity_type="proposal",
            entity_id=str(proposal.id),
            payload={"src": src.value, "dst": dst.value, "actor": actor, "note": note},
            session=session,
        )


__all__ = ["ExecutionRouter"]
