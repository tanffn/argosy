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


def leumi_usd_oracle(path: Path) -> GroundTruth:
    """Leumi USD ('פמ"ח') brokerage/holding HTML-as-xls export.

    Same HTML wrapper as the Osh file, but the largest table carries
    named Hebrew headers (תאריך / תיאור / תאור מורחב / אסמכתא /
    חובה / זכות / יתרה) and the numbers are USD, not NIS. We sum the
    debit and credit columns directly from the named-column table.

    NOTE: the field names ``sum_debits_nis`` / ``sum_credits_nis`` are a
    naming caveat — for the USD oracle these carry USD totals, mirroring
    the parser's ``parsed_total_nis`` field which also re-uses the NIS
    name for USD parsers (legacy schema, predates the USD parser).
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx = max(tables, key=lambda t: t.shape[0])
    # Drop rows whose date column is NaN
    data = tx[tx["תאריך"].notna()].copy()
    debits = sum(_to_float(v) for v in data["חובה"])
    credits = sum(_to_float(v) for v in data["זכות"])
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),    # USD; see docstring caveat
        sum_credits_nis=round(credits, 2),  # USD; see docstring caveat
        declared_total_nis=None,
    )


def isracard_oracle(path: Path) -> GroundTruth:
    """Isracard ``פירוט עסקאות`` export.

    The header row carries תאריך רכישה | שם בית עסק | סכום עסקה | מטבע עסקה |
    סכום חיוב | מטבע חיוב | מס' שובר | פירוט נוסף. The header's *index* is not
    fixed — most files put it at row 12, but some have it at row 11 (when the
    metadata block has fewer lines). We locate it dynamically.

    The declared total appears at row 4 col 7 in NIS.
    """
    df = pd.read_excel(path, sheet_name="פירוט עסקאות", header=None)
    declared_str = str(df.iat[4, 7])
    declared_match = _NIS_NUM.search(declared_str.replace(",", ""))
    declared = float(declared_match.group()) if declared_match else None

    # Locate the header row dynamically — col 0 == 'תאריך רכישה'. Hardcoding
    # row 13 silently dropped the first transaction in files where the header
    # sat at row 11.
    header_idx = next(
        (i for i in range(min(20, len(df)))
         if str(df.iat[i, 0]).strip() == "תאריך רכישה"),
        None,
    )
    if header_idx is None:
        raise ValueError(f"Isracard oracle: header row not found in {path}")

    # Keep only rows whose col 0 looks like a date string (dd.mm.yy). This
    # filters NaN rows AND the trailing legal-disclaimer row Isracard appends
    # after the data block with a long string in col 0.
    data = df.iloc[header_idx + 1:].copy()
    data = data[
        data[0].apply(lambda v: bool(_ISRACARD_DATE.match(str(v))) if pd.notna(v) else False)
    ]

    debits = 0.0
    credits = 0.0
    for _, row in data.iterrows():
        tx_amount = _to_float(row[2])              # סכום עסקה
        # NIS-only sums (Bug 2 part 1): foreign rows are excluded — the parser
        # stores amount_nis=None for them and downstream FX conversion is
        # responsible for any NIS-equivalent rendering. Oracle mirrors that.
        if str(row[5]).strip() != "₪":
            continue
        charge_nis = _to_float(row[4])
        if tx_amount < 0 or charge_nis < 0:
            credits += abs(charge_nis)
        else:
            debits += abs(charge_nis)
    return GroundTruth(
        row_count=int(len(data)),
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=declared,
    )


def discount_oracle(path: Path) -> GroundTruth:
    """Discount Bank Mastercard export. Two sheets, both 16-column with
    header at row 3 (0-indexed); data from row 4 until first NaN col 0.
    Sums col 5 (סכום חיוב) across both sheets, signed.
    """
    xl = pd.ExcelFile(path)
    expected_sheets = {"עסקאות במועד החיוב", 'עסקאות חו"ל ומט"ח'}
    sheets = [s for s in xl.sheet_names if s in expected_sheets]
    if not sheets:
        raise ValueError(
            f"Discount oracle: no recognized sheet in {path}, "
            f"got {xl.sheet_names}"
        )

    debits = 0.0
    credits = 0.0
    n = 0
    for sheet in sheets:
        df = pd.read_excel(path, sheet_name=sheet, header=None)
        for i in range(4, len(df)):
            v0 = df.iat[i, 0]
            if pd.isna(v0):
                break
            charge = _to_float(df.iat[i, 5])    # col 5 = סכום חיוב
            n += 1
            if charge < 0:
                credits += abs(charge)
            else:
                debits += abs(charge)
    return GroundTruth(
        row_count=n,
        sum_debits_nis=round(debits, 2),
        sum_credits_nis=round(credits, 2),
        declared_total_nis=None,        # Discount export has no machine-readable footer total
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
