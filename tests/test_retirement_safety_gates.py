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

    def test_us_domiciled_etf_at_israeli_broker_is_counted(self, client_with_db):
        """REGRESSION: US-situs must be classified by instrument DOMICILE, not
        broker location. A US-domiciled ETF (SCHD) held at an Israeli broker
        (Leumi) is exactly as US-situs-exposed as one held at Schwab. The old
        ``location.startswith("schwab")`` heuristic silently dropped the entire
        Leumi-held US book — understating Ariel's real exposure by ~₪2.5M and
        defeating the very UCITS-first policy the estate gate exists to drive.

        Domicile-clean instruments held at the SAME Leumi account (CSPX, an
        Irish UCITS) and cash must still be excluded — proving we switched the
        axis from broker to domicile, not merely flipped the broker filter."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, positions=[
                # US-domiciled ETF at an Israeli broker — MUST count.
                {
                    "location": "Leumi", "currency": "USD",
                    "asset_type": "Dividend",
                    "details": "(שוואב ארה\"ב דיבידנד) SCHD",
                    "symbol": "SCHD", "usd_value_k": 252.0,
                },
                # Irish UCITS at the same Israeli broker — MUST NOT count.
                {
                    "location": "Leumi", "currency": "USD",
                    "asset_type": "Core Equity",
                    "details": "(ISHR CORE S&P500) CSPX LN",
                    "symbol": "CSPX", "usd_value_k": 120.0,
                },
                # Foreign-currency cash at the Israeli broker — never US-situs.
                {
                    "location": "Leumi", "currency": "USD",
                    "asset_type": "Cash", "details": "", "symbol": "",
                    "usd_value_k": 265.0,
                },
            ])
            verdict = compute_nra_estate_gate(user_id="ariel", session=s)
        # Only the US-domiciled SCHD ($252K) is US-situs; UCITS + cash excluded.
        assert verdict.value.value == pytest.approx(252_000.0)
        assert verdict.status == "FAIL"  # $252K > $200K plan-block threshold


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
    def test_returns_two_gates_when_conflict_excluded(self, client_with_db):
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
            verdicts = compute_safety_gates(
                user_id="ariel", session=s, include_conflict=False,
            )
        assert len(verdicts) == 2
        ids = [v.gate_id for v in verdicts]
        assert "nra_estate" in ids
        assert "emergency_liquidity" in ids
        assert "conflict_scenario" not in ids


class TestConflictScenarioGate:
    def test_returns_verdict_under_conflict_pack(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            # Conflict gate needs full pension state for compute_ruin_probability
            from argosy.state.models import UserContext as UC
            _seed_user(s)
            s.add(UC(
                user_id="ariel",
                identity_yaml=(
                    "user_date_of_birth: '1982-08-28'\n"
                    "fx_rate:\n  usd_nis: 3.0\n"
                    "pensions:\n"
                    "  kupat_pensia:\n    balance_nis: 800000\n"
                    "    contribution_rate_pct: 6.0\n"
                    "    employer_match_pct: 6.5\n"
                    "  keren_hishtalmut:\n    balance_nis: 380000\n"
                    "    contribution_rate_pct: 2.5\n"
                    "    employer_match_pct: 7.5\n"
                    "  executive_insurance:\n    balance_nis: 755000\n"
                    "  kupat_gemel:\n    balance_nis: 75000\n"
                    "clal_pension_salary_basis_monthly_nis: 27000\n"
                    "clal_pension_employee_pct: 6.0\n"
                    "clal_pension_employer_pct: 6.5\n"
                    "clal_pension_severance_pct: 8.33\n"
                ),
                goals_yaml="",
                constraints_yaml="",
                current_stage="complete",
            ))
            _seed_snapshot(s, positions=[
                {"location": "schwab", "currency": "USD", "asset_type": "NVIDIA", "usd_value_k": 2400.0, "current_value_local": 2_400_000.0},
            ])
            _seed_household_budget(s, monthly_burn_nis=20_000.0)
            s.commit()
            from argosy.services.retirement.safety_gates import (
                compute_conflict_scenario_gate,
            )
            v = compute_conflict_scenario_gate(
                user_id="ariel", session=s, seed=42,
            )
        assert v.gate_id == "conflict_scenario"
        assert v.status in ("PASS", "WARN", "FAIL")
        assert 0 <= v.value.value <= 1
