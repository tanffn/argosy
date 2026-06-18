"""COLLECTION nodes for the derivation graph — set-edges that close the
dependency-completeness hole.

The headline estate / concentration figures are derived FROM a holdings
*collection*. Modeling the holdings list as a single COLLECTION ``INPUT`` node
and wiring the derived figures with an edge to it means a change in the
collection's MEMBERSHIP (a position added or removed — not just a scalar moving)
invalidates and re-derives those figures. Previously the estate figure was a
hand-set scalar with no edge back to the positions that produced it, so adding a
US-domiciled holding left it *stale but valid* — the exact hole this closes.

The recipes REUSE the authoritative, already-tested functions; they do not
reinvent the money math:
  * ``concentration.us_situs_estate_nis`` =
    ``safety_gates._us_situs_assets_usd(holdings) * fx``
  * ``concentration.us_situs_symbol_breakdown`` = per-position
    ``{symbol, name, usd_value, classification}`` — the included/excluded list,
    classified the SAME way ``_us_situs_assets_usd`` counts.
  * ``portfolio.net_worth_nis`` and ``concentration.nvda_current_pct`` — summed
    from the holdings collection (NVDA pct via the canonical
    ``wealth_dashboard.nvda_concentration_pct``).

Pure: no DB, no LLM, no I/O. Positions/fx are passed in.
"""
from __future__ import annotations

from typing import Any

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

# Recipe / classification version — bump when the recipe logic changes so the
# nodes go stale on a compute change even when inputs are unchanged.
COMPUTE_VERSION = "graph_collections.v1"


def _estate_classifier_version() -> str:
    """Stamp the estate-classification TABLE into the compute_version of the
    domicile-dependent nodes (us_situs + symbol breakdown). codex review: those
    nodes depend not only on (holdings, fx) but on instrument_reference's
    domicile classification — if the table flips a ticker's estate_safe, the
    exposure changes with holdings/fx unchanged. Folding a hash of the
    {symbol: estate_safe} table into compute_version makes the engine invalidate
    those nodes automatically when the classifier table changes."""
    import hashlib

    from argosy.services.instrument_reference import _REFERENCE

    table = {sym: ref.estate_safe for sym, ref in sorted(_REFERENCE.items())}
    digest = hashlib.sha256(repr(table).encode("utf-8")).hexdigest()[:12]
    return f"{COMPUTE_VERSION}+ir:{digest}"

HOLDINGS_KEY = "holdings"
FX_KEY = "fx.usd_nis"
# The snapshot's stored USD/NIS rate. NIS-native positions are held at their
# nominal shekel value (a shekel balance does not move in shekel terms when
# USD/NIS moves), recovered from the stored ``usd_value_k`` via this snapshot
# rate — NOT the current BOI rate (which marks the USD-denominated sleeve). This
# mirrors ``plan_numeric_resolver._resolve_net_worth`` so the collection's
# net-worth figure equals the authoritative resolver figure exactly.
SNAP_FX_KEY = "fx.usd_nis_snapshot"
US_SITUS_KEY = "concentration.us_situs_estate_nis"
BREAKDOWN_KEY = "concentration.us_situs_symbol_breakdown"
NET_WORTH_KEY = "portfolio.net_worth_nis"
NVDA_PCT_KEY = "concentration.nvda_current_pct"


def _classify(position: dict) -> str:
    """Classify a position EXACTLY as ``_us_situs_assets_usd`` counts it:

      - ``"cash"``      -> cash row (portfolio-interest exemption; excluded)
      - ``"non-US"``    -> UCITS / non-US-domiciled / Israeli (estate-safe; excluded)
      - ``"excluded"``  -> no real symbol (physical real estate / residual)
      - ``"US"``        -> US-domiciled OR uncurated-with-symbol (counted, conservative)
    """
    from argosy.services.instrument_reference import estate_safe_for

    asset_type = (position.get("asset_type") or "").lower()
    details = position.get("details") or ""
    symbol = (position.get("symbol") or "").strip()

    if "cash" in asset_type:
        return "cash"
    if "ucits" in details.lower() or "ucits" in asset_type:
        return "non-US"
    estate_safe = estate_safe_for(symbol, details)
    if estate_safe is True:
        return "non-US"
    if estate_safe is None:
        if not symbol or symbol in {"-", "—"}:
            return "excluded"
        # Uncurated but real symbol — counted as US-situs conservatively.
        return "US"
    # estate_safe is False -> US-domiciled.
    return "US"


def _usd_value(position: dict) -> float:
    try:
        return float(position.get("usd_value_k") or 0.0) * 1000.0
    except (TypeError, ValueError):
        return 0.0


def _recipe_us_situs(inbound: dict[str, Any]) -> float:
    from argosy.services.retirement.safety_gates import _us_situs_assets_usd

    holdings = inbound[HOLDINGS_KEY]
    fx = float(inbound[FX_KEY])
    return _us_situs_assets_usd(holdings) * fx


