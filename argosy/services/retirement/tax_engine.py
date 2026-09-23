"""Account-aware tax engine for Israeli retirement cashflows.

Closes BLOCKER #2 from the 2026-05-28 SDD review. Replaces the prior flat
``tax_rate`` slider (0-50%) with per-source, per-account tax computation
honoring Israeli tax rules.

Per-source rules (corrected per codex review):

  - capital_gain (taxable equity): flat 25% Israeli CGT per
    ``israeli_tax_authority_cgt_2026``.

  - dividend_us_source (eligible Israeli-resident individual, ordinary dividend,
    valid W-8BEN): treaty withholding 25% at US
    source; Israeli tax 25% on gross; foreign-tax-credit reduces Israeli
    liability:
        israeli_tax_due = max(0, 0.25 * gross - 0.25 * us_gross)
        net = gross - us_withholding - israeli_tax_due - applicable_surtax
    Assumes the base-tax credit is fully usable; not an account withholding
    observation, surtax-credit ruling, REIT/QIE or exempt RIC-distribution model.
    Per operative Article 12(2)(a) and IRS Treaty Table 1 (Israel), checked
    2026-09-12: https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf .

  - dividend_israeli_source: flat 25% Israeli withholding at source.

  - pension_annuity (kupat_pensia post-67): rights-fixation regime per
    ``israeli_tax_authority_pension_exemption_2025``. Exemption envelope:
    Maximum 57% in 2025, 57.5% in 2026, 62.5% in 2027 and 67% from
    2028, applied to the qualifying-pension CEILING, not the whole annuity.
    Personal usable exemption must be supplied; otherwise no exemption is
    assumed. Marginal-rate estimate, not a full personal tax return.

  - hishtalmut_lump_taxfree / hishtalmut_lump_taxable: handled by the
    Wave 5b hishtalmut module per eligibility.

  - kupat_gemel_lump: handled by Wave 5b gemel module (pre-2008 vs
    post-2008 splits).

  - salary / rsu_vest: marginal income tax (47% top bracket) + bituach
    leumi (capped at the insurable ceiling).

Default marginal rate: 47% (top bracket for high earners; user-overridable
via identity_yaml.retirement_reference_overrides.tax.marginal_top_rate).

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 5a.
"""
from dataclasses import dataclass
import math
from typing import Literal

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import resolve
from argosy.services.tax_curve import (
    ISRAELI_CGT_RATE,
    SURTAX_THRESHOLD_ANNUAL_NIS as SURTAX_THRESHOLD_ANNUAL_NIS,  # backwards-compatible re-export
    annual_surtax,
)


Source = Literal[
    "capital_gain",
    "dividend_us_source",
    "dividend_israeli_source",
    "pension_annuity",
    "salary",
    "rsu_vest",
    "interest",
    "rental",
]

Account = Literal[
    "taxable",
    "kupat_pensia",
    "keren_hishtalmut",
    "kupat_gemel",
    "executive_insurance",
]


@dataclass(frozen=True)
class TaxableCashflow:
    source: Source
    gross_amount_nis: float
    account: Account = "taxable"
    holding_years: int = 0
    user_age: int = 40
    us_gross_amount_for_treaty: float | None = None  # None = full ordinary US dividend gross
    is_post_67: bool = False  # for pension_annuity rights-fixation
    pension_period_months: int = 1  # pension gross is monthly unless explicitly annual (12)
    pension_exemption_monthly_nis: float = 0.0  # usable personal entitlement; no inference from age


# ITA 2026 161d guide and January 2026 withholding booklet, checked 2026-09-12.
# https://www.gov.il/BlobFolder/guide/2026-filling-out-form-161d/he/Guides_IncomeTax_filling-out-form-161d-2026.pdf
_PENSION_EXEMPTION_BY_YEAR: dict[int, float] = {
    2024: 0.52, 2025: 0.57, 2026: 0.575, 2027: 0.625,
}
PENSION_QUALIFYING_CEILING_2026_MONTHLY_NIS = 9_430.0


def _pension_exemption_rate(year: int) -> float:
    """Return the exemption fraction of pension qualifying income at the year."""
    if year < 2024:
        raise ValueError('Historical pension exemption schedule before 2024 is not supported')
    if year >= 2028:
        return 0.67  # max under current ITA phasing
    return _PENSION_EXEMPTION_BY_YEAR[year]


