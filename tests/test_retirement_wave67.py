"""Tests for Waves 6 + 7 — balance sheet completeness + companion UX."""
import pytest

from argosy.services.retirement.action_engine import (
    PrioritizedAction,
    make_action,
    prioritize_actions,
)
from argosy.services.retirement.behavioral import (
    check_fomo_buy,
    check_panic_sell,
)
from argosy.services.retirement.insurance_gaps import compute_insurance_gaps
from argosy.services.retirement.mortgage import (
    build_mortgage_schedule,
    payoff_month,
)
from argosy.services.retirement.multi_goal import (
    GoalConstraint,
    balance_multi_goals,
)
from argosy.services.retirement.partner_state import (
    extract_partner_state,
    household_retire_ready_age,
)
from argosy.services.retirement.real_estate import extract_real_estate_state
from argosy.services.retirement.replan_triggers import list_known_triggers
from argosy.services.retirement.severance import (
    effective_pension_for_annuity,
    extract_severance_state,
)


# ─── Wave 6 ──────────────────────────────────────────────────────────────


class TestRealEstate:
    def test_equity_is_value_minus_mortgage(self):
        rs = extract_real_estate_state(
            primary_residence_value_nis=3_000_000,
            mortgage_balance_nis=1_200_000,
        )
        assert rs.equity_nis.value == 1_800_000

    def test_zero_mortgage_full_equity(self):
        rs = extract_real_estate_state(primary_residence_value_nis=2_000_000)
        assert rs.equity_nis.value == 2_000_000

    def test_default_appreciation_3_5_pct(self):
        rs = extract_real_estate_state(primary_residence_value_nis=1_000_000)
        assert rs.appreciation_annual.value == pytest.approx(0.035)


class TestMortgage:
    def test_schedule_length_matches_term(self):
        schedule = build_mortgage_schedule(
            initial_balance_nis=1_000_000,
            annual_rate=0.045,
            term_months=240,
        )
        assert len(schedule) == 240

    def test_balance_decreases_monotone(self):
        schedule = build_mortgage_schedule(
            initial_balance_nis=1_000_000,
            annual_rate=0.045,
            term_months=120,
        )
        balances = [s.remaining_balance_nis.value for s in schedule]
        for a, b in zip(balances, balances[1:]):
            assert b <= a + 1e-6

    def test_balance_hits_zero_at_end(self):
        schedule = build_mortgage_schedule(
            initial_balance_nis=1_000_000,
            annual_rate=0.045,
            term_months=120,
        )
        assert schedule[-1].remaining_balance_nis.value == pytest.approx(0.0, abs=1.0)

    def test_zero_rate_simple_split(self):
        schedule = build_mortgage_schedule(
            initial_balance_nis=120_000,
            annual_rate=0.0,
            term_months=12,
        )
        # 120K / 12 = 10K/mo
        assert schedule[0].payment_nis.value == pytest.approx(10_000.0)

    def test_payoff_returns_term_months(self):
        assert payoff_month(initial_balance_nis=1_000_000, annual_rate=0.045, term_months=240) == 240


class TestPartner:
    def test_no_partner_returns_none(self):
        p = extract_partner_state()
        assert p is None

    def test_partner_with_income(self):
        p = extract_partner_state(age_years=42, monthly_income_nis=25_000)
        assert p is not None
        assert p.monthly_income_nis.value == 25_000

    def test_household_retire_age_takes_later(self):
        p = extract_partner_state(age_years=42, retirement_age=65)
        v = household_retire_ready_age(primary_retire_age=49, partner=p)
        assert v.value == 65

    def test_no_partner_uses_primary_age(self):
        v = household_retire_ready_age(primary_retire_age=49, partner=None)
        assert v.value == 49


class TestSeverance:
    def test_effective_pension_includes_partial_severance(self):
        sev = extract_severance_state(
            accrued_pizurim_nis=400_000,
            annuitization_probability=0.50,
        )
        eff = effective_pension_for_annuity(
            kupat_pensia_balance_nis=1_000_000,
            severance=sev,
        )
        assert eff.value == pytest.approx(1_200_000)

    def test_no_severance_returns_pensia_only(self):
        sev = extract_severance_state()
        eff = effective_pension_for_annuity(
            kupat_pensia_balance_nis=1_000_000, severance=sev,
        )
        assert eff.value == pytest.approx(1_000_000)


# ─── Wave 7 ──────────────────────────────────────────────────────────────


