"""Tests for the stale-reviewer-text gate (run-106 finding [6])."""
from __future__ import annotations

from argosy.quality.gate_types import GateCheck
from argosy.quality.stale_reviewer_text_gate import check_stale_reviewer_text


def test_run106_stale_objection_flagged() -> None:
    """Planted run-106 defect: the pending FM objection cites a medium target of
    3,000 sh/yr while the current draft's medium target now reads 5,600 sh/yr.
    The objection is stale → STALE_REVIEWER_TEXT violation."""
    objection = (
        "FUND MANAGER OBJECTION (pending): the medium target is still 3,000 "
        "sh/yr, which under-deploys the NVDA-sale cash. Reject until raised."
    )
    plan = (
        "Medium horizon. The medium target: 5,600 sh/yr of accumulation against "
        "the reinvestment glide."
    )
    violations = check_stale_reviewer_text(plan_text=plan, objection_text=objection)
    assert len(violations) == 1
    assert violations[0].check == GateCheck.STALE_REVIEWER_TEXT


def test_objection_agrees_with_draft_clean() -> None:
    """Objection and draft both cite 5,600 sh/yr for the medium target → []."""
    objection = (
        "FUND MANAGER NOTE: the medium target of 5,600 sh/yr is acceptable."
    )
    plan = "Medium horizon. The medium target: 5,600 sh/yr."
    assert check_stale_reviewer_text(plan_text=plan, objection_text=objection) == []


def test_no_objection_text_returns_empty() -> None:
    """No pending objection → nothing to reconcile → []."""
    plan = "Medium horizon. The medium target: 5,600 sh/yr."
    assert check_stale_reviewer_text(plan_text=plan, objection_text=None) == []
    assert check_stale_reviewer_text(plan_text=plan, objection_text="") == []


def test_rounding_difference_not_flagged() -> None:
    """A sub-rounding difference (5,600 vs 5,601 sh/yr) is not a stale defect."""
    objection = "Note: the medium target of 5,601 sh/yr is fine."
    plan = "The medium target: 5,600 sh/yr."
    assert check_stale_reviewer_text(plan_text=plan, objection_text=objection) == []
