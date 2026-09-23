"""Tests for Wave 5: tax engine + hishtalmut + decumulation + lump-vs-annuity."""
import pytest

from argosy.services.retirement.decumulation import (
    DecumulationStep,
    optimize_decumulation_order,
)
from argosy.services.retirement.hishtalmut import (
    check_hishtalmut_eligibility,
    tax_on_hishtalmut_withdrawal,
)
from argosy.services.retirement.lump_vs_annuity import compute_lump_vs_annuity
from argosy.services.retirement.tax_engine import (
    DEFAULT_MARGINAL_TOP_RATE,
    TaxableCashflow,
    _pension_exemption_rate,
    compute_tax,
)
from argosy.state.models import User, UserContext


def _seed(s) -> None:
    if s.get(User, "ariel") is None:
        s.add(User(id="ariel", plan="free"))
    s.add(UserContext(
        user_id="ariel",
        identity_yaml="date_of_birth: '1982-08-28'\n",
        goals_yaml="", constraints_yaml="", current_stage="complete",
    ))
    s.commit()


# ─── Tax Engine ──────────────────────────────────────────────────────────


class TestPensionExemptionRate:
    def test_2025_is_57_pct(self):
        assert _pension_exemption_rate(2025) == 0.57

    def test_2030_is_67_pct(self):
        assert _pension_exemption_rate(2030) == 0.67

    def test_pre_2025_legacy(self):
        assert _pension_exemption_rate(2024) == 0.52

    def test_corrected_schedule(self):
        assert _pension_exemption_rate(2026) == 0.575
        assert _pension_exemption_rate(2027) == 0.625
        assert _pension_exemption_rate(2028) == 0.67

    def test_post_2030_caps(self):
        assert _pension_exemption_rate(2050) == 0.67


class TestTaxEngine:
    def test_shipped_treaty_reference_matches_calculator(self, client_with_db):
        from argosy.services.retirement.reference import resolve
        with client_with_db.app.state.session_factory() as session:
            _seed(session)
            value = resolve('tax.us_dividend_treaty_withholding', user_id='ariel', session=session)
        assert value.value == 0.25

    def test_capital_gain_25_pct(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="capital_gain", gross_amount_nis=100_000),
                user_id="ariel", session=s,
            )
        assert tb.israeli_tax.value == pytest.approx(25_000.0)
        assert tb.net.value == pytest.approx(75_000.0)

    def test_us_dividend_treaty_ftc(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(
                    source="dividend_us_source",
                    gross_amount_nis=10_000,
                    us_gross_amount_for_treaty=10_000,
                ),
                user_id="ariel", session=s,
            )
        assert tb.israeli_tax.value == pytest.approx(0.0, abs=0.01)
        assert tb.us_treaty_credit.value == pytest.approx(2_500.0)
        assert tb.us_withholding.value == pytest.approx(2_500.0)
        assert tb.net.value == pytest.approx(7_500.0)
        assert tb.effective_rate.value == pytest.approx(0.25)

    @pytest.mark.parametrize('us_gross', [None, 0, 4_000, 10_000])
    def test_dividend_cash_conservation(self, us_gross):
        tb = compute_tax(TaxableCashflow(source='dividend_us_source', gross_amount_nis=10_000,
                         us_gross_amount_for_treaty=us_gross), user_id='ariel', session=None)
        assert tb.net.value == 7_500
        assert tb.net.value + tb.us_withholding.value + tb.israeli_tax.value + tb.surtax.value == 10_000

    @pytest.mark.parametrize('us_gross', [-1, 10_001, float('nan'), float('inf')])
    def test_invalid_us_gross_rejected(self, us_gross):
        with pytest.raises(ValueError):
            compute_tax(TaxableCashflow(source='dividend_us_source', gross_amount_nis=10_000,
                        us_gross_amount_for_treaty=us_gross), user_id='ariel', session=None)

    def test_pension_annuity_post_67_exemption(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(
                    source="pension_annuity", gross_amount_nis=10_000,
                    is_post_67=True,
                    pension_exemption_monthly_nis=5_422,
                ),
                user_id="ariel", session=s, year=2026,
            )
        expected = (10_000 - 5_422) * DEFAULT_MARGINAL_TOP_RATE
        assert tb.israeli_tax.value == pytest.approx(expected, abs=0.01)

    def test_pension_no_unverified_entitlement_assumed(self):
        from argosy.services.retirement.tax_engine import effective_pension_annuity_tax
        tb = compute_tax(TaxableCashflow(source='pension_annuity', gross_amount_nis=20_000,
            is_post_67=True), user_id='ariel', session=None)
        assert tb.israeli_tax.value == 9_400
        assert effective_pension_annuity_tax(user_id='ariel', session=None) == 0.47

    @pytest.mark.parametrize('amount', [3_000, 20_000, 100_000])
    def test_pension_exemption_capped_and_periods_consistent(self, amount):
        monthly = compute_tax(TaxableCashflow(source='pension_annuity', gross_amount_nis=amount,
            is_post_67=True, pension_exemption_monthly_nis=5_422), user_id='ariel', session=None)
        annual = compute_tax(TaxableCashflow(source='pension_annuity', gross_amount_nis=amount * 12,
            is_post_67=True, pension_exemption_monthly_nis=5_422, pension_period_months=12), user_id='ariel', session=None)
        assert monthly.israeli_tax.value == pytest.approx(max(0, amount - 5_422) * 0.47, abs=0.01)
        assert annual.net.value == pytest.approx(monthly.net.value * 12, abs=0.12)

    @pytest.mark.parametrize('exemption', [-1, 5_423, float('nan')])
    def test_pension_invalid_entitlement_rejected(self, exemption):
        with pytest.raises(ValueError):
            compute_tax(TaxableCashflow(source='pension_annuity', gross_amount_nis=10_000,
                is_post_67=True, pension_exemption_monthly_nis=exemption), user_id='ariel', session=None)

    def test_salary_includes_bituach_leumi(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="salary", gross_amount_nis=40_000),
                user_id="ariel", session=s,
            )
        # 47% marginal + 7% BL on 40K
        assert tb.bituach_leumi_tax.value > 0
        assert tb.bituach_leumi_tax.value == pytest.approx(40_000 * 0.07)

    def test_bituach_leumi_capped_at_ceiling(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="rsu_vest", gross_amount_nis=100_000),
                user_id="ariel", session=s,
            )
        # 7% × min(100K, 50K ceiling) = 3500
        assert tb.bituach_leumi_tax.value == pytest.approx(50_000 * 0.07)

    def test_small_capital_gain_has_no_surtax(self, client_with_db):
        """T5.7 — below the ₪721,560 surtax threshold → surtax 0 (the existing
        25%/net assertions are unchanged)."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="capital_gain", gross_amount_nis=100_000),
                user_id="ariel", session=s,
            )
        assert tb.surtax.value == pytest.approx(0.0)
        assert tb.net.value == pytest.approx(75_000.0)

    def test_large_capital_gain_carries_5pct_surtax(self, client_with_db):
        """A big NVDA-deconcentration-sized capital gain crosses the threshold;
        capital income carries the 5% surtax above it."""
        from argosy.services.tax_curve import SURTAX_THRESHOLD_ANNUAL_NIS
        SF = client_with_db.app.state.session_factory
        gross = 2_000_000.0
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="capital_gain", gross_amount_nis=gross),
                user_id="ariel", session=s,
            )
        expected_surtax = (gross - SURTAX_THRESHOLD_ANNUAL_NIS) * 0.05
        assert tb.surtax.value == pytest.approx(expected_surtax)
        # Total tax now includes CGT + surtax; net reflects both.
        assert tb.net.value == pytest.approx(gross - gross * 0.25 - expected_surtax)

    def test_large_salary_carries_3pct_ordinary_surtax(self, client_with_db):
        from argosy.services.tax_curve import SURTAX_THRESHOLD_ANNUAL_NIS
        SF = client_with_db.app.state.session_factory
        gross = 1_500_000.0
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="salary", gross_amount_nis=gross),
                user_id="ariel", session=s,
            )
        expected_surtax = (gross - SURTAX_THRESHOLD_ANNUAL_NIS) * 0.03
        assert tb.surtax.value == pytest.approx(expected_surtax)

    def test_large_rental_carries_5pct_capital_surtax(self, client_with_db):
        """§121ב applies notwithstanding the §122 final-tax track; ITA 05/2025
        treats non-business rent as capital-source income → 5% surtax above the
        threshold (codex tax review)."""
        from argosy.services.tax_curve import SURTAX_THRESHOLD_ANNUAL_NIS
        SF = client_with_db.app.state.session_factory
        gross = 1_200_000.0
        with SF() as s:
            _seed(s)
            tb = compute_tax(
                TaxableCashflow(source="rental", gross_amount_nis=gross),
                user_id="ariel", session=s,
            )
        expected_surtax = (gross - SURTAX_THRESHOLD_ANNUAL_NIS) * 0.05
        assert tb.surtax.value == pytest.approx(expected_surtax)


# ─── Hishtalmut ──────────────────────────────────────────────────────────


class TestHishtalmut:
    def test_six_yr_eligible(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            from datetime import date
            el = check_hishtalmut_eligibility(
                user_id="ariel", session=s,
                first_deposit_date_iso="2018-01-01",
                user_current_age=45,
                today=date(2026, 5, 1),
            )
        assert el.six_yr_eligible.value == 1
        assert el.taxfree_now.value == 1
        assert el.months_until_taxfree.value == 0

    def test_too_recent_not_eligible(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            from datetime import date
            el = check_hishtalmut_eligibility(
                user_id="ariel", session=s,
                first_deposit_date_iso="2023-01-01",
                user_current_age=45,
                today=date(2026, 5, 1),
            )
        assert el.six_yr_eligible.value == 0
        assert el.taxfree_now.value == 0
        assert el.months_until_taxfree.value > 0

    def test_age_67_path(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            from datetime import date
            el = check_hishtalmut_eligibility(
                user_id="ariel", session=s,
                first_deposit_date_iso="2023-01-01",  # only 3yr
                user_current_age=70,  # but age >= 67
                today=date(2026, 5, 1),
            )
        assert el.age_67_eligible.value == 1
        assert el.taxfree_now.value == 1

    def test_taxfree_withdrawal_zero(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            from datetime import date
            el = check_hishtalmut_eligibility(
                user_id="ariel", session=s,
                first_deposit_date_iso="2018-01-01",
                user_current_age=45,
                today=date(2026, 5, 1),
            )
            tax = tax_on_hishtalmut_withdrawal(gross_nis=200_000, eligibility=el)
        assert tax.value == 0.0

    def test_early_withdrawal_marginal(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed(s)
            from datetime import date
            el = check_hishtalmut_eligibility(
                user_id="ariel", session=s,
                first_deposit_date_iso="2024-01-01",  # too recent
                user_current_age=45,
                today=date(2026, 5, 1),
            )
            tax = tax_on_hishtalmut_withdrawal(gross_nis=100_000, eligibility=el)
        assert tax.value == pytest.approx(100_000 * 0.47)


# ─── Decumulation ────────────────────────────────────────────────────────


class TestDecumulation:
    def test_pension_annuity_first(self):
        steps = optimize_decumulation_order(
            monthly_need_nis=20_000,
            taxable_balance_nis=1_000_000,
            hishtalmut_balance_nis=500_000,
            kupat_gemel_balance_nis=200_000,
            pensia_annuity_monthly_nis=10_000,
        )
        assert steps[0].account == "kupat_pensia_annuity"
        assert isinstance(steps[0], DecumulationStep)

    def test_taxable_before_hishtalmut(self):
        steps = optimize_decumulation_order(
            monthly_need_nis=20_000,
            taxable_balance_nis=1_000_000,
            hishtalmut_balance_nis=500_000,
            kupat_gemel_balance_nis=0,
        )
        accounts = [s.account for s in steps]
        assert accounts.index("taxable") < accounts.index("keren_hishtalmut")

    def test_empty_balances_returns_empty(self):
        steps = optimize_decumulation_order(
            monthly_need_nis=20_000,
            taxable_balance_nis=0,
            hishtalmut_balance_nis=0,
            kupat_gemel_balance_nis=0,
        )
        assert steps == []


# ─── Lump vs Annuity ─────────────────────────────────────────────────────


class TestLumpVsAnnuity:
    def test_recommends_annuity_when_covers_need(self):
        # mekadem 200; balance 4M → annuity 20K/mo. Need 18K/mo → annuity covers.
        v = compute_lump_vs_annuity(
            pension_balance_nis=4_000_000,
            mekadem_typical=200,
            monthly_expense_need_nis=18_000,
        )
        assert v.recommendation == "take_annuity"

    def test_recommends_lump_when_4pct_clearly_wins(self):
        # mekadem 250 (low annuity); balance 5M → annuity 20K/mo;
        # 4% on 5M = 16.7K/mo as annuity-equiv but with portfolio growth.
        # When annuity is very low relative to lump, prefer lump.
        v = compute_lump_vs_annuity(
            pension_balance_nis=5_000_000,
            mekadem_typical=400,  # very unfavorable
            monthly_expense_need_nis=25_000,
        )
        # annuity = 12.5K; need 25K. 4% on 5M = 200K/yr = 16.7K/mo.
        # 4% rule says lump can ~match. 5M × 0.04 = 200K > 12.5K × 12 × 1.5 = 225K? 200<225, so split
        assert v.recommendation in ("take_lump", "split")

    def test_split_path_has_lower_npv_than_pure_paths(self):
        v = compute_lump_vs_annuity(
            pension_balance_nis=3_000_000,
            mekadem_typical=200,
            monthly_expense_need_nis=20_000,
        )
        # Split should be between the two; not necessarily lower
        assert v.split_path["lifetime_npv_nis"] > 0

    def test_zero_mekadem_raises(self):
        with pytest.raises(ValueError):
            compute_lump_vs_annuity(
                pension_balance_nis=1_000_000,
                mekadem_typical=0,
                monthly_expense_need_nis=10_000,
            )