class TestInsuranceGaps:
    def test_life_insurance_gap_for_household_with_kids(self):
        gaps = compute_insurance_gaps(
            monthly_income_nis=60_000,
            monthly_expenses_nis=20_000,
            dependents_count=2,
            has_kids_under_18=True,
            assets_nis=2_000_000,
            actual_life_coverage_nis=0.0,
        )
        life = next(g for g in gaps if g.insurance_type == "life")
        # 60K × 12 × 10 = 7.2M minus 50% of 2M = 6.2M recommended
        assert life.recommended_coverage_nis.value == pytest.approx(6_200_000)
        assert life.gap_nis.value > 0

    def test_disability_70_pct_recommendation(self):
        gaps = compute_insurance_gaps(
            monthly_income_nis=50_000, monthly_expenses_nis=20_000,
            dependents_count=2, has_kids_under_18=True,
            assets_nis=1_000_000, actual_disability_monthly_nis=20_000,
        )
        dis = next(g for g in gaps if g.insurance_type == "disability")
        assert dis.recommended_coverage_nis.value == pytest.approx(50_000 * 0.70)
        assert dis.gap_nis.value > 0  # actual 20K < recommended 35K

    def test_ltc_gap_at_default(self):
        gaps = compute_insurance_gaps(
            monthly_income_nis=50_000, monthly_expenses_nis=20_000,
            dependents_count=0, has_kids_under_18=False,
            assets_nis=1_000_000,
        )
        ltc = next(g for g in gaps if g.insurance_type == "ltc")
        assert ltc.gap_nis.value == pytest.approx(10_000)

    def test_no_dependents_skips_life_insurance(self):
        gaps = compute_insurance_gaps(
            monthly_income_nis=50_000, monthly_expenses_nis=20_000,
            dependents_count=0, has_kids_under_18=False,
            assets_nis=1_000_000,
        )
        life = [g for g in gaps if g.insurance_type == "life"]
        assert life == []


class TestActionEngine:
    def test_prioritize_sorts_blockers_first(self):
        actions = [
            make_action(id="a", title="A", rationale="r", severity="MEDIUM"),
            make_action(id="b", title="B", rationale="r", severity="BLOCKER"),
            make_action(id="c", title="C", rationale="r", severity="LOW"),
        ]
        sorted_actions = prioritize_actions(actions)
        assert sorted_actions[0].severity == "BLOCKER"
        assert sorted_actions[-1].severity == "LOW"

    def test_consequence_score_tiebreaks_within_severity(self):
        actions = [
            make_action(id="a", title="A", rationale="r", severity="HIGH", consequence_score_nis=100_000),
            make_action(id="b", title="B", rationale="r", severity="HIGH", consequence_score_nis=500_000),
        ]
        sorted_actions = prioritize_actions(actions)
        assert sorted_actions[0].id == "b"


class TestReplanTriggers:
    def test_known_triggers_include_market_drawdown(self):
        triggers = list_known_triggers()
        kinds = [t["kind"] for t in triggers]
        assert "market_drawdown_15pct" in kinds
        assert "tax_law_change" in kinds


class TestMultiGoal:
    def test_hard_constraints_funded_first(self):
        result = balance_multi_goals(
            available_capital_nis=500_000,
            constraints=[
                GoalConstraint(
                    goal_id="education",
                    constraint_type="hard_floor",
                    target_nis=200_000,
                    deadline="2028-09-01",
                    priority=1,
                    rationale="kids 1+2 tuition due 2028",
                ),
                GoalConstraint(
                    goal_id="retirement",
                    constraint_type="soft_target",
                    target_nis=1_000_000,
                    deadline=None,
                    priority=5,
                    rationale="retire at 49",
                ),
            ],
        )
        edu = next(r for r in result if r.goal_id == "education")
        ret = next(r for r in result if r.goal_id == "retirement")
        assert edu.funded_pct == 1.0
        assert ret.funded_pct == pytest.approx(0.30, abs=0.01)  # 300K of 1M

    def test_soft_split_proportional_to_priority(self):
        result = balance_multi_goals(
            available_capital_nis=100_000,
            constraints=[
                GoalConstraint(
                    goal_id="a", constraint_type="soft_target",
                    target_nis=100_000, deadline=None, priority=3,
                    rationale="",
                ),
                GoalConstraint(
                    goal_id="b", constraint_type="soft_target",
                    target_nis=100_000, deadline=None, priority=1,
                    rationale="",
                ),
            ],
        )
        a = next(r for r in result if r.goal_id == "a")
        b = next(r for r in result if r.goal_id == "b")
        # priority 3/4 vs 1/4 → 75% vs 25%
        assert a.funded_pct == pytest.approx(0.75)
        assert b.funded_pct == pytest.approx(0.25)


class TestBehavioral:
    def test_panic_sell_fires_after_drawdown(self):
        cp = check_panic_sell(
            proposed_sell_pct=0.20,
            days_since_market_peak=30,
            peak_to_now_drawdown_pct=0.20,
        )
        assert cp.triggered is True
        assert cp.cooldown_hours == 24

    def test_panic_sell_inert_at_steady_state(self):
        cp = check_panic_sell(
            proposed_sell_pct=0.20,
            days_since_market_peak=200,
            peak_to_now_drawdown_pct=0.01,
        )
        assert cp.triggered is False

    def test_fomo_buy_fires_on_concentrated_runup(self):
        cp = check_fomo_buy(
            proposed_buy_pct=0.10,
            asset_30d_return_pct=0.40,
            asset_concentration_pct=0.50,
        )
        assert cp.triggered is True

    def test_fomo_buy_inert_at_low_concentration(self):
        cp = check_fomo_buy(
            proposed_buy_pct=0.10,
            asset_30d_return_pct=0.40,
            asset_concentration_pct=0.05,
        )
        assert cp.triggered is False
