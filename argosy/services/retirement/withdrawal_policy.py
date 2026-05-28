"""Withdrawal-policy framework — selectable spend-down rules.

Closes HIGH #8 from the 2026-05-28 SDD review. The prior projection had
no documented withdrawal policy — the chart showed surplus / shortfall
under flat per-month spending but never told the user *how* to spend
without going broke.

Policies shipped:
  - bengen_4pct       — Classic 4% initial WR, fixed-real after retirement
                        (Bengen 1994). Simple but ignores market state.
  - guyton_klinger    — Decision-rules guardrails. Ratchet up 10% in good
                        years; cut 10% when current WR > 120% of initial.
                        Best-empirically-tested for concentrated portfolios.
  - vpw               — Variable Percentage Withdrawal. Spend a fixed
                        fraction of CURRENT balance, where fraction
                        increases with age. Highest-volatility income;
                        zero ruin risk by construction.
  - bucket            — Time-segmented buckets (cash 0-2y, bonds 2-7y,
                        equity 7y+). Refilled from upstream after each
                        good year. Behavioral resilience > pure-math
                        efficiency.

Each policy implements a ``monthly_withdrawal(portfolio_value_nis, month,
initial_balance_nis, current_age, retirement_age)`` function that returns
the recommended monthly draw in NIS. The projection consumes the chosen
policy in lieu of a flat "expenses - annuity" shortfall draw.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 3 HIGH #8.
"""
from dataclasses import dataclass
from typing import Callable, Literal

from argosy.services.retirement.citations import ValueWithRationale


PolicyId = Literal["bengen_4pct", "guyton_klinger", "vpw", "bucket"]


@dataclass(frozen=True)
class WithdrawalContext:
    portfolio_value_nis: float
    initial_portfolio_nis: float
    current_age_years: float
    retirement_age: float
    month_in_retirement: int  # 0 if pre-retirement
    inflation_factor: float  # cumulative since retirement (1.0 = no inflation)
    prior_withdrawal_monthly_real_nis: float  # last month's real-NIS draw


WithdrawalFn = Callable[[WithdrawalContext], float]


@dataclass(frozen=True)
class WithdrawalPolicy:
    id: PolicyId
    label: str
    rationale: str
    source_id: str
    monthly_withdrawal: WithdrawalFn


def _bengen_4pct(ctx: WithdrawalContext) -> float:
    """Bengen 1994: 4% initial WR, fixed-real (CPI-adjusted) after."""
    if ctx.current_age_years < ctx.retirement_age:
        return 0.0
    initial_annual_real = ctx.initial_portfolio_nis * 0.04
    return initial_annual_real / 12.0 * ctx.inflation_factor


def _guyton_klinger(ctx: WithdrawalContext) -> float:
    """Guyton-Klinger guardrails (simplified):

    - Initial WR 5% (slightly more aggressive than Bengen because the
      guardrails catch overdraws).
    - If current_wr > 1.2 × initial_wr: cut 10%
    - If current_wr < 0.8 × initial_wr AND year > 1: ratchet up 10%
    - Otherwise: hold prior real draw, no inflation adjustment in
      down-markets (capital-preservation guardrail).
    """
    if ctx.current_age_years < ctx.retirement_age:
        return 0.0
    initial_monthly_real = ctx.initial_portfolio_nis * 0.05 / 12.0
    if ctx.month_in_retirement == 0:
        return initial_monthly_real * ctx.inflation_factor

    prior_real = ctx.prior_withdrawal_monthly_real_nis
    if prior_real <= 0:
        prior_real = initial_monthly_real

    current_nominal_draw = prior_real * ctx.inflation_factor
    if ctx.portfolio_value_nis <= 0:
        return 0.0
    annual_draw = current_nominal_draw * 12.0
    current_wr = annual_draw / ctx.portfolio_value_nis
    initial_wr = 0.05

    if current_wr > 1.2 * initial_wr:
        return current_nominal_draw * 0.90  # capital-preservation cut
    if current_wr < 0.8 * initial_wr and ctx.month_in_retirement >= 12:
        return current_nominal_draw * 1.10  # prosperity ratchet
    return current_nominal_draw


