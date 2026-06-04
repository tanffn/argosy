"""Account-aware tax engine for Israeli retirement cashflows.

Closes BLOCKER #2 from the 2026-05-28 SDD review. Replaces the prior flat
``tax_rate`` slider (0-50%) with per-source, per-account tax computation
honoring Israeli tax rules.

Per-source rules (corrected per codex review):

  - capital_gain (taxable equity): flat 25% Israeli CGT per
    ``israeli_tax_authority_cgt_2026``.

  - dividend_us_source (Israeli resident): treaty withholding 15% at US
    source; Israeli tax 25% on gross; foreign-tax-credit reduces Israeli
    liability:
        israeli_tax_due = max(0, 0.25 * gross - 0.15 * gross_us_withheld)
    Per ``us_israel_tax_treaty``.

  - dividend_israeli_source: flat 25% Israeli withholding at source.

  - pension_annuity (kupat_pensia post-67): rights-fixation regime per
    ``israeli_tax_authority_pension_exemption_2025``. Exemption envelope:
    57% in 2025, phasing to 67% by 2030 (Argosy uses a year-indexed
    table; see ``_pension_exemption_rate``). Marginal tax (47% top
    bracket assumed) on the non-exempt portion only.

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
from typing import Literal

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import resolve


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
    us_gross_amount_for_treaty: float = 0.0  # for dividend_us_source
    is_post_67: bool = False  # for pension_annuity rights-fixation


# Year-indexed pension exemption rate per ITA Jan 2025 procedure.
# Rights-fixation regime: 57% in 2025 stepping to 67% by 2030.
_PENSION_EXEMPTION_BY_YEAR: dict[int, float] = {
    2025: 0.57, 2026: 0.59, 2027: 0.61, 2028: 0.63, 2029: 0.65, 2030: 0.67,
}


def _pension_exemption_rate(year: int) -> float:
    """Return the exemption fraction of pension qualifying income at the year."""
    if year < 2025:
        return 0.35  # legacy regime
    if year > 2030:
        return 0.67  # max under current ITA phasing
    return _PENSION_EXEMPTION_BY_YEAR[year]


# Default top marginal Israeli income tax bracket for high earners.
DEFAULT_MARGINAL_TOP_RATE = 0.47
ISRAELI_CGT_RATE = 0.25
US_DIVIDEND_TREATY_RATE = 0.15
# Bituach leumi insurable ceiling — applies to salary + RSU; surplus uninsured.
DEFAULT_BL_CEILING_NIS_MONTHLY = 50_000.0
DEFAULT_BL_RATE = 0.07  # employee portion ~7% (simplified; depends on bracket)


@dataclass(frozen=True)
class TaxBreakdown:
    gross: ValueWithRationale
    net: ValueWithRationale
    israeli_tax: ValueWithRationale
    us_treaty_credit: ValueWithRationale  # 0 unless US-source dividends
    bituach_leumi_tax: ValueWithRationale
    effective_rate: ValueWithRationale


def compute_tax(
    cashflow: TaxableCashflow,
    *,
    user_id: str,
    session: Session,
    year: int = 2026,
) -> TaxBreakdown:
    """Returns full tax breakdown — gross, net, per-component taxes,
    effective_rate — for a single Israeli-resident cashflow."""
    src = cashflow.source
    gross = max(0.0, cashflow.gross_amount_nis)

    israeli_tax = 0.0
    us_credit = 0.0
    bl_tax = 0.0

    if src == "capital_gain":
        israeli_tax = gross * ISRAELI_CGT_RATE
        rationale = "Israeli CGT flat 25% on equity capital gains."
        source_id = "israeli_tax_authority_cgt_2026"

    elif src == "dividend_us_source":
        # US treaty withholding 15%; Israeli 25%; FTC interaction
        us_credit = cashflow.us_gross_amount_for_treaty * US_DIVIDEND_TREATY_RATE
        israeli_gross = gross * ISRAELI_CGT_RATE
        israeli_tax = max(0.0, israeli_gross - us_credit)
        rationale = (
            "US-source dividend: 15% US treaty withholding becomes a "
            "foreign-tax-credit against Israeli 25% dividend tax. "
            "israeli_tax = max(0, 0.25 × gross - 0.15 × us_gross)."
        )
        source_id = "us_israel_tax_treaty"

    elif src == "dividend_israeli_source":
        israeli_tax = gross * ISRAELI_CGT_RATE
        rationale = "Israeli-source dividend: flat 25% withholding at source."
        source_id = "israeli_tax_authority_cgt_2026"

    elif src == "pension_annuity":
        if cashflow.is_post_67:
            exemption = _pension_exemption_rate(year)
            taxable_portion = gross * (1.0 - exemption)
            marginal = _marginal_rate(user_id, session)
            israeli_tax = taxable_portion * marginal
            rationale = (
                f"Pension annuity post-67 under rights-fixation regime: "
                f"{exemption*100:.0f}% exempt in year {year}; remaining "
                f"{(1-exemption)*100:.0f}% × marginal {marginal*100:.0f}%."
            )
            source_id = "israeli_tax_authority_pension_exemption_2025"
        else:
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

    total_tax = israeli_tax + bl_tax
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
        bituach_leumi_tax=ValueWithRationale(
            value=round(bl_tax, 2), unit="NIS",
            source_id="bituach_leumi_ceiling_2026" if bl_tax > 0 else None,
            rationale=(
                f"Bituach leumi {DEFAULT_BL_RATE*100:.0f}% capped at "
                f"₪{DEFAULT_BL_CEILING_NIS_MONTHLY:,.0f}."
                if bl_tax > 0 else "No BL applies to this source."
            ),
        ),
        effective_rate=ValueWithRationale(
            value=round(effective_rate, 4), unit="fraction", source_id=None,
            rationale=(
                f"Total tax ₪{total_tax:,.0f} / gross ₪{gross:,.0f}. "
                "Includes Israeli income/CGT + bituach leumi - US treaty credit."
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
) -> float:
    """Effective income-tax rate on a post-67 private pension annuity.

    The non-exempt (taxable) fraction of the annuity × the household marginal
    rate, under the ITA rights-fixation exemption phasing. Sourced from
    :func:`_pension_exemption_rate` (ITA exemption schedule) and
    :func:`_marginal_rate` (household marginal, default top 47% — conservative,
    overstates tax slightly, which is the safe direction for a retirement
    GO/NO-GO). Used to net the annuity income credited in the retirement MC,
    instead of crediting it gross (codex review 2026-06-04). Bituach Leumi
    old-age pension is income-tax-exempt and is NOT subject to this.
    """
    exemption = _pension_exemption_rate(year)
    marginal = _marginal_rate(user_id, session)
    return max(0.0, min(1.0, (1.0 - exemption) * marginal))
