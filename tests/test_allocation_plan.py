"""Unit tests for the canonical allocation_plan service.

The target allocation is the multi-agent panel's agreed asset-class mix, with
the contested fixed-income weight DERIVED (not asserted) so the allocation is
self-consistent with the plan's steady-state volatility anchor
``SIGMA_DIVERSIFIED`` — the exact sigma the deconcentration optimizer used to
certify the earliest-safe retirement age.
"""
from __future__ import annotations

from datetime import date

import pytest

from argosy.services.allocation_plan import (
    CASH_FRAC_OF_FI,
    NVDA_TARGET_PCT,
    build_redistribution_schedule,
    build_target_allocation,
    to_synth_targets,
    to_waypoint_targets,
)
from argosy.services.retirement.scenario_mc import SIGMA_DIVERSIFIED
from argosy.services.sigma_glidepath import (
    map_glidepath_class_to_sigma_class,
    sigma_from_composition,
)


class TestDeriveFiWeight:
    def test_blended_sigma_sits_on_the_plan_anchor(self) -> None:
        alloc = build_target_allocation()
        # The whole point of deriving FI: the allocation blends to the plan's
        # steady-state anchor (so age-47 stays self-consistent), not above it.
        assert alloc.blended_sigma <= SIGMA_DIVERSIFIED + 1e-6

    def test_fi_is_minimal_one_step_less_breaches_anchor(self) -> None:
        alloc = build_target_allocation(fi_step=0.5)
        # FI is the MINIMUM that clears the anchor — half a point less must
        # breach it (otherwise we're over-building conservatism).
        ratios = {c.label: c.target_pct for c in alloc.classes}
        fi_pct = alloc.fi_pct
        lighter = dict(ratios)
        # shift 0.5pp from cash back into the largest equity sleeve
        biggest = max(
            (c for c in alloc.classes if c.sigma_class == "us_equity"),
            key=lambda c: c.target_pct,
        ).label
        cash_label = next(c.label for c in alloc.classes if c.sigma_class == "cash")
        lighter[cash_label] -= 0.5
        lighter[biggest] += 0.5
        assert sigma_from_composition(lighter) > SIGMA_DIVERSIFIED
        assert fi_pct > 16.0  # strictly more than the panel's contested estimate

    def test_panel_sixteen_pct_would_breach_the_anchor(self) -> None:
        # Documents the finding: the panel's headline 16% FI blends ABOVE the
        # anchor in the real engine — i.e. it is NOT consistent with age 47.
        comp = {
            "US broad-market core": 28.0,
            "Dividend-quality income": 19.0,
            "US growth tilt (ex-NVDA)": 6.0,
            "International developed (ex-US)": 12.0,
            "US low-volatility equity": 6.0,
            "Strategic single-stock (NVDA)": 12.0,
            "Real assets (REIT/TIPS)": 1.0,
            "Cash & T-bills (incl. ILS tranche)": 16.0 * CASH_FRAC_OF_FI,
            "Short-duration IG bonds": 16.0 * (1.0 - CASH_FRAC_OF_FI),
        }
        assert sigma_from_composition(comp) > SIGMA_DIVERSIFIED


class TestBuildTargetAllocation:
    def test_weights_sum_to_100(self) -> None:
        alloc = build_target_allocation()
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)

    def test_nvda_held_at_user_pick(self) -> None:
        alloc = build_target_allocation()
        nvda = next(c for c in alloc.classes if "NVDA" in c.label and c.sigma_class == "concentrated_equity")
        assert nvda.target_pct == pytest.approx(NVDA_TARGET_PCT)

    def test_fi_derived_above_panel_estimate(self) -> None:
        alloc = build_target_allocation()
        assert 18.0 <= alloc.fi_pct <= 24.0  # derived band; not the panel's 16

    def test_every_label_maps_to_its_intended_sigma_class(self) -> None:
        # Auditability gate: no label may silently mis-map (the ex-NVDA /
        # defensive-as-bonds traps must be dead).
        alloc = build_target_allocation()
        for c in alloc.classes:
            assert map_glidepath_class_to_sigma_class(c.label) == c.sigma_class, c.label

    def test_contested_classes_carry_dissent(self) -> None:
        alloc = build_target_allocation()
        fi = next(c for c in alloc.classes if c.sigma_class == "cash")
        nvda = next(c for c in alloc.classes if c.sigma_class == "concentrated_equity")
        assert fi.agreement == "contested" and fi.dissent
        assert nvda.agreement == "contested" and nvda.dissent


_TODAY_FULL_BOOK = {
    "Strategic single-stock (NVDA)": 60.47,
    "US growth tilt (ex-NVDA)": 11.04,
    "US broad-market core": 10.53,
    "Dividend-quality income": 7.01,
    "Cash & T-bills (incl. ILS tranche)": 4.95,
    "Short-duration IG bonds": 3.29,
    "Real assets (REIT/TIPS)": 1.82,
    "International developed (ex-US)": 0.90,
}


class TestRedistributionSchedule:
    def _sched(self, quarters: int = 8):
        alloc = build_target_allocation()
        return build_redistribution_schedule(
            today_composition=_TODAY_FULL_BOOK,
            target=alloc,
            start=date(2026, 6, 8),
            quarters=quarters,
        )

    def test_nvda_tapers_from_today_to_target(self) -> None:
        sched = self._sched()
        nvda = [w for w in sched.waypoints if w.label == "Strategic single-stock (NVDA)"]
        nvda.sort(key=lambda w: w.quarter)
        # monotone decreasing, ends at the 12% target
        assert nvda[0].pct < 60.47  # already moving down by Q1
        assert nvda[-1].pct == pytest.approx(NVDA_TARGET_PCT, abs=0.01)
        for a, b in zip(nvda, nvda[1:]):
            assert b.pct <= a.pct + 1e-9

    def test_every_quarter_composition_sums_to_100(self) -> None:
        sched = self._sched()
        for q in range(1, sched.quarters + 1):
            total = sum(w.pct for w in sched.waypoints if w.quarter == q)
            assert total == pytest.approx(100.0, abs=0.05)

    def test_underweight_class_rises_monotonically(self) -> None:
        sched = self._sched()
        intl = sorted(
            (w for w in sched.waypoints if w.label == "International developed (ex-US)"),
            key=lambda w: w.quarter,
        )
        assert intl[0].pct > 0.90  # rising from today's ~0.9%
        for a, b in zip(intl, intl[1:]):
            assert b.pct >= a.pct - 1e-9

    def test_waypoint_targets_quarterly_pct_of_portfolio(self) -> None:
        sched = self._sched(quarters=8)
        targets = to_waypoint_targets(sched, stated_at=date(2026, 6, 8))
        assert all(t.unit == "pct_of_portfolio" for t in targets)
        # 8 quarterly waypoints per class
        nvda_targets = [t for t in targets if t.label == "Strategic single-stock (NVDA)"]
        assert len(nvda_targets) == 8
        # revisit dates strictly increasing per class
        dts = [t.revisit_after for t in nvda_targets]
        assert dts == sorted(dts) and len(set(dts)) == 8


class TestToSynthTargets:
    def test_targets_are_pct_of_portfolio_with_rationale(self) -> None:
        alloc = build_target_allocation()
        targets = to_synth_targets(
            alloc, stated_at=date(2026, 6, 8), revisit_after=date(2028, 6, 8)
        )
        assert len(targets) == len(alloc.classes)
        assert all(t.unit == "pct_of_portfolio" for t in targets)
        assert all(t.rationale for t in targets)
        assert all(t.label == c.label for t, c in zip(targets, alloc.classes))
