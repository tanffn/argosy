"""Tests for the Wave 2 safety gates (BLOCKER #4 NRA estate + #5 Liquidity)."""
import json
from datetime import date, datetime, timezone

import pytest

from argosy.services.retirement.safety_gates import (
    GateVerdict,
    compute_liquidity_gate,
    compute_nra_estate_gate,
    compute_safety_gates,
)
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_user_context(session, *, user_id: str = "ariel") -> None:
    session.add(
        UserContext(
            user_id=user_id,
            identity_yaml=(
                "date_of_birth: '1982-08-28'\n"
                "fx_rate:\n  usd_nis: 3.0\n"
            ),
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.commit()


def _seed_snapshot(
    session,
    *,
    user_id: str = "ariel",
    positions: list[dict],
    fx_usd_nis: float = 3.0,
) -> None:
    session.add(
        PortfolioSnapshotRow(
            user_id=user_id,
            snapshot_date=date(2026, 5, 1),
            imported_at=datetime.now(timezone.utc),
            source_path="/tmp/seed.tsv",
            positions_json=json.dumps(positions),
            allocations_json="[]",
            nvda_sales_json="[]",
            real_estate_json="[]",
            totals_json=json.dumps({"fx_usd_nis": fx_usd_nis}),
        )
    )
    session.commit()


def _seed_household_budget(
    session, *, user_id: str = "ariel", monthly_burn_nis: float,
) -> None:
    session.add(
        AgentReport(
            user_id=user_id,
            agent_role="household_budget",
            response_text=json.dumps({
                "monthly_burn_nis": monthly_burn_nis,
                "monthly_income_nis": 50000.0,
            }),
            decision_id="test",
        )
    )
    session.commit()


class TestNraEstateGate:
    def test_fail_above_200k(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # NVDA: 12,000 shares × $200 = $2.4M — US-situs at Schwab
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "NVIDIA",
                    "details": "RSU",
                    "symbol": "NVDA",
                    "usd_value_k": 2400.0,
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        assert isinstance(verdict, GateVerdict)
        assert verdict.gate_id == "nra_estate"
        assert verdict.status == "FAIL"
        assert verdict.value.value == 2_400_000.0
        assert "ucits migration" in verdict.suggested_action.value.lower()

    def test_warn_in_60k_to_200k_band(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "etf",
                    "details": "VOO",
                    "symbol": "VOO",
                    "usd_value_k": 100.0,  # $100K
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        assert verdict.status == "WARN"
        assert verdict.value.value == 100_000.0

    def test_pass_below_60k(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "etf",
                    "details": "VOO",
                    "symbol": "VOO",
                    "usd_value_k": 30.0,  # $30K
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        assert verdict.status == "PASS"

    def test_cash_in_schwab_not_counted(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "Cash",
                    "details": "MMF",
                    "symbol": "-",
                    "usd_value_k": 500.0,  # $500K cash — NOT US-situs
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        assert verdict.status == "PASS"
        assert verdict.value.value == 0

    def test_ucits_not_counted(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                {
                    "location": "leumi",
                    "currency": "USD",
                    "asset_type": "etf UCITS",
                    "details": "VWRA UCITS",
                    "symbol": "VWRA",
                    "usd_value_k": 300.0,
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        assert verdict.status == "PASS"


class TestLiquidityGate:
    def test_pass_when_buffer_above_12_months(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # ₪200K cash, ₪10K monthly burn → essential 6K → 33 months coverage
            _seed_snapshot(s, positions=[
                {
                    "location": "leumi",
                    "currency": "NIS",
                    "asset_type": "Cash",
                    "details": "Checking",
                    "current_value_local": 200_000.0,
                },
            ])
            _seed_household_budget(s, monthly_burn_nis=10_000.0)
            verdict = compute_liquidity_gate(user_id="ariel", session=s)
        assert verdict.status == "PASS"
        assert verdict.value.value > 12

    def test_warn_at_6_to_12_months(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # ₪50K cash, ₪10K burn → essential 6K → ~8 months
            _seed_snapshot(s, positions=[
                {
                    "location": "leumi",
                    "currency": "NIS",
                    "asset_type": "Cash",
                    "details": "Checking",
                    "current_value_local": 50_000.0,
                },
            ])
            _seed_household_budget(s, monthly_burn_nis=10_000.0)
            verdict = compute_liquidity_gate(user_id="ariel", session=s)
        assert verdict.status == "WARN"
        assert 6 <= verdict.value.value < 12

    def test_fail_below_6_months(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # ₪20K cash, ₪10K burn → essential 6K → ~3.3 months
            _seed_snapshot(s, positions=[
                {
                    "location": "leumi",
                    "currency": "NIS",
                    "asset_type": "Cash",
                    "details": "Checking",
                    "current_value_local": 20_000.0,
                },
            ])
            _seed_household_budget(s, monthly_burn_nis=10_000.0)
            verdict = compute_liquidity_gate(user_id="ariel", session=s)
        assert verdict.status == "FAIL"
        assert verdict.value.value < 6

    def test_usd_cash_converted_to_nis(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # $50K USD cash at FX 3.0 = ₪150K
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "Cash",
                    "details": "MMF",
                    "current_value_local": 50_000.0,
                },
            ], fx_usd_nis=3.0)
            _seed_household_budget(s, monthly_burn_nis=10_000.0)
            verdict = compute_liquidity_gate(user_id="ariel", session=s)
        # ₪150K / (10K × 0.6) = 25 months
        assert verdict.value.value == pytest.approx(25.0, abs=0.5)


class TestComposeSafetyGates:
    def test_returns_both_gates(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                {
                    "location": "schwab",
                    "currency": "USD",
                    "asset_type": "Cash",
                    "details": "MMF",
                    "usd_value_k": 100.0,
                    "current_value_local": 100_000.0,
                },
            ])
            _seed_household_budget(s, monthly_burn_nis=10_000.0)
            verdicts = compute_safety_gates(user_id="ariel", session=s)
        assert len(verdicts) == 2
        ids = [v.gate_id for v in verdicts]
        assert "nra_estate" in ids
        assert "emergency_liquidity" in ids
