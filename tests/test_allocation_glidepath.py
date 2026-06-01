"""Unit tests for the allocation_glidepath service (Wave 8 Piece B1).

Tests the pure-function math layer: pct-unit filtering, waypoint
grouping, direction-reversal collapse, linear interpolation. The DB
orchestrator is tested via its inputs (a stub portfolio + a list of
SynthTargets) so the math layer needs no DB fixture.
"""
from __future__ import annotations

import logging
from datetime import date

import pytest

from argosy.agents.plan_synthesizer_types import SynthTarget
from argosy.services.allocation_glidepath import (
    GlidepathPoint,
    build_glidepath,
    collapse_direction_reversals,
    filter_targets_by_pct_unit,
    group_targets_by_label,
    interpolate_glidepath,
)


def _t(
    label: str,
    *,
    value: float,
    unit: str = "pct_of_portfolio",
    revisit_after: date = date(2027, 1, 1),
    stated_at: date = date(2026, 6, 1),
) -> SynthTarget:
    return SynthTarget(
        label=label,
        value=value,
        unit=unit,
        stated_at=stated_at,
        revisit_after=revisit_after,
    )


# Unit filtering ---------------------------------------------------------


class TestFilterTargetsByPctUnit:
    @pytest.mark.parametrize("unit", ["pct_of_portfolio", "pct_of_liquid"])
    def test_includes_supported_pct_units(self, unit: str) -> None:
        targets = [_t("NVDA", value=15.0, unit=unit)]
        eligible, excluded = filter_targets_by_pct_unit(targets)
        assert len(eligible) == 1
        assert len(excluded) == 0

    @pytest.mark.parametrize(
        "unit",
        ["usd", "nis", "shares", "ratio", "years", "months", "days"],
    )
    def test_excludes_non_pct_units(self, unit: str) -> None:
        # SynthTarget validates the unit literal; pct_of_net_worth is
        # supported by the model but not pct-of-allocation for glidepath
        # purposes, so it doesn't appear here.
        targets = [_t("Some target", value=1.0, unit=unit)]
        eligible, excluded = filter_targets_by_pct_unit(targets)
        assert len(eligible) == 0
        assert len(excluded) == 1
        assert excluded[0].reason.startswith("non-pct unit")

    def test_excludes_pct_of_net_worth(self) -> None:
        # pct_of_net_worth is a valid SynthTarget unit but is NOT in the
        # allocation glidepath inclusion list per spec: only
        # pct_of_portfolio + pct_of_liquid land here. net-worth-based
        # targets compete with real-estate / pension assets that aren't
        # part of the "portfolio mix" we're plotting.
        targets = [_t("US-situs estate", value=1370.0, unit="pct_of_net_worth")]
        eligible, excluded = filter_targets_by_pct_unit(targets)
        assert eligible == []
        assert len(excluded) == 1


# Label grouping ---------------------------------------------------------


class TestGroupTargetsByLabel:
    def test_collates_targets_with_same_label_case_insensitive(self) -> None:
        a = _t("NVDA", value=55.0, revisit_after=date(2026, 12, 1))
        b = _t("nvda", value=15.0, revisit_after=date(2027, 6, 1))
        c = _t("US-Equity", value=35.0, revisit_after=date(2027, 6, 1))
        groups = group_targets_by_label([a, b, c])
        # Keys lowercased + stripped; order preserved from first encounter.
        assert list(groups.keys()) == ["nvda", "us-equity"]
        assert len(groups["nvda"]) == 2
        assert len(groups["us-equity"]) == 1

    def test_within_group_targets_sorted_by_revisit_after(self) -> None:
        late = _t("NVDA", value=15.0, revisit_after=date(2027, 6, 1))
        early = _t("NVDA", value=55.0, revisit_after=date(2026, 12, 1))
        groups = group_targets_by_label([late, early])
        assert [t.value for t in groups["nvda"]] == [55.0, 15.0]


# Direction-reversal guardrail ------------------------------------------


