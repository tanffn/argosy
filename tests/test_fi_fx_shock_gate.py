"""Tests for the FX-shock sufficiency gate (run-106 finding [0]).

Twin of the NVDA-shock gate (`check_fi_sufficiency_under_shock`): an
unqualified "capital sufficiency reached" claim must be robust to a −10%
USD/NIS move, not just the NVDA tail. Sentence-scoped, biased to
false-positive (fail-loud): a caveat in a different sentence does not save it.
A negated clause ("FI is not yet reached") is a denial, not an assertion.
"""
from __future__ import annotations

from argosy.quality.fi_fx_shock_gate import check_fi_sufficiency_under_fx_shock
from argosy.quality.gate_types import GateCheck


def test_planted_defect_unqualified_claim_breaks_under_fx_shock() -> None:
    """An unqualified 'capital sufficiency reached' claim, with the −10% FX
    row breaking the perpetuity base and no FX caveat in the sentence → flag."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": False, "perpetuity_reached": False, "net_worth_nis": 9_900_000}
    }
    plan_text = "Capital sufficiency reached. You can retire today on the perpetuity base."
    violations = check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    )
    assert len(violations) == 1
    assert violations[0].check is GateCheck.FI_FX_SHOCK_SUFFICIENCY


def test_cannot_be_claimed_reached_is_a_denial_not_an_assertion() -> None:
    """'FI cannot be claimed reached' DENIES sufficiency — must not fire. Same
    pv53 regression as the NVDA gate: 'cannot'/'can't' were absent from the
    negation set, flagging an honest not-reached statement."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": False, "perpetuity_reached": False, "net_worth_nis": 10_713_284}
    }
    plan_text = "Accumulation must continue and FI cannot be claimed reached."
    assert check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    ) == []


def test_qualified_claim_with_fx_caveat_in_same_sentence_passes() -> None:
    """Same breaking shock, but the sentence carries the FX caveat → no flag."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": False, "perpetuity_reached": False, "net_worth_nis": 9_900_000}
    }
    plan_text = (
        "Capital sufficiency reached, though a −10% USD/NIS move would erase the surplus."
    )
    violations = check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    )
    assert violations == []


def test_shock_does_not_break_perpetuity_passes() -> None:
    """If the −10% FX row still clears the perpetuity base → nothing to flag."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": True, "perpetuity_reached": True, "net_worth_nis": 12_000_000}
    }
    plan_text = "Capital sufficiency reached. You can retire today."
    violations = check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    )
    assert violations == []


def test_negated_denial_is_not_flagged() -> None:
    """A denial of sufficiency ('FI is not yet reached') is not an assertion."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": False, "perpetuity_reached": False, "net_worth_nis": 9_900_000}
    }
    plan_text = "FI is not yet reached; the surplus is still below the perpetuity base."
    violations = check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    )
    assert violations == []


def test_caveat_in_different_sentence_still_flags() -> None:
    """Fail-loud: an FX caveat in a SEPARATE sentence does not save a bare claim."""
    fx_shock_result = {
        "fx_shock_-0.10": {"total_reached": False, "perpetuity_reached": False, "net_worth_nis": 9_900_000}
    }
    plan_text = (
        "Capital sufficiency reached. Separately, a −10% USD/NIS move could erase the surplus."
    )
    violations = check_fi_sufficiency_under_fx_shock(
        fx_shock_result=fx_shock_result, plan_text=plan_text
    )
    assert len(violations) == 1
    assert violations[0].check is GateCheck.FI_FX_SHOCK_SUFFICIENCY
