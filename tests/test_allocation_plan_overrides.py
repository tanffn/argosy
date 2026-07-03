"""Tests for authored_overrides support in build_target_allocation.

TDD: tests were written BEFORE the implementation. Each test was verified
to fail (RED) before the implementation was added (GREEN).

Semantics under test:
  - authored_overrides=None (or {}) → byte-identical output to the baseline.
  - Named sleeves are fixed at the override value; non-overridden sleeves
    renormalise to fill the remainder in proportion to their engine-derived weights.
  - Total always ≈ 100.
  - Validation: negative value → ValueError; sum > 100+eps → ValueError;
    unknown label → ValueError; all sleeves overridden but not summing to 100 → ValueError.
"""
from __future__ import annotations

import pytest

from argosy.services.allocation_plan import (
    NVDA_TARGET_PCT,
    build_target_allocation,
)
from argosy.services.retirement.scenario_mc import SIGMA_DIVERSIFIED


# ---------------------------------------------------------------------------
# Helper: known valid labels.  We derive them from the engine itself so the
# tests don't embed brittle string constants.
# ---------------------------------------------------------------------------

def _all_labels() -> list[str]:
    alloc = build_target_allocation()
    return [c.label for c in alloc.classes]


def _baseline() -> dict[str, float]:
    alloc = build_target_allocation()
    return {c.label: c.target_pct for c in alloc.classes}


# ---------------------------------------------------------------------------
# 1. None / empty overrides → identical output
# ---------------------------------------------------------------------------

class TestOverridesNoneIsIdentical:
    """authored_overrides=None or {} must produce byte-identical weights."""

    def test_none_override_matches_baseline_all_labels(self) -> None:
        base = build_target_allocation()
        with_none = build_target_allocation(authored_overrides=None)
        base_map = {c.label: c.target_pct for c in base.classes}
        none_map = {c.label: c.target_pct for c in with_none.classes}
        assert base_map == none_map

    def test_empty_dict_override_matches_baseline(self) -> None:
        base = build_target_allocation()
        with_empty = build_target_allocation(authored_overrides={})
        base_map = {c.label: c.target_pct for c in base.classes}
        empty_map = {c.label: c.target_pct for c in with_empty.classes}
        assert base_map == empty_map

    def test_none_override_fi_pct_identical(self) -> None:
        base = build_target_allocation()
        with_none = build_target_allocation(authored_overrides=None)
        assert base.fi_pct == with_none.fi_pct
        assert base.nvda_pct == with_none.nvda_pct
        assert base.cash_pct == with_none.cash_pct
        assert base.bonds_pct == with_none.bonds_pct

    def test_none_override_blended_sigma_identical(self) -> None:
        base = build_target_allocation()
        with_none = build_target_allocation(authored_overrides=None)
        assert base.blended_sigma == with_none.blended_sigma

    def test_snapshot_us_growth_sleeve_nonzero_in_baseline(self) -> None:
        # Sanity: the US growth sleeve has a derived non-zero target in baseline.
        base = _baseline()
        assert base["US growth tilt (ex-NVDA)"] > 0.0


# ---------------------------------------------------------------------------
# 2. Single sleeve override
# ---------------------------------------------------------------------------