class TestCollapseDirectionReversals:
    def test_no_collapse_for_monotone_descending_path(self) -> None:
        # Today 64.9 → 55 → 30 → 15: all DOWN; nothing collapses.
        kept, collapsed = collapse_direction_reversals(
            today_value=64.9,
            waypoints=[
                (date(2026, 12, 1), 55.0, "medium"),
                (date(2027, 1, 1), 30.0, "long"),
                (date(2027, 6, 1), 15.0, "long"),
            ],
        )
        assert [w[1] for w in kept] == [55.0, 30.0, 15.0]
        assert collapsed == []

    def test_collapses_intermediate_that_reverses_direction(self) -> None:
        # Today 64.9 → 70 (UP) → 15 (DOWN to eventual). 70 reverses;
        # collapse it. Spec example.
        kept, collapsed = collapse_direction_reversals(
            today_value=64.9,
            waypoints=[
                (date(2026, 12, 1), 70.0, "medium"),
                (date(2027, 1, 1), 15.0, "long"),
            ],
        )
        assert [w[1] for w in kept] == [15.0]
        assert len(collapsed) == 1
        assert collapsed[0].target_pct == 70.0
        assert "direction-reversal" in collapsed[0].reason.lower()

    def test_collapses_multiple_intermediates_that_all_reverse(self) -> None:
        kept, collapsed = collapse_direction_reversals(
            today_value=64.9,
            waypoints=[
                (date(2026, 9, 1), 70.0, "medium"),
                (date(2026, 12, 1), 80.0, "medium"),
                (date(2027, 6, 1), 15.0, "long"),
            ],
        )
        assert [w[1] for w in kept] == [15.0]
        assert {c.target_pct for c in collapsed} == {70.0, 80.0}

    def test_keeps_intermediates_that_follow_macro_direction(self) -> None:
        # Today 50 → 30 (DOWN) → 20 (DOWN) → 15 (eventual). All same
        # direction as today→eventual; nothing collapses.
        kept, collapsed = collapse_direction_reversals(
            today_value=50.0,
            waypoints=[
                (date(2026, 9, 1), 30.0, "short"),
                (date(2026, 12, 1), 20.0, "medium"),
                (date(2027, 6, 1), 15.0, "long"),
            ],
        )
        assert [w[1] for w in kept] == [30.0, 20.0, 15.0]
        assert collapsed == []

    def test_no_collapse_when_today_equals_eventual_no_macro_direction(
        self,
    ) -> None:
        # today == eventual means there's no macro direction; v1 keeps
        # everything (no reversal possible).
        kept, collapsed = collapse_direction_reversals(
            today_value=50.0,
            waypoints=[
                (date(2026, 9, 1), 70.0, "medium"),
                (date(2027, 6, 1), 50.0, "long"),
            ],
        )
        assert [w[1] for w in kept] == [70.0, 50.0]
        assert collapsed == []

    def test_single_waypoint_never_collapses(self) -> None:
        # A single waypoint IS the eventual; no intermediates.
        kept, collapsed = collapse_direction_reversals(
            today_value=50.0,
            waypoints=[(date(2027, 1, 1), 15.0, "long")],
        )
        assert [w[1] for w in kept] == [15.0]
        assert collapsed == []


# Linear interpolation --------------------------------------------------


