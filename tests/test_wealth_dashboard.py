"""Tests for the Wealth Dashboard service + route.

Covers:
  * Math primitives (``years_to_target``, ``project_wealth_curve``,
    ``compute_current_age``) over their full case-matrix: positive
    return + positive contribution; zero-return; negative-contribution;
    already-at-target; unreachable / capped.
  * Per-block computers via the public ``compute_wealth_dashboard``
    entry point on a seeded DB — each block independently:
      - net-worth + 3 scenarios with the seeded snapshot/budget.
      - cash runway: cash + SGOV ÷ burn.
      - concentration: NVDA % + plan target.
      - savings rate.
      - FX exposure bucketing.
      - RSU income (next 12 months filter + NVDA spot price).
      - Estate exposure vs $60K NRA exemption + 40% liability.
  * Missing-data graceful degradation (no snapshot, no budget report,
    no plan target).
  * Route smoke test through ``/api/portfolio/wealth-dashboard?user_id=``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from argosy.services.wealth_dashboard import (
    DEFAULT_CURRENT_AGE,
    DEFAULT_FX_USD_NIS,
    SWR,
    US_NRA_ESTATE_EXEMPTION_USD,
    US_NRA_ESTATE_RATE,
    compute_current_age,
    compute_wealth_dashboard,
    get_scenario_returns,
    project_wealth_curve,
    years_to_target,
)


def test_typical_scenario_return_is_canonical_central() -> None:
    """H4: /portfolio's 'typical' real return must equal the canonical
    RetirementAssumptions.mu_real_typical that /plan + /retirement use — it was
    a stale 0.045 (which is actually the dual-track CONSERVATIVE case), so the
    three surfaces disagreed on the single most important number."""
    from argosy.services.retirement.retirement_plan import RetirementAssumptions
    from argosy.services.wealth_dashboard import get_scenario_returns

    assert get_scenario_returns()["typical"] == pytest.approx(
        RetirementAssumptions().mu_real_typical
    )
from argosy.state.models import (
    AgentReport,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


# ===========================================================================
# Pure-math primitives
# ===========================================================================


class TestYearsToTarget:
    def test_already_at_target_returns_zero(self):
        assert years_to_target(
            starting_portfolio=100.0,
            annual_contribution=0.0,
            real_return=0.045,
            target=100.0,
        ) == 0.0

    def test_already_above_target_returns_zero(self):
        assert years_to_target(
            starting_portfolio=200.0,
            annual_contribution=0.0,
            real_return=0.045,
            target=100.0,
        ) == 0.0

    def test_zero_return_linear_solve(self):
        # P0=100, C=10, target=200 -> t=10 years
        y = years_to_target(
            starting_portfolio=100.0,
            annual_contribution=10.0,
            real_return=0.0,
            target=200.0,
        )
        assert y is not None
        assert y == pytest.approx(10.0, abs=1e-9)

    def test_zero_return_zero_contrib_unreachable(self):
        assert (
            years_to_target(
                starting_portfolio=100.0,
                annual_contribution=0.0,
                real_return=0.0,
                target=200.0,
            )
            is None
        )

    def test_positive_return_solves_to_known_answer(self):
        # P0=100, C=0, r=10%, target=200 -> (1.10)^t = 2 -> t = ln2/ln1.10 ≈ 7.27
        y = years_to_target(
            starting_portfolio=100.0,
            annual_contribution=0.0,
            real_return=0.10,
            target=200.0,
        )
        assert y is not None
        assert y == pytest.approx(7.272540, abs=1e-3)

    def test_positive_return_with_contributions_strictly_lower(self):
        # Adding contributions must reach the target faster than the
        # pure-compounding case above.
        y_no_c = years_to_target(
            starting_portfolio=100.0, annual_contribution=0.0,
            real_return=0.10, target=200.0,
        )
        y_with_c = years_to_target(
            starting_portfolio=100.0, annual_contribution=10.0,
            real_return=0.10, target=200.0,
        )
        assert y_no_c is not None and y_with_c is not None
        assert y_with_c < y_no_c

    def test_unreachable_at_zero_return_with_negative_contrib(self):
        # Negative annual contribution + zero return + below target -> None.
        assert (
            years_to_target(
                starting_portfolio=100.0, annual_contribution=-10.0,
                real_return=0.0, target=200.0,
            )
            is None
        )

    def test_capped_by_max_years(self):
        # Tiny return + huge target should overshoot max_years and return None.
        assert (
            years_to_target(
                starting_portfolio=100.0, annual_contribution=1.0,
                real_return=0.001, target=1_000_000_000.0, max_years=60,
            )
            is None
        )

    def test_target_zero_or_negative_returns_none(self):
        assert (
            years_to_target(
                starting_portfolio=100.0, annual_contribution=10.0,
                real_return=0.05, target=0.0,
            )
            is None
        )


class TestProjectWealthCurve:
    def test_zero_return_linear(self):
        out = project_wealth_curve(
            starting_portfolio=100.0, annual_contribution=10.0,
            real_return=0.0, years=3,
        )
        assert out == [100.0, 110.0, 120.0, 130.0]

    def test_positive_return_compounds(self):
        out = project_wealth_curve(
            starting_portfolio=100.0, annual_contribution=0.0,
            real_return=0.10, years=2,
        )
        assert out[0] == pytest.approx(100.0)
        assert out[1] == pytest.approx(110.0)
        assert out[2] == pytest.approx(121.0)

    def test_handles_none_starting(self):
        assert project_wealth_curve(
            starting_portfolio=None,  # type: ignore[arg-type]
            annual_contribution=0.0, real_return=0.05, years=5,
        ) == []


class TestComputeCurrentAge:
    def test_known_dob_pre_birthday(self):
        age, inferred = compute_current_age(
            "1982-08-28", today=date(2026, 5, 27),
        )
        assert (age, inferred) == (43, False)

    def test_known_dob_after_birthday(self):
        age, inferred = compute_current_age(
            "1982-08-28", today=date(2026, 9, 1),
        )
        assert (age, inferred) == (44, False)

    def test_birthday_same_day(self):
        age, inferred = compute_current_age(
            "1982-08-28", today=date(2026, 8, 28),
        )
        assert (age, inferred) == (44, False)

    def test_missing_dob_falls_back(self):
        age, inferred = compute_current_age(None)
        assert age == DEFAULT_CURRENT_AGE
        assert inferred is True

    def test_invalid_dob_falls_back(self):
        age, inferred = compute_current_age("not-a-date")
        assert age == DEFAULT_CURRENT_AGE
        assert inferred is True


# ===========================================================================
# Per-block: DB-seeded integration via compute_wealth_dashboard
# ===========================================================================


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_snapshot(
    session,
    *,
    user_id: str = "ariel",
    fx_usd_nis: float = 3.0,
    positions: list[dict] | None = None,
    total_usd_value_k: float = 1000.0,
    snapshot_date: date | None = None,
) -> PortfolioSnapshotRow:
    if positions is None:
        positions = [
            {
                "location": "schwab",
                "currency": "USD",
                "asset_type": "NVIDIA",
                "details": "RSU",
                "symbol": "NVDA",
                "shares": 1000.0,
                "current_price": 200.0,
                "current_value_local": 200_000.0,
                "usd_value_k": 200.0,
            },
            {
                "location": "schwab 876",
                "currency": "USD",
                "asset_type": "Cash",
                "details": "Cash",
                "symbol": "-",
                "shares": 50_000.0,
                "current_price": 1.0,
                "current_value_local": 50_000.0,
                "usd_value_k": 50.0,
            },
            {
                "location": "schwab 876",
                "currency": "USD",
                "asset_type": "etf",
                "details": "Treasury",
                "symbol": "SGOV",
                "shares": 2_000.0,
                "current_price": 100.0,
                "current_value_local": 200_000.0,
                "usd_value_k": 200.0,
            },
            {
                "location": "leumi",
                "currency": "NIS",
                "asset_type": "Cash",
                "details": "Cash",
                "symbol": "-",
                "shares": None,
                "current_price": None,
                "current_value_local": 60_000.0,
                "usd_value_k": 20.0,
            },
            {
                "location": "leumi",
                "currency": "USD",
                "asset_type": "etf",
                "details": "Core Equity",
                "symbol": "VOO",
                "shares": 100.0,
                "current_price": 600.0,
                "current_value_local": 60_000.0,
                "usd_value_k": 60.0,
            },
        ]
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date or date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/seed.tsv",
        positions_json=json.dumps(positions),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps(
            {
                "total_usd_value_k": total_usd_value_k,
                "cash_balances_usd_k": 50.0,
            }
        ),
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    return row


def _seed_household_budget_report(
    session,
    *,
    user_id: str = "ariel",
    monthly_burn_nis: float = 23_000.0,
    monthly_income_nis: float = 55_000.0,
) -> None:
    body = {
        "runway_class": "comfortable",
        "monthly_burn_nis": monthly_burn_nis,
        "monthly_income_nis": monthly_income_nis,
        "monthly_net_nis": monthly_income_nis - monthly_burn_nis,
        "safe_withdrawal_monthly_usd": 10_000.0,
        "headroom_summary": "seeded",
        "key_concerns": [],
        "confidence": "MEDIUM",
        "cited_sources": ["household_budget/identity_yaml"],
    }
    session.add(
        AgentReport(
            user_id=user_id,
            agent_role="household_budget",
            decision_id=None,
            prompt_hash="x",
            response_text=f"```json\n{json.dumps(body)}\n```",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            model="seed",
        )
    )
    session.commit()


def _seed_user_context(
    session,
    *,
    user_id: str = "ariel",
    identity_yaml: str | None = None,
) -> None:
    if identity_yaml is None:
        identity_yaml = (
            "date_of_birth: '1982-08-28'\n"
            "fx_rate:\n"
            "  usd_nis: 2.9\n"
            "rsu_vest_schedule:\n"
            "  quarterly_vests:\n"
            "    - date: '2026-06-17'\n"
            "      period: 'June 2026'\n"
            "      shares: 729\n"
            "      value_usd: 156771\n"
            "    - date: '2026-09-16'\n"
            "      period: 'September 2026'\n"
            "      shares: 449\n"
            "      value_usd: 96557\n"
            "    - date: '2027-03'\n"
            "      period: 'March 2027'\n"
            "      shares: 450\n"
            "      value_usd: 96773\n"
        )
    session.add(
        UserContext(
            user_id=user_id,
            identity_yaml=identity_yaml,
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.commit()


def _seed_plan_with_nvda_target(
    session,
    *,
    user_id: str = "ariel",
    nvda_target_pct: float = 45.0,
) -> PlanVersion:
    targets = {
        "targets": [
            {
                "label": "NVDA share of portfolio (12-month target)",
                "value": nvda_target_pct,
                "unit": "pct_of_portfolio",
            }
        ]
    }
    pv = PlanVersion(
        user_id=user_id,
        version_label="seed-draft",
        source_path="",
        raw_markdown="",
        role="draft",
        horizon_medium_json=json.dumps(targets),
    )
    session.add(pv)
    session.commit()
    return pv


class TestRetirementBlock:
    def test_full_pipeline_seeded(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0, total_usd_value_k=1000.0)
            _seed_household_budget_report(s, monthly_burn_nis=20_000.0, monthly_income_nis=50_000.0)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))

        r = dash.retirement
        # Net worth: 1000k USD * 3.0 = 3,000,000 NIS
        assert r.net_worth_usd == pytest.approx(1_000_000.0)
        assert r.net_worth_nis == pytest.approx(3_000_000.0)
        assert r.monthly_burn_nis == 20_000.0
        assert r.monthly_income_nis == 50_000.0
        assert r.monthly_surplus_nis == 30_000.0
        assert r.annual_expenses_nis == 240_000.0
        # Target portfolio = 240k / 0.035 ≈ 6,857,142.86 NIS
        assert r.target_portfolio_nis == pytest.approx(240_000.0 / SWR)
        assert r.swr_rate == SWR
        # 3 scenarios in canonical order: bear, conservative, typical.
        assert [s.name for s in r.scenarios] == ["bear", "conservative", "typical"]
        # 'typical' is single-sourced from the canonical central return (5.0%),
        # not the stale 0.045 (= the dual-track CONSERVATIVE case) that used to
        # make /portfolio disagree with /plan + /retirement (H4).
        assert [s.real_return for s in r.scenarios] == [
            0.0,
            0.02,
            get_scenario_returns()["typical"],
        ]
        # Retirement age now binds to the canonical MC-based earliest-feasible
        # age (the SAME source as /plan + /retirement), NOT the deterministic
        # years-to-target. This seed has no FI spend basis in identity_yaml, so
        # the canonical can't be sourced → every scenario age is None (never a
        # fabricated/deterministic 'retire now'). Each scenario's age, when
        # present, must be an int ≥ current age.
        for sc in r.scenarios:
            assert sc.target_age is None or sc.target_age >= r.current_age
            if sc.target_age is None:
                assert sc.years_to_target is None
        # Trajectory has PROJECTION_YEARS+1 points starting at year 0 = nw_nis.
        assert len(r.trajectory) == 26
        assert r.trajectory[0].bear == pytest.approx(r.net_worth_nis)
        assert r.trajectory[0].typical == pytest.approx(r.net_worth_nis)
        # Year-25 typical must be > bear (positive return).
        assert r.trajectory[-1].typical > r.trajectory[-1].bear

    def test_missing_budget_report_falls_back_gracefully(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))
        r = dash.retirement
        assert r.monthly_burn_nis is None
        assert r.monthly_income_nis is None
        assert r.target_portfolio_nis is None
        # Scenarios still emit cards but with None years_to_target.
        assert all(s.years_to_target is None for s in r.scenarios)
        assert "monthly_burn_nis" in " ".join(r.missing_reasons)

    def test_missing_snapshot_falls_back(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_household_budget_report(s)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))
        r = dash.retirement
        assert r.net_worth_nis is None
        assert r.net_worth_usd is None
        assert r.trajectory == []
        assert "trajectory" in " ".join(r.missing_reasons)


class TestCashRunway:
    def test_cash_plus_sgov_over_burn(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0)
            _seed_household_budget_report(s, monthly_burn_nis=20_000.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        cr = dash.cash_runway
        # cash: 50k USD (Schwab cash) * 3 + 60k NIS Leumi = 150k + 60k = 210k NIS.
        assert cr.cash_nis == pytest.approx(210_000.0)
        # SGOV: 200k USD * 3 = 600k NIS.
        assert cr.sgov_nis == pytest.approx(600_000.0)
        assert cr.defensive_total_nis == pytest.approx(810_000.0)
        # 810k / 20k burn = 40.5 months
        assert cr.months_of_runway == pytest.approx(40.5)

    def test_no_burn_marks_missing(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.cash_runway.months_of_runway is None
        assert dash.cash_runway.missing_reasons


class TestConcentration:
    def test_nvda_vs_plan_target(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, total_usd_value_k=1000.0)
            _seed_plan_with_nvda_target(s, nvda_target_pct=45.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        c = dash.concentration
        # NVDA = 200k / 1000k = 20%
        assert c.current_pct == pytest.approx(20.0)
        assert c.target_pct == 45.0
        assert c.target_source is not None and "horizon_medium" in c.target_source

    def test_missing_plan_target(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        c = dash.concentration
        assert c.current_pct is not None
        assert c.target_pct is None
        assert c.missing_reasons


class TestSavingsRate:
    def test_basic(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s)
            _seed_household_budget_report(s, monthly_burn_nis=30_000.0, monthly_income_nis=60_000.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        # (60k − 30k)/60k = 50%
        assert dash.savings_rate.rate_pct == pytest.approx(50.0)


class TestFxExposure:
    def test_buckets_normalised_to_nis_and_pct(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        buckets = {b.currency: b for b in dash.fx_exposure.buckets}
        # USD: NVDA 200k + cash 50k + SGOV 200k + VOO 60k = 510k USD * 3 = 1,530k NIS
        assert "USD" in buckets and buckets["USD"].value_nis == pytest.approx(1_530_000.0)
        # NIS: 60k cash @ leumi (current_value_local)
        assert "NIS" in buckets and buckets["NIS"].value_nis == pytest.approx(60_000.0)
        total = sum(b.value_nis for b in dash.fx_exposure.buckets)
        assert sum(b.pct for b in dash.fx_exposure.buckets) == pytest.approx(100.0, abs=0.01)
        assert dash.fx_exposure.usd_pct == pytest.approx(
            buckets["USD"].value_nis / total * 100, abs=0.01
        )


class TestRsuIncome:
    def test_next_12_months_filter_and_pricing(self, client_with_db):
        # NVDA spot price seeded at 200.0 in _seed_snapshot. FX = 3.0.
        # Three seeded vests fall within next 12 months from 2026-05-27
        # (cutoff = 2027-05-27): 2026-06-17 (729 sh), 2026-09-16 (449 sh),
        # and 2027-03-01 (450 sh). All three should qualify.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))
        rsu = dash.rsu_income
        assert rsu.nvda_price_usd == pytest.approx(200.0)
        assert rsu.fx_usd_nis == pytest.approx(3.0)
        periods = [q.period for q in rsu.quarters]
        assert "June 2026" in periods
        assert "September 2026" in periods
        assert "March 2027" in periods
        # Sanity: NIS values use NVDA spot * FX, not the historical
        # value_usd at award time.
        june = next(q for q in rsu.quarters if q.period == "June 2026")
        assert june.value_nis == pytest.approx(729 * 200.0 * 3.0)
        # Total = sum of all 3 qualifying quarters at NVDA spot price.
        assert rsu.next_12_months_nis == pytest.approx(
            (729 + 449 + 450) * 200.0 * 3.0
        )

    def test_excludes_vests_outside_12_month_window(self, client_with_db):
        # Vest in June 2028 (far future) should NOT appear in the
        # next-12-months list.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(
                s,
                identity_yaml=(
                    "date_of_birth: '1982-08-28'\n"
                    "rsu_vest_schedule:\n"
                    "  quarterly_vests:\n"
                    "    - date: '2028-06-17'\n"
                    "      period: 'June 2028'\n"
                    "      shares: 100\n"
                    "      value_usd: 20000\n"
                ),
            )
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))
        assert dash.rsu_income.quarters == []
        assert dash.rsu_income.next_12_months_nis is None

    def test_missing_schedule(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            # identity_yaml WITHOUT rsu_vest_schedule
            _seed_user_context(
                s,
                identity_yaml="date_of_birth: '1982-08-28'\nfx_rate:\n  usd_nis: 2.9\n",
            )
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.rsu_income.next_12_months_nis is None
        assert dash.rsu_income.quarters == []
        assert dash.rsu_income.missing_reasons


class TestEstateExposure:
    def test_us_situs_above_exemption(self, client_with_db):
        # US-situs is classified by instrument DOMICILE, not broker location:
        #   NVDA 200k (US, Schwab)  + SGOV 200k (US, Schwab)
        #   + VOO 60k (US-domiciled, held at the ISRAELI broker — counts!)
        #   = 460k USD US-situs.
        # Excluded: Schwab USD cash 50k + Leumi NIS cash 20k (portfolio-interest
        # exemption — cash is never US-situs). The Leumi VOO used to be silently
        # dropped (broker-location heuristic) and the Schwab cash wrongly added;
        # the domicile classifier fixes both.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        e = dash.estate_exposure
        assert e.us_situs_usd == pytest.approx(460_000.0)
        assert e.us_situs_nis == pytest.approx(460_000.0 * 3.0)
        assert e.above_exemption_usd == pytest.approx(
            460_000.0 - US_NRA_ESTATE_EXEMPTION_USD
        )
        assert e.potential_liability_usd == pytest.approx(
            (460_000.0 - US_NRA_ESTATE_EXEMPTION_USD) * US_NRA_ESTATE_RATE
        )
        assert e.potential_liability_nis == pytest.approx(
            e.potential_liability_usd * 3.0
        )


class TestAssumptionsAndDefaults:
    def test_uses_user_date_of_birth_over_spouse(self, client_with_db):
        """Verify the user's DOB (not the spouse's) drives current_age.

        Real identity_yaml carries BOTH ``user_date_of_birth`` and
        ``spouse_date_of_birth``. The dashboard must select the user's.
        """
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(
                s,
                identity_yaml=(
                    "user_date_of_birth: '1982-06-17'\n"
                    "spouse_date_of_birth: '1990-01-01'\n"
                ),
            )
            _seed_snapshot(s)
            _seed_household_budget_report(s)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 6, 17))
        # Born 1982-06-17, today 2026-06-17 -> age 44 (birthday-same-day branch).
        assert dash.assumptions.current_age == 44
        assert "user_date_of_birth" in dash.assumptions.current_age_source

    def test_uses_user_age_current_when_no_dob(self, client_with_db):
        """When identity_yaml has only ``user_age_current`` (some intake
        flows skip DOB), use that integer directly."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s, identity_yaml="user_age_current: 43\n")
            _seed_snapshot(s)
            dash = compute_wealth_dashboard(s, user_id="ariel", today=date(2026, 5, 27))
        assert dash.assumptions.current_age == 43
        assert "user_age_current" in dash.assumptions.current_age_source

    def test_no_dob_falls_back_to_default_age(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            # No identity_yaml entry at all.
            _seed_snapshot(s)
            _seed_household_budget_report(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.assumptions.current_age == DEFAULT_CURRENT_AGE
        assert dash.assumptions.current_age_source.startswith("default")

    def test_fx_resolution_prefers_snapshot(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)  # identity has fx_rate.usd_nis = 2.9
            _seed_snapshot(s, fx_usd_nis=3.123)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.assumptions.fx_usd_nis == pytest.approx(3.123)
        assert "snapshot" in dash.assumptions.fx_source

    def test_fx_falls_back_to_identity_then_default(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)  # identity has fx_rate.usd_nis = 2.9
            # No snapshot at all.
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.assumptions.fx_usd_nis == pytest.approx(2.9)
        assert "identity_yaml" in dash.assumptions.fx_source

    def test_fx_default_when_nothing_available(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.assumptions.fx_usd_nis == pytest.approx(DEFAULT_FX_USD_NIS)
        assert "default" in dash.assumptions.fx_source

    def test_scenario_returns_match_constants(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s)
            _seed_household_budget_report(s)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        assert dash.assumptions.scenario_returns == get_scenario_returns()
        assert dash.assumptions.swr_rate == SWR


class TestCompositionBreakdowns:
    """Asset-class + sector donut composition.

    The compositions are built from the same positions_json as the rest
    of the dashboard, but classified through two static taxonomies. The
    sector taxonomy is hand-curated and an unknown ticker MUST fall into
    "Other" rather than break the layout.
    """

    def test_asset_class_composition_sums_to_100pct(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0, total_usd_value_k=1000.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        # Sum to ~100% across all asset-class slices.
        total_pct = sum(sl.pct for sl in dash.asset_class_composition)
        assert total_pct == pytest.approx(100.0, abs=0.01)
        # Names come from the canonical asset-class taxonomy.
        names = {sl.name for sl in dash.asset_class_composition}
        # Seeded positions: NVDA (NVIDIA→Equity), Cash, SGOV (→Cash by
        # special-case), NIS Cash, VOO (Core Equity → Equity).
        assert "Equity" in names
        assert "Cash" in names
        # NVDA should be inside the Equity slice's holdings list.
        equity = next(sl for sl in dash.asset_class_composition if sl.name == "Equity")
        assert "NVDA" in equity.holdings
        # SGOV should land in Cash (special-case in classifier).
        cash = next(sl for sl in dash.asset_class_composition if sl.name == "Cash")
        assert "SGOV" in cash.holdings

    def test_sector_composition_includes_all_holdings(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0)
            dash = compute_wealth_dashboard(s, user_id="ariel")
        # Sum to ~100%.
        total_pct = sum(sl.pct for sl in dash.sector_composition)
        assert total_pct == pytest.approx(100.0, abs=0.01)
        # Every position with positive value should appear in exactly one
        # sector slice's holdings list.
        all_holdings: list[str] = []
        for sl in dash.sector_composition:
            all_holdings.extend(sl.holdings)
        # Seeded tickers (NVDA→Tech, SGOV→T-Bill, VOO→Broad Index, Cash labels)
        assert "NVDA" in all_holdings
        assert "SGOV" in all_holdings
        assert "VOO" in all_holdings
        # NVDA → Tech bucket.
        tech = next((sl for sl in dash.sector_composition if sl.name == "Tech"), None)
        assert tech is not None
        assert "NVDA" in tech.holdings
        # SGOV → T-Bill bucket (§20.4 reference: structure ETF, exposure T-Bill).
        tbill = next(
            (sl for sl in dash.sector_composition if sl.name == "T-Bill"), None,
        )
        assert tbill is not None
        assert "SGOV" in tbill.holdings

    def test_asset_class_falls_back_to_per_ticker_map_when_field_missing(
        self, client_with_db,
    ):
        """When asset_type is empty, the per-ticker fallback map fills in."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            # Position with BLANK asset_type — VOO is in the fallback map.
            _seed_snapshot(
                s,
                fx_usd_nis=3.0,
                positions=[
                    {
                        "location": "schwab",
                        "currency": "USD",
                        "asset_type": "",  # blank — must fall back by ticker
                        "details": "ETF",
                        "symbol": "VOO",
                        "shares": 100.0,
                        "current_price": 600.0,
                        "current_value_local": 60_000.0,
                        "usd_value_k": 60.0,
                    },
                    {
                        "location": "schwab",
                        "currency": "USD",
                        "asset_type": "",
                        "details": "Treasury",
                        "symbol": "SGOV",
                        "shares": 100.0,
                        "current_price": 100.0,
                        "current_value_local": 10_000.0,
                        "usd_value_k": 10.0,
                    },
                ],
                total_usd_value_k=70.0,
            )
            dash = compute_wealth_dashboard(s, user_id="ariel")
        names = {sl.name: sl for sl in dash.asset_class_composition}
        # VOO (blank asset_type) must land in Equity via fallback map.
        assert "Equity" in names
        assert "VOO" in names["Equity"].holdings
        # SGOV (blank asset_type) must land in Cash via the special-case
        # rule in the classifier (SGOV is hard-coded to Cash regardless
        # of asset_type because the spec treats it as a cash equivalent).
        assert "Cash" in names
        assert "SGOV" in names["Cash"].holdings

    def test_unknown_ticker_classified_as_other(self, client_with_db):
        """A ticker not in either map MUST fall into 'Other' rather than
        crash the route. Also: asset_class still classifies it via
        asset_type even though sector falls through to Other."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(
                s,
                fx_usd_nis=3.0,
                positions=[
                    {
                        "location": "schwab",
                        "currency": "USD",
                        # NOTE: asset_type=Growth so asset-class still says Equity.
                        "asset_type": "Growth",
                        "details": "Stock",
                        "symbol": "ZZZUNKNOWN",  # not in any map
                        "shares": 10.0,
                        "current_price": 100.0,
                        "current_value_local": 1_000.0,
                        "usd_value_k": 1.0,
                    },
                ],
                total_usd_value_k=1.0,
            )
            dash = compute_wealth_dashboard(s, user_id="ariel")
        # Sector: unknown ticker → "Other".
        sector_names = {sl.name: sl for sl in dash.sector_composition}
        assert "Other" in sector_names
        assert "ZZZUNKNOWN" in sector_names["Other"].holdings
        # Asset-class: Growth asset_type → Equity (NOT Other).
        ac_names = {sl.name: sl for sl in dash.asset_class_composition}
        assert "Equity" in ac_names
        assert "ZZZUNKNOWN" in ac_names["Equity"].holdings

    def test_israeli_etf_classified_by_name_pattern(self, client_with_db):
        """Hebrew-character symbols/details go to the 'Israeli ETF' sector
        bucket regardless of asset_type."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(
                s,
                fx_usd_nis=3.0,
                positions=[
                    {
                        "location": "leumi",
                        "currency": "NIS",
                        "asset_type": "Core Equity",
                        "details": "ETF",
                        "symbol": "מחקה ת\"א-200",
                        "shares": 100.0,
                        "current_price": 1000.0,
                        "current_value_local": 100_000.0,
                        "usd_value_k": 33.0,
                    },
                ],
                total_usd_value_k=33.0,
            )
            dash = compute_wealth_dashboard(s, user_id="ariel")
        sector_names = {sl.name for sl in dash.sector_composition}
        assert "Israeli" in sector_names
        # Asset-class still says Equity (Core Equity keyword match).
        ac_names = {sl.name for sl in dash.asset_class_composition}
        assert "Equity" in ac_names


class TestSectorClassifierIsraeliDetection:
    """Regression: a US holding bought through Leumi carries a Hebrew
    *description* in ``details`` but a latin *ticker* in ``symbol``. It
    must NOT be classified 'Israeli ETF' — only genuinely TASE-listed
    instruments (Hebrew/non-latin ticker) are Israeli. The prior rule
    scanned ``details`` for Hebrew, which mislabeled AMD/GOOG/VOO/SCHD/…
    (every Leumi-held US name) as Israeli."""

    def test_us_stock_with_hebrew_description_is_not_israeli(self):
        # The §20.4 reference is the authority: AMD/VOO carry a Hebrew
        # parenthetical name but a latin ticker → their real exposure, never
        # Israeli. (The wealth_dashboard fallback only sees Hebrew TICKERS.)
        from argosy.services.instrument_reference import lookup
        from argosy.services.wealth_dashboard import _classify_sector
        assert lookup("AMD", "(אדוונסד מיקרו דיווייסז) AMD").sector == "Tech"
        assert lookup("VOO", "(ואנגארד S&P 500) VOO").sector == "Broad Index"
        # The fallback classifier (reference miss) never tags a latin ticker
        # Israeli off a Hebrew description.
        assert _classify_sector("AMD", "(אדוונסד מיקרו דיווייסז) AMD") != "Israeli"

    def test_tase_listed_instrument_is_israeli(self):
        from argosy.services.wealth_dashboard import _classify_sector
        # TA-200 tracker: Hebrew ticker, no latin symbol → genuinely Israeli.
        assert _classify_sector('מחקה ת"א-200', 'ATF מחקה ת"א-200') == "Israeli"


# ===========================================================================
# Route-level smoke
# ===========================================================================


class TestRouteSmoke:
    def test_route_returns_200_with_seeded_data(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            _seed_snapshot(s, fx_usd_nis=3.0, total_usd_value_k=1000.0)
            _seed_household_budget_report(s, monthly_burn_nis=20_000.0, monthly_income_nis=50_000.0)
            _seed_plan_with_nvda_target(s, nvda_target_pct=45.0)

        res = client_with_db.get("/api/portfolio/wealth-dashboard?user_id=ariel")
        assert res.status_code == 200
        body = res.json()
        # Top-level keys present.
        assert set(body.keys()) >= {
            "user_id", "generated_at", "retirement", "cash_runway",
            "concentration", "savings_rate", "fx_exposure", "rsu_income",
            "estate_exposure", "asset_class_composition",
            "sector_composition", "assumptions",
        }
        # Composition slices serialise as plain dicts with the four
        # documented fields.
        assert isinstance(body["asset_class_composition"], list)
        assert isinstance(body["sector_composition"], list)
        for sl in body["asset_class_composition"]:
            assert set(sl.keys()) >= {"name", "value_nis", "pct", "holdings"}
        assert body["retirement"]["net_worth_nis"] == pytest.approx(3_000_000.0)
        assert len(body["retirement"]["scenarios"]) == 3
        assert len(body["retirement"]["trajectory"]) == 26
        assert body["concentration"]["target_pct"] == 45.0
        assert body["assumptions"]["swr_rate"] == SWR

    def test_route_returns_200_with_no_data(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
        res = client_with_db.get("/api/portfolio/wealth-dashboard?user_id=ariel")
        assert res.status_code == 200
        body = res.json()
        # Every block tolerates missing data.
        assert body["retirement"]["net_worth_nis"] is None
        assert body["retirement"]["monthly_burn_nis"] is None
        assert body["cash_runway"]["months_of_runway"] is None
        assert body["concentration"]["current_pct"] is None
        assert body["concentration"]["target_pct"] is None
        assert body["savings_rate"]["rate_pct"] is None
        assert body["fx_exposure"]["buckets"] == []
        assert body["rsu_income"]["next_12_months_nis"] is None
        assert body["estate_exposure"]["us_situs_usd"] is None
        # No snapshot → no compositions; empty lists, not nulls.
        assert body["asset_class_composition"] == []
        assert body["sector_composition"] == []
        # Assumptions still populated with defaults.
        assert body["assumptions"]["swr_rate"] == SWR
        assert body["assumptions"]["current_age"] == DEFAULT_CURRENT_AGE


def test_net_worth_swaps_real_estate_stub_for_full_equity():
    """Net worth must include the FULL real-estate net equity, not the legacy
    $69K stub carried in the position block."""
    import json as _json
    from types import SimpleNamespace
    from argosy.services.wealth_dashboard import _net_worth
    snap = SimpleNamespace(
        totals_json=_json.dumps({"total_usd_value_k": 1069.0}),  # incl 69 stub
        positions_json=_json.dumps([
            {"asset_type": "Real estate", "usd_value_k": 69.0, "symbol": "-"},
            {"asset_type": "Equity", "usd_value_k": 1000.0, "symbol": "VOO"},
        ]),
        real_estate_json=_json.dumps([
            {"location": "Keret", "currency": "NIS", "role": "Home", "value_local": 2_500_000.0},
            {"location": "Keret", "currency": "NIS", "role": "Loan", "value_local": -350_000.0},
        ]),
        fx_usd_nis=2.94161, fx_usd_eur=0.84931,
    )
    _nis, usd = _net_worth(snapshot=snap, fx_usd_nis=2.94161)
    # 1069 - 69 stub + (2,150,000 NIS / 2.94161 / 1000 = 730.9K) = 1730.9K
    assert round(usd / 1000.0) == round(1000 + 730.9)


def test_cash_runway_excludes_real_estate():
    """The "Aborad" property (symbol "-") must not be counted as cash."""
    import json as _json
    from types import SimpleNamespace
    from argosy.services.wealth_dashboard import _cash_runway
    snap = SimpleNamespace(positions_json=_json.dumps([
        {"asset_type": "Real estate", "usd_value_k": 69.0, "symbol": "-", "currency": "USD"},
        {"asset_type": "Cash", "usd_value_k": 10.0, "symbol": "-", "currency": "USD"},
    ]))
    blk = _cash_runway(snapshot=snap, burn_nis=10000.0, fx_usd_nis=3.0)
    # Only the $10K cash counts (×1000×3 = 30,000 NIS), not the $69K property.
    assert round(blk.cash_nis) == 30000


def test_classify_region_buckets():
    from argosy.services.wealth_dashboard import _classify_region
    from argosy.services.instrument_reference import lookup
    L = lambda s, d="": lookup(s, d)  # noqa: E731
    assert _classify_region("AMD", "", "Individual Stocks", "USD", L("AMD")) == "US"
    assert _classify_region("EIMI", "(ISHR CORE EM IMI) EIMI LN", "REIT", "USD",
                            L("EIMI", "(ISHR CORE EM IMI) EIMI LN")) == "Emerging Markets"
    assert _classify_region("STOXX Europe 600", "אי בי אי מחקה STOXX Europe 600",
                            "REIT", "USD",
                            L("STOXX Europe 600", "אי בי אי מחקה STOXX Europe 600")) == "Europe"
    # Cash mapped by currency.
    assert _classify_region("", "Cash", "Cash", "NIS", None) == "Israel"
    assert _classify_region("", "Cash", "Cash", "USD", None) == "US"
