"""Tests for the FI-timeline coherence gate (run-106 finding [1]).

The defect class: the SAME FI-crossing concept reported three incompatible
ways at once — "already crossed today" AND a future "FI age 47" / "FI age 45
with 2.0 years remaining". Distinct FI ages are allowed ONLY when each carries
its defining label; an UNLABELED "crossed today" alongside a future FI age /
remaining-years claim is the contradiction.
"""
from __future__ import annotations

from argosy.quality.fi_timeline_gate import check_fi_timeline_coherence
from argosy.quality.gate_types import GateCheck


def test_run106_crossed_today_plus_future_fi_age_is_flagged():
    """Planted run-106 defect: 'crossed today' co-exists with a future FI age
    and remaining-years — a FI_TIMELINE_COHERENCE contradiction."""
    plan_text = (
        "Capital sufficiency: FI has already been crossed today. "
        "The deterministic FI age is 47. "
        "Under the Typical scenario, FI age is 45 with 2.0 years remaining."
    )
    violations = check_fi_timeline_coherence(plan_text=plan_text)
    assert violations, "expected a FI_TIMELINE_COHERENCE violation"
    assert all(v.check is GateCheck.FI_TIMELINE_COHERENCE for v in violations)


def test_consistent_not_yet_reached_with_fi_age_and_remaining_is_clean():
    """FI consistently 'not yet reached', FI age 47, 1 year remaining — no
    'crossed today' claim, so no contradiction."""
    plan_text = (
        "FI is not yet reached. The FI age is 47, with 1 year remaining "
        "until you cross the FI line."
    )
    assert check_fi_timeline_coherence(plan_text=plan_text) == []


def test_distinct_labeled_ages_without_crossed_today_is_clean():
    """Distinct FI ages each carry their defining label and there is NO
    'crossed today' claim — the S22/S23 distinct-FI-age-label rule allows this."""
    plan_text = (
        "The deterministic FI age is 47. "
        "The Typical-scenario FI age is 45, with 2.0 years remaining. "
        "Both ages are reported under their own definitions; FI is not yet reached."
    )
    assert check_fi_timeline_coherence(plan_text=plan_text) == []
