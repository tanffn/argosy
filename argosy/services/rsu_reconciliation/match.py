"""Greedy disbursement-to-credit matcher.

Given a SchwabReport (with disbursements) and a list of LeumiCredit rows from
the database, pair each disbursement with the closest unmatched credit in the
``[disbursement_date, disbursement_date + tolerance_days]`` window whose
USD amount is within ``tolerance_usd``.

Tie-break order when several Leumi credits qualify:
  1. Smallest absolute amount delta (closest dollar match wins).
  2. Smallest day delta (earlier credit wins).
  3. Smallest tx_id (deterministic across runs).

The matcher is read-only — it never mutates the inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabDisbursement, SchwabReport,
)


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
    days_diff: int           # credit.date - disbursement.date (>= 0 by design)
    amount_diff_usd: float   # credit.amount_usd - disbursement.amount_usd


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
    tolerance_days: int = 7,
) -> ReconciliationReport:
    """Pair Schwab disbursements with Leumi USD credits.

    Greedy assignment: walk disbursements in chronological order, pick the
    best candidate from the unmatched-credit pool, mark the credit as taken,
    move on. Anything left over goes into the unmatched buckets.
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
        # Build candidate set: same-currency credits within both windows.
        candidates: list[LeumiCredit] = []
        window_end = disb.date + timedelta(days=tolerance_days)
        for c in available:
            if c.tx_id in consumed_ids:
                continue
            if c.date < disb.date or c.date > window_end:
                continue
            if abs(c.amount_usd - disb.amount_usd) > tolerance_usd:
                continue
            candidates.append(c)

        if not candidates:
            out.unmatched_disbursements.append(disb)
            continue

        # Pick the best — closest amount, then closest date, then smallest id.
        best = min(
            candidates,
            key=lambda c: (
                abs(c.amount_usd - disb.amount_usd),
                (c.date - disb.date).days,
                c.tx_id,
            ),
        )
        consumed_ids.add(best.tx_id)
        total_matched_usd += disb.amount_usd
        out.matches.append(Match(
            disbursement=disb,
            credit=best,
            days_diff=(best.date - disb.date).days,
            amount_diff_usd=round(best.amount_usd - disb.amount_usd, 2),
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
