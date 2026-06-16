"""Tests for the retirement-age-label gate (run-106 finding [2]).

The headline retirement age (`earliest_safe_age`) and the FIRE-bridge sizing
age (`fi_age`) are DELIBERATELY distinct. The invariant is NOT equality. It is:
  (a) each age is labeled by its definition wherever it appears, AND
  (b) the bridge sleeve is sized from the resolver's CHOSEN sizing age
      (today `fi_age`): `bridge_start_age == fi_age`.
"""
from __future__ import annotations

from argosy.quality.gate_types import GateCheck
from argosy.quality.retirement_age_label_gate import check_retirement_age_labels


def test_run106_defect_bridge_sized_from_wrong_age() -> None:
    # The planted run-106 regression: headline 46 / fi_age 46 but the bridge is
    # now sized from age 47 (one year of bridge funding silently dropped).
    violations = check_retirement_age_labels(
        plan_text=(
            "Earliest-safe retirement age is 46. The FI-bridge sizing age is 46. "
            "The FIRE bridge sleeve funds the gap from age 47 to 60."
        ),
        earliest_safe_age=46,
        fi_age=46,
        bridge_start_age=47,
    )
    assert violations, "bridge_start_age 47 != fi_age 46 must be flagged"
    assert all(v.check is GateCheck.RETIREMENT_AGE_LABEL for v in violations)
    assert any("bridge" in v.detail.lower() for v in violations)


def test_clean_aligned_and_labeled() -> None:
    # Bridge == fi_age, and the two ages (both 46) carry distinct defining labels.
    violations = check_retirement_age_labels(
        plan_text=(
            "Your earliest-safe retirement age is 46. "
            "The FI-bridge sizing age is also 46, "
            "so the bridge sleeve is sized from age 46 to 60."
        ),
        earliest_safe_age=46,
        fi_age=46,
        bridge_start_age=46,
    )
    assert violations == []


def test_deliberately_distinct_but_labeled() -> None:
    # The resolver deliberately distinguishes the two ages: headline 46,
    # sizing age 47, bridge sized from 47 (== fi_age). Each labeled by its
    # definition. This must NOT be forced equal — it is the intended design.
    violations = check_retirement_age_labels(
        plan_text=(
            "Your earliest-safe retirement age is 46. "
            "The FI-bridge sizing age is 47, "
            "so the FIRE bridge sleeve is sized from age 47 to 60."
        ),
        earliest_safe_age=46,
        fi_age=47,
        bridge_start_age=47,
    )
    assert violations == []


def test_unlabeled_two_ages_reads_as_contradiction() -> None:
    # Two different ages stated in prose with NO distinguishing labels:
    # "retirement age 46" alongside "bridge from age 47" reads as a
    # contradiction. Bias to false-positive per fail-loud doctrine.
    violations = check_retirement_age_labels(
        plan_text=(
            "Your retirement age is 46. The bridge is funded from age 47 to 60."
        ),
        earliest_safe_age=None,
        fi_age=None,
        bridge_start_age=None,
    )
    assert violations, "unlabeled distinct ages must be flagged as a contradiction"
    assert all(v.check is GateCheck.RETIREMENT_AGE_LABEL for v in violations)


def test_no_inputs_no_prose_is_clean() -> None:
    assert check_retirement_age_labels(plan_text="") == []


def test_rounding_tolerance_on_bridge_vs_fi_age() -> None:
    # 47.0 vs 47 is the same age within rounding — no violation from (1).
    violations = check_retirement_age_labels(
        plan_text=(
            "Earliest-safe retirement age is 46. The FI-bridge sizing age is 47. "
            "The FIRE bridge sleeve is sized from age 47 to 60."
        ),
        earliest_safe_age=46,
        fi_age=47.0,
        bridge_start_age=47,
    )
    assert violations == []
