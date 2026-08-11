"""HOLE #32 — plan-attributed concentration-number grounding in
``flow._coerce_verdict_falsifiers``.

The NVDA SELL verdict armed a falsifier "NVDA concentration below ~40% of book →
stop trimming". The plan's REAL NVDA concentration figures are 8% steering / 13%
hard cap; 40% is the ESTATE-TAX rate, conflated. A CONTEXT-AWARE check
neutralizes (drops from the armed set) + surfaces (warns) only the mis-grounded
concentration tripwire, leaving estate / fundamental / correctly-grounded items
intact. When no plan targets are supplied the behavior is unchanged (fail-safe).
"""
from __future__ import annotations

from argosy.decisions.flow import _coerce_verdict_falsifiers

# (target_pct, cap_pct) as PERCENTAGES — the authoritative plan figures.
_TARGETS = (8.0, 13.0)


def test_misgrounded_concentration_40pct_neutralized_others_kept():
    class TP:
        falsifiers = [
            "NVDA concentration below ~40% of book -> stop trimming",  # mis-grounded
            "China export ban reinstated on advanced GPUs",  # fundamental — keep
        ]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["China export ban reinstated on advanced GPUs"]


def test_grounded_concentration_13pct_kept():
    class TP:
        falsifiers = ["Trim NVDA whenever concentration falls under 13% of book"]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["Trim NVDA whenever concentration falls under 13% of book"]


def test_estate_tax_40pct_falsifier_not_flagged():
    class TP:
        falsifiers = [
            "US estate-tax exposure on non-exempt situs assets exceeds 40%",
        ]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["US estate-tax exposure on non-exempt situs assets exceeds 40%"]


def test_non_concentration_and_unparseable_kept():
    class TP:
        falsifiers = [
            "gross margin compresses below 55% for two quarters running",  # not concentration
            "NVDA concentration keeps climbing with no numeric threshold",  # no %
        ]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == [
        "gross margin compresses below 55% for two quarters running",
        "NVDA concentration keeps climbing with no numeric threshold",
    ]


def test_misgrounded_concentration_metric_trigger_neutralized():
    class TP:
        falsifiers: list = []
        revisit_triggers = [
            {"kind": "metric_condition", "metric": "nvda_concentration_pct",
             "op": "<", "value": 40},  # mis-grounded → drop
            {"kind": "metric_condition", "metric": "nvda_concentration_pct",
             "op": "<", "value": 13},  # grounded to the cap → keep
        ]

    _, trigs = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert len(trigs) == 1
    assert trigs[0]["value"] == 13.0


def test_cross_ticker_concentration_falsifier_kept():
    """DEFECT 2: a concentration statement about ANOTHER ticker (MSFT) inside an
    NVDA verdict must NOT be grounded against NVDA's 8/13 → KEPT."""
    class TP:
        falsifiers = [
            "MSFT concentration over 40% of book",  # other ticker → keep
            "NVDA concentration below 40%",  # subject's own → drop
        ]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["MSFT concentration over 40% of book"]


def test_fundamental_margin_percent_not_dropped():
    """DEFECT 3: a % attached to a fundamental metric (margin) is not a
    concentration threshold → KEPT even with 'Trim' in the sentence."""
    class TP:
        falsifiers = ["Trim NVDA if gross margin falls below 55% for two quarters"]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["Trim NVDA if gross margin falls below 55% for two quarters"]


def test_revenue_growth_percent_not_dropped():
    """DEFECT 3: revenue-growth % is fundamental, not concentration → KEPT."""
    class TP:
        falsifiers = ["NVDA revenue growth decelerates below 40% year over year"]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == ["NVDA revenue growth decelerates below 40% year over year"]


def test_concentration_of_book_and_weight_phrasings_dropped():
    """DEFECT 3: genuine concentration phrasings ('% of book', whole-word
    'weight') with a mis-grounded 40% → DROPPED."""
    class TP:
        falsifiers = [
            "NVDA above 40% of book",
            "NVDA weight over 40%",
        ]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP(), plan_targets=_TARGETS, subject="NVDA")
    assert fals == []


def test_no_plan_targets_is_backward_compatible():
    """Without plan targets, nothing is neutralized (existing behavior)."""
    class TP:
        falsifiers = ["NVDA concentration below ~40% of book -> stop trimming"]
        revisit_triggers: list = []

    fals, _ = _coerce_verdict_falsifiers(TP())
    assert fals == ["NVDA concentration below ~40% of book -> stop trimming"]
