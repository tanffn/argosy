"""Portfolio-snapshot ingestion (2026-05-29).

Companion to `argosy/services/expense_ingest/` for portfolio-side
file ingest. The expense pipeline parses bank-statement transactions;
this pipeline parses position snapshots.

Today the only parser is `leumi_xls` (Bank Leumi's SpreadsheetML XML
portfolio export). The legacy "Family Finances Status" TSV format
the user used to maintain by hand is still consumed via
`argosy/ingest/tsv.py::parse_portfolio_tsv` -- the new flow lets
Argosy produce that TSV from the raw bank XLS instead of asking
the user to run `update_leumi_tsv.py` outside Argosy.

Conceptual scope (per the 2026-05-29 reframe):
  - PORTFOLIO snapshot = positions held (the XLS export, this package).
  - CASH balance       = running balance from the bank-statement (Osh)
                         export -- lives in `expense_ingest`.
  - "Current cash" is DERIVED: latest bank-statement closing balance
                         plus / minus subsequent ingested transactions.
                         Argosy does this merge so the user doesn't
                         have to manually combine the two files into
                         a Family Finances Status TSV.
"""

from argosy.services.portfolio_ingest.parsers.leumi_xls import (
    LeumiPortfolioPosition,
    LeumiPortfolioSnapshot,
    PARSER_NAME as LEUMI_PARSER_NAME,
    PARSER_VERSION as LEUMI_PARSER_VERSION,
    is_leumi_portfolio_xls,
    parse_leumi_portfolio_xls,
)

__all__ = [
    "LeumiPortfolioPosition",
    "LeumiPortfolioSnapshot",
    "LEUMI_PARSER_NAME",
    "LEUMI_PARSER_VERSION",
    "is_leumi_portfolio_xls",
    "parse_leumi_portfolio_xls",
]
