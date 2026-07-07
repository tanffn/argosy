"""DB-state → decision-packet assembly — the ONE wiring every deploy-author
caller shares.

``build_decision_packet`` is pure; this module does the impure half: gather the
real NVDA look-through, the canonical current-vs-target sleeve attribution, the
live market/macro regime and fresh per-candidate research from the DB/network,
then shape them into the packet. Extracted verbatim from the ``/deploy-cash``
route so the daily period-directive job feeds the author the SAME holistic view
the on-demand route does — two callers, one packet contract.

Every enrichment is best-effort: a failed sub-fetch logs and degrades that field,
never blocks the packet.
"""
from __future__ import annotations

from typing import Any

from argosy.logging import get_logger

_log = get_logger("argosy.allocation_author.packet_assembly")


def assemble_author_packet(
    db: Any,
    *,
    user_id: str,
    doc: Any,
    holdings_usd: dict[str, float],
    cash_usd: float,
    deployable_usd: float,
) -> dict[str, Any]:
    """Assemble the full deployment-author decision packet from live state.

    ``doc`` is the canonical ``TargetAllocationDoc``; ``holdings_usd`` the current
    tradeable book; ``deployable_usd`` the net-of-tax amount to place. ``db`` is a
    sync Session used for the snapshot-bound enrichments.
    """
    from argosy.services.allocation_author.packet import build_decision_packet
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    # Feed REAL NVDA look-through (CSPX/R1GR/FWRA re-buying NVDA is invisible
    # to raw holdings["NVDA"]) so the author reasons over TRUE concentration —
    # a core reason the deterministic engine lost. Best-effort.
    _nvda_ltv = None
    _book = None
    try:
        from argosy.services.deployment_funnel.from_plan import build_gate_inputs
        _gi = build_gate_inputs(doc=doc, holdings_usd=holdings_usd, cash_usd=cash_usd)
        _nvda_ltv = _gi.current_effective_nvda_usd
        _book = _gi.book_usd
    except Exception as exc:  # noqa: BLE001 — fall back to raw holdings NVDA
        _log.warning("author_packet.lookthrough_failed", error=str(exc)[:120])
    # Canonical current-vs-target attribution (look-through aware) so the
    # author fills the real under-target sleeves — plan-fit from within.
    _cur_by_sleeve: dict[str, float] = {}
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        _snap_row = get_latest_snapshot_row(db, user_id)
        if _snap_row is not None:
            for _cb in build_allocation_breakdown(row_to_snapshot(_snap_row), doc):
                _cur_by_sleeve[_cb.label] = float(_cb.current_pct)
    except Exception as exc:  # noqa: BLE001 — gaps are best-effort
        _log.warning("author_packet.breakdown_failed", error=str(exc)[:120])
    # Live market/macro regime so the author reasons about the current
    # environment (equity-vs-bond, US-vs-exUS) instead of deploying blind.
    # Best-effort; the author still runs if this is unavailable.
    _market_signals: dict = {}
    try:
        from argosy.services.deployment_market_context import (
            assemble_deployment_market_context,
        )
        _mc = assemble_deployment_market_context(db)
        _market_signals = {
            "as_of": _mc.overall_age_label,
            "is_stale": _mc.is_any_stale,
            "snapshot": {k: float(v) for k, v in _mc.snapshot.items()},
        }
        if _mc.nvda is not None:
            _market_signals["nvda_quote"] = {
                "price": _mc.nvda.price, "consistent": _mc.nvda.consistent,
            }
    except Exception as exc:  # noqa: BLE001 — market ctx is best-effort
        _log.warning("author_packet.market_context_failed", error=str(exc)[:120])
    # Fetch-before-buy: fresh per-candidate research (live news + price) on
    # the INDIVIDUAL-STOCK candidates (the moonshot / single-name sleeves),
    # where fresh diligence matters most — broad diversified ETFs don't need
    # per-name news. Best-effort + bounded; author reasons over CURRENT data
    # instead of a static menu. Absent research leaves the packet unchanged.
    _candidate_research: dict[str, str] = {}
    try:
        from argosy.services.stock_decision.fetchers import (
            news_fetcher,
            price_fetcher,
        )
        _single_name_syms: list[str] = []
        for _c in getattr(doc, "classes", []) or []:
            if (getattr(_c, "snapshot_category", "") or "") != "Individual Stocks":
                continue  # only single-name sleeves (high-growth + NVDA)
            for _i in getattr(_c, "instruments", []) or []:
                _s = (getattr(_i, "symbol", "") or "").strip()
                if _s and _s.upper() != "NVDA":  # NVDA won't be bought (over cap)
                    _single_name_syms.append(_s)
        for _s in dict.fromkeys(_single_name_syms):  # dedup, preserve order
            _parts = []
            _p = price_fetcher(_s)
            _n = news_fetcher(_s)
            if _p:
                _parts.append(_p)
            if _n:
                _parts.append("news: " + _n[:200])
            if _parts:
                _candidate_research[_s] = " | ".join(_parts)
    except Exception as exc:  # noqa: BLE001 — research is additive/best-effort
        _log.warning("author_packet.candidate_research_failed", error=str(exc)[:120])
    return build_decision_packet(
        doc=doc, holdings_usd=holdings_usd, deployable_usd=deployable_usd,
        cash_usd=cash_usd,
        nvda_cap_pct=float(getattr(doc, "nvda_cap_pct", 0.0) or 0.0),
        nvda_lookthrough_usd=_nvda_ltv, book_usd=_book,
        current_pct_by_sleeve=_cur_by_sleeve,
        policy_signals=_market_signals,
        candidate_research=_candidate_research,
        user_constraints=(
            "Earliest safe retirement is the prime directive. NVDA single-name "
            "over-concentration is handled by the plan's SCHEDULED SELLS, not by "
            "refusing equity buys — so fill the plan's under-target sleeves by "
            "gap, INCLUDING its US-equity sleeves; a broad fund's incidental "
            "few-percent NVDA look-through is not a reason to decline it. Only "
            "avoid instruments that are themselves NVDA-heavy / single-name "
            "concentrated. Prefer Irish UCITS / estate-safe instruments."
        ),
    )


__all__ = ["assemble_author_packet"]
