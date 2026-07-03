"""The decision-packet builder — assembles the ONE holistic view the deployment
author reasons over (and the deterministic verifier gates against).

The pivot's premise is that the fleet lost because it never saw the whole picture:
the coarse water-fill couldn't know FWRA is ~62% US, never weighed the pending
NVDA-sale CGT, and poured more US onto a concentrated book. This builder hands the
author everything it needs in one object — holdings, deployable cash, the plan menu
(with domicile), NVDA concentration by look-through, the reserve shortfall, the
pending tax liability, sourced instrument look-through facts, and the period's
policy signals — so it can make the three judgment calls the prompt made in one pass.

Pure: no LLM, no DB, no network. The route assembles the raw inputs; this shapes
them into the packet consumed by both ``DeploymentAuthorAgent`` and
``verify_allocation_proposal``.
"""
from __future__ import annotations

from typing import Any, Callable

from argosy.services.allocation_author.instrument_facts import lookup_facts


def build_decision_packet(
    *,
    doc: Any,
    holdings_usd: dict[str, float],
    deployable_usd: float,
    cash_usd: float | None = None,
    reserve_target_usd: float = 0.0,
    reserve_current_usd: float = 0.0,
    nvda_lookthrough_usd: float | None = None,
    nvda_cap_pct: float | None = None,
    book_usd: float | None = None,
    current_pct_by_sleeve: dict[str, float] | None = None,
    policy_signals: dict | None = None,
    user_constraints: str = "",
    extra_known_symbols: set[str] | None = None,
    facts_lookup: Callable[[str], Any] = lookup_facts,
) -> dict[str, Any]:
    """Shape the raw deploy inputs into the author/verifier decision packet.

    ``doc`` is a ``TargetAllocationDoc`` (its ``classes`` carry each sleeve's
    label / target_pct / instruments with domicile). ``holdings_usd`` maps a held
    symbol to its USD value. ``deployable_usd`` is the net-of-tax amount to place.
    All concentration / reserve / tax figures are inputs (computed upstream from the
    snapshot + sell tranche) so this stays pure and testable.
    """
    holdings_usd = {k: float(v) for k, v in (holdings_usd or {}).items()}

    # --- plan menu (sleeve → target → tickers → domiciles → current/gap) --
    # current_pct_by_sleeve is Argosy's canonical current-vs-target attribution
    # (from build_allocation_breakdown, look-through aware). Carrying the gap lets
    # the author fill the most under-target sleeves from real numbers — plan-fit
    # authored FROM WITHIN, not enforced by a deterministic gate.
    cur_by = {str(k): float(v) for k, v in (current_pct_by_sleeve or {}).items()}
    plan_menu: list[dict[str, Any]] = []
    menu_symbols: set[str] = set()
    for c in getattr(doc, "classes", []) or []:
        tickers: list[str] = []
        domiciles: list[str] = []
        for inst in getattr(c, "instruments", []) or []:
            sym = (getattr(inst, "symbol", "") or "").strip()
            if not sym:
                continue
            tickers.append(sym)
            menu_symbols.add(sym.upper())
            domiciles.append((getattr(inst, "domicile", "") or "").strip())
        label = getattr(c, "label", "")
        target_pct = float(getattr(c, "target_pct", 0.0) or 0.0)
        entry = {
            "sleeve": label,
            "snapshot_category": getattr(c, "snapshot_category", ""),
            "target_pct": target_pct,
            "tickers": tickers,
            "domiciles": domiciles,
        }
        if label in cur_by:
            entry["current_pct"] = round(cur_by[label], 1)
            entry["gap_to_target_pct"] = round(target_pct - cur_by[label], 1)
        plan_menu.append(entry)

    # --- known symbols (verifier's invented-ticker gate) -------------------
    known = set(menu_symbols)
    known |= {s.upper() for s in holdings_usd}
    known |= {s.upper() for s in (extra_known_symbols or set())}

    # --- concentration -----------------------------------------------------
    book = book_usd if book_usd is not None else sum(holdings_usd.values())
    nvda_ltv = (
        nvda_lookthrough_usd if nvda_lookthrough_usd is not None
        else holdings_usd.get("NVDA", 0.0)
    )
    nvda_pct = round(100.0 * nvda_ltv / book, 1) if book > 0 else 0.0
    cap = nvda_cap_pct if nvda_cap_pct is not None else float(
        getattr(doc, "nvda_cap_pct", 0.0) or 0.0
    )

    # --- reserve shortfall -------------------------------------------------
    shortfall = max(0.0, float(reserve_target_usd) - float(reserve_current_usd))

    # --- sourced instrument look-through facts -----------------------------
    # Carry facts for every symbol in play (menu tickers + holdings) so the
    # author can't mistake a US-heavy all-world fund for ex-US and the verifier
    # can catch it if it does.
    facts: list[dict[str, Any]] = []
    for sym in sorted(menu_symbols | {s.upper() for s in holdings_usd}):
        f = facts_lookup(sym)
        if f is None:
            continue
        facts.append({
            "symbol": f.symbol,
            "us_weight": f.us_weight,
            "source": f.source,
            "confidence": f.confidence,
        })

    return {
        "deployable_usd": float(deployable_usd),
        "total_cash_usd": float(cash_usd) if cash_usd is not None else None,
        "holdings": holdings_usd,
        "known_symbols": known,
        "plan_menu": plan_menu,
        "nvda": {
            "lookthrough_usd": round(float(nvda_ltv), 2),
            "book_usd": round(float(book), 2),
            "pct": nvda_pct,
            "cap_pct": cap,
        },
        "reserve": {
            "target_usd": float(reserve_target_usd),
            "current_usd": float(reserve_current_usd),
            "shortfall_usd": round(shortfall, 2),
        },
        "instrument_facts": facts,
        "policy_signals": dict(policy_signals or {}),
        "user_constraints": user_constraints or "",
    }


__all__ = ["build_decision_packet"]
