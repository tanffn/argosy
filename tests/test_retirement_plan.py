"""Tests for the dual-track retirement plan core (retirement_plan.py).

``compute_retirement_plan`` is the pure, DB-free core: given resolved
household/pension state + deployable capital + central/stress spend, it builds
the drawdown + capital-preservation tracks across bull/typical/bear, the estate
frontier, the spend-to-retire-now solver, and the FX what-if band. These tests
assert the structural invariants that must hold regardless of MC seed noise
(monotonic solvency in age, ordering of the regimes/tracks, reserve PV discount,
deployable accounting) rather than brittle exact ages.
"""
from datetime import date

import pytest

from argosy.services.cashflow_projection import HouseholdState, PensionState
from argosy.services.retirement.retirement_plan import (
    RetirementAssumptions,
    _reserve_pv,
    compute_retirement_plan,
)


def _household() -> HouseholdState:
    return HouseholdState(
        monthly_expenses_nis=0.0,       # overridden per-frontier by the core
        portfolio_value_nis=9_000_000.0,  # overridden per-frontier by the core
        fx_usd_nis=2.895,
        current_age_years=44.0,
        monthly_savings_nis=29_916.0,
    )


def _pensions() -> PensionState:
    return PensionState(
        kupat_pensia_balance_nis=800_147.0,
        kupat_pensia_contribution_monthly_nis=0.0,
        executive_insurance_balance_nis=755_907.0,
        keren_hishtalmut_balance_nis=384_000.0,
        keren_hishtalmut_contribution_monthly_nis=0.0,
        kupat_gemel_balance_nis=75_000.0,
    )


FULL, CGT = 10_992_315.0, 855_092.0


def _plan(**over):
    a = RetirementAssumptions(n_paths=500, max_age=58, seed=42, **over)
    reserve_raw = 1_450_000.0
    reserve_pv = _reserve_pv(reserve_raw, a.reserve_discount_real, a.reserve_avg_liability_years)
    deployable = FULL - reserve_pv - CGT
    return compute_retirement_plan(
        household=_household(), pensions=_pensions(), deployable_nis=deployable,
        spend_central_nis=281_584.0, spend_stress_nis=311_584.0,
        bl_monthly_nis=1_710.0, bl_source="test", annuity_tax_rate=0.155,
        sigma_current=0.3442, full_portfolio_nis=FULL, cgt_haircut_nis=CGT,
        reserve_raw_nis=reserve_raw, reserve_pv_nis=reserve_pv, assumptions=a,
        today=date(2026, 6, 5),
    )


def test_reserve_pv_discounts_below_raw():
    pv = _reserve_pv(1_450_000.0, 0.02, 5.0)  # safe real rate, not equity return
    assert pv == pytest.approx(1_450_000.0 / (1.02 ** 5), rel=1e-9)
    assert 1_250_000.0 < pv < 1_400_000.0  # below raw 1.45M but only modestly (safe discount)


def test_deployable_accounting():
    p = _plan()
    assert p.deployable_nis == pytest.approx(p.full_portfolio_nis - p.cgt_haircut_nis - p.reserve_pv_nis)
    assert 0.0 < p.reserve_pv_nis < p.reserve_raw_nis


def test_three_regimes_present():
    p = _plan()
    assert {t.name for t in p.tracks} == {"typical", "bull", "bear"}


def test_solvency_monotonic_in_age():
    # Later retirement is never materially less safe (sequence risk eases).
    p = _plan()
    for t in p.tracks:
        ps = [fp.p_solvent_95 for fp in t.frontier]
        for earlier, later in zip(ps, ps[1:]):
            assert later >= earlier - 0.03  # allow small MC noise


def test_track_and_regime_ordering():
    p = _plan()
    by = {t.name: t for t in p.tracks}
    # bull retires no later than typical no later than bear
    assert by["bull"].drawdown_age is not None
    assert by["bull"].drawdown_age <= by["typical"].drawdown_age
    assert by["typical"].drawdown_age <= by["bear"].drawdown_age
    # preservation is a stricter bar than drawdown -> never earlier
    for t in p.tracks:
        if t.drawdown_age is not None and t.preservation_age is not None:
            assert t.preservation_age >= t.drawdown_age


def test_typical_drawdown_in_plausible_range():
    p = _plan()
    typ = next(t for t in p.tracks if t.name == "typical")
    assert typ.drawdown_age is not None
    assert 44 <= typ.drawdown_age <= 52  # honest corrected-set band


def test_fx_stress_pushes_age_out():
    p = _plan()
    ages = [age for _, age in p.fx_stress_band if age is not None]
    assert ages == sorted(ages)  # stronger adverse FX -> retire no earlier


def test_spend_to_retire_now_is_bounded_and_consistent():
    p = _plan()
    typ = next(t for t in p.tracks if t.name == "typical")
    if p.spend_to_retire_now_nis is not None:
        assert 60_000.0 < p.spend_to_retire_now_nis < 600_000.0
        # If retire-now isn't safe at the central spend (drawdown_age > current),
        # the safe retire-now spend must be below the central spend.
        if typ.drawdown_age and typ.drawdown_age > int(p.current_age):
            assert p.spend_to_retire_now_nis < p.spend_central_nis


