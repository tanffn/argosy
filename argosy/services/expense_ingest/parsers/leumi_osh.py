"""Parser for Leumi 'Osh' (current-account) HTML-disguised-as-xls export."""

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


# Captures the account-number block following "חשבון" (Hebrew "account") or
# "account". Real Leumi exports format the account a few different ways:
#   "חשבון 882-44745280"      — branch-account, 8-digit account
#   "מס' חשבון: 882-447452/80" — branch-account/checksum (slash-separated)
# We accept any run of digits / dashes / slashes; the caller normalizes by
# stripping non-digits and (if there's a 3-digit branch prefix) dropping it.
_LEUMI_ACCOUNT_RE = re.compile(
    r"(?:חשבון|account)[\s:#\-]*([\d/\-]{6,20})", re.IGNORECASE,
)


def _extract_account_number(path: Path) -> str | None:
    """Read the Leumi HTML and pull the account number from the header.

    Leumi statements include a Hebrew label like 'חשבון 882-44745280' or
    'מס\' חשבון: 882-447452/80' near the top of the document. We grab the
    digits following the label, strip non-digits (commonly '/' or '-'),
    and — if the result is longer than 8 digits — drop the leading branch
    prefix so the returned external_id is the bare 8-digit account number.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = _LEUMI_ACCOUNT_RE.search(text)
    if not m:
        return None
    digits = "".join(c for c in m.group(1) if c.isdigit())
    if not digits:
        return None
    # Leumi accounts are 8 digits. If we captured branch+account
    # (e.g. 88244745280), drop the leading prefix so callers get just
    # the account number.
    return digits[-8:] if len(digits) > 8 else digits


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
                # col 7 is Leumi's 'הערה' (note/remark); col 8 is consistently NaN
                # padding in observed fixtures — kept as a soft fallback for variants.
                "note":          (None if len(row) <= 7 or pd.isna(row[7]) else str(row[7])),
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
        source_hint=SourceHint(
            kind="bank",
            issuer="leumi",
            external_id=_extract_account_number(path) or "",
            display_name="Leumi current account",
        ),
    )