class TestSingleSleeveOverride:
    """Override one sleeve → exact authored value; others renormalise; total ≈ 100."""

    def test_overridden_sleeve_is_exact(self) -> None:
        # Override US growth from its derived ~13 to 8.0.
        override_label = "US growth tilt (ex-NVDA)"
        authored_pct = 8.0
        alloc = build_target_allocation(authored_overrides={override_label: authored_pct})
        result = {c.label: c.target_pct for c in alloc.classes}
        assert result[override_label] == pytest.approx(authored_pct, abs=1e-9)

    def test_overridden_sleeve_differs_from_baseline(self) -> None:
        # The baseline has a different (higher) value so the override actually changed it.
        override_label = "US growth tilt (ex-NVDA)"
        authored_pct = 8.0
        base = _baseline()
        alloc = build_target_allocation(authored_overrides={override_label: authored_pct})
        result = {c.label: c.target_pct for c in alloc.classes}
        # Engine would have derived something different from 8.0.
        assert abs(base[override_label] - authored_pct) > 0.5  # meaningful change
        assert result[override_label] == pytest.approx(authored_pct, abs=1e-9)

    def test_total_sums_to_100_with_single_override(self) -> None:
        alloc = build_target_allocation(authored_overrides={"US growth tilt (ex-NVDA)": 8.0})
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)

    def test_non_overridden_sleeves_fill_remainder_proportionally(self) -> None:
        # The non-overridden sleeves together must sum to (100 - override).
        override_label = "US growth tilt (ex-NVDA)"
        authored_pct = 8.0
        alloc = build_target_allocation(authored_overrides={override_label: authored_pct})
        non_overridden_total = sum(
            c.target_pct for c in alloc.classes if c.label != override_label
        )
        assert non_overridden_total == pytest.approx(100.0 - authored_pct, abs=0.05)

    def test_non_overridden_sleeves_maintain_relative_proportions(self) -> None:
        # Among non-overridden sleeves, their relative weights should be the same
        # as in the baseline (renormalised proportionally).
        override_label = "US growth tilt (ex-NVDA)"
        base = _baseline()
        alloc = build_target_allocation(authored_overrides={override_label: 8.0})
        result = {c.label: c.target_pct for c in alloc.classes}

        labels_no_override = [lbl for lbl in base if lbl != override_label]
        base_sum = sum(base[lbl] for lbl in labels_no_override)
        result_sum = sum(result[lbl] for lbl in labels_no_override)

        for lbl in labels_no_override:
            expected_ratio = base[lbl] / base_sum
            actual_ratio = result[lbl] / result_sum
            assert actual_ratio == pytest.approx(expected_ratio, rel=0.01), lbl

    def test_override_nvda_respects_authored_value(self) -> None:
        # NVDA can be overridden like any other sleeve.
        nvda_label = "Strategic single-stock (NVDA)"
        alloc = build_target_allocation(authored_overrides={nvda_label: 10.0})
        result = {c.label: c.target_pct for c in alloc.classes}
        assert result[nvda_label] == pytest.approx(10.0, abs=1e-9)
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)

    def test_override_fi_cash_sleeve(self) -> None:
        # The cash FI sleeve can be overridden; total still ≈ 100.
        cash_label = "Cash & T-bills (incl. ILS tranche)"
        alloc = build_target_allocation(authored_overrides={cash_label: 10.0})
        result = {c.label: c.target_pct for c in alloc.classes}
        assert result[cash_label] == pytest.approx(10.0, abs=1e-9)
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)


# ---------------------------------------------------------------------------
# 3. Two-sleeve override
# ---------------------------------------------------------------------------

class TestTwoSleeveOverride:
    """Override two sleeves → both exact; rest renormalise; total ≈ 100."""

    def test_both_overrides_exact(self) -> None:
        overrides = {
            "US growth tilt (ex-NVDA)": 8.0,
            "Dividend-quality income": 5.0,
        }
        alloc = build_target_allocation(authored_overrides=overrides)
        result = {c.label: c.target_pct for c in alloc.classes}
        for label, pct in overrides.items():
            assert result[label] == pytest.approx(pct, abs=1e-9), label

    def test_total_sums_to_100_with_two_overrides(self) -> None:
        overrides = {
            "US growth tilt (ex-NVDA)": 8.0,
            "Dividend-quality income": 5.0,
        }
        alloc = build_target_allocation(authored_overrides=overrides)
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)

    def test_non_overridden_sleeves_proportional_with_two_overrides(self) -> None:
        overrides = {
            "US growth tilt (ex-NVDA)": 8.0,
            "Dividend-quality income": 5.0,
        }
        base = _baseline()
        alloc = build_target_allocation(authored_overrides=overrides)
        result = {c.label: c.target_pct for c in alloc.classes}

        others = [lbl for lbl in base if lbl not in overrides]
        base_sum = sum(base[lbl] for lbl in others)
        result_sum = sum(result[lbl] for lbl in others)

        for lbl in others:
            exp_ratio = base[lbl] / base_sum
            act_ratio = result[lbl] / result_sum
            assert act_ratio == pytest.approx(exp_ratio, rel=0.01), lbl


# ---------------------------------------------------------------------------
# 4. Override all sleeves summing to 100 exactly
# ---------------------------------------------------------------------------

