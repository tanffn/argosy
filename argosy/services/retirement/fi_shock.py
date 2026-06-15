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
