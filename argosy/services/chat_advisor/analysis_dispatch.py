"""Bounded, tenant-scoped dispatcher for private-chat ticker analysis."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, is_dataclass
from typing import Any

from argosy.decisions.tiers import Tier
from argosy.services.chat_advisor.contracts import (
    AnalysisRequest,
    ExecutionPolicy,
    Principal,
    RunProgress,
)
from argosy.services.chat_advisor.progress import bind_progress
from argosy.services.decision_funnel.deep_decision import run_deep_decision

MAX_INSTRUMENTS = 3
_INSTRUMENT = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_HOUSEHOLD_LOCKS: dict[str, asyncio.Lock] = {}


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_request(value: Any) -> AnalysisRequest:
    if isinstance(value, AnalysisRequest):
        return value
    return AnalysisRequest(
        id=str(_field(value, "id")),
        instruments=list(_field(value, "instruments", []) or []),
        state=str(_field(value, "state", _field(value, "status", "queued"))),
        run_ids=[int(v) for v in (_field(value, "run_ids", []) or [])],
        error=_field(value, "error"),
        prompt_text=_field(value, "prompt_text", "") or "",
    )


def normalize_instruments(instruments: Iterable[str]) -> list[str]:
    """Normalize and bound an explicitly authorized ticker set."""
    result: list[str] = []
    for raw in instruments:
        ticker = str(raw or "").strip().upper().lstrip("$")
        if ticker.isdigit():
            raise ValueError(
                "numeric TASE security IDs are not supported by the ticker fleet; "
                "use the instrument's listed market ticker"
            )
        if not ticker or not _INSTRUMENT.fullmatch(ticker):
            raise ValueError(f"invalid or ambiguous instrument: {raw!r}")
        if ticker not in result:
            result.append(ticker)
    if not result:
        raise ValueError("at least one instrument is required")
    if len(result) > MAX_INSTRUMENTS:
        raise ValueError(f"at most {MAX_INSTRUMENTS} instruments per request")
    return result


class AnalysisDispatcher:
    """Persist first, then run one canonical T2 fleet per household at a time.

    ``store`` is tenant-bound by the transport layer. Its methods may be sync
    or async; the dispatcher always exposes an async API.
    """

    def __init__(
        self,
        store: Any,
        *,
        runner: Callable[..., Awaitable[Any]] = run_deep_decision,
        ownership_classifier: Callable[[str, str], Awaitable[bool]] | None = None,
        auto_schedule: bool = True,
    ) -> None:
        self.store = store
        self.runner = runner
        self.ownership_classifier = ownership_classifier or self._is_owned
        self.auto_schedule = auto_schedule
        self._worker_lock = _HOUSEHOLD_LOCKS.setdefault(self._household_user_id(), asyncio.Lock())
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def request(
        self,
        principal: Principal,
        instruments: Iterable[str],
        inbound_message_id: str,
        *,
        review_context: str = "",
    ) -> AnalysisRequest:
        self._assert_principal(principal)
        if not str(inbound_message_id or "").strip():
            raise ValueError("inbound_message_id is required for idempotency")
        existing = await _await(self.store.get_request_by_inbound(str(inbound_message_id)))
        if existing is not None:
            row = _as_request(existing)
            if self.auto_schedule:
                await self.schedule(row.id)
            return row
        normalized = normalize_instruments(instruments)
        if len(review_context) > 80000:
            raise ValueError("review evidence exceeds the bounded request size; narrow the event scope")
        created = await _await(
            self.store.create_request(
                inbound_message_id=str(inbound_message_id),
                channel_id=principal.channel_id,
                instruments=normalized,
                prompt_text=review_context,
            )
        )
        row = _as_request(created)
        if self.auto_schedule:
            await self.schedule(row.id)
        return row

    async def cancel(self, principal: Principal, request_id: str) -> AnalysisRequest:
        row = await self._owned_request(principal, request_id)
        if row.state in _TERMINAL:
            return row
        task = self._tasks.get(row.id)
        if task is not None and not task.done():
            task.cancel()
        updated = await _await(self.store.update_request(row.id, status="cancelled", error=None))
        return _as_request(updated or {**asdict(row), "state": "cancelled"})

    async def retry(self, principal: Principal, request_id: str) -> AnalysisRequest:
        row = await self._owned_request(principal, request_id)
        if row.state not in _TERMINAL:
            return row
        retry_method = getattr(self.store, "retry_request", None)
        if retry_method is not None:
            updated = await _await(retry_method(row.id))
        else:
            updated = await _await(self.store.update_request(row.id, status="queued", error=None))
        retried = _as_request(updated or {**asdict(row), "state": "queued", "error": None})
        if self.auto_schedule:
            await self.schedule(retried.id)
        return retried

    async def schedule(self, request_id: str) -> None:
        """Idempotently schedule a queued request and retain its cancel handle."""
        task = self._tasks.get(str(request_id))
        if task is not None and not task.done():
            return
        row = await _await(self.store.get_request(str(request_id)))
        if row is None:
            raise LookupError(f"analysis request {request_id} not found")
        if _as_request(row).state != "queued":
            return
        self._tasks[str(request_id)] = asyncio.create_task(self.worker_entry(str(request_id)))

    async def wait(self, request_id: str) -> None:
        """Wait for a scheduled request; intended for workers/probes, not chat IO."""
        await self.schedule(request_id)
        task = self._tasks.get(str(request_id))
        if task is not None:
            await asyncio.shield(task)

    async def stop(self) -> None:
        """Cancel and drain dispatcher-owned workers during transport shutdown."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def progress(self, principal: Principal, request_id: str) -> RunProgress:
        row = await self._owned_request(principal, request_id)
        getter = getattr(self.store, "progress_rows", None) or getattr(
            self.store, "get_progress", None
        )
        progress_rows = await _await(getter(row.id)) if getter is not None else []
        agents = []
        for value in progress_rows or []:
            if isinstance(value, dict):
                agents.append(value)
            elif is_dataclass(value):
                agents.append(asdict(value))
            else:
                agents.append(
                    {
                        key: getattr(value, key, None)
                        for key in (
                            "sequence",
                            "run_id",
                            "agent",
                            "state",
                            "detail",
                            "error",
                            "created_at",
                        )
                    }
                )
        result_getter = getattr(self.store, "request_result", None)
        result = await _await(result_getter(row.id)) if result_getter else None
        return RunProgress(
            request_id=row.id,
            state=row.state,
            run_ids=row.run_ids,
            agents=agents,
            result=result,
            error=row.error,
        )

    async def recover(self) -> list[str]:
        """Schedule every durable queued/running request after worker restart."""
        rows = await _await(self.store.list_recoverable_requests())
        scheduled: list[str] = []
        for value in rows or []:
            row = _as_request(value)
            if row.state == "running":
                await self._fail_interrupted(row)
                continue
            if row.id not in self._tasks or self._tasks[row.id].done():
                self._tasks[row.id] = asyncio.create_task(self.worker_entry(row.id))
                scheduled.append(row.id)
        return scheduled

    async def run_queued_once(self) -> str | None:
        """Run the oldest recoverable request; supervised jobs may poll this."""
        rows = await _await(self.store.list_recoverable_requests())
        for value in rows or []:
            row = _as_request(value)
            if row.state == "running":
                await self._fail_interrupted(row)
                continue
            await self.worker_entry(row.id)
            return row.id
        return None

    async def worker_entry(self, request_id: str) -> None:
        """Resume a persisted request through the full canonical T2 path."""
        current_task = asyncio.current_task()
        if current_task is not None:
            self._tasks[request_id] = current_task
        async with self._worker_lock:  # one active fleet for this household store
            row = _as_request(await _await(self.store.get_request(request_id)))
            if row.state in _TERMINAL:
                return
            await _await(self.store.update_request(row.id, status="running", error=None))

            async def sink(**event: Any) -> None:
                await _await(self.store.append_progress(**event))

            outcomes: list[dict[str, Any]] = []
            try:
                with bind_progress(row.id, sink):
                    for ticker in row.instruments:
                        latest = _as_request(await _await(self.store.get_request(row.id)))
                        if latest.state == "cancelled":
                            return
                        is_owned = await self.ownership_classifier(
                            self._household_user_id(), ticker
                        )

                        async def linked(run_id: int) -> None:
                            await _await(self.store.link_run(row.id, run_id))

                        outcome = await self.runner(
                            user_id=self._household_user_id(),
                            ticker=ticker,
                            tier=Tier.T2,
                            consult_mode="long_hold",
                            subject_type="holding" if is_owned else "discovery",
                            execution_policy=ExecutionPolicy.ANALYSIS_ONLY,
                            on_run_opened=linked,
                            **({"review_context": row.prompt_text} if row.prompt_text else {}),
                        )
                        run_id = _field(outcome, "decision_run_id")
                        if run_id and _field(outcome, "blocked_by") != "verdict_defended" and int(run_id) not in row.run_ids:
                            await _await(self.store.link_run(row.id, int(run_id)))
                        outcomes.append(
                            {
                                "ticker": ticker,
                                "status": _field(outcome, "status"),
                                "decision_run_id": run_id,
                                "proposal_id": _field(outcome, "proposal_id"),
                                "action": _field(outcome, "action"),
                                "blocked_reason": _field(outcome, "blocked_reason"),
                                "blocked_by": _field(outcome, "blocked_by"),
                                "news_assessment": _field(outcome, "news_assessment"),
                                **await self._canonical_result(
                                    ticker=ticker,
                                    run_id=int(run_id) if run_id else None,
                                    proposal_id=_field(outcome, "proposal_id"),
                                    allow_standing=(
                                        _field(outcome, "blocked_by") == "verdict_defended"
                                    ),
                                    **({"standing_verdict_id": _field(outcome, "news_assessment")["standing_verdict_id"]}
                                       if isinstance(_field(outcome, "news_assessment"), dict)
                                       and _field(outcome, "news_assessment").get("standing_verdict_id") else {}),
                                ),
                            }
                        )
                incomplete = [
                    item for item in outcomes if item["status"] in {"error", "quorum_failed"}
                ]
                await _await(
                    self.store.update_request(
                        row.id,
                        status="failed" if incomplete else "completed",
                        error=(
                            "; ".join(
                                f"{item['ticker']}: {item.get('blocked_reason') or item['status']}"
                                for item in incomplete
                            )[:1000]
                            if incomplete
                            else None
                        ),
                        result={"outcomes": outcomes},
                    )
                )
            except asyncio.CancelledError:
                latest = _as_request(await _await(self.store.get_request(row.id)))
                await self._close_runs(latest.run_ids, "cancelled")
                await _await(self.store.update_request(row.id, status="cancelled", error=None))
                raise
            except Exception as exc:  # retain partial canonical runs/reports
                latest = _as_request(await _await(self.store.get_request(row.id)))
                await self._close_runs(latest.run_ids, "failed")
                await _await(
                    self.store.update_request(
                        row.id,
                        status="failed",
                        error=str(exc)[:1000],
                        result={"outcomes": outcomes},
                    )
                )

    async def _fail_interrupted(self, row: AnalysisRequest) -> None:
        """Never blindly replay a fleet whose process died mid-analysis."""
        await self._close_runs(row.run_ids, "failed")
        await _await(
            self.store.update_request(
                row.id,
                status="failed",
                error="worker restarted during analysis; explicitly retry to start a new attempt",
            )
        )

    async def _close_runs(self, run_ids: Iterable[int], status: str) -> None:
        from argosy.decisions.per_ticker_analysts import close_decision_run_from_chat

        for run_id in run_ids:
            await close_decision_run_from_chat(
                decision_run_id=int(run_id),
                user_id=self._household_user_id(),
                status=status,  # type: ignore[arg-type]
            )

    async def _canonical_result(
        self,
        *,
        ticker: str,
        run_id: int | None,
        proposal_id: int | None,
        allow_standing: bool = False,
        standing_verdict_id: int | None = None,
    ) -> dict[str, Any]:
        """Read final user-owned proposal/verdict facts, never model scratchwork."""
        from sqlalchemy import func, select

        from argosy.state import db as db_mod
        from argosy.state.models import Prediction, Proposal, Verdict

        def parsed(value: str | None) -> Any:
            try:
                return json.loads(value or "null")
            except (TypeError, json.JSONDecodeError):
                return None

        async with db_mod.get_session() as session:
            proposal = None
            if proposal_id is not None:
                proposal = (
                    await session.execute(
                        select(Proposal).where(
                            Proposal.id == int(proposal_id),
                            Proposal.user_id == self._household_user_id(),
                        )
                    )
                ).scalar_one_or_none()
            verdict_query = select(Verdict).where(
                Verdict.user_id == self._household_user_id(),
                Verdict.subject == ticker,
            )
            if allow_standing:
                verdict_query = verdict_query.where(Verdict.settled.is_(True))
                if standing_verdict_id is not None:
                    verdict_query = verdict_query.where(Verdict.id == standing_verdict_id)
            elif run_id is not None:
                verdict_query = verdict_query.where(Verdict.source_decision_run_id == run_id)
            else:
                verdict_query = verdict_query.where(Verdict.id == -1)
            verdict = (
                await session.execute(verdict_query.order_by(Verdict.id.desc()).limit(1))
            ).scalar_one_or_none()
            prediction = None
            if verdict is not None:
                prediction = (
                    await session.execute(
                        select(Prediction)
                        .where(
                            Prediction.user_id == self._household_user_id(),
                            Prediction.source == "signal_stream:deep_decision_verdict",
                            func.json_extract(Prediction.source_ref, "$.verdict_id") == verdict.id,
                        )
                        .order_by(Prediction.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        return {
            "verdict": getattr(verdict, "verdict", None),
            "rationale": (
                getattr(verdict, "reasoning_md", None)
                or getattr(proposal, "rationale_summary", None)
            ),
            "confidence": (
                getattr(verdict, "conviction", None) or getattr(proposal, "confidence", None)
            ),
            "size": (
                {
                    "value": float(proposal.size_shares_or_currency),
                    "units": proposal.size_units,
                }
                if proposal is not None
                else None
            ),
            "risk": (
                {"stop_price": float(proposal.stop_price)}
                if proposal is not None and proposal.stop_price is not None
                else None
            ),
            "falsifiers": parsed(getattr(verdict, "falsifiers_json", None)),
            "catalysts": parsed(getattr(verdict, "revisit_triggers_json", None)),
            "as_of": (
                prediction.event_at.isoformat()
                if prediction is not None
                else (verdict.created_at.isoformat() if verdict is not None else None)
            ),
            "evaluation_due_at": (
                prediction.evaluation_due_at.isoformat()
                if prediction is not None
                else (
                    verdict.next_validation.isoformat()
                    if verdict is not None and verdict.next_validation
                    else None
                )
            ),
            "next_validation": (
                verdict.next_validation.isoformat()
                if verdict is not None and verdict.next_validation
                else None
            ),
            "verdict_id": getattr(verdict, "id", None),
        }

    async def _owned_request(self, principal: Principal, request_id: str) -> AnalysisRequest:
        self._assert_principal(principal)
        row = await _await(self.store.get_request(str(request_id)))
        if row is None:
            raise LookupError(f"analysis request {request_id} not found")
        return _as_request(row)

    def _household_user_id(self) -> str:
        value = getattr(self.store, "household_user_id", None) or getattr(
            self.store, "user_id", None
        )
        if not value:
            raise RuntimeError("analysis store must expose its tenant household_user_id")
        return str(value)

    def _assert_principal(self, principal: Principal) -> None:
        if principal.household_user_id != self._household_user_id():
            raise PermissionError("principal does not own this analysis store")

    @staticmethod
    async def _is_owned(user_id: str, ticker: str) -> bool:
        from argosy.services.decision_funnel.position_context import (
            position_context_block,
        )

        context = await position_context_block(user_id=user_id, ticker=ticker)
        return "NOT HELD" not in context


__all__ = ["AnalysisDispatcher", "MAX_INSTRUMENTS", "normalize_instruments"]
