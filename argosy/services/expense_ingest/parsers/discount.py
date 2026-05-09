"""Parser for Discount Bank Mastercard Excel exports.

Two sheets (domestic + foreign), 16 columns each, header at row 3 (0-indexed),
data starting at row 4 until first NaN in col 0.

Card last-4 is read from column 3 (per-row, consistent within file).
Per-row pre-categorized in column 2 (קטגוריה — Hebrew).

Issuer name: 'discount'. external_id: card last-4 (e.g. '2923').

Refund detection (either condition triggers):
  - Negative סכום חיוב (col 5)
  - Notes (col 10) contain 'ביטול'

Both the card-fee charge and its discount-rebate are preserved as separate
line items (per project direction on card 2923 fee-waiver promotion).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)

PARSER_VERSION = "0.1.0"

_DOMESTIC_SHEET = "עסקאות במועד החיוב"
_FOREIGN_SHEET = 'עסקאות חו"ל ומט"ח'

_TX_TYPE_MAP = {
    "רגילה": "regular",
    "חיוב חודשי": "regular",          # foreign-charge wrapper; not really installment
    "חיוב עסקות מיידי": "regular",    # immediate-charge variant seen in real samples
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


def _parse_dmy_dashed(s) -> date | None:
    """Parse DD-MM-YYYY string (or datetime/Timestamp object) → date.

    Discount exports dates as plain strings '01-01-2025' (not datetime objects),
    but we guard against pandas reading them as Timestamps just in case.
    """
    if pd.isna(s):
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, pd.Timestamp):
        return s.to_pydatetime().date()
    s = str(s).strip()
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except ValueError:
        return None


def _normalize_currency(c) -> str:
    if pd.isna(c):
        return "NIS"
    s = str(c).strip()
    return {"₪": "NIS", "$": "USD", "€": "EUR"}.get(s, s.upper() or "NIS")


def _parse_sheet(df: pd.DataFrame) -> list[NormalizedTransaction]:
    txs: list[NormalizedTransaction] = []
    for i in range(4, len(df)):
        row = df.iloc[i]
        v0 = row[0]
        # Stop at first NaN col 0 — trailing footer rows ('סך הכל' etc.) come after
        if pd.isna(v0):
            break

        d = _parse_dmy_dashed(v0)
        if d is None:
            # Non-date string in col 0 means we've hit the footer section
            break

        merchant_raw = str(row[1]).strip() if not pd.isna(row[1]) else ""
        category_he = str(row[2]).strip() if not pd.isna(row[2]) else None
        # col 3: card last-4 — used at parser level; skipped here
        tx_type_he = str(row[4]).strip() if not pd.isna(row[4]) else ""
        charge_amount = _to_float(row[5])
        charge_ccy = _normalize_currency(row[6])
        orig_amount = _to_float(row[7])
        orig_ccy = _normalize_currency(row[8])
        notes = str(row[10]).strip() if not pd.isna(row[10]) else ""

        is_refund = charge_amount < 0 or "ביטול" in notes

        if is_refund:
            tx_type = "refund"
            direction = "credit"
        elif "הוראת קבע" in notes or tx_type_he == "הוראת קבע":
            tx_type = "standing_order"
            direction = "debit"
        else:
            tx_type = _TX_TYPE_MAP.get(tx_type_he, "regular")
            direction = "debit"

        # Only set amount_orig / currency_orig when the original currency differs
        has_foreign = orig_ccy not in ("NIS", "")
        txs.append(NormalizedTransaction(
            occurred_on=d,
            posted_on=_parse_dmy_dashed(row[9]) if not pd.isna(row[9]) else None,
            merchant_raw=merchant_raw,
            merchant_normalized=normalize(merchant_raw),
            amount_nis=abs(charge_amount),
            amount_orig=abs(orig_amount) if has_foreign else None,
            currency_orig=orig_ccy if has_foreign else None,
            direction=direction,
            tx_type=tx_type,
            reference=None,
            issuer_category=category_he,
            raw_row={
                "date": str(v0),
                "merchant": merchant_raw,
                "category": category_he,
                "tx_type_he": tx_type_he,
                "charge_amount": charge_amount,
                "charge_ccy": charge_ccy,
                "orig_amount": orig_amount,
                "orig_ccy": orig_ccy,
                "notes": notes,
            },
        ))
    return txs


def parse(path: Path) -> ParseResult:
    xl = pd.ExcelFile(path)
    sheets = xl.sheet_names

    txs: list[NormalizedTransaction] = []
    last4: str | None = None

    for sheet_name in (_DOMESTIC_SHEET, _FOREIGN_SHEET):
        if sheet_name not in sheets:
            continue
        df = pd.read_excel(path, sheet_name=sheet_name, header=None)
        sheet_txs = _parse_sheet(df)
        txs.extend(sheet_txs)

        # Pull card last-4 from the first data row of the first non-empty sheet
        if last4 is None and sheet_txs:
            for i in range(4, len(df)):
                v3 = df.iat[i, 3]
                if pd.notna(v3):
                    raw = str(v3).strip()
                    # Might be numeric float '2923.0' from pandas — normalise
                    try:
                        last4 = str(int(float(raw)))
                    except ValueError:
                        last4 = raw
                    break

    if not txs:
        raise ValueError(f"Discount parser: 0 rows parsed from {path}")
    if last4 is None:
        raise ValueError(f"Discount parser: card last-4 not found in {path}")

    parsed_total = sum(
        t.amount_nis * (-1 if t.direction == "credit" else 1) for t in txs
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=None,           # Discount has per-row charge dates, not a single one
            declared_total_nis=None,    # Export has no footer total in machine-readable form
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card",
            issuer="discount",
            external_id=last4,
            cardholder_name=None,
        ),
    )
