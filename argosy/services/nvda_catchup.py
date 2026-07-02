"""Catch-up sell sizing — the exception that repairs a MISSED scheduled glide
tranche, distinct from both the routine policy pace and the risk-budget floor test.

Codex methodology: catch-up repairs missed *policy actions* (tranches the schedule
said to sell that were not executed), NOT the price run-up (appreciation lifting the
weight is risk-budget's concern). So it is measured on EXECUTION, not on weight:
``missed = waypoints_due − tranches_executed``. The sale is those missed tranches at
the per-quarter size, capped at the full over-cap amount (it can never sell more than
the missed policy action justifies).
"""
from __future__ import annotations


def catchup_sale_nis(
    *,
    waypoints_due: int,
    tranches_executed: int,
    tranche_nis: float,
    total_over_cap_nis: float,
) -> float:
    """The catch-up sale (NIS): the missed scheduled tranches at the per-quarter
    size, capped at the total over-cap. ``0.0`` when on/ahead of schedule."""
    missed = max(0, int(waypoints_due) - int(tranches_executed))
    if missed <= 0 or tranche_nis <= 0:
        return 0.0
    return round(min(missed * float(tranche_nis), max(0.0, float(total_over_cap_nis))), 2)


__all__ = ["catchup_sale_nis"]
