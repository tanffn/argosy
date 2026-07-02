"""Catch-up sell sizing — repair MISSED scheduled glide tranches (executed count
behind the schedule), never the price run-up (that's risk-budget's job) and never
beyond what the missed policy action justifies."""
from __future__ import annotations

from argosy.services.nvda_catchup import catchup_sale_nis


def test_on_schedule_returns_zero():
    """As many tranches executed as waypoints due → nothing to catch up."""
    assert catchup_sale_nis(
        waypoints_due=3, tranches_executed=3, tranche_nis=500_000.0,
        total_over_cap_nis=4_000_000.0,
    ) == 0.0


def test_behind_schedule_sizes_to_missed_tranches():
    """3 waypoints due, only 1 executed → 2 missed tranches × the per-quarter size."""
    assert catchup_sale_nis(
        waypoints_due=3, tranches_executed=1, tranche_nis=500_000.0,
        total_over_cap_nis=4_000_000.0,
    ) == 1_000_000.0


def test_never_exceeds_the_over_cap():
    """Catch-up repairs the missed policy action — it can never sell more than the
    whole over-cap amount, even if many tranches were missed."""
    assert catchup_sale_nis(
        waypoints_due=20, tranches_executed=0, tranche_nis=500_000.0,
        total_over_cap_nis=4_000_000.0,
    ) == 4_000_000.0


def test_ahead_of_schedule_is_zero_not_negative():
    assert catchup_sale_nis(
        waypoints_due=1, tranches_executed=3, tranche_nis=500_000.0,
        total_over_cap_nis=4_000_000.0,
    ) == 0.0
