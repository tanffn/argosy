"""Default best-effort fetchers that assemble a per-stock research bundle from
LIVE data sources (finnhub company news + yfinance price + the plan's own thesis).

Each fetcher returns a compact text summary or ``None``; every one is wrapped so a
missing key / network failure yields ``None`` (the field is simply absent from the
bundle and the decision agent lowers confidence + records the gap). These are
DIRECT data pulls, not the heavy LLM analysts — fast enough to run per-name.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Callable

from argosy.logging import get_logger

log = get_logger(__name__)


def news_fetcher(ticker: str, *, lookback_days: int = 14, max_items: int = 5) -> str | None:
    """Recent company-news headlines for ``ticker`` via finnhub (best-effort)."""
    try:
        from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

        today = date.today()
        items = asyncio.run(
            FinnhubAdapter().get_company_news(
                ticker, start=today - timedelta(days=lookback_days), end=today,
            )
        )
        if not items:
            return None
        heads = [i.get("headline", "").strip() for i in items[:max_items] if i.get("headline")]
        return "; ".join(heads) if heads else None
    except Exception as exc:  # noqa: BLE001 — best-effort; absent field is fine
        log.info("stock_decision.news_fetch_miss", ticker=ticker, err=str(exc)[:120])
        return None


def price_fetcher(ticker: str) -> str | None:
    """Current price for ``ticker`` via the deploy quote provider (yfinance,
    UCITS-suffix aware). Best-effort."""
    try:
        from argosy.services.deployment_funnel.from_plan import SnapshotOrLiveProvider

        p = SnapshotOrLiveProvider().quote(ticker)
        return f"last price {p}" if p is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.price_fetch_miss", ticker=ticker, err=str(exc)[:120])
        return None


def make_thesis_fetcher(db: Any, user_id: str) -> Callable[[str], "str | None"]:
    """A fetcher that returns the current plan's stance on ``ticker`` — which sleeve
    it belongs to and its rationale, or that the plan wants it EXITED (a redeploy /
    0%-target position). Best-effort; captures the plan doc once."""
    doc = None
    try:
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.thesis_load_miss", err=str(exc)[:120])
        doc = None

    def _fetch(ticker: str) -> str | None:
        if doc is None:
            return None
        t = (ticker or "").upper()
        for c in getattr(doc, "classes", []) or []:
            for inst in getattr(c, "instruments", []) or []:
                if (getattr(inst, "symbol", "") or "").upper() == t:
                    target = getattr(c, "target_pct", None)
                    stance = "plan wants to EXIT (0% target)" if (target == 0) else f"sleeve target {target}%"
                    rat = (getattr(inst, "rationale", "") or getattr(c, "rationale", "") or "")[:200]
                    return f"in sleeve '{getattr(c, 'label', '')}' ({stance}). {rat}".strip()
        return "not a plan-target instrument (candidate or legacy holding)"

    return _fetch


def default_fetchers(db: Any, user_id: str) -> dict[str, Callable[[str], "str | None"]]:
    """The live fetcher registry for a holdings review / candidate decision."""
    return {
        "news": news_fetcher,
        "price": price_fetcher,
        "thesis": make_thesis_fetcher(db, user_id),
    }


__all__ = ["news_fetcher", "price_fetcher", "make_thesis_fetcher", "default_fetchers"]
