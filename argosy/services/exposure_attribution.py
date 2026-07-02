"""Exposure attribution — credit the instruments you ALREADY hold to the plan
sleeve they cover, so cash deployment stops opening a new ticker when the exposure
is already on the book.

Two separate ideas (per the codex methodology):

  * **Attribution** — does a held instrument ``Y`` economically belong in plan
    sleeve ``X``'s exposure? (Reduces the sleeve's gap.) Decided from the curated
    ``instrument_reference`` (asset_class / sector / region), NOT raw snapshot
    labels: factor sleeves (value / momentum / low-vol) do not collide, and region
    matters for equity so US-core is never credited to an ex-US sleeve.
  * **Implementation equivalence** — should fresh cash buy ``Y`` instead of the
    plan's ``X``? Stricter: only when ``Y`` is estate-safe and a diversified fund
    of the same wrapper. A US-domiciled substitute (SCHD, O) still CREDITS the
    sleeve (so we don't overbuild the exposure) but is flagged **migrate** — the
    plan's UCITS instrument is the estate-safe implementation, and adding fresh
    cash to the US-domiciled holding would only grow the US-situs estate tail.

This module is pure (plan doc + holdings + reference → classification). Wiring it
into ``cash_only_deploy`` is a separate, separately-reviewed step.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.services.instrument_reference import lookup

# Asset classes where the geographic region is part of the exposure identity, so a
# region mismatch means "different sleeve" (US core ≠ ex-US). For real assets /
# bonds the sleeve is about the asset, not the region, so region is not required.
_REGION_SENSITIVE_ASSETS = {"Equity"}


@dataclass(frozen=True)
class SleeveSubstitute:
    plan_sleeve: str        # the plan class label
    plan_instrument: str    # X — the plan's designated instrument for the sleeve
    held_ticker: str        # Y — the instrument you already hold
    held_value_usd: float
    disposition: str        # "topup" (estate-safe, same wrapper) | "migrate" (US-situs / single-name)
    reason: str


def _attributable(xref, yref) -> bool:
    """Does held ``yref`` belong in the sleeve implemented by ``xref``?"""
    if xref.asset_class != yref.asset_class or xref.sector != yref.sector:
        return False
    if xref.asset_class in _REGION_SENSITIVE_ASSETS and xref.region != yref.region:
        return False
    return True


def classify_plan_substitutes(doc, holdings: dict[str, float]) -> list[SleeveSubstitute]:
    """Held instruments that cover a plan sleeve's exposure, each tagged ``topup``
    (add to the held fund; do not open the plan ticker) or ``migrate`` (the held
    fund is US-domiciled / a single name — credit it but move toward the plan's
    estate-safe instrument). Holdings equal to the plan's own instrument are not
    substitutes (the deploy engine tops those up directly)."""
    subs: list[SleeveSubstitute] = []
    # A held ticker credits AT MOST ONE sleeve (first match in class order) — never
    # double-count the same dollars across two sleeves of the same taxonomy.
    claimed: set[str] = set()
    for cls in getattr(doc, "classes", []) or []:
        instruments = getattr(cls, "instruments", []) or []
        if not instruments:
            continue
        x = (getattr(instruments[0], "symbol", "") or "").strip().upper()
        xref = lookup(x)
        if xref is None:
            continue
        for held, value in holdings.items():
            y = (held or "").strip().upper()
            if not y or y == x or value <= 0 or y in claimed:
                continue
            yref = lookup(y)
            if yref is None or not _attributable(xref, yref):
                continue
            claimed.add(y)
            estate_safe_topup = yref.estate_safe and yref.structure == xref.structure
            if estate_safe_topup:
                disposition = "topup"
                reason = (
                    f"{y} already covers the {cls.label} sleeve and is estate-safe — "
                    f"add to it rather than opening {x}."
                )
            else:
                why = "US-situs (estate-exposed)" if not yref.estate_safe else "a single/less-diversified holding"
                disposition = "migrate"
                reason = (
                    f"{y} covers the {cls.label} exposure but is {why}; the plan's "
                    f"{x} is the estate-safe implementation — credit {y} but move "
                    f"toward {x} (a paired sell, not a silent parallel buy)."
                )
            subs.append(SleeveSubstitute(
                plan_sleeve=cls.label, plan_instrument=x, held_ticker=y,
                held_value_usd=round(float(value), 2), disposition=disposition,
                reason=reason,
            ))
    return subs


__all__ = ["SleeveSubstitute", "classify_plan_substitutes"]
