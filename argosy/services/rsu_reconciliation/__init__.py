"""RSU reconciliation service.

Pairs Schwab Equity Awards Center sale/disbursement records against Leumi USD
account credits to confirm RSU proceeds reach the bank.

This is a *verification* tool, not an ingest path: it never writes to the DB.
The CSV is read directly from disk; Leumi credits are read from the existing
``expense_transactions`` table; the output is a reconciliation report.

Public surface:
  * ``parse_csv`` (in ``schwab_csv``) — parse a Schwab CSV into a
    ``SchwabReport`` (sales + disbursements + unparsed-action visibility).
  * ``reconcile`` (in ``match``) — pair disbursements with Leumi credits,
    return matched/unmatched buckets and a summary string.
"""

from __future__ import annotations

from argosy.services.rsu_reconciliation.match import (
    LeumiCredit,
    Match,
    ReconciliationReport,
    reconcile,
)
from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabDisbursement,
    SchwabReport,
    SchwabSale,
    SchwabSaleLot,
    parse_csv,
)
from argosy.services.rsu_reconciliation.withholding_check import (
    WithholdingVerdict,
    check_withholding,
)

__all__ = [
    "LeumiCredit",
    "Match",
    "ReconciliationReport",
    "SchwabDisbursement",
    "SchwabReport",
    "SchwabSale",
    "SchwabSaleLot",
    "WithholdingVerdict",
    "check_withholding",
    "parse_csv",
    "reconcile",
]
