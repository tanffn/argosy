"""Live current-allocation vs canonical plan-target, by class, with per-symbol
drill-down — the data behind the /portfolio 'Allocation vs target' card.

The prior chart compared the plan glide's *modelled* today-anchor (q0) to its
end-state (qN) — neither is your LIVE allocation. This builds the honest view:

- **current** %: the live snapshot holdings, grouped by their ``asset_type``
  (the TSV category: NVIDIA / Core Equity / Dividend / Growth / Cash / …),
  mapped to the canonical plan's class labels.
- **target** %: the canonical ``TargetAllocationDoc`` class ``target_pct`` for
  the same label (the plan's destination).
- **holdings**: the actual symbols that fell into each class (symbol, name,
  value, % of book) — the drill-down.

A held category with no plan class (e.g. legacy single names being wound down,
or an unrecognised asset type) is surfaced under its own row with a 0% target —
never dropped — so the current %s always conserve to 100.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from argosy.services.target_allocation_doc import OTHER_SINGLES_LABEL

# Live snapshot ``asset_type`` (lower-cased) -> canonical plan class label.
# Covers the categories the Family-Finances TSV emits; anything unmapped is
# surfaced under its own (target-less) row rather than silently bucketed.
_ASSET_TYPE_TO_LABEL: dict[str, str] = {
    "nvidia": "Strategic single-stock (NVDA)",
    "core equity": "US broad-market core",
    "dividend": "Dividend-quality income",
    "growth": "US growth tilt (ex-NVDA)",
    "international": "International developed (ex-US)",
    "cash": "Cash & T-bills (incl. ILS tranche)",
    "defensive": "Short-duration IG bonds",
    "reit": "Real assets (REIT/TIPS)",
    "real estate": "Real assets (REIT/TIPS)",
    "alternative": "Real assets (REIT/TIPS)",
    "individual stocks": OTHER_SINGLES_LABEL,
}


@dataclass(frozen=True)
class HoldingRow:
    symbol: str
    name: str
    value_k: float
    pct: float            # % of the full book


@dataclass(frozen=True)
class CategoryBreakdown:
    label: str
    current_pct: float
    target_pct: float | None   # None when the plan has no target for this class
    current_value_k: float
    holdings: tuple[HoldingRow, ...] = field(default=())


def _label_for(asset_type: str) -> str:
    at = (asset_type or "").strip().lower()
    return _ASSET_TYPE_TO_LABEL.get(at, asset_type.strip() or "Unclassified")


def _doc_targets_by_label(doc) -> dict[str, float]:
    if doc is None:
        return {}
    return {c.label: c.target_pct for c in getattr(doc, "classes", [])}


def _is_cash(p) -> bool:
    return (getattr(p, "asset_type", "") or "").strip().lower() in ("cash", "money market")


def _holding_symbol(p) -> str:
    """Cash rows carry a blank/"-" symbol; label them by currency so the NIS
    and USD cash tranches are distinct rather than both showing as "—"."""
    sym = (getattr(p, "symbol", "") or "").strip()
    if _is_cash(p) and sym in ("", "-"):
        ccy = (getattr(p, "currency", "") or "").strip().upper()
        return f"{ccy} cash" if ccy else "Cash"
    return sym or "—"


def _holding_name(p) -> str:
    if _is_cash(p):
        ccy = (getattr(p, "currency", "") or "").strip().upper()
        return f"{ccy} cash balance" if ccy else "Cash balance"
    return (getattr(p, "details", "") or "").strip()


def _is_nvda(p) -> bool:
    sym = (getattr(p, "symbol", "") or "").strip().upper()
    at = (getattr(p, "asset_type", "") or "").strip().lower()
    return sym == "NVDA" or "nvidia" in at


def build_allocation_breakdown(snapshot, doc, *, exclude_nvda: bool = False) -> list[CategoryBreakdown]:
    """Group live holdings into plan classes; pair current % with the canonical
    class target %; attach the per-symbol drill-down. Sorted by current weight.

    ``exclude_nvda`` drops the NVDA RSU concentration and renormalises the
    percentages over the ex-NVDA book — NVDA at ~61% otherwise flattens every
    other class to a sliver, so the diversified core is unreadable."""
    positions = list(getattr(snapshot, "positions", []) or [])
    if exclude_nvda:
        positions = [p for p in positions if not _is_nvda(p)]
    total = sum(float(getattr(p, "usd_value_k", 0.0) or 0.0) for p in positions)
    if total <= 0:
        return []

    targets = _doc_targets_by_label(doc)
    grouped: dict[str, list] = {}
    for p in positions:
        v = float(getattr(p, "usd_value_k", 0.0) or 0.0)
        if v <= 0:
            continue
        grouped.setdefault(_label_for(getattr(p, "asset_type", "")), []).append(p)

    rows: list[CategoryBreakdown] = []
    for label, ps in grouped.items():
        cat_value = sum(float(getattr(p, "usd_value_k", 0.0) or 0.0) for p in ps)
        holdings = tuple(sorted(
            (
                HoldingRow(
                    symbol=_holding_symbol(p),
                    name=_holding_name(p),
                    value_k=round(float(getattr(p, "usd_value_k", 0.0) or 0.0), 2),
                    pct=round(100.0 * float(getattr(p, "usd_value_k", 0.0) or 0.0) / total, 2),
                )
                for p in ps
            ),
            key=lambda h: -h.value_k,
        ))
        rows.append(CategoryBreakdown(
            label=label,
            current_pct=round(100.0 * cat_value / total, 2),
            target_pct=targets.get(label),  # None when the plan has no such class
            current_value_k=round(cat_value, 2),
            holdings=holdings,
        ))
    rows.sort(key=lambda r: -r.current_pct)
    return rows


__all__ = ["HoldingRow", "CategoryBreakdown", "build_allocation_breakdown"]
