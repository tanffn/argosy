"""T5.5 — macro-shift per-symptom detector removed.

Design contract: macro-event anomaly detection flows exclusively through the
emergent StateObserverAgent. The old ``check_macro_shift`` function (already
``@deprecated`` before T5.5) is gone. This file now asserts:

  1. ``check_macro_shift`` is NOT importable from ``argosy.services.plan_monitor``.
  2. The state-diff comparator map covers FX (the original motivation for the
     macro-shift detector was FX/macro signals) so the observer CAN detect
     these anomalies emergently.
"""
from __future__ import annotations


def test_check_macro_shift_removed_from_plan_monitor() -> None:
    """check_macro_shift must NOT exist in plan_monitor (T5.5)."""
    import argosy.services.plan_monitor as pm
    assert not hasattr(pm, "check_macro_shift"), (
        "check_macro_shift is a forbidden per-symptom detector (T5.5). "
        "It must be removed; macro anomaly detection flows through the emergent observer."
    )


def test_macro_shift_types_removed() -> None:
    """MacroShiftFlag / MacroShiftCheckResult must also be gone (T5.5)."""
    import argosy.services.plan_monitor as pm
    for forbidden in ("MacroShiftFlag", "MacroShiftCheckResult", "get_active_macro_shift_flags"):
        assert not hasattr(pm, forbidden), (
            f"{forbidden!r} must not exist in plan_monitor after T5.5 removal."
        )


def test_state_diff_comparator_map_covers_fx() -> None:
    """Emergent coverage contract: the state-diff comparator map must pair
    macro.fx_usd_nis_spot against plan_inputs.assumed_fx_usd_nis.

    This is the named FX-emergence gate in state_diff.py's CRITICAL comment.
    If this assertion fails, the observer CANNOT emergently detect the FX
    deviation the macro-shift detector was designed to catch.
    """
    from argosy.services.state_diff import PLAN_BASELINE_COMPARATOR_MAP

    assert "macro.fx_usd_nis_spot" in PLAN_BASELINE_COMPARATOR_MAP, (
        "macro.fx_usd_nis_spot missing from PLAN_BASELINE_COMPARATOR_MAP; "
        "FX coverage is NOT emergent — the macro-shift detector removal would "
        "silently drop FX anomaly coverage."
    )
    assert (
        PLAN_BASELINE_COMPARATOR_MAP["macro.fx_usd_nis_spot"]
        == "plan_inputs.assumed_fx_usd_nis"
    ), (
        "macro.fx_usd_nis_spot must be paired with plan_inputs.assumed_fx_usd_nis "
        "in the comparator map for emergent FX detection to work."
    )