class TestInterpolateGlidepath:
    def test_single_class_two_waypoints_interpolates_midpoint(self) -> None:
        # NVDA today=60% → 30% at +12 months. At t=6, value should be 45%.
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),  # today
                (date(2027, 6, 1), 30.0),  # eventual
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        # 13 months of points (t=0 through t=12 inclusive).
        assert len(series) == 13
        midpoint = next(p for p in series if p.months_out == 6)
        # Half-way between 60 and 30 → 45 ± small rounding tolerance
        assert midpoint.composition_pct_by_class["nvda"] == pytest.approx(45.0, abs=1e-3)

    def test_class_with_late_waypoint_holds_flat_after_last(self) -> None:
        # Class with target only at month 6 should hold flat at that
        # value for months 6 → end_date.
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),  # today
                (date(2026, 12, 1), 30.0),  # 6 months later
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        # Months 6 onwards should all be 30%
        for p in series:
            if p.months_out >= 6:
                assert p.composition_pct_by_class["nvda"] == pytest.approx(30.0, abs=1e-3)

    def test_multiple_classes_each_interpolated_independently(self) -> None:
        per_class = {
            "nvda": [(date(2026, 6, 1), 60.0), (date(2027, 6, 1), 30.0)],
            "us-equity": [(date(2026, 6, 1), 20.0), (date(2027, 6, 1), 40.0)],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        last = series[-1]
        assert last.composition_pct_by_class["nvda"] == pytest.approx(30.0, abs=1e-3)
        assert last.composition_pct_by_class["us-equity"] == pytest.approx(40.0, abs=1e-3)


# End-to-end build_glidepath --------------------------------------------


class TestBuildGlidepath:
    def test_full_pipeline_nvda_only_with_descending_targets(self) -> None:
        targets = [
            _t("NVDA", value=55.0, revisit_after=date(2026, 12, 1)),
            _t("NVDA", value=15.0, revisit_after=date(2027, 6, 1)),
        ]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9},
            targets=targets,
            today=date(2026, 6, 1),
        )
        # NVDA glidepath: 64.9 (today) → 55 (2026-12) → 15 (2027-06)
        assert glidepath.collapsed_waypoints == []
        assert glidepath.excluded_targets == []
        assert "nvda" in glidepath.asset_classes
        first = glidepath.points[0]
        last = glidepath.points[-1]
        assert first.composition_pct_by_class["nvda"] == pytest.approx(64.9, abs=1e-3)
        assert last.composition_pct_by_class["nvda"] == pytest.approx(15.0, abs=1e-3)

    def test_full_pipeline_collapses_reversal_and_warns(self) -> None:
        # Spec scenario: today 64.9, medium 70 (UP), long 15 (DOWN).
        # Medium collapses; loud warning logged in collapsed_waypoints.
        targets = [
            _t("NVDA", value=70.0, revisit_after=date(2026, 12, 1)),
            _t("NVDA", value=15.0, revisit_after=date(2027, 6, 1)),
        ]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9},
            targets=targets,
            today=date(2026, 6, 1),
        )
        assert len(glidepath.collapsed_waypoints) == 1
        assert glidepath.collapsed_waypoints[0].target_pct == 70.0
        # The eventual 15% should be the last point of the series.
        last = glidepath.points[-1]
        assert last.composition_pct_by_class["nvda"] == pytest.approx(15.0, abs=1e-3)

    def test_full_pipeline_excludes_non_pct_targets(self) -> None:
        # A USD-unit target gets routed to excluded_targets and never
        # touches the glidepath math.
        targets = [
            _t("NVDA", value=15.0, revisit_after=date(2027, 6, 1)),
            _t(
                "Total US-situs estate tail",
                value=1370.0,
                unit="usd",
                revisit_after=date(2027, 1, 1),
            ),
        ]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9},
            targets=targets,
            today=date(2026, 6, 1),
        )
        assert len(glidepath.excluded_targets) == 1
        assert glidepath.excluded_targets[0].target_unit == "usd"
        assert "nvda" in glidepath.asset_classes

    def test_class_without_snapshot_match_starts_at_zero(self) -> None:
        # Target on a label that has no matching portfolio_categories
        # entry → start the glidepath at 0 (new asset class being
        # introduced).
        targets = [_t("Commodities", value=10.0, revisit_after=date(2027, 6, 1))]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9, "us-equity": 20.0},
            targets=targets,
            today=date(2026, 6, 1),
        )
        first = glidepath.points[0]
        assert first.composition_pct_by_class["commodities"] == pytest.approx(
            0.0, abs=1e-3
        )
        last = glidepath.points[-1]
        assert last.composition_pct_by_class["commodities"] == pytest.approx(
            10.0, abs=1e-3
        )

    def test_normalizes_fraction_scale_synthesizer_output_to_percent(self) -> None:
        # Real synthesizers in the wild sometimes emit
        # ``pct_of_portfolio`` as a fraction of 1 (e.g., 0.35 = 35%)
        # instead of whole-percentage. The service auto-detects the
        # scale per asset class and scales up to 0-100 so the chart
        # always sees consistent units.
        targets = [
            _t(
                "Info-tech sector cap", value=0.35, revisit_after=date(2027, 6, 1)
            ),
        ]
        glidepath = build_glidepath(
            portfolio_categories={},  # no snapshot match → starts at 0
            targets=targets,
            today=date(2026, 6, 1),
        )
        assert glidepath.points[-1].composition_pct_by_class[
            "info-tech sector cap"
        ] == pytest.approx(35.0, abs=1e-3)

    def test_does_not_rescale_when_already_whole_percent(self) -> None:
        # A class whose waypoints are already in 0-100 scale stays untouched.
        targets = [_t("NVDA", value=15.0, revisit_after=date(2027, 6, 1))]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9},
            targets=targets,
            today=date(2026, 6, 1),
        )
        assert glidepath.points[-1].composition_pct_by_class[
            "nvda"
        ] == pytest.approx(15.0, abs=1e-3)

    def test_returns_empty_when_no_eligible_targets(self) -> None:
        targets = [
            _t(
                "Total US-situs estate tail",
                value=1370.0,
                unit="usd",
                revisit_after=date(2027, 1, 1),
            ),
        ]
        glidepath = build_glidepath(
            portfolio_categories={"nvda": 64.9},
            targets=targets,
            today=date(2026, 6, 1),
        )
        assert glidepath.points == []
        assert glidepath.asset_classes == []
        assert len(glidepath.excluded_targets) == 1


