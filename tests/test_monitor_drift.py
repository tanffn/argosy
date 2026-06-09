"""T5.5 — allocation-drift per-symptom detector removed.

Design contract: anomaly detection for allocation drift flows exclusively
through the emergent StateObserverAgent. The old ``check_allocation_drift``
function is gone; this file now asserts:

  1. ``check_allocation_drift`` is NOT importable from ``argosy.services.plan_monitor``
     (the detector has been deleted).
  2. The state-diff comparator map includes the allocation-drift field pairs
     so the observer CAN detect allocation drift emergently (coverage preserved).
"""
from __future__ import annotations

import importlib
import types


def test_check_allocation_drift_removed_from_plan_monitor() -> None:
    """check_allocation_drift must NOT exist in plan_monitor (T5.5)."""
    import argosy.services.plan_monitor as pm
    assert not hasattr(pm, "check_allocation_drift"), (
        "check_allocation_drift is a forbidden per-symptom detector (T5.5). "
        "It must be removed; allocation drift flows through the emergent observer."
    )


def test_allocation_drift_not_in_plan_monitor_all() -> None:
    """__all__ must not export the deleted per-symptom symbols (T5.5)."""
    import argosy.services.plan_monitor as pm
    for forbidden in (
        "check_allocation_drift",
        "AllocationDriftFlag",
        "DriftCheckResult",
        "get_active_drift_flags",
        "check_macro_shift",
        "MacroShiftFlag",
        "MacroShiftCheckResult",
        "get_active_macro_shift_flags",
    ):
        assert forbidden not in pm.__all__, (
            f"{forbidden!r} must not be in plan_monitor.__all__ (T5.5 removal)"
        )


def test_state_diff_comparator_map_covers_allocation_drift() -> None:
    """Emergent coverage contract: the state-diff comparator map must pair
    portfolio.allocations[].current_pct against portfolio.allocations[].target_pct.

    If this assertion fails, removing check_allocation_drift would silently
    drop allocation-drift anomaly coverage — that is the blocker condition.
    """
    from argosy.services.state_diff import PLAN_BASELINE_COMPARATOR_MAP

    assert "portfolio.allocations[].current_pct" in PLAN_BASELINE_COMPARATOR_MAP, (
        "portfolio.allocations[].current_pct missing from PLAN_BASELINE_COMPARATOR_MAP; "
        "allocation-drift coverage is NOT emergent — removal of check_allocation_drift "
        "would silently drop coverage."
    )
    assert (
        PLAN_BASELINE_COMPARATOR_MAP["portfolio.allocations[].current_pct"]
        == "portfolio.allocations[].target_pct"
    ), (
        "portfolio.allocations[].current_pct must be paired with target_pct in the "
        "comparator map for emergent drift detection to work."
    )
