"""Tests for Wave 4 decision-policy modules: glide path, rebalancing,
phase expenses, lifecycle income, healthcare. (HIGHs #9/#10/#13/#14 +
MEDs #21/#22).
"""
import json
from datetime import date, datetime, timezone

import pytest

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


# ─── Rebalancing ─────────────────────────────────────────────────────────
#
# T5.4: rebalancing targets the CANONICAL plan (TargetAllocationDoc), not the
# textbook Vanguard age-decline curve. No plan → no target → no alerts.


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


def _seed_doc_targets(monkeypatch, *, equity=80.0, bonds=15.0, cash=5.0) -> None:
    """Point rebalancing at a canonical doc with the given equity/bond/cash %."""
    from argosy.services.retirement import rebalancing as rb
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    def _cls(label, sigma_class, pct, sym):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class=sigma_class,
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary", weight_within_class_pct=100.0)],
        )

    doc = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=21.3,
        provenance="test",
        classes=[
            _cls("Equity", "us_equity", equity, "VOO"),
            _cls("Bonds", "bonds", bonds, "BND"),
            _cls("Cash", "cash", cash, "-"),
        ],
        glide=[],
    )
    monkeypatch.setattr(rb, "get_current_plan", lambda session, user_id: object())
    monkeypatch.setattr(rb, "load_plan_target_allocation", lambda pv: doc)


class TestRebalancing:
    def test_overweight_equity_vs_plan_triggers_alert(self, client_with_db, monkeypatch):
        _seed_doc_targets(monkeypatch)  # plan target 80/15/5
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
        # ~96% equity vs the plan's 80% target → equity overweight alert,
        # reconciled to the canonical doc (NOT a Vanguard age curve).
        classes = [a.asset_class for a in alerts]
        assert "equity" in classes
        eq_alert = next(a for a in alerts if a.asset_class == "equity")
        assert eq_alert.target_pct.value == pytest.approx(0.80)
        assert eq_alert.target_pct.source_id == "canonical_target_allocation_doc"

    def test_no_alerts_when_aligned_with_plan(self, client_with_db, monkeypatch):
        _seed_doc_targets(monkeypatch)  # plan target 80/15/5
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            # Exactly the plan's 80/15/5 → no drift, no alerts.
            _seed_snap(s, [
                {"symbol": "VOO", "asset_type": "etf", "usd_value_k": 800.0},
                {"symbol": "BND", "asset_type": "etf", "details": "Bond index", "usd_value_k": 150.0},
                {"symbol": "-", "asset_type": "Cash", "usd_value_k": 50.0},
            ])
            alerts = detect_rebalancing_alerts(
                user_id="ariel", current_age=50, session=s,
            )
        assert alerts == []

    def test_no_doc_returns_empty(self, client_with_db):
        # Snapshot present but NO canonical plan → no target → no alerts
        # (never rebalance toward a fabricated textbook curve).
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snap(s, [
                {"symbol": "NVDA", "asset_type": "NVIDIA", "usd_value_k": 2400.0},
            ])
            alerts = detect_rebalancing_alerts(
                user_id="ariel", current_age=43, session=s,
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
