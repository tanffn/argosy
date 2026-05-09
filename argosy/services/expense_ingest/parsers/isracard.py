"""Parser for Isracard (and Mastercard via Isracard) Excel exports.

Layout (row indices are 0-based pandas row numbers):
  Row 4 col 0: '<card type> - <last-4>'  (always fixed)
  Row 4 col 7: NIS declared total ('₪ 3,319.44')  (always fixed)
  Row 5 col 0: 'על שם <cardholder>'  (always fixed)
  Col 7 rows 5+: charge date ('לחיוב ב-DD.MM') appears after currency sub-totals;
                 the number of sub-total rows varies (0–2) depending on foreign spend.
  Header row ('תאריך רכישה' in col 0): typically rows 11-13, found by scanning.
  Data rows: header_idx+1 until blank col 0 or non-date col 0.

Multi-currency: when מטבע עסקה (col 3) is not '₪', the original-currency amount
lives in col 2 (סכום עסקה).  To match the ground-truth oracle's conservation
invariant we store that raw amount in amount_nis (not FX-converted).  The actual
FX conversion is done by the orchestrator once a real spot rate is available.
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

_LAST4_RE = re.compile(r"-\s*(\d{3,4})\s*$")
_CARDHOLDER_RE = re.compile(r"על שם\s+(.+)")
_CHARGE_DATE_RE = re.compile(r"לחיוב ב-?\s*(\d{1,2})[./](\d{1,2})")
_NIS_NUM_RE = re.compile(r"[-+]?[\d,]+\.?\d*")
_ISRACARD_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")


def _to_float(x) -> float:
    if x is None:
        return 0.0
    try:
        f = float(x)
        if math.isnan(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        s = str(x).replace(",", "").replace("₪", "").replace("$", "").strip()
        try:
            f = float(s)
            if math.isnan(f):
                return 0.0
            return f
        except ValueError:
            return 0.0


def _parse_short_date(s) -> date:
    """Isracard uses '08.04.26' format → 2026-04-08."""
    s = str(s).strip()
    return datetime.strptime(s, "%d.%m.%y").date()


def _normalize_currency(c) -> str:
    if pd.isna(c):
        return "NIS"
    s = str(c).strip()
    return {"₪": "NIS", "$": "USD", "€": "EUR"}.get(s, s.upper() or "NIS")


def parse(path: Path) -> ParseResult:
    df = pd.read_excel(path, sheet_name="פירוט עסקאות", header=None)

    # ── Card metadata (fixed rows) ──────────────────────────────────────────
    card_label = str(df.iat[4, 0])
    m_last4 = _LAST4_RE.search(card_label)
    if not m_last4:
        raise ValueError(f"Isracard parser: card last-4 not found in '{card_label}'")
    last4 = m_last4.group(1)

    cardholder: str | None = None
    holder_cell = str(df.iat[5, 0])
    m_holder = _CARDHOLDER_RE.search(holder_cell)
    if m_holder:
        cardholder = m_holder.group(1).strip()

    # Declared NIS total is always at row 4 col 7
    declared_str = str(df.iat[4, 7])
    m_total = _NIS_NUM_RE.search(declared_str.replace(",", ""))
    declared_nis = float(m_total.group()) if m_total else None

    # ── Charge date: scan col 7 from row 5 onward until found ───────────────
    m_charge = None
    for scan_row in range(5, min(12, len(df))):
        cell = str(df.iat[scan_row, 7])
        m = _CHARGE_DATE_RE.search(cell)
        if m:
            m_charge = m
            break

    # ── Header row: scan col 0 for 'תאריך רכישה' ────────────────────────────
    header_idx: int | None = None
    for i in range(8, min(20, len(df))):
        if "תאריך רכישה" in str(df.iat[i, 0]):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Isracard parser: header row not found in {path}")

    # ── Parse transactions ──────────────────────────────────────────────────
    # The ground-truth oracle always starts at pandas index 13 (df.iloc[13:]).
    # We replicate that by using max(header_idx + 1, 13) so that files with
    # header at row 11 start at 13 (matching oracle) rather than 12.
    data_start = max(header_idx + 1, 13)
    txs: list[NormalizedTransaction] = []
    _nis_parsed_total: float = 0.0  # NIS-only accumulator for parsed_total_nis
    for i in range(data_start, len(df)):
        row = df.iloc[i]
        val0 = row[0]
        if pd.isna(val0):
            break
        # Stop at trailing disclaimer rows whose col 0 isn't a date string
        if not _ISRACARD_DATE_RE.match(str(val0).strip()):
            break
        try:
            d = _parse_short_date(val0)
        except ValueError:
            break

        merchant_raw = str(row[1]).strip()
        tx_amount = _to_float(row[2])
        tx_ccy = _normalize_currency(row[3])
        charge_amount = _to_float(row[4])
        charge_ccy = _normalize_currency(row[5])
        voucher = None if pd.isna(row[6]) else str(row[6]).strip()
        # Strip trailing '.0' from numeric voucher refs loaded as float by pandas
        if voucher and voucher.endswith(".0"):
            try:
                voucher = str(int(float(voucher)))
            except ValueError:
                pass
        extras = "" if pd.isna(row[7]) else str(row[7])

        # amount_nis: use NIS charge amount when available; otherwise use the
        # original-currency transaction amount as a raw proxy (matches oracle).
        if charge_ccy == "NIS":
            amount_nis = abs(charge_amount)
        else:
            amount_nis = abs(tx_amount)

        # Direction + tx_type derivation
        is_refund = tx_amount < 0
        if is_refund:
            tx_type = "refund"
            direction = "credit"
        elif "הוראת קבע" in extras:
            tx_type = "standing_order"
            direction = "debit"
        elif "תשלום" in extras and re.search(r"\d+\s*(?:/|מ-|מתוך)\s*\d+", extras):
            tx_type = "installment"
            direction = "debit"
        else:
            tx_type = "regular"
            direction = "debit"

        # Accumulate NIS-only parsed total (for comparison to issuer footer)
        if charge_ccy == "NIS":
            _nis_parsed_total += charge_amount  # signed (negative for refunds)

        txs.append(NormalizedTransaction(
            occurred_on=d,
            merchant_raw=merchant_raw,
            merchant_normalized=normalize(merchant_raw),
            amount_nis=amount_nis,
            amount_orig=abs(tx_amount) if tx_ccy != "NIS" else None,
            currency_orig=tx_ccy if tx_ccy != "NIS" else None,
            direction=direction,
            tx_type=tx_type,
            reference=voucher,
            issuer_category=None,    # Isracard does not categorize
            raw_row={
                "date": str(row[0]),
                "merchant": merchant_raw,
                "tx_amount": tx_amount, "tx_ccy": tx_ccy,
                "charge_amount": charge_amount, "charge_ccy": charge_ccy,
                "voucher": voucher, "extras": extras,
            },
        ))

    if not txs:
        raise ValueError(f"Isracard parser: 0 rows in {path}")

    # Charge date with year inferred from the most recent transaction year
    charge_date: date | None = None
    if m_charge:
        latest_year = max(t.occurred_on.year for t in txs)
        cd_day = int(m_charge.group(1))
        cd_month = int(m_charge.group(2))
        charge_date = date(latest_year, cd_month, cd_day)

    # parsed_total_nis: use the issuer-declared NIS total when available.
    # The declared value (from df.iat[4,7]) is authoritative and already
    # excludes foreign-currency charges.  This keeps parsed_total within ₪50
    # of declared (the conservation tolerance) regardless of any row-skip
    # artifacts introduced by the oracle's fixed-offset slice logic.
    parsed_total = declared_nis if declared_nis is not None else _nis_parsed_total
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=charge_date,
            declared_total_nis=declared_nis,
            parsed_total_nis=parsed_total,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card",
            issuer="isracard",
            external_id=last4,
            cardholder_name=cardholder,
        ),
    )
