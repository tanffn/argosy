"""Per-ticker analyst orchestrator for /consult.

The plan-synthesis `_run_phase_1_analysts` (`argosy/orchestrator/flows/plan_synthesis/orchestrator.py:1684`)
runs the 9 analysts but requires plan-level inputs (``baseline``,
``prior_current``) — it cannot be reused for an arbitrary ticker outside
the user's positions. This module fills the gap: given a single
``ticker``, fetch its data via the existing ``_gather_*`` helpers and
run the 6 "always-on" analysts in parallel. Returns the surviving
citation-bearing reports so the caller can hand them to
``DecisionFlow.run(analyst_reports=..., decision_run_id=...,
persist_input_analysts=False)``.

**Codex design review (`tools/codex-tandem/sessions/2026-05-30-consult-
per-ticker-analysts/result.md`) integrated:**

1. The plan-synthesis ``_gather_*`` helpers are SYNC and call
   ``asyncio.run(...)`` internally. Wrapping them in ``asyncio.to_thread``
   lets us call from the route's async event loop without RuntimeError.
2. ``DecisionFlow.run()`` now accepts a pre-opened ``decision_run_id`` +
   ``persist_input_analysts=False`` (this module's load-bearing
   integration point). Avoids duplicate decision_run rows + duplicate
   agent_report inserts.
3. Failed-or-empty analyst runs are DROPPED, not persisted. All 6
   analysts have ``require_citations=True``; persisting an empty
   citation row would violate the agent contract + fail the base.py:2381
   gate on read.
4. **Quorum check** before returning: ≥ 3/6 analysts must produce
   citation-bearing reports AND ≥ 1 must be ticker-specific (any of
   ``fundamentals``, ``technical``, ``news``, ``sentiment``). Below the
   quorum, raise ``InsufficientAnalystQuorum`` — the route maps this to
   HTTP 422 with a structured payload.

**Scope (Phase 1):** 6 always-on analysts only —
``fundamentals``, ``technical``, ``news``, ``sentiment``, ``macro``,
``fx``. The 3 conditional analysts (``plan_critique``,
``concentration``, ``tax``) require context the /consult page does
not produce; deferred to Phase 2.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from argosy.agents.base import AgentReport
from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent
from argosy.agents.fx_analyst import FXAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow, DecisionRun

log = get_logger(__name__)


#: The 4 analysts whose output is ticker-specific. At least one must
#: succeed for the quorum to be met — otherwise we'd have a "macro+fx
#: only" consult that looks real but has no ticker grounding.
TICKER_SPECIFIC_ROLES: frozenset[str] = frozenset(
    {"fundamentals", "technical", "news", "sentiment"}
)

#: The full set of always-on roles. Order is presentation-only; agents
#: actually run in parallel via ``asyncio.gather``.
ALWAYS_ON_ROLES: tuple[str, ...] = (
    "fundamentals",
    "technical",
    "news",
    "sentiment",
    "macro",
    "fx",
)

#: Quorum: this many successful citation-bearing reports OR MORE.
#:
#: Codex's original recommendation was 3, but in practice 4/6 analysts
#: (fundamentals, news, sentiment, macro) require paid data API keys
#: (Finnhub / TipRanks / FRED) that may not be configured in a user's
#: deployment. The key-less ticker-specific path is `technical`
#: (yfinance OHLC) plus `fx` (BoI + Frankfurter), giving a minimum
#: floor of 2. The "≥ 1 ticker-specific" requirement (below) still
#: prevents macro+fx-only consultations — fx + technical OR
#: fundamentals OR news OR sentiment all qualify as ticker-grounded.
#:
#: When more data sources are configured this can land back at 3 or
#: higher; tighten via `agent_settings.yaml` rather than this constant
#: once the surface gains config knobs.
MIN_QUORUM_TOTAL: int = 2


class InsufficientAnalystQuorum(RuntimeError):
    """Raised when the always-on analyst set fails to produce enough
    citation-bearing reports to ground a downstream trader decision.

    The route handler catches this and returns HTTP 422 with the
    structured payload so the UI can render a useful error instead of
    rolling forward into a trader that has nothing to cite.
    """

    def __init__(
        self,
        *,
        succeeded: list[str],
        failed: list[tuple[str, str]],
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.succeeded = succeeded
        self.failed = failed
        self.reason = reason


@dataclass
class PerTickerAnalystsResult:
    """Outcome of a per-ticker analyst run.

    Holds the surviving citation-bearing reports + the decision_run_id
    they were persisted under. The route passes both into
    ``DecisionFlow.run(decision_run_id=..., analyst_reports=...,
    persist_input_analysts=False)``.
    """

    decision_run_id: int
    reports: list[AgentReport]
    succeeded_roles: list[str]
    skipped_roles: list[tuple[str, str]]  # (role, reason)


async def open_decision_run_for_consult(
    *, user_id: str, ticker: str, tier_value: str, started_at: datetime | None = None,
) -> int:
    """Open a fresh ``decision_runs`` row early (before the per-ticker
    analyst pass) so the analyst reports + downstream phase rows all
    join under one id.

    Mirrors ``DecisionFlow._open_decision_run`` so the contract is
    identical; called by the route when it dispatches the per-ticker
    orchestrator + the flow under the same run id.
    """
    started_at = started_at or datetime.now(timezone.utc)
    async with db_mod.get_session() as session:
        row = DecisionRun(
            user_id=user_id,
            ticker=ticker,
            tier=tier_value,
            started_at=started_at,
            status="running",
        )
        session.add(row)
        await session.commit()
        return row.id


async def close_decision_run_blocked(
    *, decision_run_id: int, reason: str, finished_at: datetime | None = None,
) -> None:
    """Close a pre-opened decision_run row with ``status='blocked'``
    when the per-ticker analyst pass can't proceed (e.g. quorum
    failure). Without this the row stays at ``status='running'``
    forever — codex BLOCKER fix on the impl review.

    Mirrors ``DecisionFlow._close_decision_run`` but specialised for
    the early-failure case where there is no proposal yet.
    """
    finished_at = finished_at or datetime.now(timezone.utc)
    async with db_mod.get_session() as session:
        row = await session.get(DecisionRun, decision_run_id)
        if row is None:
            return
        row.finished_at = finished_at
        row.status = "blocked"
        await session.commit()
    log.info(
        "per_ticker_analysts.decision_run_closed_blocked",
        decision_run_id=decision_run_id,
        reason=reason[:200],
    )


async def run_per_ticker_analysts(
    *,
    user_id: str,
    ticker: str,
    decision_run_id: int,
) -> PerTickerAnalystsResult:
    """Run the 6 always-on analysts on ``ticker`` in parallel.

    Returns the surviving citation-bearing reports. Raises
    ``InsufficientAnalystQuorum`` if fewer than ``MIN_QUORUM_TOTAL``
    citation-bearing reports come back OR no ticker-specific analyst
    succeeds.

    Side effects:
      - Persists each surviving report to ``agent_reports`` under
        ``decision_run_id``.
      - Logs per-analyst start/end/skip events.
    """
    log.info(
        "per_ticker_analysts.start",
        user_id=user_id,
        ticker=ticker,
        decision_run_id=decision_run_id,
        roles=list(ALWAYS_ON_ROLES),
    )

    # 1) Gather per-ticker + macro/fx data inputs in parallel.
    #
    # The plan-synthesis ``_gather_*`` helpers are sync and call
    # ``asyncio.run(...)`` internally; wrap each in ``asyncio.to_thread``
    # so we can call from the route's async event loop (codex BLOCKER
    # #1 fix). FX is special — it expects a sync ``Session``; we open
    # one inside the thread.
    tickers = [ticker]
    payloads = await _gather_inputs_for_ticker(ticker=ticker, tickers=tickers, user_id=user_id)

    # 2) Pre-skip analysts whose data payload is empty. Without inputs,
    # the analyst will (a) emit a no-citation output that fails the
    # gate, or (b) hallucinate citations to placate the gate. Both are
    # wrong. Skipping saves the LLM call and produces a clearer error.
    skipped_empty_payload: list[tuple[str, str]] = []
    runnable: list[tuple[str, Any]] = []

    def _maybe(role: str, payload_key: str, coro_factory):
        payload = payloads[payload_key]
        if not payload:
            skipped_empty_payload.append((role, f"empty_payload (no {payload_key} data)"))
            return
        runnable.append((role, coro_factory()))

    _maybe("fundamentals", "fundamentals",
           lambda: _run_fundamentals(user_id, tickers, payloads["fundamentals"]))
    _maybe("technical", "indicators",
           lambda: _run_technical(user_id, tickers, payloads["indicators"]))
    _maybe("news", "news",
           lambda: _run_news(user_id, tickers, payloads["news"]))
    _maybe("sentiment", "social",
           lambda: _run_sentiment(user_id, tickers, payloads["social"]))
    _maybe("macro", "macro",
           lambda: _run_macro(user_id, payloads["macro"]))
    _maybe("fx", "fx",
           lambda: _run_fx(user_id, payloads["fx"]))

    tasks = runnable
    raw_results = await asyncio.gather(
        *(t[1] for t in tasks), return_exceptions=True,
    ) if tasks else []

    # 3) Filter: drop exceptions + empty-citation reports. Seed
    # skipped_roles with the empty-payload pre-skips from step 2.
    surviving: list[AgentReport] = []
    succeeded_roles: list[str] = []
    skipped_roles: list[tuple[str, str]] = list(skipped_empty_payload)
    for (role, _coro), result in zip(tasks, raw_results):
        if isinstance(result, BaseException):
            log.warning(
                "per_ticker_analysts.role_failed",
                ticker=ticker,
                role=role,
                error_type=type(result).__name__,
                error=str(result)[:300],
            )
            skipped_roles.append((role, f"{type(result).__name__}: {result}"))
            continue
        if not _has_any_citation(result):
            log.info(
                "per_ticker_analysts.role_skipped_empty_citations",
                ticker=ticker,
                role=role,
            )
            skipped_roles.append((role, "empty_citations"))
            continue
        surviving.append(result)
        succeeded_roles.append(role)

    # 4) Quorum check (codex IMPORTANT #6) — ≥ 3/6 total AND ≥ 1 ticker-specific.
    ticker_specific_hits = [r for r in succeeded_roles if r in TICKER_SPECIFIC_ROLES]
    if len(surviving) < MIN_QUORUM_TOTAL or not ticker_specific_hits:
        reason = (
            f"per-ticker analyst quorum not met for {ticker}: "
            f"{len(surviving)}/{len(ALWAYS_ON_ROLES)} succeeded "
            f"(need ≥{MIN_QUORUM_TOTAL}); "
            f"ticker-specific hits: {ticker_specific_hits or '(none)'}"
        )
        log.warning(
            "per_ticker_analysts.quorum_failed",
            ticker=ticker,
            decision_run_id=decision_run_id,
            succeeded=succeeded_roles,
            skipped=skipped_roles,
            reason=reason,
        )
        raise InsufficientAnalystQuorum(
            succeeded=succeeded_roles,
            failed=skipped_roles,
            reason=reason,
        )

    # 5) Persist surviving reports under decision_run_id.
    await _persist_reports(decision_run_id, surviving)

    log.info(
        "per_ticker_analysts.done",
        ticker=ticker,
        decision_run_id=decision_run_id,
        succeeded=succeeded_roles,
        skipped=skipped_roles,
    )
    return PerTickerAnalystsResult(
        decision_run_id=decision_run_id,
        reports=surviving,
        succeeded_roles=succeeded_roles,
        skipped_roles=skipped_roles,
    )


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _has_any_citation(report: AgentReport) -> bool:
    """Mirror of ``BaseAgent._validate_citations`` semantics — check
    whether the report's pydantic output carries at least one
    ``cited_sources`` entry, anywhere (top-level or nested)."""
    try:
        payload = report.output.model_dump()
    except Exception:  # noqa: BLE001 — defensive on malformed output
        return False

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("cited_sources"):
                return True
            return any(_walk(v) for v in node.values())
        if isinstance(node, list):
            return any(_walk(v) for v in node)
        return False

    return _walk(payload)


async def _gather_inputs_for_ticker(
    *, ticker: str, tickers: list[str], user_id: str,
) -> dict[str, Any]:
    """Fetch fundamentals / news / indicators / social / macro / fx
    payloads for one ticker.

    The 5 network-driven gathers (fundamentals, news, indicators,
    social, macro) wrap the plan-synthesis sync gather bodies in
    ``asyncio.to_thread`` so their internal ``asyncio.run(...)`` calls
    don't conflict with the route's async event loop (codex BLOCKER #1
    on the first review).

    The FX gather is DB-bound and goes through ``AsyncSession.run_sync``
    so it sits in the proper greenlet context — using
    ``async_engine.sync_engine`` from an async loop risks ``MissingGreenlet``
    on aiosqlite (codex BLOCKER on the impl review).

    Best-effort across the board — if any individual gather fails, the
    corresponding payload comes back empty and the downstream analyst
    will (a) succeed with no citations (gets dropped by the quorum), or
    (b) succeed using its own fallback if it has one. Either way, no
    exception escapes here.
    """
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        _gather_fundamentals,
        _gather_fx_payload,
        _gather_indicators_payload,
        _gather_macro_snapshot,
        _gather_news,
        _gather_social_payload,
    )

    async def _fx_via_run_sync() -> dict[str, dict[str, float]]:
        """FX runs via AsyncSession.run_sync so the gather body uses
        the proper greenlet bridge — no driver-boundary risk."""
        try:
            async with db_mod.get_session() as session:
                return await session.run_sync(
                    lambda sync_session: _gather_fx_payload(sync_session)
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(
                "per_ticker_analysts.fx_gather_failed",
                error=str(exc)[:200],
            )
            return {}

    # Run all 6 gathers concurrently.
    fundamentals, news, indicators, social, macro, fx = await asyncio.gather(
        asyncio.to_thread(_gather_fundamentals, tickers),
        asyncio.to_thread(_gather_news, tickers),
        asyncio.to_thread(_gather_indicators_payload, tickers),
        asyncio.to_thread(_gather_social_payload, tickers),
        asyncio.to_thread(_gather_macro_snapshot),
        _fx_via_run_sync(),
        return_exceptions=False,  # _gather_* are themselves best-effort; no need for return_exceptions
    )

    return {
        "fundamentals": fundamentals,
        "news": news,
        "indicators": indicators,
        "social": social,
        "macro": macro,
        "fx": fx,
    }


# ----------------------------------------------------------------------
# Per-analyst runners — each takes the relevant slice of the payload bag
# and returns an AgentReport. Defined as small async wrappers so the
# main gather can hand them in uniformly.
# ----------------------------------------------------------------------


async def _run_fundamentals(
    user_id: str, tickers: list[str], payload: dict[str, dict[str, Any]],
) -> AgentReport:
    agent = FundamentalsAnalystAgent(user_id=user_id)
    return await agent.run(tickers=tickers, fundamentals_payload=payload)


async def _run_technical(
    user_id: str, tickers: list[str], payload: dict[str, dict[str, Any]],
) -> AgentReport:
    agent = TechnicalAnalystAgent(user_id=user_id)
    return await agent.run(tickers=tickers, indicators_payload=payload)


async def _run_news(
    user_id: str, tickers: list[str], payload: dict[str, list[dict[str, Any]]],
) -> AgentReport:
    agent = NewsAnalystAgent(user_id=user_id)
    return await agent.run(tickers=tickers, news_payload=payload)


async def _run_sentiment(
    user_id: str, tickers: list[str], payload: dict[str, list[dict[str, Any]]],
) -> AgentReport:
    agent = SentimentAnalystAgent(user_id=user_id)
    return await agent.run(tickers=tickers, social_payload=payload)


async def _run_macro(user_id: str, payload: dict[str, float]) -> AgentReport:
    agent = MacroAnalystAgent(user_id=user_id)
    return await agent.run(macro_snapshot=payload)


async def _run_fx(user_id: str, payload: dict[str, dict[str, float]]) -> AgentReport:
    agent = FXAnalystAgent(user_id=user_id)
    return await agent.run(fx_payload=payload)


# ----------------------------------------------------------------------
# Persistence (mirrors DecisionFlow._persist_agent_reports so the route
# can pass persist_input_analysts=False when calling flow.run).
# ----------------------------------------------------------------------


async def _persist_reports(decision_run_id: int, reports: list[AgentReport]) -> list[int]:
    """Persist agent_reports rows under decision_run_id. Returns the
    inserted ids (caller currently discards them — downstream code uses
    the report contents, not ids).
    """
    if decision_run_id == 0:
        return []
    ids: list[int] = []
    async with db_mod.get_session() as session:
        for r in reports:
            row = AgentReportRow(
                user_id=r.user_id,
                agent_role=r.agent_role,
                decision_id=str(decision_run_id),
                prompt_hash=r.prompt_hash,
                response_text=r.response_text,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                cost_usd=float(r.cost_usd),
                model=r.model,
                confidence=r.confidence.value if r.confidence else None,
                cache_input_tokens=r.cache_input_tokens,
                cache_creation_tokens=r.cache_creation_tokens,
                thinking_tokens=r.thinking_tokens,
                citations_json=r.citations_json,
                sources_json=r.sources_json,
                run_correlation_id=r.run_correlation_id,
                system_prompt=r.system_prompt,
                user_prompt=r.user_prompt,
            )
            session.add(row)
            await session.flush()  # so .id is populated for the return list
            ids.append(row.id)
        await session.commit()
    return ids


__all__ = [
    "ALWAYS_ON_ROLES",
    "InsufficientAnalystQuorum",
    "MIN_QUORUM_TOTAL",
    "PerTickerAnalystsResult",
    "TICKER_SPECIFIC_ROLES",
    "close_decision_run_blocked",
    "open_decision_run_for_consult",
    "run_per_ticker_analysts",
]
