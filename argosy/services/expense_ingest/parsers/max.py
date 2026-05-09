"""Parser for Max card Excel exports.

Layout:
  sheet name: 'לאומי לישראל <account-number>'
  row 1 col 1: title with account # and date range
  row 3 col 1: 'עסקאות לחיוב ב-DD/MM/YYYY: NNN.NN ₪'
  row 4   : header — תאריך עסקה|שם בית עסק|סכום עסקה|סכום חיוב|סוג עסקה|ענף|הערות
  rows 5+ : transactions until trailing notes row

Distinguishing feature: column 6 (ענף) is a pre-categorized issuer hint
that flows through to NormalizedTransaction.issuer_category.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)

PARSER_VERSION = "0.1.0"

_ACCOUNT_RE = re.compile(r"לאומי לישראל\s+([\d-]+)")
_CHARGE_RE = re.compile(r"לחיוב ב-?\s*(\d{1,2})/(\d{1,2})/(\d{4}):\s*([\d,.]+)")

_TX_TYPE_MAP = {
    "רגילה": "regular",
    "הוראת קבע": "standing_order",
    "תשלומים": "installment",
    "זיכוי": "refund",
}


def _to_float(x) -> float:
    if x is None:
        return 0.0
    try:
        f = float(x)
        if math.isnan(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").strip()
        try:
            f = float(s)
            if math.isnan(f):
                return 0.0
            return f
        except ValueError:
            return 0.0


def parse(path: Path) -> ParseResult:
    xl = pd.ExcelFile(path)
    sheet = next((s for s in xl.sheet_names
                  if s.startswith("לאומי לישראל")), None)
    if sheet is None:
        raise ValueError(f"Max parser: no 'לאומי לישראל' sheet in {path}, "
                         f"got {xl.sheet_names}")

    # Account number → last-4 of post-dash chunk
    m_acc = _ACCOUNT_RE.search(sheet)
    if not m_acc:
        raise ValueError(f"Max parser: account # not found in sheet name '{sheet}'")
    account_full = m_acc.group(1)               # e.g. '882-44745280'
    last4 = account_full.split("-")[-1][-4:]

    df = pd.read_excel(path, sheet_name=sheet, header=None)
    charge_str = str(df.iat[2, 0])
    m_charge = _CHARGE_RE.search(charge_str)
    declared = float(m_charge.group(4).replace(",", "")) if m_charge else None
    charge_date = (
        date(int(m_charge.group(3)), int(m_charge.group(2)), int(m_charge.group(1)))
        if m_charge else None
    )

    # Header validation — Max uses newline-separated headers in some files.
    expected_headers = ["תאריך\nעסקה", "שם בית עסק", "סכום\nעסקה",
                        "סכום\nחיוב", "סוג\nעסקה", "ענף", "הערות"]
    actual_headers = [str(df.iat[3, j]).strip() if not pd.isna(df.iat[3, j]) else ""
                      for j in range(7)]
    # Compare with newlines stripped (whitespace is tolerable variation).
    if [h.replace("\n", "").replace(" ", "") for h in actual_headers] != \
       [h.replace("\n", "").replace(" ", "") for h in expected_headers]:
        raise ValueError(f"Max parser: unexpected header row {actual_headers}")

    txs: list[NormalizedTransaction] = []
    for i in range(4, len(df)):
        row = df.iloc[i]
        date_cell = row[0]
        if pd.isna(date_cell):
            continue
        # Per Task 6: Max appends a trailing note paragraph whose col 0 is plain
        # str (not a datetime). Filter strictly to actual datetime objects.
        if isinstance(date_cell, (datetime, pd.Timestamp)):
            d = date_cell.date() if isinstance(date_cell, datetime) else date_cell
        else:
            # Try to coerce — but guard against non-date strings (disclaimer rows).
            try:
                d = pd.to_datetime(date_cell).date()
            except Exception:
                continue

        merchant_raw = str(row[1]).strip()
        tx_amount = _to_float(row[2])
        charge_amount = _to_float(row[3])
        tx_type_he = str(row[4]).strip() if not pd.isna(row[4]) else ""
        anaf = None if pd.isna(row[5]) else str(row[5]).strip()

        tx_type = _TX_TYPE_MAP.get(tx_type_he, "regular")
        is_refund = tx_type == "refund" or charge_amount < 0
        if is_refund:
            tx_type = "refund"
            direction = "credit"
        else:
            direction = "debit"

        txs.append(NormalizedTransaction(
            occurred_on=d,
            merchant_raw=merchant_raw,
            merchant_normalized=normalize(merchant_raw),
            amount_nis=abs(charge_amount),
            direction=direction,
            tx_type=tx_type,
            reference=None,
            issuer_category=anaf,
            raw_row={
                "date": str(date_cell),
                "merchant": merchant_raw,
                "tx_amount": tx_amount,
                "charge_amount": charge_amount,
                "tx_type_he": tx_type_he,
                "anaf": anaf,
            },
        ))

    if not txs:
        raise ValueError(f"Max parser: 0 rows in {path}")

    parsed_total = sum(
        t.amount_nis * (-1 if t.direction == "credit" else 1) for t in txs
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=charge_date,
            declared_total_nis=declared,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card", issuer="max", external_id=last4,
            cardholder_name=None,        # Max sheet doesn't carry cardholder
        ),
    )