# Default top marginal Israeli income tax bracket for high earners.
DEFAULT_MARGINAL_TOP_RATE = 0.47
# ISRAELI_CGT_RATE + the surtax constants are sourced from ``tax_curve`` — the
# single tax-band source (T5.7) — and imported above, so the 25% CGT can't
# drift between the calculator, the deterministic path and the MC.
US_DIVIDEND_TREATY_RATE = 0.25
# Bituach leumi insurable ceiling — applies to salary + RSU; surplus uninsured.
DEFAULT_BL_CEILING_NIS_MONTHLY = 50_000.0
DEFAULT_BL_RATE = 0.07  # employee portion ~7% (simplified; depends on bracket)


@dataclass(frozen=True)
class TaxBreakdown:
    gross: ValueWithRationale
    net: ValueWithRationale
    israeli_tax: ValueWithRationale
    us_treaty_credit: ValueWithRationale  # 0 unless US-source dividends
    us_withholding: ValueWithRationale  # cash tax paid to US; not additional to the credit
    bituach_leumi_tax: ValueWithRationale
    surtax: ValueWithRationale  # mas yesef on income above the annual threshold
    effective_rate: ValueWithRationale


# Surtax (mas yesef) source classification: capital/passive income carries the
# higher 5% rate above the threshold; salary/RSU/pension carry the ordinary 3%.
# §122 rental IS in the surtax base at the capital rate: §121ב applies
# notwithstanding the §122 final-tax track, and ITA instruction 05/2025 treats
# non-business rent as capital-source income subject to the 2% capital surcharge
# (codex tax review, ITA 05/2025). NOTE: for ``capital_gain`` the cashflow's
# gross_amount_nis is the realized TAXABLE GAIN (the engine taxes the whole of
# it at 25%), so the surtax is correctly levied on the gain — not on sale
# proceeds and not on a pre-gain-fraction figure (the 0.6 gain fraction lives
# only in the MC's withdrawal gross-up, a different code path).
_SURTAX_CAPITAL_SOURCES = frozenset({
    "capital_gain", "dividend_us_source", "dividend_israeli_source", "interest",
    "rental",
})
_SURTAX_ORDINARY_SOURCES = frozenset({"salary", "rsu_vest", "pension_annuity"})


