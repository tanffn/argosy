"""FI-sufficiency-under-NVDA-shock — the compositional sufficiency check.

A promoted plan can claim "FI reached" while that claim is true ONLY at the
full NVDA mark: a single concentrated position carries most of the surplus, so a
modest NVDA drawdown drops net worth below the perpetuity base. That defect is
invisible to any single agent — it only appears when you COMPOSE the
synthesizer's sufficiency claim with the risk officer's concentration tail.

This module re-derives sufficiency after marking NVDA down by each shock, so the
gate can fail an unqualified "reached" claim that the plan's own NVDA tail
breaks. Pure arithmetic; no I/O.
"""
from __future__ import annotations

from typing import Any, Protocol


# Primary shocks the promote gate + synthesizer cite (must stay in lockstep).
PRIMARY_NVDA_SHOCK = 0.30
PRIMARY_FX_SHOCK = 0.10


class _ResolvedLike(Protocol):
    def get(self, key: str) -> Any: ...


def _resolved_float(resolved: _ResolvedLike, key: str) -> float | None:
    rv = resolved.get(key)
    if rv is None:
        return None
    status = getattr(rv, "status", "resolved")
    value = getattr(rv, "value", rv if not hasattr(rv, "value") else rv.value)
    if status != "resolved" or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_nvda_shock_inputs(resolved: _ResolvedLike) -> dict[str, float] | None:
    """Gate/synth shared inputs for ``fi_sufficiency_under_shock``.

    Returns kwargs when every required value is RESOLVED, else None.

    Prefers absolute ``concentration.nvda_value_nis`` (TOTAL book) so a
    deliberately unmanaged NVDA still shocks net worth. Falls back to
    ``net_worth × nvda_current_pct`` only when the absolute value is absent
    and the pct is resolved (legacy path).
    """
    net_worth = _resolved_float(resolved, "portfolio.net_worth_nis")
    perpetuity_base = _resolved_float(resolved, "retirement.fi_target_nis")
    fi_total = _resolved_float(resolved, "retirement.fi_total_capital_nis")
    if None in (net_worth, perpetuity_base, fi_total):
        return None
    nvda_value = _resolved_float(resolved, "concentration.nvda_value_nis")
    if nvda_value is None:
        nvda_frac = _resolved_float(resolved, "concentration.nvda_current_pct")
        if nvda_frac is None:
            return None
        nvda_value = net_worth * nvda_frac
    return {
        "net_worth_nis": net_worth,
        "nvda_value_nis": nvda_value,
        "perpetuity_base_nis": perpetuity_base,
        "fi_total_nis": fi_total,
    }


def derive_fx_shock_inputs(resolved: _ResolvedLike) -> dict[str, float] | None:
    """Gate/synth shared inputs for ``fi_sufficiency_under_fx_shock``."""
    net_worth = _resolved_float(resolved, "portfolio.net_worth_nis")
    perpetuity_base = _resolved_float(resolved, "retirement.fi_target_nis")
    fi_total = _resolved_float(resolved, "retirement.fi_total_capital_nis")
    usd_exposure = _resolved_float(resolved, "portfolio.usd_exposure_nis")
    if None in (net_worth, perpetuity_base, fi_total, usd_exposure):
        return None
    return {
        "net_worth_nis": net_worth,
        "usd_exposure_nis": usd_exposure,
        "perpetuity_base_nis": perpetuity_base,
        "fi_total_nis": fi_total,
    }


def primary_nvda_shock_net_worth_nis(
    *,
    net_worth_nis: float,
    nvda_value_nis: float,
    shock: float = PRIMARY_NVDA_SHOCK,
) -> float:
    """Net worth after the primary NVDA mark-down (gate row ``shock_0.30``)."""
    return round(net_worth_nis - shock * nvda_value_nis, 2)


def primary_fx_shock_net_worth_nis(
    *,
    net_worth_nis: float,
    usd_exposure_nis: float,
    fx_shock: float = PRIMARY_FX_SHOCK,
) -> float:
    """Net worth after the primary adverse FX move (gate row ``fx_shock_-0.10``)."""
    return round(net_worth_nis - fx_shock * usd_exposure_nis, 2)


def fi_sufficiency_under_shock(
    *,
    net_worth_nis: float,
    nvda_value_nis: float,
    perpetuity_base_nis: float,
    fi_total_nis: float,
    shocks: tuple[float, ...] = (0.30, 0.50),
) -> dict:
    """Recompute FI sufficiency after marking NVDA down by each shock.

    Returns a dict with a ``base`` row + one ``shock_{s:.2f}`` row per shock,
    each carrying the (shocked) net worth and whether the perpetuity base and
    full FI total still clear.
    """

    def row(nw: float) -> dict:
        return {
            "net_worth_nis": round(nw, 2),
            "perpetuity_reached": nw >= perpetuity_base_nis,
            "total_reached": nw >= fi_total_nis,
        }

    out = {"base": row(net_worth_nis)}
    for s in shocks:
        out[f"shock_{s:.2f}"] = row(net_worth_nis - s * nvda_value_nis)
    return out


def fi_sufficiency_under_fx_shock(
    *,
    net_worth_nis: float,
    usd_exposure_nis: float,
    perpetuity_base_nis: float,
    fi_total_nis: float,
    fx_shock: float = 0.10,
) -> dict:
    """Recompute FI sufficiency after a ``fx_shock`` adverse USD/NIS move.

    A non-US-person's plan can claim "FI reached" while that claim is fragile to
    routine currency movement: a chunk of net worth is USD-denominated, so a
    shekel strengthening (USD/NIS down ``fx_shock``) cuts the NIS value of that
    sleeve and can drop net worth below the perpetuity base. This is the FX twin
    of :func:`fi_sufficiency_under_shock` (which marks the NVDA tail): it marks
    the USD sleeve down by ``fx_shock`` and re-checks sufficiency.

    ``usd_exposure_nis`` is the NIS value of USD-denominated assets (the FX-
    sensitive base). The shocked net worth is
    ``net_worth_nis - fx_shock * usd_exposure_nis``. Returns a ``base`` row + a
    ``fx_shock_-{fx_shock:.2f}`` row (negative sign = adverse move), each row
    matching the NVDA-shock row shape. Pure arithmetic; no I/O.
    """

    def row(nw: float) -> dict:
        return {
            "net_worth_nis": round(nw, 2),
            "perpetuity_reached": nw >= perpetuity_base_nis,
            "total_reached": nw >= fi_total_nis,
        }

    return {
        "base": row(net_worth_nis),
        f"fx_shock_-{fx_shock:.2f}": row(net_worth_nis - fx_shock * usd_exposure_nis),
    }