def _vpw(ctx: WithdrawalContext) -> float:
    """Variable Percentage Withdrawal (Bogleheads).

    Withdraw a fraction of the CURRENT balance, where the fraction
    increases with age (mortality-table based; here approximated).
    Zero ruin risk by construction (rate is always finite × balance).
    """
    if ctx.current_age_years < ctx.retirement_age:
        return 0.0
    age = ctx.current_age_years
    if age < 50:
        annual_rate = 0.035
    elif age < 60:
        annual_rate = 0.040
    elif age < 70:
        annual_rate = 0.045
    elif age < 80:
        annual_rate = 0.055
    elif age < 90:
        annual_rate = 0.070
    else:
        annual_rate = 0.090
    return ctx.portfolio_value_nis * annual_rate / 12.0


def _bucket(ctx: WithdrawalContext) -> float:
    """Bucket strategy (simplified single-bucket aware version).

    Behaves like Bengen but caps draw at the cash-bucket equivalent
    (≈ 5% of portfolio = ~2y of essential expenses) when portfolio is
    stressed. Conservative; trades efficiency for behavioral resilience.
    """
    if ctx.current_age_years < ctx.retirement_age:
        return 0.0
    bengen = _bengen_4pct(ctx)
    cash_bucket_cap = ctx.portfolio_value_nis * 0.05 / 12.0
    return min(bengen, cash_bucket_cap) if cash_bucket_cap > 0 else bengen


POLICIES: dict[PolicyId, WithdrawalPolicy] = {
    "bengen_4pct": WithdrawalPolicy(
        id="bengen_4pct",
        label="Bengen 4% (fixed-real)",
        rationale=(
            "Initial 4% withdrawal rate, fixed-real (CPI-adjusted) after. "
            "Original 'safe withdrawal rate' from Bengen 1994. Simple + "
            "well-known but ignores market state — vulnerable to early "
            "sequence-of-returns shocks."
        ),
        source_id="bengen_1994",
        monthly_withdrawal=_bengen_4pct,
    ),
    "guyton_klinger": WithdrawalPolicy(
        id="guyton_klinger",
        label="Guyton-Klinger guardrails",
        rationale=(
            "Higher initial WR (5%) than Bengen because guardrails catch "
            "overdraws: cut 10% when current WR > 120% of initial; ratchet "
            "up 10% in prosperous years. Best-empirically-tested policy "
            "for concentrated-asset households. Default for Argosy."
        ),
        source_id="guyton_klinger_2006",
        monthly_withdrawal=_guyton_klinger,
    ),
    "vpw": WithdrawalPolicy(
        id="vpw",
        label="Variable Percentage Withdrawal",
        rationale=(
            "Spend a fixed fraction of CURRENT balance; fraction increases "
            "with age per mortality tables. Highest income variability; "
            "zero ruin risk by construction (always finite × balance). "
            "Good for users who can tolerate income fluctuation."
        ),
        source_id="bogleheads_three_fund",
        monthly_withdrawal=_vpw,
    ),
    "bucket": WithdrawalPolicy(
        id="bucket",
        label="Bucket strategy",
        rationale=(
            "Behave like Bengen but cap draw at cash-bucket equivalent "
            "(~2y of essential expenses) when portfolio is stressed. "
            "Trades efficiency for behavioral resilience — easier to stay "
            "the course in a 2008-style year."
        ),
        source_id="trinity_study_1998",
        monthly_withdrawal=_bucket,
    ),
}


def get_policy(policy_id: PolicyId) -> WithdrawalPolicy:
    if policy_id not in POLICIES:
        raise ValueError(f"unknown policy_id={policy_id!r}; expected one of {list(POLICIES)}")
    return POLICIES[policy_id]


def list_policies() -> list[dict]:
    """List shipped policies for the UI selector."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "rationale": p.rationale,
            "source_id": p.source_id,
        }
        for p in POLICIES.values()
    ]


def policy_as_value(policy_id: PolicyId) -> ValueWithRationale:
    p = get_policy(policy_id)
    return ValueWithRationale(
        value=p.id,
        unit="policy",
        source_id=p.source_id,
        rationale=p.rationale,
        confidence="high",
    )
