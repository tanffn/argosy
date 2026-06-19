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


def test_two_tax_treatment_rates_are_not_a_contradiction() -> None:
    """Live pv56: ~47% net retention is the AT-VEST ordinary-income rate; ~72% net
    retention is the CAPITAL-TRACK (Section-102 long-term) rate. Two different tax
    treatments yield two legitimate rates — NOT a contradiction. Only same-bucket
    divergence should flag."""
    text = (
        "At-vest sales bear the ordinary marginal rate, leaving ~47% net retention. "
        "The capital-track deconcentration program runs at ~72% net retention on "
        "Section-102 long-term lots."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []


def test_surtax_percent_is_not_bound_as_the_retention_rate() -> None:
    """Live pv56 mis-extraction: '~50% marginal plus 3% surtax; ~47% net retention'
    must bind the NEAREST retention number (47%), not the 3% surtax across it. With
    only one retention value, there is no contradiction → no violation."""
    text = "RSU at vest: ~50% marginal plus 3% surtax; ~47% net retention on the vest."
    assert check_rsu_retention_consistency(plan_text=text) == []


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


def test_bare_retain_verb_for_positions_not_flagged() -> None:
    """"retain the position/sleeve at X%" is allocation prose, not net retention.

    The only true retention figure here is the 65% net retention; the 13% NVDA
    cap and 8% gold sleeve are ordinary "retain ... at X%" allocation prose and
    must NOT be collected as retention values.
    """
    text = (
        "RSU net retention is 65% after tax. We retain the NVDA position at a 13% "
        "cap. Retain the gold sleeve at 8%."
    )
    assert check_rsu_retention_consistency(plan_text=text) == []


def test_retained_pct_of_gains_not_flagged() -> None:
    """"retained Y% of gains" is fund-performance prose, not equity-comp retention.

    Only the 65% after-tax retention is an equity-comp figure; "the fund retained
    40% of gains" must NOT be collected as a retention value.
    """
    text = "After-tax retention is 65%. The fund retained 40% of gains."
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
