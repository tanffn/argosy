"""Tests for the EVENT_CURRENCY_CONSISTENCY gate (run-106 finding [4]).

A named/dated money event (e.g. the June-17 RSU tax) must not flip currency
between NIS and USD across surfaces — the magnitude changes by ~the FX rate, so
it is not a harmless typo.
"""
from __future__ import annotations

from argosy.quality.event_currency_gate import check_event_currency_consistency
from argosy.quality.gate_types import GateCheck


def test_run106_defect_same_event_flips_nis_to_usd() -> None:
    """The planted run-106 defect: the June-17 RSU tax is ₪180,000 in one clause
    and $52,000 in another → a EVENT_CURRENCY_CONSISTENCY violation."""
    text = (
        "Heads up: June 17 RSU tax estimated at ₪180,000 — set this aside.\n"
        "Elsewhere the appendix lists the June 17 RSU tax of $52,000 due at vest."
    )
    violations = check_event_currency_consistency(plan_text=text)
    assert violations, "expected a currency-flip violation for the June-17 RSU tax"
    assert all(v.check is GateCheck.EVENT_CURRENCY_CONSISTENCY for v in violations)


def test_clean_event_consistent_nis_across_surfaces() -> None:
    """Same event in NIS everywhere → no violation."""
    text = (
        "June 17 RSU tax estimated at ₪180,000 — set this aside.\n"
        "The appendix again lists the June 17 RSU tax of ₪180,000 due at vest."
    )
    assert check_event_currency_consistency(plan_text=text) == []


def test_two_different_events_each_own_currency_is_clean() -> None:
    """Two DIFFERENT events, each consistently in its own currency (no flip of
    the SAME event) → no violation."""
    text = (
        "The June 17 RSU tax is estimated at ₪180,000, payable in Israel.\n"
        "Separately, the US estate filing fee is $4,500 due in September."
    )
    assert check_event_currency_consistency(plan_text=text) == []


def test_two_different_taxes_same_date_each_own_currency_is_clean() -> None:
    """Two genuinely DIFFERENT taxes near the same date — an Israeli RSU tax in
    NIS and the US federal tax on the same vest in USD — must NOT collapse to one
    anchor key and spuriously flag. Two distinct taxes in two currencies is
    normal."""
    text = (
        "The June 17 RSU tax estimate is ₪180,000.\n"
        "The US federal tax estimate on the same vest is $52,000."
    )
    assert check_event_currency_consistency(plan_text=text) == []


def test_nis_with_usd_equivalence_cue_is_clean() -> None:
    """A NIS amount shown with its USD equivalent ('≈ $52,000 at the current
    rate') is an explicit FX equivalence, not a currency flip → no violation."""
    text = "June 17 RSU tax: ₪180,000 (≈ $52,000 at the current rate)."
    assert check_event_currency_consistency(plan_text=text) == []


def test_dollar_cost_average_is_not_a_usd_amount() -> None:
    """The word 'dollar' in 'dollar-cost average' is not a USD-denominated amount;
    only ₪180,000 (NIS) is present → no flip, no violation."""
    text = "On June 17 we will dollar-cost average ₪180,000 into the index."
    assert check_event_currency_consistency(plan_text=text) == []
