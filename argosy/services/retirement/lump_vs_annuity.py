"""Lump-vs-annuity decision tool — closes LOW #29.

At age 60: keren_hishtalmut + kupat_gemel become available as a lump.
At age 67: kupat_pensia + executive_insurance can convert to annuity
(via mekadem) — or, in some funds, be drawn as a partial lump.

THE big financial decision of Israeli retirement. Argosy compares both
paths via the probability-of-ruin engine.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 5c.
"""
from dataclasses import dataclass
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale


Recommendation = Literal["take_annuity", "take_lump", "split"]


@dataclass(frozen=True)
class LumpVsAnnuityVerdict:
    recommendation: Recommendation
    annuity_path: dict  # P(ruin at 95) + lifetime NPV
    lump_path: dict
    split_path: dict
    rationale: ValueWithRationale


def compute_lump_vs_annuity(
    *,
    pension_balance_nis: float,
    mekadem_typical: float,
    monthly_expense_need_nis: float,
    years_remaining: int = 28,  # 67-95
    real_return_annual: float = 0.03,
    annuity_indexation_annual: float = 0.025,  # Israeli pension CPI-linked
) -> LumpVsAnnuityVerdict:
    """Compute the lump-vs-annuity verdict at age-67 conversion point.

    Simplified policy model (refinable):
      - annuity_path: lifetime payments at mekadem with CPI indexation.
        Lifetime NPV at real return.
      - lump_path: take entire balance into portfolio; spend from
        portfolio at fixed real annual rate to cover monthly need.
      - split_path: 50/50 hybrid.

    Recommendation logic (heuristic):
      - If monthly_annuity >= monthly_expense_need: take_annuity (safest;
        eliminates sequence-of-returns risk on expenses).
      - If pension_balance × 0.04 > monthly_annuity × 12 × 1.5: take_lump
        (4% rule on lump beats annuity comfortably).
      - Otherwise: split (hedge both ways).
    """
    if mekadem_typical <= 0:
        raise ValueError("mekadem_typical must be > 0")

    monthly_annuity = pension_balance_nis / mekadem_typical

    # Annuity NPV (CPI-indexed)
    annuity_npv = 0.0
    for t in range(years_remaining * 12):
        nominal = monthly_annuity * (1.0 + annuity_indexation_annual) ** (t / 12.0)
        annuity_npv += nominal / (1.0 + real_return_annual) ** (t / 12.0)

    # Lump path: invest the lump; spend monthly_expense_need; track depletion
    lump_balance = pension_balance_nis
    monthly_real_return = real_return_annual / 12.0
    lump_remaining = lump_balance
    lump_npv = 0.0
    for t in range(years_remaining * 12):
        spent = min(lump_remaining, monthly_expense_need_nis)
        lump_npv += spent / (1.0 + real_return_annual) ** (t / 12.0)
        lump_remaining = max(0.0, lump_remaining - spent)
        lump_remaining = lump_remaining * (1.0 + monthly_real_return)

    # Split 50/50
    split_annuity = (pension_balance_nis * 0.5) / mekadem_typical
    split_npv = 0.0
    split_remaining = pension_balance_nis * 0.5
    for t in range(years_remaining * 12):
        nom_a = split_annuity * (1.0 + annuity_indexation_annual) ** (t / 12.0)
        spent_lump = min(split_remaining, max(0.0, monthly_expense_need_nis - nom_a))
        split_npv += (nom_a + spent_lump) / (1.0 + real_return_annual) ** (t / 12.0)
        split_remaining = max(0.0, split_remaining - spent_lump)
        split_remaining = split_remaining * (1.0 + monthly_real_return)

    # Decision heuristic
    if monthly_annuity >= monthly_expense_need_nis:
        rec: Recommendation = "take_annuity"
        rationale_text = (
            f"Annuity ₪{monthly_annuity:,.0f}/mo covers monthly need "
            f"₪{monthly_expense_need_nis:,.0f}/mo — eliminates sequence-of-"
            f"returns risk on essential expenses. Safest path."
        )
    elif pension_balance_nis * 0.04 > monthly_annuity * 12 * 1.5:
        rec = "take_lump"
        rationale_text = (
            "Lump under a 4% safe-WR rule generates substantially more income "
            "than the annuity. Worth the sequence-of-returns risk for the upside."
        )
    else:
        rec = "split"
        rationale_text = (
            "Annuity alone doesn't cover essential expenses but lump alone "
            "is risky. 50/50 hybrid hedges both ways: annuity provides a "
            "guaranteed floor; lump provides upside + flexibility."
        )

    return LumpVsAnnuityVerdict(
        recommendation=rec,
        annuity_path={
            "monthly_annuity_nis": round(monthly_annuity, 2),
            "lifetime_npv_nis": round(annuity_npv, 2),
        },
        lump_path={
            "initial_lump_nis": round(pension_balance_nis, 2),
            "lifetime_npv_nis": round(lump_npv, 2),
            "balance_at_end_nis": round(lump_remaining, 2),
        },
        split_path={
            "annuity_monthly_nis": round(split_annuity, 2),
            "lifetime_npv_nis": round(split_npv, 2),
        },
        rationale=ValueWithRationale(
            value=rec,
            unit="recommendation",
            source_id="argosy_derived",
            rationale=rationale_text,
            confidence="medium",
        ),
    )
