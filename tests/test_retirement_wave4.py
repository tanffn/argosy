"""Tests for Wave 4 decision-policy modules: glide path, rebalancing,
phase expenses, lifecycle income, healthcare. (HIGHs #9/#10/#13/#14 +
MEDs #21/#22).
"""
import json
from datetime import date, datetime, timezone

import pytest

from argosy.services.retirement.glide_path import (
    GlidePathPoint,
    compute_glide_path,
    target_at_age,
)
from argosy.services.retirement.healthcare import (
    build_healthcare_curve,
    healthcare_share_of_burn,
)
from argosy.services.retirement.lifecycle_income import (
    build_lifecycle_timeline,
)
from argosy.services.retirement.phase_expenses import (
    build_phase_expense_curve,
    idf_service_phases,
)
from argosy.services.retirement.rebalancing import (
    detect_rebalancing_alerts,
)
from argosy.state.models import PortfolioSnapshotRow, User


# ─── Glide Path ──────────────────────────────────────────────────────────


class TestGlidePath:
    def test_returns_per_year_table(self):
        path = compute_glide_path(start_age=40, end_age=60)
        assert len(path) == 21
        assert all(isinstance(p, GlidePathPoint) for p in path)

    def test_equity_monotone_non_increasing(self):
        path = compute_glide_path(start_age=30, end_age=85)
        equities = [float(p.target_equity_pct.value or 0) for p in path]
        for a, b in zip(equities, equities[1:]):
            assert b <= a + 1e-9

    def test_age_30_equity_high(self):
        gp = target_at_age(30)
        assert gp.target_equity_pct.value == pytest.approx(0.90)

    def test_age_65_equity_50_pct(self):
        gp = target_at_age(65)
        assert gp.target_equity_pct.value == pytest.approx(0.50)

    def test_allocations_sum_to_one(self):
        path = compute_glide_path(start_age=30, end_age=85)
        for p in path:
            total = (
                float(p.target_equity_pct.value or 0)
                + float(p.target_bond_pct.value or 0)
                + float(p.target_cash_pct.value or 0)
            )
            assert total == pytest.approx(1.0, abs=0.01)

    def test_age_minus_30_policy_more_aggressive_early(self):
        a_vanguard = target_at_age(35, policy="vanguard_target_date")
        a_minus30 = target_at_age(35, policy="age_minus_30_bonds")
        # age-minus-30 has only 5% bonds at 35, vs Vanguard ~17%
        assert (a_minus30.target_bond_pct.value or 0) < (a_vanguard.target_bond_pct.value or 0)


# ─── Rebalancing ─────────────────────────────────────────────────────────


def _seed_user(s, user_id: str = "ariel") -> None:
    if s.get(User, user_id) is None:
        s.add(User(id=user_id, plan="free"))
        s.commit()


def _seed_snap(s, positions: list[dict]) -> None:
    s.add(PortfolioSnapshotRow(
        user_id="ariel",
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/s.tsv",
        positions_json=json.dumps(positions),
        allocations_json="[]", nvda_sales_json="[]", real_estate_json="[]",
        totals_json="{}",
    ))
    s.commit()


