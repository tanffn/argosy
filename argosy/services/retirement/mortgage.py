"""Mortgage amortization schedule (MED #17).

Standard fixed-rate amortization:
  monthly_payment = principal × r × (1+r)^n / ((1+r)^n - 1)
where r = annual_rate / 12, n = term_months.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class MortgageScheduleRow:
    month: int
    payment_nis: ValueWithRationale
    principal_paid_nis: ValueWithRationale
    interest_paid_nis: ValueWithRationale
    remaining_balance_nis: ValueWithRationale


def build_mortgage_schedule(
    *,
    initial_balance_nis: float,
    annual_rate: float,
    term_months: int,
    start_month: int = 0,
) -> list[MortgageScheduleRow]:
    """Standard fixed-rate amortization schedule."""
    if initial_balance_nis <= 0 or term_months <= 0:
        return []
    r = annual_rate / 12.0
    if r > 0:
        payment = (
            initial_balance_nis * r * (1 + r) ** term_months
            / ((1 + r) ** term_months - 1)
        )
    else:
        payment = initial_balance_nis / term_months

    schedule: list[MortgageScheduleRow] = []
    balance = initial_balance_nis
    for m in range(term_months):
        interest = balance * r
        principal = payment - interest
        balance = max(0.0, balance - principal)
        schedule.append(MortgageScheduleRow(
            month=start_month + m,
            payment_nis=ValueWithRationale(
                value=round(payment, 2), unit="NIS/mo", source_id=None,
                rationale="Fixed monthly payment over term.",
            ),
            principal_paid_nis=ValueWithRationale(
                value=round(principal, 2), unit="NIS/mo", source_id=None,
                rationale="Principal portion this month.",
            ),
            interest_paid_nis=ValueWithRationale(
                value=round(interest, 2), unit="NIS/mo", source_id=None,
                rationale="Interest portion this month.",
            ),
            remaining_balance_nis=ValueWithRationale(
                value=round(balance, 2), unit="NIS", source_id=None,
                rationale="Remaining principal after this month's payment.",
            ),
        ))
    return schedule


def payoff_month(
    *,
    initial_balance_nis: float,
    annual_rate: float,
    term_months: int,
) -> int:
    """Return the month index when mortgage hits zero (== term_months)."""
    if initial_balance_nis <= 0:
        return 0
    return term_months
