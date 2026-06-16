from datetime import date

from argosy.quality.freshness_gate import check_output_date_staleness
from argosy.quality.gate_types import GateCheck


_TODAY = date(2026, 6, 16)


def test_overdue_date_rendered_on_deck_is_flagged():
    """Run4: the 2026-06-10 retainer (past) shown "on-deck" as if not overdue."""
    text = "The retainer gate (2026-06-10) is on-deck."
    viol = check_output_date_staleness(today=_TODAY, text=text)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.OUTPUT_DATE_STALENESS
    assert viol[0].locator == "2026-06-10"


def test_overdue_date_labelled_overdue_passes():
    text = "The retainer gate (2026-06-10) is overdue 5 days."
    assert check_output_date_staleness(today=_TODAY, text=text) == []


def test_future_date_on_deck_passes():
    text = "The retainer gate (2026-06-30) is on-deck."
    assert check_output_date_staleness(today=_TODAY, text=text) == []


def test_due_in_n_days_with_past_date_is_flagged():
    text = "Action 2026-06-01 is due in 3 days."
    viol = check_output_date_staleness(today=_TODAY, text=text)
    assert len(viol) == 1
    assert viol[0].locator == "2026-06-01"


def test_unparseable_date_is_ignored():
    text = "Action 2026-13-99 is on-deck."
    assert check_output_date_staleness(today=_TODAY, text=text) == []


def test_one_violation_per_offending_clause():
    text = (
        "The retainer (2026-06-10) is on-deck. "
        "The review (2026-06-05) is upcoming."
    )
    viol = check_output_date_staleness(today=_TODAY, text=text)
    assert len(viol) == 2
    assert {v.locator for v in viol} == {"2026-06-10", "2026-06-05"}