class TestRebalancing:
    def test_nvda_heavy_triggers_overweight_equity_alert(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snap(s, [
                {"symbol": "NVDA", "asset_type": "NVIDIA", "usd_value_k": 2400.0},
                {"symbol": "BND", "asset_type": "etf", "details": "Bond index", "usd_value_k": 100.0},
            ])
            alerts = detect_rebalancing_alerts(
                user_id="ariel", current_age=43, session=s,
            )
        # At 43 with Vanguard glide, equity target is ~83%; actual ~96% → trigger
        classes = [a.asset_class for a in alerts]
        assert "equity" in classes

    def test_no_alerts_when_aligned(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            # Roughly aligned with age-50 Vanguard: 70% equity, 28% bonds, 2% cash
            _seed_snap(s, [
                {"symbol": "VOO", "asset_type": "etf", "usd_value_k": 700.0},
                {"symbol": "BND", "asset_type": "etf", "details": "Bond index", "usd_value_k": 280.0},
                {"symbol": "-", "asset_type": "Cash", "usd_value_k": 20.0},
            ])
            alerts = detect_rebalancing_alerts(
                user_id="ariel", current_age=50, session=s,
            )
        assert alerts == []

    def test_no_snapshot_returns_empty(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            alerts = detect_rebalancing_alerts(
                user_id="ariel", current_age=43, session=s,
            )
        assert alerts == []


# ─── Phase Expenses + IDF ────────────────────────────────────────────────


class TestPhaseExpenses:
    def test_phase_curve_includes_healthcare_ramp(self):
        phases = build_phase_expense_curve(has_kids=True)
        labels = [p.label for p in phases]
        assert "healthcare_ramp" in labels
        assert "late_life_ltc" in labels

    def test_kids_peak_multiplier_above_one(self):
        phases = build_phase_expense_curve(has_kids=True)
        kids_peak = next(p for p in phases if p.label == "kids_peak")
        assert (kids_peak.monthly_multiplier.value or 0) > 1.0

    def test_empty_nest_dips_below_one(self):
        phases = build_phase_expense_curve(has_kids=True)
        empty = next(p for p in phases if p.label == "empty_nest")
        assert (empty.monthly_multiplier.value or 0) < 1.0

    def test_no_kids_skips_kids_phases(self):
        phases = build_phase_expense_curve(has_kids=False)
        labels = [p.label for p in phases]
        assert "kids_peak" not in labels
        assert "empty_nest" not in labels


class TestIDFService:
    def test_one_kid_one_phase(self):
        phases = idf_service_phases(kids_birth_years=[2010])
        assert len(phases) == 1
        # Service window: 2010 + 18 to 2010 + 21
        assert phases[0].start_age == 2028
        assert phases[0].end_age == 2031

    def test_two_kids_two_phases(self):
        phases = idf_service_phases(kids_birth_years=[2010, 2013])
        assert len(phases) == 2

    def test_no_kids_no_phases(self):
        assert idf_service_phases() == []
        assert idf_service_phases(kids_birth_years=[]) == []

    def test_multiplier_below_one(self):
        phases = idf_service_phases(kids_birth_years=[2010])
        assert (phases[0].monthly_multiplier.value or 0) < 1.0


# ─── Lifecycle Income ────────────────────────────────────────────────────


class TestLifecycleIncome:
    def test_empty_inputs_empty_output(self):
        events = build_lifecycle_timeline(
            current_age=43.0,
            unemployment_annual_probability=0.0,
        )
        assert events == []

    def test_rsu_vests_emit_events(self):
        events = build_lifecycle_timeline(
            current_age=43.0,
            rsu_quarterly_vests=[
                {"date": "2026-06", "period": "Jun 2026", "value_usd": 100000.0},
            ],
            unemployment_annual_probability=0.0,
        )
        rsu_events = [e for e in events if e.event_type == "rsu_vest"]
        assert len(rsu_events) == 1

    def test_unemployment_risk_added_at_default(self):
        events = build_lifecycle_timeline(current_age=43.0)
        unemployment = [e for e in events if e.event_type == "unemployment_risk"]
        assert len(unemployment) == 1
        assert (unemployment[0].monthly_impact_nis.value or 0) < 0  # negative

    def test_partner_and_side_income(self):
        events = build_lifecycle_timeline(
            current_age=43.0,
            partner_income_monthly_nis=20_000.0,
            side_income_monthly_nis=5_000.0,
            unemployment_annual_probability=0.0,
        )
        income_events = [e for e in events if e.event_type == "side_income"]
        assert len(income_events) == 2


# ─── Healthcare ──────────────────────────────────────────────────────────


class TestHealthcare:
    def test_curve_monotone_non_decreasing(self):
        curve = build_healthcare_curve(start_age=30, end_age=95)
        costs = [float(p.monthly_cost_nis.value or 0) for p in curve]
        for a, b in zip(costs, costs[1:]):
            assert b >= a - 1e-9

    def test_age_70_higher_than_age_40(self):
        curve = build_healthcare_curve(start_age=30, end_age=95)
        age_40 = next(p for p in curve if p.age == 40)
        age_70 = next(p for p in curve if p.age == 70)
        assert (age_70.monthly_cost_nis.value or 0) > (age_40.monthly_cost_nis.value or 0)

    def test_share_of_burn(self):
        share = healthcare_share_of_burn(age=70, monthly_burn_nis=20_000.0)
        # ₪1500 / ₪20000 = 7.5%
        assert share.value == pytest.approx(0.075, abs=0.005)

    def test_share_handles_zero_burn(self):
        share = healthcare_share_of_burn(age=70, monthly_burn_nis=0)
        assert share.value is None
