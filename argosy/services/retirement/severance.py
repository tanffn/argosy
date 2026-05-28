"""Severance (pizurim) split from kupat_pensia (MED #19).

Closes the documented "optimistic bias" from the codex tandem review of
cashflow_projection.py. Severance contribution (~8.33% of salary) had
been folded into kupat_pensia which overstates age-67 annuity by ~67%
if severance is in practice withdrawn pre-annuitization.

This module surfaces severance as a separate account with explicit
withdrawal/annuitization probability so the projection no longer
silently overstates pension annuity.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class SeveranceState:
    accrued_pizurim_nis: ValueWithRationale
    withdrawn_history_nis: ValueWithRationale
    annuitization_probability: ValueWithRationale
    tax_treatment: ValueWithRationale


def extract_severance_state(
    *,
    accrued_pizurim_nis: float = 0.0,
    withdrawn_history_nis: float = 0.0,
    annuitization_probability: float = 0.50,
) -> SeveranceState:
    """Build a severance state from intake.

    ``annuitization_probability``: user's stated intent — what fraction
    of pizurim will they actually leave in the pension fund for
    annuitization vs. withdraw early. Default 50% (split the middle).
    """
    available_for_annuity = accrued_pizurim_nis * annuitization_probability
    return SeveranceState(
        accrued_pizurim_nis=ValueWithRationale(
            value=accrued_pizurim_nis, unit="NIS", source_id="argosy_derived",
            rationale=(
                "Accrued severance (פיצויים) at 8.33% of salary. Tracked "
                "separately from kupat_pensia to avoid the 'folded-into-"
                "annuity' optimistic bias documented in the codex review."
            ),
            confidence="medium",
        ),
        withdrawn_history_nis=ValueWithRationale(
            value=withdrawn_history_nis, unit="NIS", source_id="argosy_derived",
            rationale="Severance already withdrawn (e.g. on job change).",
        ),
        annuitization_probability=ValueWithRationale(
            value=annuitization_probability, unit="fraction",
            source_id="argosy_derived",
            rationale=(
                "Fraction of pizurim user intends to leave in the pension "
                "fund for age-67 annuitization. Most Israeli households "
                "withdraw at least some severance pre-retirement; default "
                "0.50 reflects this uncertainty."
            ),
            confidence="medium",
        ),
        tax_treatment=ValueWithRationale(
            value=f"Annuitized portion enters kupat_pensia exemption regime; "
                  f"early withdrawals are subject to marginal income tax with "
                  f"the §164 exemption ceiling (~₪13K/yr per service year).",
            unit="text",
            source_id="argosy_derived",
            rationale="Severance tax treatment summary.",
        ),
    )


def effective_pension_for_annuity(
    *,
    kupat_pensia_balance_nis: float,
    severance: SeveranceState,
) -> ValueWithRationale:
    """Return the effective pension balance for annuity calculation, with
    severance broken out per the user's stated annuitization probability.
    """
    accrued = float(severance.accrued_pizurim_nis.value or 0.0)
    prob = float(severance.annuitization_probability.value or 0.5)
    effective = kupat_pensia_balance_nis + accrued * prob
    return ValueWithRationale(
        value=round(effective, 2), unit="NIS", source_id=None,
        rationale=(
            f"Kupat_pensia ₪{kupat_pensia_balance_nis:,.0f} + "
            f"severance × annuitization_prob ({prob:.0%}) of ₪{accrued:,.0f}. "
            "Replaces prior model which folded all severance into pensia, "
            "overstating annuity by ~{int(prob*100)}%."
        ),
        confidence="medium",
    )