class TestAllSleevesOverride:
    """Overriding ALL sleeves that sum to 100 → accepted; used as-is."""

    def test_all_sleeves_summing_to_100_accepted(self) -> None:
        base = _baseline()
        # Tweak all values by a small constant shift, keeping the sum at 100.
        labels = list(base.keys())
        n = len(labels)
        shift = 1.0 / n  # each gets a tiny bump, normalised to keep sum=100
        raw = {lbl: base[lbl] + shift for lbl in labels}
        total = sum(raw.values())
        normalised = {lbl: v / total * 100.0 for lbl, v in raw.items()}
        assert sum(normalised.values()) == pytest.approx(100.0, abs=1e-6)

        alloc = build_target_allocation(authored_overrides=normalised)
        for lbl, pct in normalised.items():
            result_pct = next(c.target_pct for c in alloc.classes if c.label == lbl)
            assert result_pct == pytest.approx(pct, abs=0.01), lbl


# ---------------------------------------------------------------------------
# 5. Validation errors
# ---------------------------------------------------------------------------

class TestOverrideValidationErrors:
    """Fail loud on bad override inputs."""

    def test_negative_override_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            build_target_allocation(authored_overrides={"US growth tilt (ex-NVDA)": -1.0})

    def test_zero_override_is_allowed(self) -> None:
        # Zero is a valid authored override (remove the sleeve).
        alloc = build_target_allocation(authored_overrides={"US growth tilt (ex-NVDA)": 0.0})
        result = {c.label: c.target_pct for c in alloc.classes}
        assert result["US growth tilt (ex-NVDA)"] == pytest.approx(0.0, abs=1e-9)
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)

    def test_sum_over_100_raises(self) -> None:
        overrides = {
            "US growth tilt (ex-NVDA)": 60.0,
            "Dividend-quality income": 50.0,  # 60+50 > 100
        }
        with pytest.raises(ValueError, match="sum"):
            build_target_allocation(authored_overrides=overrides)

    def test_unknown_label_raises(self) -> None:
        with pytest.raises(ValueError, match="[Uu]nknown"):
            build_target_allocation(authored_overrides={"Nonexistent sleeve XYZ": 5.0})

    def test_unknown_label_is_not_silently_ignored(self) -> None:
        # Confirm the unknown label causes an error and does NOT fall through
        # to a successful allocation.
        try:
            build_target_allocation(authored_overrides={"Magic unicorn sleeve": 5.0})
            pytest.fail("Expected ValueError for unknown label but got none")
        except ValueError:
            pass  # correct

    def test_all_sleeves_not_summing_to_100_raises(self) -> None:
        # All known labels overridden but summing to 90 (not ~100) → error.
        base = _baseline()
        labels = list(base.keys())
        # Scale them all to sum to 90.
        overrides = {lbl: base[lbl] * 0.9 for lbl in labels}
        assert sum(overrides.values()) == pytest.approx(90.0, abs=0.1)
        with pytest.raises(ValueError):
            build_target_allocation(authored_overrides=overrides)

    def test_partial_override_large_value_but_under_100_is_ok(self) -> None:
        # A single override of 80% is valid (leaves 20% for the rest).
        alloc = build_target_allocation(authored_overrides={"US broad-market core": 80.0})
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)
        result = {c.label: c.target_pct for c in alloc.classes}
        assert result["US broad-market core"] == pytest.approx(80.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. Rationale consistency — overall_rationale must use POST-override values
# ---------------------------------------------------------------------------

class TestRationaleConsistency:
    """overall_rationale must quote the same fi_pct / nvda_pct as the
    TargetAllocation struct fields (post-override), not the pre-override derived
    values. The DeploymentAuthorAgent reads overall_rationale for plan-fit, so
    stale numbers would mislead it."""

    def test_fi_pct_quoted_in_rationale_matches_struct_fi_pct(self) -> None:
        # Override the cash FI sleeve to a value that differs from the derived one.
        # The reported fi_pct on the struct = cash_override + bonds_renorm.
        cash_label = "Cash & T-bills (incl. ILS tranche)"
        alloc = build_target_allocation(authored_overrides={cash_label: 12.0})
        # The struct's fi_pct is the post-override sum of both FI sub-sleeves.
        struct_fi = alloc.fi_pct
        # The overall_rationale must contain the post-override fi_pct value.
        rationale = alloc.overall_rationale
        fi_str = f"{struct_fi:.1f}"
        assert fi_str in rationale, (
            f"overall_rationale quotes a stale fi_pct; expected '{fi_str}' "
            f"(post-override struct fi_pct) to appear in:\n{rationale}"
        )

    def test_nvda_pct_quoted_in_rationale_matches_struct_nvda_pct(self) -> None:
        # Override NVDA to a value that differs from the 12% default.
        nvda_label = "Strategic single-stock (NVDA)"
        alloc = build_target_allocation(authored_overrides={nvda_label: 9.0})
        struct_nvda = alloc.nvda_pct
        rationale = alloc.overall_rationale
        nvda_str = f"{struct_nvda:.0f}"
        assert nvda_str in rationale, (
            f"overall_rationale quotes a stale nvda_pct; expected '{nvda_str}' "
            f"(post-override struct nvda_pct={struct_nvda}) to appear in:\n{rationale}"
        )

    def test_no_override_rationale_fi_consistent(self) -> None:
        # Sanity: without overrides the rationale and struct must also agree.
        alloc = build_target_allocation()
        fi_str = f"{alloc.fi_pct:.1f}"
        assert fi_str in alloc.overall_rationale


# ---------------------------------------------------------------------------
# 7. Sum guard — hard ValueError on sum breach
# ---------------------------------------------------------------------------

class TestSumGuard:
    """The internal sum guard must raise ValueError (not assert, which is
    suppressible under python -O). Valid allocations must pass the tight 1e-6
    tolerance without raising."""

    def test_valid_allocation_does_not_raise(self) -> None:
        # Normal path: engine-derived allocation is always balanced.
        alloc = build_target_allocation()
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)

    def test_valid_override_does_not_raise(self) -> None:
        # An override that keeps the sum at 100 must not raise.
        alloc = build_target_allocation(authored_overrides={"US growth tilt (ex-NVDA)": 8.0})
        total = sum(c.target_pct for c in alloc.classes)
        assert total == pytest.approx(100.0, abs=0.05)

    def test_sum_guard_raises_value_error_not_assertion_error(self) -> None:
        # Verify the sum guard is a hard ValueError (not a suppressible assert).
        # Strategy: inspect the function source to confirm no bare `assert` guards
        # the sum, AND verify the guard fires as ValueError by forcing the bad-sum
        # branch via monkeypatching builtins.sum inside the module.
        import builtins
        import inspect
        from unittest.mock import patch
        from argosy.services import allocation_plan

        src = inspect.getsource(allocation_plan._apply_authored_overrides)
        # The old guard was: assert abs(sum(result.values()) - 100.0) < 0.1
        # It must have been replaced by a ValueError raise.
        assert "assert abs(sum(result" not in src, (
            "Sum guard must not use a bare assert (suppressible under python -O)"
        )
        assert "ValueError" in src, "Sum guard must raise ValueError"

        # Also confirm a valid call through the override path does not raise.
        weights = {"A": 50.0, "B": 30.0, "C": 20.0}
        result = allocation_plan._apply_authored_overrides(weights, {"A": 45.0})
        assert abs(sum(result.values()) - 100.0) < 1e-6

        # Force the guard to fire: patch builtins.sum so the guard's _total
        # computation returns 95.0 for the result dict produced inside the function.
        real_sum = builtins.sum
        call_count = {"n": 0}

        def patched_sum(iterable, *args, **kwargs):
            # Return a bad total only for the specific result-dict values() call
            # inside _apply_authored_overrides (heuristic: the 3rd dict-values sum
            # in a call that has overrides). For safety, return 95.0 only once.
            if call_count["n"] == 0:
                call_count["n"] += 1
                return 95.0
            return real_sum(iterable, *args, **kwargs)

        with patch.object(builtins, "sum", patched_sum):
            with pytest.raises(ValueError, match="allocation weights sum to"):
                allocation_plan._apply_authored_overrides(
                    {"A": 50.0, "B": 30.0, "C": 20.0}, {"A": 45.0}
                )
