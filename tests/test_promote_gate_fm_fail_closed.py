"""The fund-manager promotion authority must FAIL CLOSED on absence.

Regression pin for a live fail-open found 2026-08-23. ``/accept`` computed
``_fm_clear = run.fund_manager_decision != "rejected"``, so a run whose FM agent
never executed carried ``None``, ``None != "rejected"`` was True, and the
promote gate recorded the authority as ``"approved"``.

Plan 119 (amendment run 440) is the concrete case: four authorities correctly
blocked on missing verdicts while the fund-manager one returned approval off a
NULL column — a plan no fund manager had ever seen, clearing the FM gate inside
a barrier whose contract is "a missing verdict fails closed".
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from argosy.api.routes.plan import fund_manager_cleared
from argosy.quality.promote_gate import evaluate_promotion


@dataclass
class _Run:
    fund_manager_decision: str | None


def test_never_ran_does_not_clear():
    """The exact plan-119 shape: the FM agent never executed."""
    assert fund_manager_cleared(_Run(None)) is False
    assert fund_manager_cleared(_Run("")) is False
    assert fund_manager_cleared(_Run("   ")) is False


def test_approved_clears():
    # The literal the synthesis flow writes.
    assert fund_manager_cleared(_Run("approved")) is True
    assert fund_manager_cleared(_Run("APPROVED")) is True
    assert fund_manager_cleared(_Run(" approved ")) is True


def test_rejected_does_not_clear():
    assert fund_manager_cleared(_Run("rejected")) is False


@pytest.mark.parametrize("decision", ["hold", "green_light", "block", "insufficient_data"])
def test_trade_flow_verdicts_do_not_clear_a_plan_promotion(decision):
    """These belong to the per-TRADE decision flow. If one ever reaches a plan
    draft it must NOT be read as plan-level FM approval."""
    assert fund_manager_cleared(_Run(decision)) is False


def test_no_decision_run_still_clears():
    """A draft with no decision_run is not a synthesis product — the promote
    barrier documents that it does not apply to that class."""
    assert fund_manager_cleared(None) is True


def test_explicit_override_clears_a_rejection():
    assert fund_manager_cleared(_Run("rejected"), override_fm_rejection=True) is True


def test_gate_blocks_when_fm_never_ran():
    """End-to-end through the gate: absence must not promote."""
    authorities = {
        "codex": "APPROVE",
        "deterministic_gate": True,
        "fund_manager": (
            "approved" if fund_manager_cleared(_Run(None)) else "rejected"
        ),
        "whole_artifact_reader": "APPROVE",
        "rederivation": "APPROVE",
    }
    decision = evaluate_promotion(authorities)
    assert decision.can_promote is False, (
        "a plan no fund manager reviewed must not promote"
    )
    assert "fund_manager" in decision.blocking_authorities
