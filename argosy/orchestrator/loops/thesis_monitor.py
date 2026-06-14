"""Per-holding thesis-monitor cadence loop.

Runs once after the US close (next-morning IDT, when company news + closing
prices have settled): enumerate the INDIVIDUAL-STOCK holdings (broad ETFs are
exempt), gather each name's feeds (finnhub news, yfinance price, SEC Form 4
insider), run the Opus :class:`ThesisMonitorAgent`, and —
only on a genuine thesis-level change (weakened / broken at warning+ severity) —
write a ``thesis_monitor_*`` monitor flag and fire the SAME action_proposer the
state-observer uses, so the change surfaces as a reviewable /proposals action.

Pure-seam design (mirrors :class:`StateObserverLoop`): ``holdings_fn`` /
``gather_fn`` / ``agent_factory`` / ``write_fn`` / ``now_fn`` are injectable so
the loop is unit-testable without any live feed or LLM call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

log = get_logger(__name__)

# After the US close, run next-morning IDT (09:00) so closing prices + overnight
# company news are in. Event-driven re-runs (cash/snapshot change) reuse tick().
_DEFAULT_CRON = "0 9 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"
_FLAG_TTL_DAYS = 7
# Only these thesis verdicts are actionable; intact / strengthened → no flag.
_ESCALATION_STATUSES = frozenset({"weakened", "broken"})
# AND the severity must be actionable (info-band thesis notes are not proposals).
_ACTIONABLE_SEVERITIES = frozenset({"warning", "critical"})


def _is_actionable(assessment: Any) -> bool:
    return (
        str(getattr(assessment, "thesis_status", "")) in _ESCALATION_STATUSES
        and str(getattr(assessment, "severity", "")) in _ACTIONABLE_SEVERITIES
    )


def thesis_monitor_metadata() -> JobMetadata:
    """``source_kind='monitor'`` — thesis flags join the same Red-Flag family."""
    return JobMetadata(
        name="thesis_monitor_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 09:00 IDT (after US close)",
        source_kind="monitor",
        description=(
            "Per-holding thesis monitor — for each individual-stock holding "
            "(ETFs exempt), reads company news / price / SEC Form 4 + 13F and runs "
            "the Opus thesis-monitor agent. High bar: only a genuine thesis-level "
            "change (weakened/broken) writes a flag and fires the action proposer."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Default seams (real, best-effort). Tests inject fakes instead.
# ---------------------------------------------------------------------------

def default_individual_holdings(session: Session, user_id: str) -> list[dict[str, Any]]:
    """Individual-stock holdings (structure == Stock; ETFs/bonds/cash excluded),
    each with its book weight and the plan's stated thesis/role for the name."""
    from argosy.services import instrument_reference as iref
    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.allocation_glidepath import _latest_portfolio_snapshot
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    snap = _latest_portfolio_snapshot(session, user_id)
    if snap is None:
        return []
    holdings, _cash = tradeable_holdings(snap)
    total = sum(v for v in holdings.values() if v > 0) or 1.0

    # Plan thesis per symbol: NVDA carries the strategic-single-stock rationale;
    # other singles are the non-plan redeploy band.
    plan_thesis_by_symbol: dict[str, str] = {}
    pv = get_current_plan(session, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    if doc is not None:
        for c in doc.classes:
            for inst in c.instruments:
                if getattr(inst, "symbol", None):
                    plan_thesis_by_symbol[inst.symbol.upper()] = (
                        f"{c.label}: {(c.rationale or '')[:400]}"
                    )

    out: list[dict[str, Any]] = []
    for sym, usd in holdings.items():
        if usd <= 0:
            continue
        ref = iref.lookup(sym)
        if ref is None or ref.structure != iref.STRUCT_STOCK:
            continue  # ETFs / bonds / cash are exempt from per-stock reasoning
        out.append({
            "ticker": sym.upper(),
            "weight_pct": round(100.0 * usd / total, 2),
            "plan_thesis": plan_thesis_by_symbol.get(
                sym.upper(), "(non-plan individual holding — being redeployed)"
            ),
        })
    out.sort(key=lambda h: -h["weight_pct"])
    return out


def _price_summary(bars: list) -> dict[str, Any]:
    """Reduce a year of OHLCV bars to {last, 1m/3m return, off-52w-high}."""
    closes = [c for c in (getattr(b, "close", None) for b in bars) if c]
    if not closes:
        return {}
    last = closes[-1]

    def _ret(n: int) -> float | None:
        if len(closes) > n and closes[-1 - n]:
            return round(100.0 * (last / closes[-1 - n] - 1.0), 1)
        return None

    hi = max(closes)
    return {
        "last": round(last, 2),
        "ret_1m_pct": _ret(21),
        "ret_3m_pct": _ret(63),
        "off_52w_high_pct": round(100.0 * (last / hi - 1.0), 1) if hi else None,
    }


def default_gather_feeds(holding: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Best-effort per-ticker feed bundle (news + price + insider). Every adapter
    call is guarded so a feed outage degrades to an empty section rather than
    failing the run."""
    ticker = holding["ticker"]
    bundle: dict[str, Any] = {**holding, "news": [], "insider": [], "price": {}}
    end = now.date()
    news_start = end - timedelta(days=30)
    price_start = end - timedelta(days=400)

    async def _gather() -> None:
        try:
            from argosy.adapters.data.finnhub_adapter import FinnhubAdapter
            bundle["news"] = await FinnhubAdapter().get_company_news(
                ticker, start=news_start, end=end
            ) or []
        except Exception as exc:  # noqa: BLE001 — feed outage is non-fatal
            log.warning("thesis_monitor.feed.news_failed", ticker=ticker, error=str(exc))
        try:
            from argosy.adapters.data.yfinance_adapter import YfinanceAdapter
            bars = await YfinanceAdapter().get_ohlcv(ticker, price_start, end) or []
            bundle["price"] = _price_summary(bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("thesis_monitor.feed.price_failed", ticker=ticker, error=str(exc))
        try:
            from argosy.adapters.data.sec_form4_adapter import SecForm4Adapter
            bundle["insider"] = await SecForm4Adapter().get_recent_form4_for_ticker(
                ticker, days=30
            ) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("thesis_monitor.feed.insider_failed", ticker=ticker, error=str(exc))

    try:
        asyncio.run(_gather())
    except Exception as exc:  # noqa: BLE001
        log.warning("thesis_monitor.feed.gather_failed", ticker=ticker, error=str(exc))
    return bundle


# ---------------------------------------------------------------------------
# Dedicated thesis-flag writer — reuses the action_proposer firing seam.
# ---------------------------------------------------------------------------

def write_thesis_flag(
    session: Session, user_id: str, assessment: Any, *, now: datetime
) -> int | None:
    """Persist one ``thesis_monitor_<status>`` MonitorFlag (deduped) and fire the
    action proposer for warning/critical severities. Returns the flag id, or None
    when deduped/skipped. Reuses ``_maybe_run_action_proposer_safe`` so thesis
    escalations flow through the EXACT same flag→proposal pipeline as the
    state-observer."""
    from argosy.services.state_observer_flag_writer import (
        _acknowledged_peer_exists,
        _active_peer_exists,
        _maybe_run_action_proposer_safe,
        _tombstone_expired_peers,
    )
    from argosy.state.models import MonitorFlag

    ticker = str(getattr(assessment, "ticker", "") or "").upper()
    status = str(getattr(assessment, "thesis_status", "") or "")
    severity = str(getattr(assessment, "severity", "info") or "info")
    if not ticker or not _is_actionable(assessment):
        return None

    kind = f"thesis_monitor_{status}"
    dedup_key = f"v1|thesis_monitor|{user_id}|{ticker}|{status}"

    # Dedup mirrors write_observer_flags' branch order (§4.3): an ACTIVE
    # (unack+unexpired) peer means the same thesis change is already surfaced →
    # skip; otherwise tombstone any EXPIRED unack peer (clears the partial unique
    # index so the INSERT lands) — but a user-ACKNOWLEDGED peer that we did NOT
    # just tombstone means the user already dismissed this exact change → skip.
    if _active_peer_exists(session, user_id=user_id, dedup_key=dedup_key, now=now):
        return None
    ack_before = _acknowledged_peer_exists(session, user_id=user_id, dedup_key=dedup_key)
    tombstoned = _tombstone_expired_peers(
        session, user_id=user_id, dedup_key=dedup_key, now=now
    )
    if ack_before and not tombstoned:
        return None

    payload = {
        "primary_field": f"holding.{ticker}",
        "ticker": ticker,
        "thesis_status": status,
        "severity": severity,
        "rationale_md": getattr(assessment, "rationale_md", "") or "",
        "signals": list(getattr(assessment, "signals", []) or []),
        "suggested_action": getattr(assessment, "suggested_action", "none"),
        "cited_sources": list(getattr(assessment, "cited_sources", []) or []),
        "confidence": str(getattr(assessment, "confidence", "")),
        "source": "thesis_monitor",
    }
    row = MonitorFlag(
        user_id=user_id, kind=kind, severity=severity,
        payload=json.dumps(payload), surfaced_at=now,
        expires_at=now + timedelta(days=_FLAG_TTL_DAYS), dedup_key=dedup_key,
    )
    session.add(row)
    session.commit()

    _maybe_run_action_proposer_safe(
        session, observer_flag_row=row, severity=severity, now=now
    )
    return int(row.id) if row.id is not None else None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class ThesisMonitorLoop(CadenceLoop):
    """Daily per-holding thesis monitor."""

    name = "thesis_monitor_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        holdings_fn: Callable[..., list[dict[str, Any]]] | None = None,
        gather_fn: Callable[..., dict[str, Any]] | None = None,
        agent_factory: Callable[[], Any] | None = None,
        write_fn: Callable[..., int | None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        max_holdings: int = 25,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._holdings_fn = holdings_fn or default_individual_holdings
        self._gather_fn = gather_fn or default_gather_feeds
        self._agent_factory = agent_factory
        self._write_fn = write_fn or write_thesis_flag
        self._now_fn = now_fn or _utcnow
        self._max_holdings = max_holdings
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        run_at = (now or self._now_fn)()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        summary = await asyncio.to_thread(self._run_sync, run_at=run_at)
        self.last_output_summary = summary
        log.info("thesis_monitor.tick.done", user_id=self.user_id, **summary)
        return summary

    def _run_sync(self, *, run_at: datetime) -> dict[str, Any]:
        from argosy.orchestrator.loops.state_observer import (
            _build_default_session_factory,
        )

        factory = self._session_factory or _build_default_session_factory()
        session = factory()
        summary: dict[str, Any] = {
            "assessed": 0, "escalated": 0, "flags_written": 0, "errors": []
        }
        try:
            holdings = self._holdings_fn(session, self.user_id)[: self._max_holdings]
            if not holdings:
                summary["skipped_reason"] = "no_individual_holdings"
                return summary
            bundles = [self._gather_fn(h, now=run_at) for h in holdings]
            agent = self._agent_factory() if self._agent_factory else _default_agent(self.user_id)
            report = asyncio.run(agent.run(bundles=bundles))
            assessments = list(getattr(report.output, "assessments", []))
            summary["assessed"] = len(assessments)
            for a in assessments:
                if not _is_actionable(a):
                    continue
                summary["escalated"] += 1
                try:
                    flag_id = self._write_fn(session, self.user_id, a, now=run_at)
                    if flag_id is not None:
                        summary["flags_written"] += 1
                except Exception as exc:  # noqa: BLE001 — one holding never sinks the batch
                    session.rollback()
                    summary["errors"].append(f"{getattr(a,'ticker','?')}: {exc}")
            return summary
        finally:
            session.close()


def _default_agent(user_id: str):
    from argosy.agents.thesis_monitor import ThesisMonitorAgent

    return ThesisMonitorAgent(user_id=user_id)


def run_thesis_monitor_now(
    *, user_id: str = "ariel", session_factory=None
) -> dict[str, Any] | None:
    """Manual-trigger entry (the /api/jobs '{name}/trigger' route + backfills)."""
    loop = ThesisMonitorLoop(user_id=user_id, session_factory=session_factory)
    return asyncio.run(loop.tick())


__all__ = [
    "ThesisMonitorLoop",
    "thesis_monitor_metadata",
    "write_thesis_flag",
    "default_individual_holdings",
    "default_gather_feeds",
    "run_thesis_monitor_now",
]