def test_estate_preserved_at_old_retirement():
    p = _plan()
    typ = next(t for t in p.tracks if t.name == "typical")
    assert typ.frontier[-1].principal_preserved is True


class TestMcAgeAwareTax:
    """T3.4 — the MC grosses up the net draw with the single-source age-aware
    EFFECTIVE withdrawal curve (tax_curve.effective_withdrawal_tax_at_age),
    NOT the retired flat-10% withdrawal_tax shortcut. Pre-67 the gross-up
    reflects 15% (CGT 0.25 × gain-fraction 0.6); post-67 reflects 12%."""

    @staticmethod
    def _retired_household():
        from argosy.services.cashflow_projection import HouseholdState
        # Already retired at 55, spending well above any annuity so the
        # portfolio draw is strictly positive across the horizon.
        return HouseholdState(
            monthly_expenses_nis=40_000.0,
            portfolio_value_nis=20_000_000.0,
            fx_usd_nis=2.9,
            current_age_years=55.0,
            monthly_savings_nis=0.0,
        )

    @staticmethod
    def _pensions_zero():
        return PensionState(
            kupat_pensia_balance_nis=0.0,
            kupat_pensia_contribution_monthly_nis=0.0,
            executive_insurance_balance_nis=0.0,
            keren_hishtalmut_balance_nis=0.0,
            keren_hishtalmut_contribution_monthly_nis=0.0,
            kupat_gemel_balance_nis=0.0,
        )

    def _run(self, *, apply_age_aware_tax, tax_rate=0.10):
        from argosy.services.cashflow_projection import project_monte_carlo
        return project_monte_carlo(
            household=self._retired_household(), pensions=self._pensions_zero(),
            retirement_age=55.0, years=20, mu_nominal_annual=0.075,
            sigma_annual=0.18, inflation_annual=0.025, n_paths=50, seed=7,
            today=date(2026, 6, 5), tax_rate=tax_rate,
            apply_age_aware_tax=apply_age_aware_tax,
        )

    @staticmethod
    def _pre67_point(proj):
        # First post-retirement point comfortably below the pension age.
        return next(
            p for p in proj.series
            if 58.0 <= p.age_years <= 60.0
            and p.portfolio_net_draw_monthly_nis > 0.0
        )

    def test_pre67_gross_up_uses_15pct_not_flat_10(self):
        from argosy.services.tax_curve import effective_withdrawal_tax_at_age
        proj = self._run(apply_age_aware_tax=True)
        pt = self._pre67_point(proj)
        rate = effective_withdrawal_tax_at_age(pt.age_years)
        assert rate == pytest.approx(0.15)
        ratio = pt.portfolio_gross_withdrawal_monthly_nis / pt.portfolio_net_draw_monthly_nis
        assert ratio == pytest.approx(1.0 / (1.0 - 0.15), rel=1e-6)
        # And it is materially higher than the retired flat-10% gross-up.
        assert ratio > 1.0 / (1.0 - 0.10)

    def test_age_aware_draw_exceeds_flat_10pct_shortcut(self):
        flat = self._run(apply_age_aware_tax=False, tax_rate=0.10)
        aware = self._run(apply_age_aware_tax=True)
        fp = self._pre67_point(flat)
        ap = self._pre67_point(aware)
        # Same net spend, higher tax -> higher gross withdrawal under age-aware.
        assert ap.portfolio_gross_withdrawal_monthly_nis > fp.portfolio_gross_withdrawal_monthly_nis


class TestMcSpendSplit:
    """H3: the MC spend basis excludes the flat HEALTHCARE_RAMP allowance — the
    phase curve (phase_expense_factor_series) models late-life healthcare
    time-resolved, so keeping the flat allowance too would double-count it in
    every ruin path. HOME_UPGRADE stays excluded from central (discretionary).
    The FI perpetuity keeps the allowance (M1, deferred). codex H3: decision A."""

    @staticmethod
    def _comps():
        from argosy.services.fi_methodology import FiComponent
        return [
            FiComponent(label="Tracked baseline living (ex-mortgage)",
                        kind="permanent", annual_nis=280_000.0, reserve_nis=0.0,
                        source="identity_yaml...", confidence="HIGH"),
            FiComponent(label="Car replacement (amortized)",
                        kind="permanent", annual_nis=20_000.0, reserve_nis=0.0,
                        source="planning_parameter:CAR_REPLACEMENT", confidence="MEDIUM"),
            FiComponent(label="Late-life healthcare ramp",
                        kind="permanent", annual_nis=15_000.0, reserve_nis=0.0,
                        source="planning_parameter:HEALTHCARE_RAMP", confidence="LOW"),
            FiComponent(label="Home upgrades (amortized)",
                        kind="permanent", annual_nis=30_000.0, reserve_nis=0.0,
                        source="planning_parameter:HOME_UPGRADE", confidence="LOW"),
        ]

    def test_central_and_stress_exclude_flat_healthcare(self):
        from argosy.services.retirement.retirement_plan import _mc_spend_split
        central, stress = _mc_spend_split(self._comps())
        # central = baseline + car (no healthcare, no home-upgrade)
        assert central == pytest.approx(300_000.0)
        # stress = baseline + car + home-upgrade (still no flat healthcare)
        assert stress == pytest.approx(330_000.0)
