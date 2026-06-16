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