# Codex B1 round-1 findings ---------------------------------------------


class TestCollapseLoudWarning:
    """Spec mandates a loud warning for every collapsed waypoint so
    the audit trail captures the decision. Codex finding #1."""

    def test_warning_logged_for_each_collapsed_waypoint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        targets = [
            _t("NVDA", value=70.0, revisit_after=date(2026, 12, 1)),
            _t("NVDA", value=15.0, revisit_after=date(2027, 6, 1)),
        ]
        with caplog.at_level(logging.WARNING, logger="argosy.services.allocation_glidepath"):
            build_glidepath(
                portfolio_categories={"nvda": 64.9},
                targets=targets,
                today=date(2026, 6, 1),
            )
        # The 70% intermediate reverses direction; expect one warning.
        relevant = [
            r
            for r in caplog.records
            if "allocation_glidepath.waypoint_collapsed" in r.getMessage()
        ]
        assert len(relevant) == 1


class TestNoSnapshotMatchWarning:
    """Codex finding #3 — surface no-snapshot-match anchors so the UI
    can flag the "starts from zero" curve."""

    def test_warning_logged_when_target_label_not_in_snapshot(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        targets = [_t("Commodities", value=10.0, revisit_after=date(2027, 6, 1))]
        with caplog.at_level(logging.WARNING, logger="argosy.services.allocation_glidepath"):
            build_glidepath(
                portfolio_categories={"nvda": 64.9},
                targets=targets,
                today=date(2026, 6, 1),
            )
        no_match = [
            r
            for r in caplog.records
            if "allocation_glidepath.no_snapshot_match" in r.getMessage()
        ]
        assert len(no_match) == 1


class TestFractionalMonthBoundaries:
    """Codex finding #2 — boundary tests for fractional-month
    interpolation when revisit_after lands on the 1st vs 30th vs
    mid-month."""

    def test_first_of_month_waypoint_interpolates_cleanly(self) -> None:
        # 60% today → 30% at +12mo (clean first-of-month). At month
        # 6 the value is exactly halfway.
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),
                (date(2027, 6, 1), 30.0),
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        midpoint = next(p for p in series if p.months_out == 6)
        assert midpoint.composition_pct_by_class["nvda"] == pytest.approx(
            45.0, abs=1e-3
        )

    def test_mid_month_waypoint_does_not_drift_outside_value_range(
        self,
    ) -> None:
        # Today 60% → 30% at 2026-12-15 (mid month). At month 6 we
        # haven't fully reached the waypoint; value should be between
        # 30 and 60, never overshoot.
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),
                (date(2026, 12, 15), 30.0),
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        for p in series:
            assert 30.0 - 1e-6 <= p.composition_pct_by_class["nvda"] <= 60.0 + 1e-6

    def test_last_day_of_month_waypoint_treated_like_full_month_offset(
        self,
    ) -> None:
        # 2026-12-30 should be very close to the 2027-01-01 tick (within
        # 1/30 of a month). Pin: the value at month 6 is much closer to
        # the waypoint value than at month 5.
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),
                (date(2026, 12, 30), 30.0),
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        v_at_6 = next(
            p for p in series if p.months_out == 6
        ).composition_pct_by_class["nvda"]
        v_at_5 = next(
            p for p in series if p.months_out == 5
        ).composition_pct_by_class["nvda"]
        # Both still in range; v_at_6 should be closer to 30 than v_at_5.
        assert abs(v_at_6 - 30.0) < abs(v_at_5 - 30.0)


class TestDuplicateDateWaypoints:
    """Codex finding #2 sibling — same-month duplicates can't crash the
    interpolator. Real plans have rarely repeated revisit_after dates
    across horizons; pin behaviour."""

    def test_two_waypoints_same_date_use_later_value(self) -> None:
        per_class = {
            "nvda": [
                (date(2026, 6, 1), 60.0),
                (date(2027, 6, 1), 30.0),
                (date(2027, 6, 1), 20.0),  # duplicate date
            ],
        }
        series = interpolate_glidepath(
            per_class_waypoints=per_class,
            today=date(2026, 6, 1),
            end_date=date(2027, 6, 1),
        )
        last = series[-1]
        # Behaviour: bracketing returns the second of the duplicates,
        # so the last value lands at 20.
        assert last.composition_pct_by_class["nvda"] == pytest.approx(
            20.0, abs=1e-3
        )