def compute_tax(
    cashflow: TaxableCashflow,
    *,
    user_id: str,
    session: Session,
    year: int = 2026,
    apply_surtax: bool = True,
) -> TaxBreakdown:
    """Returns full tax breakdown — gross, net, per-component taxes (incl.
    surtax), effective_rate — for a single Israeli-resident cashflow.

    Surtax note (T5.7): mas yesef is an ANNUAL tax on income above ~₪721,560.
    The calculator is single-cashflow, so it applies the surtax to THIS
    cashflow treated as the marginal income above the threshold — exact for a
    dominant one-off event (a large RSU vest or NVDA deconcentration sale),
    a lower bound when other income already fills the threshold. Set
    ``apply_surtax=False`` to suppress."""
    src = cashflow.source
    gross = max(0.0, cashflow.gross_amount_nis)

    israeli_tax = 0.0
    us_credit = 0.0
    us_withholding = 0.0
    bl_tax = 0.0

    if src == "capital_gain":
        israeli_tax = gross * ISRAELI_CGT_RATE
        rationale = "Israeli CGT flat 25% on equity capital gains."
        source_id = "israeli_tax_authority_cgt_2026"

    elif src == "dividend_us_source":
        # Ordinary dividend only, with treaty eligibility and usable base FTC.
        us_gross = gross if cashflow.us_gross_amount_for_treaty is None else cashflow.us_gross_amount_for_treaty
        if not math.isfinite(cashflow.gross_amount_nis) or cashflow.gross_amount_nis < 0 or not math.isfinite(us_gross) or not 0 <= us_gross <= gross:
            raise ValueError('Dividend gross and US treaty gross must be finite, nonnegative, and US gross cannot exceed total gross')
        us_withholding = us_gross * US_DIVIDEND_TREATY_RATE
        israeli_gross = gross * ISRAELI_CGT_RATE
        us_credit = min(us_withholding, israeli_gross)
        israeli_tax = max(0.0, israeli_gross - us_credit)
        rationale = (
            "Ordinary US dividend, eligible Israeli individual with valid W-8BEN: "
            "25% projected US withholding, assumed fully creditable against the "
            "25% Israeli base tax. US tax still reduces net cash. "
            "Exempt fund distributions, actual broker withholding and taxpayer-specific "
            "credit/surtax treatment require separate evidence."
        )
        source_id = "us_israel_tax_treaty"

    elif src == "dividend_israeli_source":
        israeli_tax = gross * ISRAELI_CGT_RATE
        rationale = "Israeli-source dividend: flat 25% withholding at source."
        source_id = "israeli_tax_authority_cgt_2026"

    elif src == "pension_annuity":
        months = cashflow.pension_period_months
        usable = cashflow.pension_exemption_monthly_nis
        if months not in (1, 12) or isinstance(months, bool):
            raise ValueError('Pension period must be 1 or 12 months')
        if not math.isfinite(cashflow.gross_amount_nis) or cashflow.gross_amount_nis < 0 or not math.isfinite(usable) or usable < 0:
            raise ValueError('Pension gross and personal exemption must be finite and nonnegative')
        taxable_portion = gross
        if cashflow.is_post_67:
            exemption = _pension_exemption_rate(year)
            # Ceiling is the 2026 reference, not a forecast of future nominal law.
            maximum = round(PENSION_QUALIFYING_CEILING_2026_MONTHLY_NIS * exemption)
            if usable > maximum:
                raise ValueError(f'Personal monthly exemption exceeds model ceiling of {maximum:g} NIS')
            taxable_portion = max(0.0, gross - usable * months)
            marginal = _marginal_rate(user_id, session)
            israeli_tax = taxable_portion * marginal
            rationale = (
                f"Pension estimate over {months} month(s): supplied personal exemption "
                f"NIS {usable:,.0f}/month, not inferred from age. Statutory fraction "
                f"{exemption*100:g}% of qualifying ceiling, NOT the whole pension. "
                f"Taxable NIS {taxable_portion:,.0f} at marginal {marginal*100:g}%. "
                "Zero exemption is the conservative default pending personal entitlement. "
                "Ceiling uses the 2026 reference; future nominal ceilings and full "
                "progressive tax/credits are not forecast here."
            )
            source_id = "israeli_tax_authority_pension_exemption_2025"
        else:
            if usable:
                raise ValueError('Personal pension exemption requires the qualifying eligibility flag')
            # Pre-67 partial annuity: no rights-fixation; assume marginal
            marginal = _marginal_rate(user_id, session)
            israeli_tax = gross * marginal
            rationale = (
                "Pension annuity pre-67: no rights-fixation; full marginal rate."
            )
            source_id = "israeli_tax_authority_pension_exemption_2025"

    elif src in ("salary", "rsu_vest"):
        marginal = _marginal_rate(user_id, session)
        israeli_tax = gross * marginal
        # Bituach leumi capped at insurable ceiling
        bl_subject = min(gross, DEFAULT_BL_CEILING_NIS_MONTHLY)
        bl_tax = bl_subject * DEFAULT_BL_RATE
        rationale = (
            f"Marginal income tax {marginal*100:.0f}% + bituach leumi "
            f"{DEFAULT_BL_RATE*100:.0f}% on first ₪{DEFAULT_BL_CEILING_NIS_MONTHLY:,.0f} (capped)."
        )
        source_id = "bituach_leumi_ceiling_2026"

    elif src == "interest":
        israeli_tax = gross * ISRAELI_CGT_RATE  # treated as CGT
        rationale = "Interest income: 25% Israeli CGT."
        source_id = "israeli_tax_authority_cgt_2026"

    elif src == "rental":
        # Israeli rental income — 10% reduced rate under §122 if eligible;
        # otherwise marginal. Assume reduced rate by default (most common).
        israeli_tax = gross * 0.10
        rationale = "Rental income at the §122 reduced 10% rate (most common)."
        source_id = "argosy_derived"

    else:
        # Unknown source — fall back to marginal
        marginal = _marginal_rate(user_id, session)
        israeli_tax = gross * marginal
        rationale = f"Unknown source '{src}'; defaulted to marginal {marginal*100:.0f}%."
        source_id = "argosy_derived"

    # Surtax (mas yesef) on income above the annual threshold — capital/passive
    # at 5%, ordinary at 3%, others (e.g. §122 rental) outside the base.
    surtax = 0.0
    surtax_rationale = "No surtax: below the annual threshold or out of base."
    if apply_surtax and src in _SURTAX_CAPITAL_SOURCES:
        surtax = annual_surtax(gross, is_capital=True)
    elif apply_surtax and src in _SURTAX_ORDINARY_SOURCES:
        if src == 'pension_annuity':
            # Annualize recurring taxable pension, then return tax for its period.
            surtax = annual_surtax(taxable_portion * 12 / months, is_capital=False) * months / 12
        else:
            surtax = annual_surtax(gross, is_capital=False)
    if surtax > 0:
        cap = src in _SURTAX_CAPITAL_SOURCES
        from argosy.services.tax_curve import _surtax_params

        _thr, _r_ord, _r_cap = _surtax_params()
        _rate_pct = (_r_cap if cap else _r_ord) * 100.0
        surtax_rationale = (
            f"Surtax (mas yesef) {_rate_pct:g}% on the "
            f"₪{max(0.0, gross - _thr):,.0f} above the "
            f"₪{_thr:,.0f} annual threshold "
            f"({'capital/passive' if cap else 'ordinary'} income)."
        )
        if src == 'pension_annuity':
            surtax_rationale = f'Ordinary surtax on annualized taxable pension, apportioned to {months} month(s); other annual income is not included.'

    total_tax = israeli_tax + us_withholding + bl_tax + surtax
    net = max(0.0, gross - total_tax)
    effective_rate = total_tax / gross if gross > 0 else 0.0

    return TaxBreakdown(
        gross=ValueWithRationale(
            value=round(gross, 2), unit="NIS", source_id=None,
            rationale=f"Pre-tax cashflow from source '{src}'.",
        ),
        net=ValueWithRationale(
            value=round(net, 2), unit="NIS", source_id=None,
            rationale=f"After {effective_rate*100:.1f}% effective rate.",
        ),
        israeli_tax=ValueWithRationale(
            value=round(israeli_tax, 2), unit="NIS",
            source_id=source_id, rationale=rationale,
        ),
        us_treaty_credit=ValueWithRationale(
            value=round(us_credit, 2), unit="NIS",
            source_id="us_israel_tax_treaty" if us_credit > 0 else None,
            rationale=(
                "US treaty withholding credited against Israeli tax liability."
                if us_credit > 0 else "No US-source income."
            ),
        ),
        us_withholding=ValueWithRationale(
            value=round(us_withholding, 2), unit="NIS",
            source_id="us_israel_tax_treaty" if src == "dividend_us_source" else None,
            rationale="Projected ordinary-dividend US cash withholding under the documented treaty assumptions; the credit is not a second cash payment.",
        ),
        bituach_leumi_tax=ValueWithRationale(
            value=round(bl_tax, 2), unit="NIS",
            source_id="bituach_leumi_ceiling_2026" if bl_tax > 0 else None,
            rationale=(
                f"Bituach leumi {DEFAULT_BL_RATE*100:.0f}% capped at "
                f"₪{DEFAULT_BL_CEILING_NIS_MONTHLY:,.0f}."
                if bl_tax > 0 else "No BL applies to this source."
            ),
        ),
        surtax=ValueWithRationale(
            value=round(surtax, 2), unit="NIS",
            source_id="israeli_tax_authority_surtax_2025" if surtax > 0 else None,
            rationale=surtax_rationale,
        ),
        effective_rate=ValueWithRationale(
            value=round(effective_rate, 4), unit="fraction", source_id=None,
            rationale=(
                f"Total tax ₪{total_tax:,.0f} / gross ₪{gross:,.0f}. "
                "Includes US cash withholding + net Israeli income/CGT "
                "(after applicable base credit) + bituach leumi + surtax."
            ),
        ),
    )


