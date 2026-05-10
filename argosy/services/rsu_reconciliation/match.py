"""Greedy disbursement-to-credit matcher.

Given a SchwabReport (with disbursements) and a list of LeumiCredit rows from
the database, pair each disbursement with the closest unmatched credit in the
``[disbursement_date - 1, disbursement_date + tolerance_days]`` window.

Two match kinds:
  * ``exact`` — Leumi credit equals Schwab disbursement to within
    ``tolerance_usd`` (typically a $1 FX/rounding fudge).
  * ``haircut`` — Leumi credit is materially smaller than the Schwab
    disbursement (``ratio in [tax_haircut_min, tax_haircut_max]``).
    Empirically this is bank-side Israeli capital-gains tax withholding
    (25% + 3% surtax = ~28% off the wire). We surface the delta as a
    signed amount + a percentage so the UI can flag it without the user
    having to do mental arithmetic.

Tie-break order when several Leumi credits qualify:
  * Exact: smallest absolute amount delta, then smallest day delta,
    then smallest tx_id (deterministic across runs).
  * Haircut: closest haircut percent to the canonical IL CGT rate
    (~27.5%), then smallest day delta, then smallest tx_id.

The matcher is read-only — it never mutates the inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabDisbursement, SchwabReport,
)


# Canonical Israeli capital-gains tax rate (25% + 3% surtax). Used as the
# ideal target when scoring multiple haircut candidates so we prefer the
# credit whose shortfall most closely resembles a CGT-withholding event.
_IL_CGT_TARGET_PCT = 27.5


@dataclass(frozen=True)
class LeumiCredit:
    """A USD credit on the Leumi USD account, projected from
    ``expense_transactions`` (source.external_id='44745200',
    direction='credit', currency_orig='USD').
    """
    date: date
    amount_usd: float
    merchant_raw: str
    reference: str | None
    tx_id: int


@dataclass(frozen=True)
class Match:
    disbursement: SchwabDisbursement
    credit: LeumiCredit
    days_diff: int                      # signed: credit.date - disb.date
    amount_diff_usd: float              # signed: disb.amount - credit.amount (positive = haircut)
    match_kind: str = "exact"           # "exact" | "haircut"
    haircut_pct: float = 0.0            # (1 - credit/disb) * 100; positive = withheld


@dataclass
class ReconciliationReport:
    matches: list[Match] = field(default_factory=list)
    unmatched_disbursements: list[SchwabDisbursement] = field(default_factory=list)
    unmatched_leumi_credits: list[LeumiCredit] = field(default_factory=list)
    summary: str = ""


def reconcile(
    report: SchwabReport,
    leumi_usd_credits: list[LeumiCredit],
    tolerance_usd: float = 1.0,
    tolerance_days: int = 14,
    tax_haircut_min: float = 0.60,
    tax_haircut_max: float = 1.05,
) -> ReconciliationReport:
    """Pair Schwab disbursements with Leumi USD credits.

    Greedy assignment: walk disbursements in chronological order, pick the
    best candidate from the unmatched-credit pool, mark the credit as taken,
    move on. Anything left over goes into the unmatched buckets.

    A candidate qualifies as **exact** when its USD amount sits within
    ``tolerance_usd`` of the disbursement, or as **haircut** when its USD
    amount sits in ``[disb * tax_haircut_min, disb * tax_haircut_max]``
    (defaults: 60-105% of the disbursement). Exact wins over haircut for
    the same credit if both are eligible.
    """
    out = ReconciliationReport()

    # Process disbursements in chronological order so earlier sales claim
    # earlier credits — this matters when two disbursements both fall within
    # the same credit's window.
    disbursements = sorted(report.disbursements, key=lambda d: (d.date, d.amount_usd))
    available = list(leumi_usd_credits)
    consumed_ids: set[int] = set()

    total_matched_usd = 0.0
    for disb in disbursements:
        # Build candidate sets (exact + haircut) within both windows. Allow
        # one day before disbursement to forgive pre-emptive bank settlement.
        exact_candidates: list[LeumiCredit] = []
        haircut_candidates: list[tuple[LeumiCredit, float]] = []
        window_start = disb.date - timedelta(days=1)
        window_end = disb.date + timedelta(days=tolerance_days)
        for c in available:
            if c.tx_id in consumed_ids:
                continue
            if c.date < window_start or c.date > window_end:
                continue
            amt_diff = abs(c.amount_usd - disb.amount_usd)
            if amt_diff <= tolerance_usd:
                exact_candidates.append(c)
                continue
            if disb.amount_usd <= 0:
                continue
            ratio = c.amount_usd / disb.amount_usd
            if tax_haircut_min <= ratio <= tax_haircut_max:
                haircut_candidates.append((c, ratio))

        if not exact_candidates and not haircut_candidates:
            out.unmatched_disbursements.append(disb)
            continue

        if exact_candidates:
            # Prefer exact — closest amount, then closest date, then smallest id.
            best = min(
                exact_candidates,
                key=lambda c: (
                    abs(c.amount_usd - disb.amount_usd),
                    abs((c.date - disb.date).days),
                    c.tx_id,
                ),
            )
            consumed_ids.add(best.tx_id)
            total_matched_usd += disb.amount_usd
            out.matches.append(Match(
                disbursement=disb,
                credit=best,
                days_diff=(best.date - disb.date).days,
                amount_diff_usd=round(disb.amount_usd - best.amount_usd, 2),
                match_kind="exact",
                haircut_pct=0.0,
            ))
            continue

        # Haircut path — pick the candidate whose haircut percent is
        # closest to the canonical IL CGT rate, breaking ties by date and id.
        best_pair = min(
            haircut_candidates,
            key=lambda pair: (
                abs(((1.0 - pair[1]) * 100.0) - _IL_CGT_TARGET_PCT),
                abs((pair[0].date - disb.date).days),
                pair[0].tx_id,
            ),
        )
        best, ratio = best_pair
        consumed_ids.add(best.tx_id)
        total_matched_usd += disb.amount_usd
        out.matches.append(Match(
            disbursement=disb,
            credit=best,
            days_diff=(best.date - disb.date).days,
            amount_diff_usd=round(disb.amount_usd - best.amount_usd, 2),
            match_kind="haircut",
            haircut_pct=round((1.0 - ratio) * 100.0, 2),
        ))

    # Anything in the credit list that wasn't consumed.
    out.unmatched_leumi_credits = [
        c for c in leumi_usd_credits if c.tx_id not in consumed_ids
    ]

    n_d = len(disbursements)
    n_m = len(out.matches)
    unmatched_total = sum(c.amount_usd for c in out.unmatched_leumi_credits)
    out.summary = (
        f"{n_m}/{n_d} disbursements matched (${total_matched_usd:,.2f} total). "
        f"{len(out.unmatched_leumi_credits)} Leumi credits unmatched "
        f"(${unmatched_total:,.2f}) — possibly from non-RSU sources."
    )
    return out
