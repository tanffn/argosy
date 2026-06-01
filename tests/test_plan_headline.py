"""Unit tests for plan_headline service (Wave 8 Piece G).

Covers the pure-function math layer (headline lines + accepted-deltas
summary + insurance-gaps summary + ISO-date parsing). The DB
orchestrator ``compute_recap_summary`` is exercised via its inputs
here; the route's integration coverage rounds out the contract.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from argosy.agents.plan_synthesizer_types import (
    Action,
    Delta,
    HorizonSection,
)
from argosy.services.plan_headline import (
    HeadlineLines,
    InsuranceGapsSummary,
    _all_actions_with_dates,
    _parse_iso_date,
    compute_headline_lines,
    summarize_accepted_deltas,
    summarize_insurance_gaps,
)


# Helpers -----------------------------------------------------------------


def _h(
    horizon: str,
    *,
    actions: list[Action] | None = None,
    deltas: list[Delta] | None = None,
) -> HorizonSection:
    return HorizonSection(
        horizon=horizon,
        freshness_expected={
            "long": "annual",
            "medium": "quarterly",
            "short": "monthly",
        }[horizon],
        status="minor_revision",
        posture="balanced",
        actions=actions or [],
        deltas_from_prior=deltas or [],
    )


def _a(label: str, *, kind: str = "dated", trigger: str | None = None) -> Action:
    return Action(
        label=label,
        horizon_kind=kind,
        trigger_or_date=trigger,
    )


def _d(item_kind: str, summary: str, *, horizon: str, accepted: bool) -> Delta:
    return Delta(
        item_kind=item_kind,
        item_id=f"{horizon}.{item_kind}.{summary[:8]}",
        horizon=horizon,
        change_kind="modified",
        summary=summary,
        accepted=accepted,
    )


# ISO date parsing --------------------------------------------------------


class TestParseIsoDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-06-15", date(2026, 6, 15)),
            ("2026-06-15T10:30:00", date(2026, 6, 15)),
            ("2026-06", date(2026, 6, 1)),
            ("2026-06-15 — review tranche size", date(2026, 6, 15)),
        ],
    )
    def test_accepts_expected_formats(self, raw: str, expected: date) -> None:
        assert _parse_iso_date(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "not a date", "tomorrow", "Q2 2026", "USD/NIS > 2.95"],
    )
    def test_returns_none_for_garbage(self, raw: str | None) -> None:
        assert _parse_iso_date(raw) is None


# Dated-actions collection -----------------------------------------------


class TestAllActionsWithDates:
    def test_skips_directional_and_parameterized_actions(self) -> None:
        h = _h(
            "medium",
            actions=[
                _a("Directional one", kind="directional"),
                _a("Param one", kind="parameterized", trigger="VIX > 30"),
                _a("Dated one", kind="dated", trigger="2026-06-15"),
            ],
        )
        pairs = _all_actions_with_dates([h])
        assert len(pairs) == 1
        assert pairs[0][0] == date(2026, 6, 15)
        assert pairs[0][1].label == "Dated one"

    def test_skips_dated_actions_with_unparseable_trigger(self) -> None:
        h = _h(
            "short",
            actions=[
                _a("Garbage trigger", kind="dated", trigger="soonish"),
                _a("Good trigger", kind="dated", trigger="2026-07-01"),
            ],
        )
        pairs = _all_actions_with_dates([h])
        assert [a.label for _, a in pairs] == ["Good trigger"]

    def test_sorts_across_horizons_by_date(self) -> None:
        long_h = _h(
            "long", actions=[_a("Long action", kind="dated", trigger="2027-01-01")]
        )
        med_h = _h(
            "medium",
            actions=[_a("Medium action", kind="dated", trigger="2026-06-15")],
        )
        short_h = _h(
            "short",
            actions=[_a("Short action", kind="dated", trigger="2026-06-08")],
        )
        pairs = _all_actions_with_dates([long_h, med_h, short_h])
        assert [a.label for _, a in pairs] == [
            "Short action",
            "Medium action",
            "Long action",
        ]


# Headline lines ---------------------------------------------------------


class TestComputeHeadlineLines:
    def test_base_plus_bear_when_meaningfully_different(self) -> None:
        lines = compute_headline_lines([], 49.0, 51.0)
        assert lines.retirement_readiness == (
            "You can safely retire at age 49 (base case), age 51 (bear case)."
        )

    def test_base_only_when_bear_within_half_year(self) -> None:
        lines = compute_headline_lines([], 49.0, 49.3)
        assert lines.retirement_readiness == (
            "You can safely retire at age 49 (base case)."
        )

    def test_fallback_when_no_base_crossing(self) -> None:
        lines = compute_headline_lines([], None, None)
        assert "not yet projected" in lines.retirement_readiness
        assert lines.next_big_move is None
        assert lines.then is None

    def test_next_big_move_and_then_pick_first_two_dated_in_order(self) -> None:
        med = _h(
            "medium",
            actions=[
                _a("Attorney retainer", kind="dated", trigger="2026-06-15"),
            ],
        )
        short = _h(
            "short",
            actions=[
                _a("RSU vest", kind="dated", trigger="2026-06-17"),
                _a("Tranche window", kind="dated", trigger="2026-06-30"),
            ],
        )
        lines = compute_headline_lines([med, short], 49.0, 51.0)
        assert lines.next_big_move == (
            "Next big move: Attorney retainer by 2026-06-15."
        )
        assert lines.then == "Then: RSU vest by 2026-06-17."

    def test_next_big_move_present_then_null_when_only_one_dated_action(self) -> None:
        h = _h(
            "short",
            actions=[_a("Just one", kind="dated", trigger="2026-06-08")],
        )
        lines = compute_headline_lines([h], 49.0, 51.0)
        assert lines.next_big_move == "Next big move: Just one by 2026-06-08."
        assert lines.then is None


# Accepted-deltas summary ------------------------------------------------


class TestSummarizeAcceptedDeltas:
    def test_filters_accepted_true_only(self) -> None:
        h = _h(
            "long",
            deltas=[
                _d("target", "US-situs $1.37M target", horizon="long", accepted=True),
                _d("theme", "FIRE bridge analysis", horizon="long", accepted=False),
            ],
        )
        out = summarize_accepted_deltas([h])
        assert len(out) == 1
        assert out[0].summary == "US-situs $1.37M target"

    def test_preserves_horizon_order_long_medium_short(self) -> None:
        long_h = _h(
            "long",
            deltas=[_d("target", "Long delta", horizon="long", accepted=True)],
        )
        med_h = _h(
            "medium",
            deltas=[_d("theme", "Medium delta", horizon="medium", accepted=True)],
        )
        short_h = _h(
            "short",
            deltas=[_d("action", "Short delta", horizon="short", accepted=True)],
        )
        # Pass in long → medium → short; output should preserve.
        out = summarize_accepted_deltas([long_h, med_h, short_h])
        assert [d.summary for d in out] == [
            "Long delta",
            "Medium delta",
            "Short delta",
        ]

    def test_empty_when_no_deltas_or_none_accepted(self) -> None:
        h = _h(
            "long",
            deltas=[
                _d("target", "Pending", horizon="long", accepted=False),
            ],
        )
        assert summarize_accepted_deltas([h]) == []


# Insurance gaps summary -------------------------------------------------


def _gap(insurance_type: str, gap_amount: float):
    """Tiny stand-in for an InsuranceGap so the test doesn't need to
    spin up the full ValueWithRationale dataclass — the summary only
    reads ``insurance_type`` and ``gap_nis.value``."""
    return SimpleNamespace(
        insurance_type=insurance_type,
        gap_nis=SimpleNamespace(value=gap_amount),
    )


class TestSummarizeInsuranceGaps:
    def test_empty_input_says_not_assessed(self) -> None:
        out = summarize_insurance_gaps([])
        assert out == InsuranceGapsSummary(
            one_line="Coverage not assessed.", has_data=False
        )

    def test_no_gaps_returns_no_major_gaps(self) -> None:
        gaps = [_gap("life", 0.0), _gap("disability", 0.0)]
        out = summarize_insurance_gaps(gaps)
        assert out.one_line == "No major gaps."
        assert out.has_data is True

    def test_lists_short_categories(self) -> None:
        gaps = [
            _gap("life", 500_000.0),
            _gap("disability", 0.0),
            _gap("ltc", 5_000.0),
        ]
        out = summarize_insurance_gaps(gaps)
        assert "Short: life, ltc" in out.one_line
        assert "Covered: disability" in out.one_line
        assert out.has_data is True

    def test_underscore_keys_get_humanized(self) -> None:
        gaps = [_gap("health_supplementary", 1.0)]
        out = summarize_insurance_gaps(gaps)
        assert "health supplementary" in out.one_line
