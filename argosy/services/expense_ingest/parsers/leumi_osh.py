"""Parser for Leumi 'Osh' (current-account) HTML-disguised-as-xls export."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, StatementMeta,
)

PARSER_VERSION = "0.1.0"


def _to_float(x) -> float:
    """Robust 'is this a number' converter. NaN/None/blank → 0.0."""
    if x is None:
        return 0.0
    try:
        f = float(x)
        if math.isnan(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        s = str(x).replace(",", "").strip()
        try:
            f = float(s)
            if math.isnan(f):
                return 0.0
            return f
        except ValueError:
            return 0.0


def _parse_dmy(s) -> date | None:
    """Leumi uses DD/MM/YYYY format. Strips leading '*' (not-final marker)."""
    if pd.isna(s):
        return None
    if isinstance(s, datetime):
        return s.date()
    cleaned = str(s).strip().lstrip("*").strip()
    return datetime.strptime(cleaned, "%d/%m/%Y").date()


def parse(path: Path) -> ParseResult:
    """Parse a Leumi current-account HTML export.

    Tables: typically 3. Transactions live in the largest. Header row
    is at index 1 (within that table) carrying the Hebrew column names.
    Data rows start at index 2; we drop blanks (no date in col 0) AND
    rows whose col 0 isn't a parseable DD/MM/YYYY date (filters trailing
    disclaimer rows that have non-null but non-date content).
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx_table = max(tables, key=lambda t: t.shape[0])
    data = tx_table.iloc[2:].copy()
    data = data[data[0].notna()]

    txs: list[NormalizedTransaction] = []
    for _, row in data.iterrows():
        try:
            d = _parse_dmy(row[0])
        except ValueError:
            # Non-date col 0 = footer/disclaimer row; skip silently
            continue
        if d is None:
            continue
        descr = str(row[2]).strip()
        ref = None if pd.isna(row[3]) else str(row[3]).strip()
        # Reference may come through as a float (e.g. 1266.0) — strip the ".0"
        if ref is not None and ref.endswith(".0"):
            try:
                ref = str(int(float(ref)))
            except ValueError:
                pass
        debit = _to_float(row[4])
        credit = _to_float(row[5])
        amount = debit if debit > 0 else credit
        direction = "debit" if debit > 0 else "credit"
        txs.append(NormalizedTransaction(
            occurred_on=d,
            posted_on=_parse_dmy(row[1]),
            merchant_raw=descr,
            merchant_normalized=normalize(descr),
            amount_nis=amount,
            direction=direction,
            tx_type="regular",
            reference=ref,
            issuer_category=None,
            raw_row={
                "date":          (None if pd.isna(row[0]) else str(row[0])),
                "value_date":    (None if pd.isna(row[1]) else str(row[1])),
                "description":   (None if pd.isna(row[2]) else str(row[2])),
                "reference":     (None if pd.isna(row[3]) else str(row[3])),
                "debit":         (None if pd.isna(row[4]) else str(row[4])),
                "credit":        (None if pd.isna(row[5]) else str(row[5])),
                "balance":       (None if pd.isna(row[6]) else str(row[6])),
                "extra_7":       (None if len(row) <= 7 or pd.isna(row[7]) else str(row[7])),
                "extra_8":       (None if len(row) <= 8 or pd.isna(row[8]) else str(row[8])),
            },
        ))

    if not txs:
        raise ValueError(f"Leumi parser produced 0 rows from {path}")

    parsed_total = sum(
        t.amount_nis for t in txs if t.direction == "debit"
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=None,
            declared_total_nis=None,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=None,
    )
