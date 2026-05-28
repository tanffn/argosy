"""Hishtalmut (קרן השתלמות) eligibility + tax-aware unlock logic.

Closes MED #20 from the 2026-05-28 SDD review. The prior model treated
all hishtalmut withdrawals as "tax-free at age 60" — too permissive.

Israeli Income Tax Ordinance §3(e) tax-free rules (codex review fix):
  1. Employee-deposited hishtalmut: tax-free after 6 years from FIRST
     deposit (employee-specific timing rule).
  2. Self-employed hishtalmut: same 6-year rule with different
     aggregation across multiple funds.
  3. Age-67 lump path: tax-free regardless of holding period.
  4. Early withdrawal (none of the above): taxed at marginal income tax.

Each user's hishtalmut typically has one ``first_deposit_date`` per
fund; intake captures it. The eligibility check is:

  six_yr_eligible = (today - first_deposit_date) >= 6 years
  age_67_eligible = user_age >= 67
  taxfree = six_yr_eligible OR age_67_eligible

This module surfaces a ``HishtalmutEligibility`` so the UI can show:
  - countdown timer if not yet eligible
  - "EligibleNow" badge if past the threshold
  - tax cost if user withdraws today

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 5b.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.tax_engine import (
    DEFAULT_MARGINAL_TOP_RATE,
    _marginal_rate,
)


@dataclass(frozen=True)
class HishtalmutEligibility:
    months_until_taxfree: ValueWithRationale  # 0 if already eligible
    first_deposit_date: ValueWithRationale
    six_yr_eligible: ValueWithRationale
    age_67_eligible: ValueWithRationale
    taxfree_now: ValueWithRationale
    early_withdrawal_marginal_rate: ValueWithRationale


def check_hishtalmut_eligibility(
    *,
    user_id: str,
    session: Session,
    first_deposit_date_iso: str,
    user_current_age: int,
    today: date | None = None,
) -> HishtalmutEligibility:
    """Check the hishtalmut tax-free eligibility for a user.

    The user provides ``first_deposit_date_iso`` via intake (per-fund).
    """
    today = today or date.today()
    fd = date.fromisoformat(first_deposit_date_iso)
    months_since_first = (today.year - fd.year) * 12 + (today.month - fd.month)
    SIX_YEARS_MONTHS = 6 * 12

    six_yr_eligible = months_since_first >= SIX_YEARS_MONTHS
    age_67_eligible = user_current_age >= 67
    taxfree_now = six_yr_eligible or age_67_eligible

    months_until_taxfree = (
        0 if taxfree_now else max(0, SIX_YEARS_MONTHS - months_since_first)
    )

    marginal = _marginal_rate(user_id, session)

    return HishtalmutEligibility(
        months_until_taxfree=ValueWithRationale(
            value=months_until_taxfree,
            unit="months",
            source_id="hishtalmut_6yr_rule",
            rationale=(
                "0 = eligible now. Otherwise months remaining until the "
                "6-year-from-first-deposit threshold is met."
            ),
            confidence="high",
        ),
        first_deposit_date=ValueWithRationale(
            value=first_deposit_date_iso,
            unit="date",
            source_id=None,
            rationale="First-deposit date provided via intake.",
            confidence="high",
        ),
        six_yr_eligible=ValueWithRationale(
            value=int(six_yr_eligible),
            unit="boolean",
            source_id="hishtalmut_6yr_rule",
            rationale=(
                f"True if 6+ years have passed since first deposit "
                f"({months_since_first}mo elapsed; threshold {SIX_YEARS_MONTHS}mo)."
            ),
        ),
        age_67_eligible=ValueWithRationale(
            value=int(age_67_eligible),
            unit="boolean",
            source_id="hishtalmut_6yr_rule",
            rationale=(
                f"True if user is at or past 67 (currently {user_current_age})."
            ),
        ),
        taxfree_now=ValueWithRationale(
            value=int(taxfree_now),
            unit="boolean",
            source_id="hishtalmut_6yr_rule",
            rationale=(
                "Tax-free if EITHER 6-yr-from-first-deposit OR age-67 path is met."
            ),
        ),
        early_withdrawal_marginal_rate=ValueWithRationale(
            value=marginal,
            unit="fraction",
            source_id="argosy_derived",
            rationale=(
                f"Marginal income tax rate applied to early hishtalmut "
                f"withdrawals if neither tax-free path is met."
            ),
        ),
    )


def tax_on_hishtalmut_withdrawal(
    *,
    gross_nis: float,
    eligibility: HishtalmutEligibility,
) -> ValueWithRationale:
    """Return the tax due on a hishtalmut withdrawal.

    Zero if tax-free; gross × marginal otherwise.
    """
    if eligibility.taxfree_now.value:
        return ValueWithRationale(
            value=0.0,
            unit="NIS",
            source_id="hishtalmut_6yr_rule",
            rationale=(
                "Withdrawal is tax-free under §3(e) — either 6-year rule "
                "or age-67 path satisfied."
            ),
            confidence="high",
        )
    marginal = float(eligibility.early_withdrawal_marginal_rate.value or DEFAULT_MARGINAL_TOP_RATE)
    tax = gross_nis * marginal
    return ValueWithRationale(
        value=round(tax, 2),
        unit="NIS",
        source_id="argosy_derived",
        rationale=(
            f"Early withdrawal: gross ₪{gross_nis:,.0f} × marginal "
            f"{marginal*100:.0f}%. Wait {eligibility.months_until_taxfree.value} "
            "more months to claim tax-free."
        ),
        confidence="medium",
    )