def _marginal_rate(user_id: str, session: Session) -> float:
    """Resolve user's marginal top rate (default 47%)."""
    try:
        v = resolve(
            "tax.marginal_top_rate", user_id=user_id, session=session,
        )
        if isinstance(v.value, (int, float)):
            return float(v.value)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_MARGINAL_TOP_RATE


def effective_pension_annuity_tax(
    *, user_id: str, session: Session, year: int = 2031,
    gross_monthly_nis: float | None = None,
    personal_exemption_monthly_nis: float = 0.0,
) -> float:
    """Effective income-tax rate on a post-67 private pension annuity.

    With an amount, use the same ceiling-bound personal-exemption arithmetic
    as the calculator. Without one, use the household marginal base-tax rate
    with no assumed personal exemption. This conservative scenario assumption
    is not a full progressive-tax or surtax forecast. Never apply the statutory
    exemption percentage to an unlimited annuity. State old-age pension is
    separate and is not taxed by this helper.
    """
    marginal = _marginal_rate(user_id, session)
    if gross_monthly_nis is None:
        if personal_exemption_monthly_nis:
            raise ValueError('A personal exemption needs the corresponding pension amount')
        # Scenario engines supply neither per-person future rights nor a fixed
        # annuity amount: do not grant an unlimited percentage exemption. This
        # is an explicit conservative marginal-rate assumption, not exact tax.
        return max(0.0, min(1.0, marginal))
    result = compute_tax(TaxableCashflow(source='pension_annuity',
        gross_amount_nis=gross_monthly_nis, is_post_67=True,
        pension_exemption_monthly_nis=personal_exemption_monthly_nis),
        user_id=user_id, session=session, year=year, apply_surtax=False)
    return result.effective_rate.value
