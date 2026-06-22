"""The book — current holdings as per-name weights (Stage-1 input).

Reuses the CANONICAL concentration helpers from ``wealth_dashboard`` so a
name's weight is the same number every surface cites (not a third definition).
A holding is a tradeable security (has a ticker, is not cash, is not physical
real estate). Cash + real estate are excluded from the routable book — you
can't diversify NVDA risk by owning a house, and idle cash is the deploy-cash
surface's job, not the per-name decision funnel's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.decision_funnel.book")


@dataclass(frozen=True)
class BookHolding:
    """One routable holding."""

    ticker: str
    asset_type: str
    usd_value_k: float
    weight_pct: float  # % of the tradeable securities book


def load_book(session: "Session", *, user_id: str) -> list[BookHolding]:
    """Return the current routable book (per-name weights), newest snapshot.

    Empty list when there is no snapshot or no tradeable book — the funnel
    then has nothing to route and no-ops cleanly.
    """
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
    from argosy.services.wealth_dashboard import tradeable_securities_usd_k

    snap = get_latest_snapshot_row(session, user_id)
    if snap is None:
        return []
    try:
        positions = json.loads(snap.positions_json or "[]")
    except (json.JSONDecodeError, TypeError):
        positions = []

    book_k = tradeable_securities_usd_k(positions)
    if book_k <= 0:
        return []

    # Aggregate by symbol (a name can appear in more than one location/lot).
    by_symbol: dict[str, dict] = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        asset_type = (p.get("asset_type") or "").lower()
        symbol = (p.get("symbol") or "").strip().upper()
        if "cash" in asset_type or not symbol or symbol in {"-", "—"}:
            continue
        try:
            v = float(p.get("usd_value_k") or 0.0)
        except (TypeError, ValueError):
            continue
        entry = by_symbol.setdefault(symbol, {"v": 0.0, "asset_type": p.get("asset_type") or ""})
        entry["v"] += v

    holdings = [
        BookHolding(
            ticker=sym,
            asset_type=entry["asset_type"],
            usd_value_k=entry["v"],
            weight_pct=entry["v"] / book_k * 100.0,
        )
        for sym, entry in by_symbol.items()
    ]
    holdings.sort(key=lambda h: -h.weight_pct)
    _log.info("decision_funnel.book_loaded", user_id=user_id, names=len(holdings))
    return holdings


__all__ = ["BookHolding", "load_book"]
