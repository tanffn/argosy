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

from argosy.services import instrument_reference
from argosy.services.target_allocation_doc import OTHER_SINGLES_LABEL

# Live snapshot ``asset_type`` (lower-cased) -> canonical plan class label.
# Covers the categories the Family-Finances TSV emits; anything unmapped is
# surfaced under its own (target-less) row rather than silently bucketed.
_ASSET_TYPE_TO_LABEL: dict[str, str] = {
    "nvidia": "Strategic single-stock (NVDA)",
    "core equity": "US broad-market core",
    # Bare "equity" (BRK/B, IUHC carry the generic word, not a tilt) → the
    # US broad-market core bucket, rather than an orphan "Equity" row with no
    # plan target.
    "equity": "US broad-market core",
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
    account: str = ""     # holding location (e.g. "Leumi", "schwab 876")


@dataclass(frozen=True)
class CategoryBreakdown:
    label: str
    current_pct: float
    target_pct: float | None   # None when the plan has no target for this class
    current_value_k: float
    holdings: tuple[HoldingRow, ...] = field(default=())


# Reference asset-class → canonical plan-class label, for the non-equity
# classes. The raw asset_type is unreliable (SGOV is tagged "Defensive" but is
# a T-bill = Cash & T-bills; EIMI is tagged "REIT" but is equity). The
# instrument reference is authoritative for what a holding *is*.
_ASSET_CLASS_TO_LABEL: dict[str, str] = {
    "Cash": "Cash & T-bills (incl. ILS tranche)",
    "Fixed Income": "Short-duration IG bonds",
    "Real Estate": "Real assets (REIT/TIPS)",
    "Alternatives": "Real assets (REIT/TIPS)",
}


def _label_for(asset_type: str, symbol: str = "", details: str = "") -> str:
    # The instrument reference is the classification authority; the raw
    # asset_type is only a fallback for instruments not in the table.
    ref = instrument_reference.lookup(symbol, details)
    if ref is not None:
        if ref.asset_class == "Equity":
            # Pure non-US equity → International (TA-200 "Core Equity", EIMI
            # "REIT"); never "Global" (partly US — codex review). US/Global
            # equity falls through to the asset_type tilt crosswalk below.
            if ref.region in (
                instrument_reference.REGION_ISRAEL,
                instrument_reference.REGION_EUROPE,
                instrument_reference.REGION_EM,
            ):
                return _ASSET_TYPE_TO_LABEL["international"]
        elif ref.asset_class in _ASSET_CLASS_TO_LABEL:
            # Cash / Fixed Income / Real Estate — e.g. SGOV → Cash & T-bills,
            # not "Short-duration IG bonds" off its "Defensive" asset_type.
            return _ASSET_CLASS_TO_LABEL[ref.asset_class]
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


def build_allocation_breakdown(snapshot, doc, *, exclude_nvda: bool = False) -> list[CategoryBreakdown]:
    """Group live holdings into plan classes; pair current % with the canonical
    class target %; attach the per-symbol drill-down. Sorted by current weight.

    ``exclude_nvda`` drops the NVDA RSU concentration and renormalises the
    percentages over the ex-NVDA book — NVDA at ~61% otherwise flattens every
    other class to a sliver, so the diversified core is unreadable."""
    positions = list(getattr(snapshot, "positions", []) or [])
    if exclude_nvda:
        positions = [p for p in positions if not _is_nvda(p)]
    # Physical real estate (the "Aborad" property aggregate) is illiquid net
    # worth, not an investable allocation sleeve — it lives in the dedicated
    # Real-estate panel and must NOT sit in "Real assets (REIT/TIPS)" next to
    # tradeable REIT ETFs (Ariel: "it's not something I can easily trade/sell").
    positions = [
        p for p in positions
        if (getattr(p, "asset_type", "") or "").strip().lower() != "real estate"
    ]
    total = sum(float(getattr(p, "usd_value_k", 0.0) or 0.0) for p in positions)
    if total <= 0:
        return []

    targets = _doc_targets_by_label(doc)
    # When NVDA is excluded, drop its plan class too and renormalise the
    # remaining targets to 100% — otherwise current %s (over the ex-NVDA book)
    # and target %s (incl. NVDA) aren't comparable, and NVDA's target would
    # appear as a phantom 0%-current row.
    if exclude_nvda and targets:
        targets = {k: v for k, v in targets.items() if "NVDA" not in k}
        tsum = sum(targets.values())
        if tsum > 0:
            targets = {k: round(v * 100.0 / tsum, 2) for k, v in targets.items()}

    # Resolve one asset_type per ticker (prefer non-blank): the export leaves
    # asset_type blank on some lots of a ticker held with a populated type
    # elsewhere (e.g. the $3K Schwab SCHG vs the Leumi SCHG "Growth"). Without
    # this the blank lot falls into "Unclassified".
    effective_type: dict[str, str] = {}
    for p in positions:
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        at = (getattr(p, "asset_type", "") or "").strip()
        if sym and at and sym not in effective_type:
            effective_type[sym] = at

    grouped: dict[str, list] = {}
    for p in positions:
        v = float(getattr(p, "usd_value_k", 0.0) or 0.0)
        if v <= 0:
            continue
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        at = (getattr(p, "asset_type", "") or "").strip() or effective_type.get(sym, "")
        label = _label_for(at, getattr(p, "symbol", ""), getattr(p, "details", ""))
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
                )
                for p in ps
            ),
            key=lambda h: -h.value_k,
        ))
        # The "non-NVDA singles to redeploy" bucket has no plan class because
        # the plan's intent is to wind it down — show target 0%, not blank.
        tgt = targets.get(label)
        if tgt is None and label == OTHER_SINGLES_LABEL:
            tgt = 0.0
        rows.append(CategoryBreakdown(
            label=label,
            current_pct=round(100.0 * cat_value / total, 2),
            target_pct=tgt,
            current_value_k=round(cat_value, 2),
            holdings=holdings,
        ))

    # Surface EVERY plan-target class, including those with no current holding
    # (e.g. US low-volatility equity, Short-duration IG bonds) as 0%-current
    # rows — otherwise the target column silently omits them and doesn't sum to
    # 100%, and the user can't see where they're under-allocated.
    held_labels = {r.label for r in rows}
    for label, tgt in targets.items():
        if label not in held_labels:
            rows.append(CategoryBreakdown(
                label=label, current_pct=0.0, target_pct=tgt,
                current_value_k=0.0, holdings=(),
            ))

    # Sort by current weight, then by target — so 0%-current plan classes are
    # ordered by how much the plan wants there (not arbitrarily).
    rows.sort(key=lambda r: (-r.current_pct, -(r.target_pct or 0.0)))
    return rows


__all__ = ["HoldingRow", "CategoryBreakdown", "build_allocation_breakdown"]
