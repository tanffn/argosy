"""Watchlist loop (SDD §3.6, §5).

Runs daily (default 08:30 user TZ). Invokes `WatchlistAgent` to refresh
the universe of tickers tracked: positions + candidates + reduce-list.
Phase 7 emits the structured output via `watchlist.updated` WebSocket.
The invocation, cost, prompts, and structured output are also written to
`agent_reports` / `agent_reports_blobs`; a dedicated mutable `watchlists`
table remains unnecessary because the daily report is append-only evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from argosy.agents.watchlist import WatchlistAgent
from argosy.api.events import publish_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.watchlist")


@dataclass
class WatchlistInputs:
    """Inputs to one watchlist run, gathered before the LLM call."""

    user_id: str
    positions_tickers: list[str]
    prior_watchlist: list[dict[str, Any]]
    plan_candidates: list[str]
    plan_reduce_list: list[str]
    snapshot_label: str = "(unknown)"


class WatchlistLoop(CadenceLoop):
    """Daily watchlist maintenance. Wired when `cadences.watchlist.enabled`."""

    name = "watchlist"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        watchlist_agent_factory: Callable[[], WatchlistAgent] | None = None,
        gather_inputs: Callable[[str], WatchlistInputs | Any] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._agent_factory = watchlist_agent_factory or (lambda: WatchlistAgent(user_id=user_id))
        self._gather = gather_inputs or _default_gather_inputs

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
        run_at = (now or _utcnow)()

        # Cost-cap pause: watchlist is non-routine; skip when budget breached.
        if await get_cost_guard().should_pause_non_routine(loop_name=self.name):
            _log.info("watchlist.cost_cap_paused", user_id=self.user_id)
            return {"status": "paused", "reason": "cost_cap"}

        inputs = await _maybe_async(self._gather(self.user_id))
        if not isinstance(inputs, WatchlistInputs):  # pragma: no cover
            raise TypeError(f"gather_inputs must return WatchlistInputs, got {type(inputs)!r}")

        agent = self._agent_factory()
        report = await agent.run(
            positions_tickers=inputs.positions_tickers,
            prior_watchlist=inputs.prior_watchlist,
            plan_candidates=inputs.plan_candidates,
            plan_reduce_list=inputs.plan_reduce_list,
            snapshot_label=inputs.snapshot_label,
        )
        from argosy.services.agent_report_persistence import (
            persist_agent_report_async,
        )

        await persist_agent_report_async(
            report,
            decision_id=f"watchlist:{run_at.date().isoformat()}",
        )

        current = getattr(report.output, "current_tickers", []) or []
        added = getattr(report.output, "added_today", []) or []
        removed = getattr(report.output, "removed_today", []) or []

        _log.info(
            "watchlist.refreshed",
            user_id=self.user_id,
            count=len(current),
            added=len(added),
            removed=len(removed),
            run_at=run_at.isoformat(),
        )

        try:
            await publish_event(
                "watchlist.updated",
                {
                    "user_id": self.user_id,
                    "run_at": run_at.isoformat(),
                    "count": len(current),
                    "added": added,
                    "removed": removed,
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("watchlist.publish_failed")
        return {
            "status": "ok",
            "count": len(current),
            "added": len(added),
            "removed": len(removed),
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _maybe_async(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _default_gather_inputs(user_id: str) -> WatchlistInputs:
    """Load the actual book, discovery state, recommendations, and prior output."""
    from sqlalchemy import desc, select

    from argosy.state import db as db_mod
    from argosy.state.models import (
        ActionProposal,
        AgentReport,
        AgentReportBlob,
        PortfolioSnapshotRow,
        Proposal,
        ScanState,
    )

    positions: set[str] = set()
    candidates: set[str] = set()
    reduce: set[str] = set()
    prior_watchlist: list[dict[str, Any]] = []
    snapshot_label = "(no portfolio snapshot ingested today)"

    async with db_mod.get_session() as session:
        snapshot = (
            await session.execute(
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.user_id == user_id)
                .order_by(
                    desc(PortfolioSnapshotRow.imported_at),
                    desc(PortfolioSnapshotRow.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if snapshot is not None:
            snapshot_label = f"portfolio_snapshot:{snapshot.id}:{snapshot.snapshot_date}"
            try:
                raw_positions = json.loads(snapshot.positions_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_positions = []
            for row in raw_positions:
                symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
                asset_type = str(row.get("asset_type") or "").lower()
                if symbol and symbol not in {"-", "—", "N/A"} and "cash" not in asset_type:
                    positions.add(symbol)

        proposal_rows = (
            await session.execute(
                select(Proposal.ticker, Proposal.action).where(
                    Proposal.user_id == user_id,
                    Proposal.status == "awaiting_human",
                    Proposal.shadow == 0,
                    Proposal.source.in_(
                        (
                            "decision_funnel",
                            "portfolio_review",
                            "verdict_trigger_sweep",
                        )
                    ),
                )
            )
        ).all()
        for ticker, action in proposal_rows:
            symbol = str(ticker or "").strip().upper()
            if not symbol:
                continue
            if action == "buy":
                candidates.add(symbol)
            elif action == "sell":
                reduce.add(symbol)

        # Explicit WATCH dispositions from research ingests are persisted as
        # quiet set_watchlist rows. They are authoritative watch candidates
        # even when the discovery radar later evicts its transient ScanState.
        watch_rows = (
            (
                await session.execute(
                    select(ActionProposal.suggested_payload).where(
                        ActionProposal.user_id == user_id,
                        ActionProposal.kind == "set_watchlist",
                        ActionProposal.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )
        for raw_payload in watch_rows:
            try:
                payload = json.loads(raw_payload or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            symbol = str(payload.get("ticker") or "").strip().upper()
            if symbol:
                candidates.add(symbol)

        scan_rows = (
            (
                await session.execute(
                    select(ScanState).where(
                        ScanState.user_id == user_id,
                        ScanState.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        cutoff = datetime.now(UTC).timestamp() - 7 * 86400
        for row in scan_rows:
            seen = row.last_fleet_at or row.last_estimated_at or row.last_seen_at
            if seen is None:
                continue
            seen_aware = seen.replace(tzinfo=UTC) if seen.tzinfo is None else seen
            if seen_aware.timestamp() < cutoff:
                continue
            symbol = str(row.ticker or "").strip().upper()
            if symbol and symbol not in positions:
                candidates.add(symbol)

        prior_report_id = (
            await session.execute(
                select(AgentReport.id)
                .where(
                    AgentReport.user_id == user_id,
                    AgentReport.agent_role == "watchlist",
                )
                .order_by(desc(AgentReport.created_at), desc(AgentReport.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior_report_id is not None:
            output_json = (
                await session.execute(
                    select(AgentReportBlob.value).where(
                        AgentReportBlob.report_id == prior_report_id,
                        AgentReportBlob.key == "output_json",
                    )
                )
            ).scalar_one_or_none()
            try:
                prior_payload = json.loads(output_json or "{}")
                prior_watchlist = list(prior_payload.get("current_tickers") or [])
            except (TypeError, ValueError, json.JSONDecodeError):
                prior_watchlist = []

    return WatchlistInputs(
        user_id=user_id,
        positions_tickers=sorted(positions),
        prior_watchlist=prior_watchlist,
        plan_candidates=sorted(candidates - positions),
        plan_reduce_list=sorted(reduce & positions),
        snapshot_label=snapshot_label,
    )


__all__ = ["WatchlistInputs", "WatchlistLoop"]
