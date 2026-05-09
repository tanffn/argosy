"""Parser-independent ground-truth oracle for expense statement files.

Reads the raw spreadsheet cells via pandas alone — completely unaware of
``argosy.services.expense_ingest``. The conservation tests in
``tests/test_expense_parsers_ground_truth.py`` use these functions as the
source of truth: parser output must match within tolerance.

If this module has a bug, it must be obvious from reading. Keep it simple.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class GroundTruth:
    row_count: int
    sum_debits_nis: float
    sum_credits_nis: float
    declared_total_nis: float | None


_NIS_NUM = re.compile(r"[-+]?[\d,]+\.?\d*")
# Isracard date strings look like "08.04.26" — dd.mm.yy
_ISRACARD_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")


def _to_float(x) -> float:
    """Robust 'is this a number' converter. NaN/None/blank → 0.0."""
    if x is None:
        return 0.0
    try:
        f = float(x)
        return 0.0 if math.isnan(f) else f
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").replace("$", "").strip()
        try:
            f = float(s)
            return 0.0 if math.isnan(f) else f
        except ValueError:
            return 0.0


def leumi_oracle(path: Path) -> GroundTruth:
    """Leumi current-account ('Osh') HTML-as-xls export.

    The file is an HTML document; pandas.read_html returns multiple tables.
    Transactions live in the largest table. The header row contains
    תאריך | תאריך ערך | תיאור | אסמכתא | בחובה | בזכות | היתרה בש"ח | הערה.
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx = max(tables, key=lambda t: t.shape[0])
    # Skip the two header-ish rows the export emits before data
    data = tx.iloc[2:].copy()
    # Drop blank separator rows (no date in col 0)
    data = data[data[0].notna()]
    debits = sum(_to_float(v) for v in data[4])    # column 4 = בחובה
    credits = sum(_to_float(v) for v in data[5])   # column 5 = בזכות
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=None,
    )


def isracard_oracle(path: Path) -> GroundTruth:
    """Isracard ``פירוט עסקאות`` export.

    Sheet header at row 12; data from row 13. Header columns:
    תאריך רכישה | שם בית עסק | סכום עסקה | מטבע עסקה |
    סכום חיוב | מטבע חיוב | מס' שובר | פירוט נוסף.

    The declared total appears at row 4 col 7 in NIS.
    """
    df = pd.read_excel(path, sheet_name="פירוט עסקאות", header=None)
    declared_str = str(df.iat[4, 7])
    declared_match = _NIS_NUM.search(declared_str.replace(",", ""))
    declared = float(declared_match.group()) if declared_match else None

    # Row 12 is the header; rows 13+ are transactions.
    # Keep only rows whose col 0 looks like a date string (dd.mm.yy). This
    # filters out both NaN rows and the trailing legal-disclaimer row that
    # Isracard appends after the data block with a long string in col 0.
    data = df.iloc[13:].copy()
    data = data[
        data[0].apply(lambda v: bool(_ISRACARD_DATE.match(str(v))) if pd.notna(v) else False)
    ]

    debits = 0.0
    credits = 0.0
    for _, row in data.iterrows():
        tx_amount = _to_float(row[2])              # סכום עסקה
        # סכום חיוב in column 4. If the original currency is NIS, that's our
        # number directly; if foreign (e.g., USD), col 4 still shows foreign,
        # so we fall back to סכום חיוב = col 4 with currency col 5 check.
        charge_nis = _to_float(row[4]) if str(row[5]).strip() == "₪" else None
        amount = charge_nis if charge_nis is not None else tx_amount
        if tx_amount < 0 or amount < 0:
            credits += abs(amount)
        else:
            debits += abs(amount)
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=declared,
    )


def max_oracle(path: Path) -> GroundTruth:
    """Max card export. Sheet name starts ``לאומי לישראל`` and ends with the
    account number. Row 0 has the title; row 2 has the declared total in
    a sentence like "...654.88 ₪". Row 3 is the header; rows 4+ are data.
    """
    xl = pd.ExcelFile(path)
    sheet = next(s for s in xl.sheet_names if s.startswith("לאומי לישראל"))
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    header_row_idx = 3
    declared_str = str(df.iat[2, 0]).replace(",", "")
    declared_match = _NIS_NUM.search(declared_str.split(":")[-1])
    declared = float(declared_match.group()) if declared_match else None

    data = df.iloc[header_row_idx + 1 :].copy()
    # Keep only rows whose date column is a real datetime. Max appends a
    # disclaimer paragraph after the data block whose col 0 is a plain string.
    data = data[data[0].apply(lambda v: isinstance(v, datetime))]

    debits = 0.0
    credits = 0.0
    n = 0
    for _, row in data.iterrows():
        charge = _to_float(row[3])                 # col 3 = סכום חיוב
        n += 1
        if charge < 0:
            credits += abs(charge)
        else:
            debits += abs(charge)
    return GroundTruth(
        row_count=n,
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=declared,
    )
