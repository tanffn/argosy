"""Tests for the RSU net-retention consistency gate (run-106 finding [3])."""
from __future__ import annotations

from argosy.quality.gate_types import GateCheck
from argosy.quality.rsu_retention_gate import check_rsu_retention_consistency


def test_planted_run106_defect_47_vs_65() -> None:
    """Ledger says RSU net retention 47%, equity-comp evidence says 65% → flag both."""
    text = (
        "The RSU ledger applies a net retention of 47% after tax to the vesting "
        "schedule. However, the equity-comp evidence cites an after-tax retention "
        "of 65% (net) for the same vest."
    )
    violations = check_rsu_retention_consistency(plan_text=text)
    assert len(violations) == 1
    v = violations[0]
    assert v.check is GateCheck.RSU_RETENTION_CONSISTENCY
    assert "47" in v.detail and "65" in v.detail


def test_clean_consistent_65_across_surfaces() -> None:
    """RSU net retention stated 65% everywhere → no violation."""
    text = (
        "The RSU ledger uses a net retention of 65% after tax. The equity-comp "
        "evidence confirms an after-tax retention of 65% (net) for the vest, and "
        "the prose RSU retention reads 65%."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []


def test_no_retention_mention() -> None:
    """Text with no RSU/equity-comp retention percentage → no violation."""
    text = (
        "The portfolio holds 62.5% NVDA. The medium-horizon sleeve targets sum to "
        "100%. USD/NIS is 3.6. No equity compensation discussion here."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []


def test_rounding_tolerance_not_flagged() -> None:
    """64% vs 65% (within 1pp) is rounding, not a contradiction."""
    text = (
        "RSU net retention 65% after tax in the ledger; the equity-comp evidence "
        "shows net-of-tax retention 64%."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []


def test_unrelated_percentages_not_picked_up() -> None:
    """A 47% NVDA weight near unrelated text must NOT count as a retention value."""
    text = (
        "RSU net retention is 65% after tax. Separately, NVDA is 47% of the book "
        "and the defensive sleeve is 21%."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []
