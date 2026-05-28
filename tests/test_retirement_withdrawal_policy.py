"""Tests for the withdrawal-policy framework (Wave 3 · HIGH #8)."""
import pytest

from argosy.services.retirement.withdrawal_policy import (
    POLICIES,
    WithdrawalContext,
    get_policy,
    list_policies,
    policy_as_value,
)


def _ctx(
    *,
    portfolio: float = 5_000_000.0,
    initial: float = 5_000_000.0,
    age: float = 67.0,
    retire_age: float = 49.0,
    month: int = 0,
    inflation_factor: float = 1.0,
    prior: float = 0.0,
) -> WithdrawalContext:
    return WithdrawalContext(
        portfolio_value_nis=portfolio,
        initial_portfolio_nis=initial,
        current_age_years=age,
        retirement_age=retire_age,
        month_in_retirement=month,
        inflation_factor=inflation_factor,
        prior_withdrawal_monthly_real_nis=prior,
    )


class TestPolicyRegistry:
    def test_all_four_policies_registered(self):
        ids = set(POLICIES.keys())
        assert ids == {"bengen_4pct", "guyton_klinger", "vpw", "bucket"}

    def test_list_policies_has_required_fields(self):
        items = list_policies()
        assert len(items) == 4
        for item in items:
            assert {"id", "label", "rationale", "source_id"} <= set(item.keys())

    def test_get_unknown_policy_raises(self):
        with pytest.raises(ValueError, match="unknown policy_id"):
            get_policy("nonexistent")  # type: ignore[arg-type]


class TestBengen:
    def test_pre_retirement_zero_withdrawal(self):
        p = get_policy("bengen_4pct")
        # age 40, retire at 49 → no draw
        ctx = _ctx(age=40.0)
        assert p.monthly_withdrawal(ctx) == 0.0

    def test_at_retirement_4pct_initial(self):
        p = get_policy("bengen_4pct")
        ctx = _ctx(portfolio=5_000_000, initial=5_000_000, age=49.0, retire_age=49.0)
        # 5M × 0.04 / 12 = ~16,667 NIS/mo
        assert p.monthly_withdrawal(ctx) == pytest.approx(16_666.67, rel=0.01)

    def test_inflation_adjusted_after_retirement(self):
        p = get_policy("bengen_4pct")
        ctx = _ctx(age=50.0, inflation_factor=1.025)
        # initial 4% × 1.025 inflation = 17,083/mo
        assert p.monthly_withdrawal(ctx) == pytest.approx(16_666.67 * 1.025, rel=0.01)


class TestGuytonKlinger:
    def test_initial_5pct_at_retirement(self):
        p = get_policy("guyton_klinger")
        ctx = _ctx(portfolio=5_000_000, initial=5_000_000, age=49.0, retire_age=49.0, month=0)
        # 5% / 12 of 5M = 20,833 NIS/mo
        assert p.monthly_withdrawal(ctx) == pytest.approx(20_833.33, rel=0.01)

    def test_overdraw_triggers_10pct_cut(self):
        p = get_policy("guyton_klinger")
        # Portfolio crashed 50%; current WR is now 10% of remaining → cut 10%
        ctx = _ctx(
            portfolio=2_500_000,
            initial=5_000_000,
            age=51.0,
            month=24,
            prior=20_833.33,
            inflation_factor=1.05,
        )
        draw = p.monthly_withdrawal(ctx)
        # Current nominal draw = 20833 × 1.05 = 21875; cut by 10% → 19687
        assert draw == pytest.approx(21_875.0 * 0.90, rel=0.01)

    def test_prosperity_ratchet_after_year_one(self):
        p = get_policy("guyton_klinger")
        # Portfolio grew; current WR < 80% of initial → ratchet up
        ctx = _ctx(
            portfolio=10_000_000,  # doubled
            initial=5_000_000,
            age=51.0,
            month=24,
            prior=20_833.33,
            inflation_factor=1.02,
        )
        draw = p.monthly_withdrawal(ctx)
        assert draw == pytest.approx(20_833.33 * 1.02 * 1.10, rel=0.01)


class TestVPW:
    def test_rate_increases_with_age(self):
        p = get_policy("vpw")
        ctx_70 = _ctx(portfolio=5_000_000, age=70.0, retire_age=49.0)
        ctx_85 = _ctx(portfolio=5_000_000, age=85.0, retire_age=49.0)
        assert p.monthly_withdrawal(ctx_85) > p.monthly_withdrawal(ctx_70)

    def test_zero_when_portfolio_zero(self):
        p = get_policy("vpw")
        ctx = _ctx(portfolio=0, age=75.0)
        assert p.monthly_withdrawal(ctx) == 0.0


class TestBucket:
    def test_caps_at_cash_bucket_when_stressed(self):
        p = get_policy("bucket")
        # Small portfolio: cash-bucket cap of 5%/12 = 0.4%/mo
        ctx = _ctx(portfolio=500_000, initial=5_000_000, age=49.0, retire_age=49.0)
        bengen_eq = 5_000_000 * 0.04 / 12  # ≈ 16,667
        cash_cap = 500_000 * 0.05 / 12  # ≈ 2,083
        assert p.monthly_withdrawal(ctx) == pytest.approx(cash_cap, rel=0.01)
        assert p.monthly_withdrawal(ctx) < bengen_eq


class TestPolicyAsValue:
    def test_returns_value_with_rationale(self):
        v = policy_as_value("guyton_klinger")
        assert v.value == "guyton_klinger"
        assert v.unit == "policy"
        assert v.source_id == "guyton_klinger_2006"
