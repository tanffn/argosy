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


def render_instrument_monitoring_meta(inst: Any) -> str:
    """Render an instrument's recorded exit triggers / review anchor as a prompt
    suffix (empty string when none). The monitor agent's weakened/broken judgment
    must evaluate against the RECORDED invalidation conditions, not vibes."""
    parts: list[str] = []
    triggers = list(getattr(inst, "exit_triggers", None) or [])
    if triggers:
        parts.append("EXIT TRIGGERS (recorded invalidation conditions): "
                     + "; ".join(str(t) for t in triggers))
    review_on = getattr(inst, "review_on", None)
    if review_on:
        parts.append(f"Review on: {review_on}")
    return (" " + " | ".join(parts)) if parts else ""


def _class_labels_by_symbol(db: Any, user_id: str, doc: Any) -> dict[str, str]:
    """Symbol -> canonical plan-class label for the LIVE book, via the SAME
    exposure-aware attribution the allocation surfaces use
    (``build_allocation_breakdown``: plan-named instruments first, then the
    snapshot asset_type / instrument-reference crosswalk). Best-effort: any
    failure returns ``{}`` and the thesis fetcher degrades to symbol-only."""
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        row = get_latest_snapshot_row(db, user_id)
        if row is None:
            return {}
        rows = build_allocation_breakdown(row_to_snapshot(row), doc)
        out: dict[str, str] = {}
        for cat in rows:
            for h in cat.holdings:
                sym = (h.symbol or "").strip().upper()
                if sym and sym not in out:
                    out[sym] = cat.label
        return out
    except Exception as exc:  # noqa: BLE001 — attribution is additive, never fatal
        log.info("stock_decision.class_attribution_miss", err=str(exc)[:120])
        return {}


def make_thesis_fetcher(db: Any, user_id: str) -> Callable[[str], "str | None"]:
    """A fetcher that returns the current plan's stance on ``ticker``:

    - a plan-named instrument gets its sleeve + rationale + recorded exit
      triggers / review anchor (or that the plan wants it EXITED);
    - a holding the plan does NOT name but whose exposure covers a plan class
      (SCHD/FWRA/CNDX/O-style substitutes) gets the CLASS rationale, honestly
      labelled as substitute coverage — attributed via the same
      snapshot-category mapping the allocation surfaces use;
    - a true off-plan single keeps the honest placeholder.

    Best-effort; captures the plan doc + attribution map once."""
    doc = None
    try:
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.thesis_load_miss", err=str(exc)[:120])
        doc = None

    # Exposure-aware attribution for NON-plan symbols (fix for the "$265k SCHD
    # is 'not a plan-target instrument'" hole). Keyed by the NORMALIZED class
    # label so a sleeve relabel never breaks the join.
    class_by_label: dict[str, Any] = {}
    label_by_symbol: dict[str, str] = {}
    if doc is not None:
        try:
            from argosy.services.allocation_plan import normalize_sleeve_label

            class_by_label = {
                normalize_sleeve_label(getattr(c, "label", "") or ""): c
                for c in getattr(doc, "classes", []) or []
            }
        except Exception as exc:  # noqa: BLE001
            log.info("stock_decision.class_index_miss", err=str(exc)[:120])
        if db is not None:
            label_by_symbol = _class_labels_by_symbol(db, user_id, doc)

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
                    meta = render_instrument_monitoring_meta(inst)
                    return f"in sleeve '{getattr(c, 'label', '')}' ({stance}). {rat}{meta}".strip()
        # Not plan-named: attribute by exposure (the class the holding covers).
        label = label_by_symbol.get(t)
        if label:
            c = class_by_label.get(label)
            if c is not None:
                primary = next(
                    (i for i in (getattr(c, "instruments", []) or [])
                     if getattr(i, "role", "") == "primary"),
                    None,
                ) or next(iter(getattr(c, "instruments", []) or []), None)
                primary_sym = getattr(primary, "symbol", "") if primary is not None else ""
                target = getattr(c, "target_pct", None)
                rat = (getattr(c, "rationale", "") or "")[:200]
                return (
                    f"covers the '{getattr(c, 'label', '')}' sleeve "
                    f"(target {target}%) as a substitute"
                    + (f" for {primary_sym}" if primary_sym else "")
                    + f"; not plan-named. Class thesis: {rat}"
                ).strip()
        return "not a plan-target instrument (candidate or legacy holding)"

    return _fetch


def default_fetchers(db: Any, user_id: str) -> dict[str, Callable[[str], "str | None"]]:
    """The live fetcher registry for a holdings review / candidate decision."""
    return {
        "news": news_fetcher,
        "price": price_fetcher,
        "thesis": make_thesis_fetcher(db, user_id),
    }


__all__ = [
    "news_fetcher", "price_fetcher", "make_thesis_fetcher", "default_fetchers",
    "render_instrument_monitoring_meta",
]
