"""Discovery dry-powder earmark — subtract from deployable cash.

Plan cash-class ``discovery_reserve`` (v76+) is earmarked for green-lit
discovery buys. Deployment tooling must not treat it as idle/deployable
general cash. Pure money math; no LLM.

Label (binding, surfaces on packet + plan caveats + funding note):
``discovery reserve — earmarked, not deployable``.
"""
from __future__ import annotations

from typing import Any

DISCOVERY_RESERVE_LABEL = "discovery reserve — earmarked, not deployable"


def _cash_classes(doc: Any) -> list[Any]:
    classes = getattr(doc, "classes", None) or []
    out: list[Any] = []
    for c in classes:
        sigma = (getattr(c, "sigma_class", None) or "").strip().lower()
        label = (getattr(c, "label", None) or "").strip().lower()
        if sigma == "cash" or "cash" in label:
            out.append(c)
    return out


def _reserve_block(doc: Any) -> dict[str, Any] | None:
    """Return the first cash-class ``discovery_reserve`` dict, or None."""
    if doc is None:
        return None
    for c in _cash_classes(doc):
        block = getattr(c, "discovery_reserve", None)
        if block is None and isinstance(c, dict):
            block = c.get("discovery_reserve")
        if isinstance(block, dict) and block:
            return block
    return None


def resolve_discovery_reserve_usd(
    doc: Any,
    *,
    book_usd: float | None = None,
) -> float:
    """USD earmarked as discovery reserve from the current plan cash class.

    Preference order:
      1. ``usd_at_apply`` / ``usd`` absolute fields when > 0
      2. ``pct_of_book`` × ``book_usd`` when book is known
      3. 0 when the field is missing or unusable (additive no-op)

    Never raises — malformed blocks resolve to 0.
    """
    block = _reserve_block(doc)
    if not block:
        return 0.0
    try:
        for key in ("usd_at_apply", "usd", "reserve_usd"):
            raw = block.get(key)
            if raw is None:
                continue
            val = float(raw)
            if val > 0:
                return round(val, 2)
        pct = block.get("pct_of_book")
        if pct is not None and book_usd is not None and float(book_usd) > 0:
            return round(float(book_usd) * float(pct) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def apply_discovery_reserve(
    *,
    cash_total_usd: float,
    reserve_usd: float,
) -> tuple[float, float]:
    """Split cash into (deployable, excluded_reserve).

    Edge cases (exhaustive):
      * missing / zero reserve → (cash, 0)
      * reserve > cash → (0, cash)  # cannot earmark more than exists
      * negative inputs clamped to 0
    """
    cash = max(0.0, round(float(cash_total_usd), 2))
    reserve = max(0.0, round(float(reserve_usd), 2))
    if reserve <= 0 or cash <= 0:
        return cash, 0.0
    excluded = min(reserve, cash)
    deployable = round(cash - excluded, 2)
    return deployable, round(excluded, 2)


def labeled_exclusion(excluded_usd: float) -> str:
    """Single labeled line for caveats / funding / packet."""
    return f"{DISCOVERY_RESERVE_LABEL} (${excluded_usd:,.2f})"


__all__ = [
    "DISCOVERY_RESERVE_LABEL",
    "apply_discovery_reserve",
    "labeled_exclusion",
    "resolve_discovery_reserve_usd",
]
