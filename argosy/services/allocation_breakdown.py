"""Live current-allocation vs canonical plan-target, by class, with per-symbol
drill-down — the data behind the /portfolio 'Allocation vs target' card.

- **current** %: live holdings grouped by ``resolve_sleeve_label`` (Block H
  instrument_plan_classes map + plan instrument list — never asset_type→US-broad).
- **target** %: canonical ``TargetAllocationDoc`` class ``target_pct``.
- **holdings**: per-symbol drill-down.

Unmapped holdings land in ``Unmapped — needs classification`` (fail-loud).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from argosy.services import instrument_reference
from argosy.services.instrument_plan_class import (
    UNMAPPED_LABEL,
    ClassificationEntry,
    resolve_sleeve_label,
)


@dataclass(frozen=True)
class HoldingRow:
    symbol: str
    name: str
    value_k: float
    pct: float            # % of the full book
    account: str = ""     # holding location (e.g. "Leumi", "schwab 876")
    estate_safe: bool | None = None  # True=non-US-situs, False=US-situs, None=unknown


@dataclass(frozen=True)
class CategoryBreakdown:
    label: str
    current_pct: float
    target_pct: float | None   # None when the plan has no target for this class
    current_value_k: float
    holdings: tuple[HoldingRow, ...] = field(default=())


def _is_physical_real_estate(p) -> bool:
    """Illiquid/direct property only — listed REITs/property ETFs stay investable.

    Mirrors ``plan_numeric_resolver._is_real_estate`` so IWDP/O never vanish
    from Current-allocation-vs-plan-target while the Pipera apartment stays out.
    """
    blob = " ".join(
        str(getattr(p, k, "") or "")
        for k in ("asset_type", "details", "category", "type")
    ).lower()
    if "real estate" not in blob and "real-estate" not in blob:
        return False
    sym = str(getattr(p, "symbol", "") or "").strip().lower()
    has_tradable = bool(sym) and sym not in {"-", "—", "n/a", "na", "none"}
    return not has_tradable


def _plan_symbol_labels(doc) -> dict[str, str]:
    """Symbol → canonical plan-class label, from the doc's instrument lists.

    Exposure-aware attribution (binding rule): a position whose ticker the
    plan explicitly names attributes to THAT class, regardless of the
    snapshot's ``asset_type`` label.
    """
    if doc is None:
        return {}
    from argosy.services.allocation_plan import normalize_sleeve_label

    out: dict[str, str] = {}
    for c in getattr(doc, "classes", []) or []:
        label = normalize_sleeve_label(c.label)
        for inst in getattr(c, "instruments", []) or []:
            sym = (getattr(inst, "symbol", "") or "").strip().upper()
            if sym and sym not in out:
                out[sym] = label
    return out


def _doc_targets_by_label(doc) -> dict[str, float]:
    """Targets keyed by CURRENT canonical label."""
    if doc is None:
        return {}
    from argosy.services.allocation_plan import normalize_sleeve_label

    return {
        normalize_sleeve_label(c.label): c.target_pct
        for c in getattr(doc, "classes", [])
    }


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


def _holding_account(p) -> str:
    """Normalised holding location — the NVDA RSU's bare "schwab" folds into
    "schwab 876" (same account) so the same ticker reads consistently."""
    loc = (getattr(p, "location", "") or "").strip()
    if loc.lower() == "schwab":
        return "schwab 876"
    return loc


def _is_nvda(p) -> bool:
    sym = (getattr(p, "symbol", "") or "").strip().upper()
    at = (getattr(p, "asset_type", "") or "").strip().lower()
    return sym == "NVDA" or "nvidia" in at


def build_allocation_breakdown(
    snapshot,
    doc,
    *,
    exclude_nvda: bool = False,
    classification_map: dict[str, ClassificationEntry] | None = None,
) -> list[CategoryBreakdown]:
    """Group live holdings into plan classes; pair current % with the canonical
    class target %; attach the per-symbol drill-down. Sorted by current weight.

    ``exclude_nvda`` drops deliberately unmanaged holdings (NVDA by
    convention) and renormalises the percentages over the managed book —
    NVDA at ~61% otherwise flattens every other class to a sliver, so the
    diversified core is unreadable. Prefer the managed-book filter
    (``holding_books.is_managed_position``) over a hard-coded symbol check
    so other unmanaged holdings behave the same way.

    ``classification_map`` is the Block H DB map (owner/fleet/plan rows).
    """
    from argosy.services.holding_books import is_managed_position

    positions = list(getattr(snapshot, "positions", []) or [])
    if exclude_nvda:
        positions = [p for p in positions if is_managed_position(p)]
    # Physical real estate is illiquid net worth — not an investable sleeve.
    positions = [p for p in positions if not _is_physical_real_estate(p)]
    total = sum(float(getattr(p, "usd_value_k", 0.0) or 0.0) for p in positions)
    if total <= 0:
        return []

    targets = _doc_targets_by_label(doc)
    if exclude_nvda and targets:
        targets = {
            k: v for k, v in targets.items()
            if "strategic single-stock" not in k.lower()
        }
        tsum = sum(targets.values())
        if tsum > 0:
            targets = {k: round(v * 100.0 / tsum, 2) for k, v in targets.items()}

    effective_type: dict[str, str] = {}
    for p in positions:
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        at = (getattr(p, "asset_type", "") or "").strip()
        if sym and at and sym not in effective_type:
            effective_type[sym] = at

    plan_symbol_labels = _plan_symbol_labels(doc)
    grouped: dict[str, list] = {}
    for p in positions:
        v = float(getattr(p, "usd_value_k", 0.0) or 0.0)
        if v <= 0:
            continue
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        at = (getattr(p, "asset_type", "") or "").strip() or effective_type.get(sym, "")
        label = resolve_sleeve_label(
            sym,
            asset_type=at,
            details=getattr(p, "details", "") or "",
            plan_symbol_labels=plan_symbol_labels,
            classification_map=classification_map,
        )
        grouped.setdefault(label, []).append(p)

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
                    account=_holding_account(p),
                    estate_safe=(
                        None if _is_cash(p) else instrument_reference.estate_safe_for(
                            getattr(p, "symbol", ""), getattr(p, "details", ""))
                    ),
                )
                for p in ps
            ),
            key=lambda h: -h.value_k,
        ))
        tgt = targets.get(label)
        if tgt is None:
            tgt = 0.0
        rows.append(CategoryBreakdown(
            label=label,
            current_pct=round(100.0 * cat_value / total, 2),
            target_pct=tgt,
            current_value_k=round(cat_value, 2),
            holdings=holdings,
        ))

    held_labels = {r.label for r in rows}
    for label, tgt in targets.items():
        if label not in held_labels:
            rows.append(CategoryBreakdown(
                label=label, current_pct=0.0, target_pct=tgt,
                current_value_k=0.0, holdings=(),
            ))

    def _sort_key(r: CategoryBreakdown):
        unmapped_boost = 1 if r.label == UNMAPPED_LABEL and r.current_pct > 0 else 0
        return (-unmapped_boost, -r.current_pct, -(r.target_pct or 0.0))

    rows.sort(key=_sort_key)
    return rows


__all__ = [
    "HoldingRow",
    "CategoryBreakdown",
    "build_allocation_breakdown",
    "resolve_sleeve_label",
    "_plan_symbol_labels",
    "_is_physical_real_estate",
    "UNMAPPED_LABEL",
]
