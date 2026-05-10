"""Parser for Leumi USD brokerage/holding ('פמ"ח') HTML-as-xls export.

Sibling of ``leumi_osh.py`` — same HTML wrapper, different table shape and
account number. The USD account is denominated in dollars (mostly RSU
sales, equity buys, dividends, MasterCard FX charges). This parser:

  * Reads the largest table (named Hebrew columns: תאריך / תיאור /
    תאור מורחב / אסמכתא / חובה / זכות / יתרה).
  * Stores ``amount_orig`` as USD with ``currency_orig='USD'`` and
    leaves ``amount_nis=None`` (downstream FX conversion in
    ``argosy.services.fx`` is responsible for any NIS rendering).
  * Re-uses the ``leumi_osh`` account-extraction helper and bidi-mark
    stripping; the regex matches both 44745280 (NIS) and 44745200 (USD).

Notes vs. ``leumi_osh.py``:
  * Date format here is ``DD/MM/YY`` (2-digit year), not ``DD/MM/YYYY``.
  * No value-date column — only one date per row.
  * No installment/standing-order semantics in this account; every row
    is ``tx_type='regular'``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.normalize import normalize
from argosy.services.expense_ingest.parsers.leumi_osh import (
    _LEUMI_BIDI_MARKS_RE, _to_float,
)
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, SourceHint, StatementMeta,
)

PARSER_VERSION = "0.1.0"


# Leumi USD ('פמ"ח') exports place the account digits much further from the
# 'חשבון' label than Osh exports do — typically ~700 chars of nested
# <span>/<table> markup separates them. We use a wider window here than
# the Osh helper (200 → 1500) and explicitly anchor on the
# 'NNNNNN/NN NNN' shape Leumi uses for foreign-currency accounts.
_LEUMI_USD_ACCOUNT_RE = re.compile(
    r"חשבון[\s\S]{0,1500}?(\d{6}/\d{2})"
)


def _extract_account_number_usd(path: Path) -> str | None:
    """Pull the 8-digit account number from a Leumi USD HTML export.

    The header carries it as 'NNNNNN/NN NNN' (the 'NNN' suffix is a
    branch-style code: '094' for the foreign-currency desk). We grab
    just the 'NNNNNN/NN' chunk and strip the slash.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    text = _LEUMI_BIDI_MARKS_RE.sub("", text)
    m = _LEUMI_USD_ACCOUNT_RE.search(text)
    if not m:
        return None
    digits = "".join(c for c in m.group(1) if c.isdigit())
    if not digits:
        return None
    return digits[-8:] if len(digits) > 8 else digits


def _parse_dmy_usd(s) -> date | None:
    """Leumi USD export uses DD/MM/YY (2-digit year). Strips leading '*'."""
    if pd.isna(s):
        return None
    if isinstance(s, datetime):
        return s.date()
    cleaned = str(s).strip().lstrip("*").strip()
    return datetime.strptime(cleaned, "%d/%m/%y").date()


def parse(path: Path) -> ParseResult:
    """Parse a Leumi USD ('פמ"ח') HTML export.

    Tables: the transactions table is the largest and pandas reads it
    with named Hebrew column headers (the row layout includes a proper
    header). We iterate each row, skip non-date rows, and emit one
    NormalizedTransaction per data row.
    """
    tables = pd.read_html(path, encoding="utf-8")
    tx_table = max(tables, key=lambda t: t.shape[0])

    # Column accessors — the file always carries these named headers.
    col_date = "תאריך"
    col_desc = "תיאור"
    col_extdesc = "תאור מורחב"
    col_ref = "אסמכתא"
    col_debit = "חובה"
    col_credit = "זכות"
    col_balance = "יתרה"

    txs: list[NormalizedTransaction] = []
    for _, row in tx_table.iterrows():
        raw_date = row[col_date]
        if pd.isna(raw_date):
            continue
        try:
            d = _parse_dmy_usd(raw_date)
        except ValueError:
            # Footer / disclaimer with a non-date value in the date column.
            continue
        if d is None:
            continue

        descr = str(row[col_desc]).strip()
        ref_val = row[col_ref]
        ref: str | None
        if pd.isna(ref_val):
            ref = None
        else:
            ref = str(ref_val).strip()
            # Reference may come through as a float (e.g. 1266.0); strip ".0"
            if ref.endswith(".0"):
                try:
                    ref = str(int(float(ref)))
                except ValueError:
                    pass

        debit = _to_float(row[col_debit])
        credit = _to_float(row[col_credit])
        amount = abs(debit if debit > 0 else credit)
        direction = "debit" if debit > 0 else "credit"

        ext_val = row[col_extdesc]
        bal_val = row[col_balance]

        txs.append(NormalizedTransaction(
            occurred_on=d,
            posted_on=d,                 # USD export carries one date per row
            merchant_raw=descr,
            merchant_normalized=normalize(descr),
            amount_nis=None,             # foreign — downstream FX converts
            amount_orig=amount,
            currency_orig="USD",
            direction=direction,
            tx_type="regular",
            reference=ref,
            issuer_category=None,
            raw_row={
                "date":                  (None if pd.isna(raw_date) else str(raw_date)),
                "description":           (None if pd.isna(row[col_desc]) else str(row[col_desc])),
                "extended_description":  (None if pd.isna(ext_val) else str(ext_val)),
                "reference":             (None if pd.isna(ref_val) else str(ref_val)),
                "debit_usd":             (None if pd.isna(row[col_debit]) else str(row[col_debit])),
                "credit_usd":            (None if pd.isna(row[col_credit]) else str(row[col_credit])),
                "balance_usd":           (None if pd.isna(bal_val) else str(bal_val)),
            },
        ))

    if not txs:
        raise ValueError(f"Leumi USD parser produced 0 rows from {path}")

    # `parsed_total_nis` is the existing schema field — for the USD parser
    # this carries the USD debit total, NOT a NIS figure. The audit /
    # conservation code handles this by treating the parser's totals as
    # whatever currency the parser declared on its rows. Field is
    # misleadingly named for historical reasons (it predates the USD
    # parser); the value is still the parser's "main number to compare".
    parsed_total = sum(
        t.amount_orig for t in txs
        if t.direction == "debit" and t.amount_orig is not None
    )
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=None,
            declared_total_nis=None,
            parsed_total_nis=parsed_total,    # USD total (see comment above)
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="bank",
            issuer="leumi",
            external_id=_extract_account_number_usd(path) or "",
            cardholder_name=None,
            display_name="Leumi USD account",
        ),
    )