def _recipe_symbol_breakdown(inbound: dict[str, Any]) -> list[dict]:
    from argosy.services.instrument_reference import name_for

    holdings = inbound[HOLDINGS_KEY]
    rows: list[dict] = []
    for p in holdings:
        symbol = (p.get("symbol") or "").strip()
        rows.append(
            {
                "symbol": symbol,
                "name": name_for(symbol, p.get("details") or ""),
                "usd_value": _usd_value(p),
                "classification": _classify(p),
            }
        )
    return rows


def _recipe_net_worth(inbound: dict[str, Any]) -> float:
    """USD-denominated assets × current FX + NIS-native at nominal shekels.
    Mirrors ``plan_numeric_resolver._resolve_net_worth`` EXACTLY so the collection
    figure equals the authoritative resolver figure:

      * USD positions: ``usd_value_k`` is the USD value → × current BOI ``fx``.
      * NIS-native positions: ``usd_value_k`` is the USD-translated value of a
        shekel balance → recover the nominal NIS via the SNAPSHOT rate
        (``snap_fx if snap_fx > 0 else fx``), held flat in shekels (a shekel
        balance does not move in shekel terms when USD/NIS moves).

    Re-translating NIS-native by the snapshot rate (not just adding the raw
    ``usd_value_k``) is what closes the ~₪191K divergence from the resolver."""
    holdings = inbound[HOLDINGS_KEY]
    fx = float(inbound[FX_KEY])
    snap_fx = float(inbound[SNAP_FX_KEY])
    nis_rate = snap_fx if snap_fx > 0 else fx
    usd_assets_usd = 0.0
    nis_native_nis = 0.0
    for p in holdings:
        v = _usd_value(p)
        if (p.get("currency") or "").upper() == "USD":
            usd_assets_usd += v
        else:
            nis_native_nis += v * nis_rate
    return usd_assets_usd * fx + nis_native_nis


def _recipe_nvda_pct(inbound: dict[str, Any]) -> float | None:
    """NVDA weight as a fraction (0–1), via the canonical wealth-dashboard
    helper (NVDA ÷ tradeable securities book). None when the book is empty."""
    from argosy.services.wealth_dashboard import nvda_concentration_pct

    holdings = inbound[HOLDINGS_KEY]
    pct = nvda_concentration_pct(holdings)
    return None if pct is None else pct / 100.0


def build_holdings_graph(
    positions: list[dict], fx: float, snap_fx: float | None = None
) -> DerivationGraph:
    """Wire a derivation graph rooted at the holdings COLLECTION + fx scalars.

    ``fx`` is the current BOI USD/NIS (marks the USD-denominated sleeve);
    ``snap_fx`` is the snapshot's stored rate (recovers nominal NIS for NIS-native
    positions). When ``snap_fx`` is omitted it defaults to ``fx`` (single-rate
    book). Returns an un-computed graph (call ``recompute()``). The derived nodes
    all carry an inbound edge to ``holdings`` so a membership change re-derives
    them.
    """
    g = DerivationGraph()
    g.add_node(Node(key=HOLDINGS_KEY, kind=NodeKind.INPUT, value=positions))
    g.add_node(Node(key=FX_KEY, kind=NodeKind.INPUT, value=fx))
    g.add_node(Node(
        key=SNAP_FX_KEY, kind=NodeKind.INPUT,
        value=fx if snap_fx is None else snap_fx,
    ))

    estate_version = _estate_classifier_version()
    g.add_node(Node(
        key=US_SITUS_KEY, kind=NodeKind.DERIVED,
        inputs=(HOLDINGS_KEY, FX_KEY),
        recipe=_recipe_us_situs, compute_version=estate_version,
    ))
    g.add_node(Node(
        key=BREAKDOWN_KEY, kind=NodeKind.DERIVED,
        inputs=(HOLDINGS_KEY,),
        recipe=_recipe_symbol_breakdown, compute_version=estate_version,
    ))
    g.add_node(Node(
        key=NET_WORTH_KEY, kind=NodeKind.DERIVED,
        inputs=(HOLDINGS_KEY, FX_KEY, SNAP_FX_KEY),
        recipe=_recipe_net_worth, compute_version=COMPUTE_VERSION,
    ))
    g.add_node(Node(
        key=NVDA_PCT_KEY, kind=NodeKind.DERIVED,
        inputs=(HOLDINGS_KEY,),
        recipe=_recipe_nvda_pct, compute_version=COMPUTE_VERSION,
    ))
    return g


__all__ = [
    "build_holdings_graph",
    "COMPUTE_VERSION",
    "HOLDINGS_KEY",
    "FX_KEY",
    "SNAP_FX_KEY",
    "US_SITUS_KEY",
    "BREAKDOWN_KEY",
    "NET_WORTH_KEY",
    "NVDA_PCT_KEY",
]
