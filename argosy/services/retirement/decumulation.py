"""Decumulation order optimizer — which account to draw from first.

Closes LOW #30 from the 2026-05-28 SDD review. Conventional wisdom +
empirical research both support a fixed order under most Israeli tax
profiles:

  Step 1: Taxable accounts first (capital gains 25%, allow tax-deferred
          accounts to keep compounding).
  Step 2: Tax-deferred (kupat_pensia / executive_insurance, only at age
          67 via annuity per Israeli rules; available before only as
          early lump with marginal tax).
  Step 3: Tax-free last (hishtalmut after 6yr / age 67 path; the longer
          held the more compounding).

Modifiers:
  - If a tax-loss-harvesting opportunity exists in taxable → realize it
    before drawing from anywhere else.
  - If user is in a low-income year (e.g., retired and waiting for
    annuity), bracket-arbitrage: realize taxable gains at the lower
    marginal bracket.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 5c.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class DecumulationStep:
    order: int
    account: str
    monthly_draw_nis: ValueWithRationale
    rationale: str


def optimize_decumulation_order(
    *,
    monthly_need_nis: float,
    taxable_balance_nis: float,
    hishtalmut_balance_nis: float,
    kupat_gemel_balance_nis: float,
    pensia_annuity_monthly_nis: float = 0.0,  # post-67 only
) -> list[DecumulationStep]:
    """Generate the per-month draw plan respecting Israeli tax order.

    Returns an ordered list of steps. First step's monthly_draw_nis is
    the most-tax-efficient source up to the user's monthly need;
    subsequent steps cover the residual.
    """
    steps: list[DecumulationStep] = []
    residual = monthly_need_nis

    # 0) Pension annuity (post-67) — always first if present (free income
    #    in the sense that it's already-converted)
    if pensia_annuity_monthly_nis > 0:
        draw = min(residual, pensia_annuity_monthly_nis)
        steps.append(DecumulationStep(
            order=0,
            account="kupat_pensia_annuity",
            monthly_draw_nis=ValueWithRationale(
                value=round(draw, 2),
                unit="NIS/mo",
                source_id="israeli_tax_authority_pension_exemption_2025",
                rationale=(
                    "Pension annuity flows automatically — counts toward "
                    "monthly need first. Taxed via rights-fixation regime "
                    "(57%+ exempt in 2025)."
                ),
            ),
            rationale="Already-converted pension annuity is the cheapest source.",
        ))
        residual = max(0.0, residual - draw)

    # 1) Taxable accounts first (CGT 25% only on the GAIN portion;
    #    cost-basis return is tax-free).
    if residual > 0 and taxable_balance_nis > 0:
        draw = min(residual, taxable_balance_nis / 240)  # spread over 20y nominally
        steps.append(DecumulationStep(
            order=1,
            account="taxable",
            monthly_draw_nis=ValueWithRationale(
                value=round(draw, 2),
                unit="NIS/mo",
                source_id="israeli_tax_authority_cgt_2026",
                rationale=(
                    "Taxable accounts first — only the GAIN portion is "
                    "taxed (25% CGT); cost-basis comes back tax-free. "
                    "Letting tax-deferred + tax-free accounts keep "
                    "compounding is the standard decumulation play."
                ),
            ),
            rationale="Draw taxable assets first to let tax-advantaged accounts compound.",
        ))
        residual = max(0.0, residual - draw)

    # 2) Kupat gemel next (post-2008 contributions taxed similarly to
    #    hishtalmut; pre-2008 different treatment).
    if residual > 0 and kupat_gemel_balance_nis > 0:
        draw = min(residual, kupat_gemel_balance_nis / 120)  # 10y
        steps.append(DecumulationStep(
            order=2,
            account="kupat_gemel",
            monthly_draw_nis=ValueWithRationale(
                value=round(draw, 2),
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale=(
                    "Kupat gemel after taxable; per-vehicle rules apply "
                    "(pre-2008 vs post-2008 contributions). At age 60+ "
                    "available as lump; tax-treatment varies."
                ),
            ),
            rationale="Kupat gemel after taxable; gentler than full marginal.",
        ))
        residual = max(0.0, residual - draw)

    # 3) Hishtalmut last (tax-free if 6yr+ held or age 67+).
    if residual > 0 and hishtalmut_balance_nis > 0:
        draw = min(residual, hishtalmut_balance_nis / 120)
        steps.append(DecumulationStep(
            order=3,
            account="keren_hishtalmut",
            monthly_draw_nis=ValueWithRationale(
                value=round(draw, 2),
                unit="NIS/mo",
                source_id="hishtalmut_6yr_rule",
                rationale=(
                    "Hishtalmut last — tax-free if 6yr-from-first-deposit "
                    "OR age 67 path satisfied. The longer it compounds, "
                    "the larger the tax-free pile."
                ),
            ),
            rationale="Hishtalmut tax-free; draw last to maximize compounding.",
        ))
        residual = max(0.0, residual - draw)

    return steps
