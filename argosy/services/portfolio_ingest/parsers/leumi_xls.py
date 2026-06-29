"""Parser for Bank Leumi's monthly portfolio-snapshot XLS export.

The file is NOT a real .xls -- it's SpreadsheetML 2003 XML disguised
under the .xls extension (`<?xml version=... ?><Workbook xmlns=...>`).
The user gets it by exporting "View > My Holdings" from the Leumi
web banking; the resulting file lands as e.g.
``Leumi_26_May_01.xls``.

Structure observed in 7+ samples (2025-2026):
  Row 0: title (Hebrew) -- "מבט אישי - האחזקות שלי" (Personal View - My Holdings)
  Row 1: ["תאריך:", "DD.MM.YY", "תיק:", "<account-number>"]
  Row 2: meta stats: ["מס' ניירות:", "<count>", "שווי תיק עדכני ב$", "<total>$", ...]
  Row 3: more meta (event totals)
  Row 4: column headers (Hebrew) -- starts with "מספר נייר", "שם הנייר"
  Rows 5..N: position rows, one per holding.

Each position row has 13 cells:
  0. security_id          7- or 8-digit Israeli securities-authority id
  1. name (Hebrew)        e.g. "(אדוונסד מיקרו דיווייסז) AMD"
                          Foreign-listed: "(ISH NASDAQ100 $A) CNDX LN"
                          Israeli-listed: "ATF מחקה ת\"א-200" (no Latin ticker)
  2. events status        "לא קיים" (none) or "קיים" (yes — corp actions pending)
  3. avg_buy_price        decimal
  4. quantity             decimal
  5. last_price           decimal
  6. holding_value        decimal (the position value; USD until mid-2026,
                          NIS thereafter — see `holding_value_currency`)
  7. daily_change_pct     decimal (0.0075 = 0.75%)
  8. gain_pct             decimal
  9. gain_usd             decimal
  10. pct_of_portfolio    decimal (0.20670... = 20.67% of portfolio)
  11. base_price          decimal
  12. portfolio_number    redundant copy of row-1 portfolio number

This parser is deliberately defensive: Leumi can change the export
format silently. Every row failure is captured in
`parse_warnings` rather than raising -- the caller can decide
whether to surface the partial parse or reject the upload.

The snapshot DOES NOT include cash. Cash position must come from
the corresponding Leumi Osh (current-account) statement; downstream
code merges the two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


PARSER_NAME = "leumi_portfolio_xls"
PARSER_VERSION = "0.1.0"


@dataclass
class LeumiPortfolioPosition:
    """One row in the Leumi portfolio snapshot."""

    security_id: str
    """7- or 8-digit Israeli-securities-authority id. Stable across
    re-exports for the same security."""

    name_he: str
    """Verbatim name cell from the Leumi export. Includes Hebrew
    description + (often) the Latin ticker in parentheses or trailing."""

    ticker: str | None
    """Latin ticker extracted from `name_he` when present (foreign-
    listed securities). None for Israeli-listed (no Latin ticker)."""

    avg_buy_price: float | None
    quantity: float
    last_price: float
    holding_value: float
    """Holding value as exported, denominated in ``holding_value_currency``.
    Leumi switched this column from USD ($) to NIS (₪) in mid-2026; the
    parser records the native value + its currency and leaves the USD
    conversion to the FX-aware caller (a pure parser has no exchange rate).
    ``quantity`` is the authoritative input — value is derivable from it."""
    holding_value_currency: str
    """'USD' or 'NIS' — which currency ``holding_value`` is in, detected from
    the column header ('שווי אחזקה ב $' vs 'שווי אחזקה ב ₪')."""
    gain_pct: float | None
    pct_of_portfolio: float | None

    def usd_value(self, fx_usd_nis: float) -> float:
        """Holding value in USD. Divides by ``fx_usd_nis`` when the export is
        NIS-denominated; returns the value unchanged when already USD."""
        if (self.holding_value_currency or "USD").upper() == "NIS":
            return self.holding_value / max(fx_usd_nis, 0.01)
        return self.holding_value


@dataclass
class LeumiPortfolioSnapshot:
    """Parsed Leumi portfolio export."""

    snapshot_date: date | None
    portfolio_number: str | None
    securities_count: int
    """Declared count from the meta row. Compare against
    `len(positions)` to spot parse losses."""
    total_value: float | None
    """Declared portfolio total from the meta row, in ``total_value_currency``.
    Compare against `sum(p.holding_value for p in positions)` (same currency)
    for the reconciliation check (the two should agree within rounding)."""
    total_value_currency: str
    """'USD' or 'NIS' — currency of ``total_value`` + every position's
    ``holding_value`` (Leumi denominates the whole export in one currency)."""
    positions: list[LeumiPortfolioPosition]
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sniffer + parser
# ---------------------------------------------------------------------------

_TITLE_MARKER_HE = "מבט אישי"  # "מבט אישי" (Personal View)


def is_leumi_portfolio_xls(content: str | bytes) -> bool:
    """Cheap content sniffer.

    Two cumulative markers: the SpreadsheetML preamble (first 1KB
    -- "Workbook" in the XML envelope opener) + the Hebrew "Personal
    View" title (anywhere in the file -- after Leumi's long style
    block, before the first row). Neither alone is sufficient
    (other Leumi exports use the same XML envelope); together
    they're specific enough.
    """
    text = (
        content if isinstance(content, str)
        else content.decode("utf-8", errors="ignore")
    )
    return "Workbook" in text[:1000] and _TITLE_MARKER_HE in text


def parse_leumi_portfolio_xls(content: str | bytes) -> LeumiPortfolioSnapshot:
    """Parse a Leumi portfolio XLS export.

    Accepts the file contents as str (UTF-8 text) or bytes (UTF-8
    decoded internally; non-UTF8 bytes are decoded with errors="ignore"
    to preserve as much as possible -- the format is XML so legal
    chars only).
    """
    text = (
        content if isinstance(content, str)
        else content.decode("utf-8", errors="ignore")
    )
    return _parse(text)


def parse_leumi_portfolio_xls_path(path: Path) -> LeumiPortfolioSnapshot:
    """Convenience wrapper for the path-based callers (CLI, tests)."""
    return parse_leumi_portfolio_xls(path.read_text(encoding="utf-8", errors="ignore"))


# ---------------------------------------------------------------------------
# Internal: row-level parsing
# ---------------------------------------------------------------------------

# Row + Cell regexes. Cell capture includes the ss:Index attribute when
# present (codex-tandem zigzag finding #3, 2026-05-29): SpreadsheetML
# allows sparse rows where a Cell specifies its column position via
# ss:Index="N", meaning the previous cells should be treated as
# empty. The pre-fix _row_cells ignored ss:Index and returned the
# Data values in appearance order, which would silently misalign
# columns on any export that emitted sparse rows.
_ROW_RE = re.compile(r"<(?:ss:)?Row[^>]*>(.*?)</(?:ss:)?Row>", re.DOTALL)
_CELL_BLOCK_RE = re.compile(
    r"<(?:ss:)?Cell\b([^>]*)>(.*?)</(?:ss:)?Cell>", re.DOTALL,
)
_INDEX_ATTR_RE = re.compile(r"""(?:ss:)?Index\s*=\s*["'](\d+)["']""")
_DATA_RE = re.compile(r"<(?:ss:)?Data[^>]*>(.*?)</(?:ss:)?Data>", re.DOTALL)

# Header-row sentinels (Hebrew). Used to LOCATE the header row, not
# to validate any specific column ordering -- we build a column-index
# map from the header below.
_HEADER_C0 = "מספר נייר"   # security id (Hebrew: מספר נייר)
_HEADER_C1 = "שם הנייר"   # security name (Hebrew: שם הנייר)

# Column-name -> internal field-name map. Built from the Leumi
# headers verbatim. Each entry maps a Hebrew header string to the
# field on LeumiPortfolioPosition we'll fill from that column.
# Codex-tandem zigzag finding #2 (2026-05-29): the v0.1 parser
# hard-coded cell indices, which would silently misassign values
# if Leumi inserted or reordered a column. The header-map approach
# binds each numeric field to its semantic Hebrew column header so
# a reorder is tolerated and a rename is loud (warning).
_HEADER_FIELD_MAP: dict[str, str] = {
    "מספר נייר":           "security_id",
    "שם הנייר":            "name_he",
    "שער קניה ממוצע":      "avg_buy_price",
    "כמות אחזקה":          "quantity",
    "שער אחרון":           "last_price",
    # Holding-value column. Leumi switched this from USD ($) to NIS (₪) in
    # mid-2026; both header forms (with/without the space before the symbol)
    # map to the same field. The CURRENCY is detected separately from the
    # matched header text ($ vs ₪) — see `_detect_value_currency`.
    "שווי אחזקה ב $":      "holding_value",
    "שווי אחזקה ב$":       "holding_value",
    "שווי אחזקה ב ₪":      "holding_value",
    "שווי אחזקה ב₪":       "holding_value",
    "רווח ב-%":            "gain_pct",
    "%  מהתיק":            "pct_of_portfolio",
    "% מהתיק":             "pct_of_portfolio",
}


def _detect_currency_from_label(label: str) -> str:
    """Return 'NIS' if a money-column header/label carries the ₪ sign, else
    'USD' (the historical default). Leumi flipped the portfolio export's
    denomination from $ to ₪ in mid-2026; the sign in the header is the
    authoritative signal."""
    return "NIS" if "₪" in (label or "") else "USD"

# Meta-row labels we extract values from.
_META_DATE = "תאריך:"          # date row label
_META_PORTFOLIO = "תיק:"       # portfolio number label
_META_SEC_COUNT = "מס' ניירות:"  # securities count label

# Latin ticker at the end of the Hebrew name, sometimes followed by a
# venue suffix (e.g. "CNDX LN" for LSE-listed). The ticker can contain
# letters, digits, '.', '/' (e.g. "BRK/B").
_LATIN_TICKER_RE = re.compile(
    r"\)\s*([A-Z][A-Z0-9./]{0,8})(?:\s+[A-Z]{2})?\s*$"
)


def _row_cells(row_xml: str) -> list[str]:
    """Extract cell Data values in their TRUE column positions.

    Handles SpreadsheetML sparse-cell syntax: `<Cell ss:Index="N">`
    means "the next Data value is at column N (1-indexed)." The
    parser pads the returned list with empty strings up to that
    index so callers can use stable positional indexing. Codex
    zigzag finding #3 (2026-05-29).
    """
    out: list[str] = []
    cursor = 1  # SpreadsheetML columns are 1-indexed
    for match in _CELL_BLOCK_RE.finditer(row_xml):
        attrs, body = match.group(1), match.group(2)
        idx_match = _INDEX_ATTR_RE.search(attrs)
        if idx_match:
            target = int(idx_match.group(1))
            # Pad out any skipped columns with empty strings.
            while cursor < target:
                out.append("")
                cursor += 1
        data_match = _DATA_RE.search(body)
        text = data_match.group(1).strip() if data_match else ""
        out.append(text)
        cursor += 1
    return out


def _safe_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _extract_ticker(name_he: str) -> str | None:
    """Extract the Latin ticker (if any) from a Leumi position name."""
    m = _LATIN_TICKER_RE.search(name_he)
    return m.group(1) if m else None


def _build_column_map(
    header_cells: list[str], warnings: list[str],
) -> tuple[dict[str, int], str]:
    """Map field-name -> column index, by matching Hebrew header text, and
    detect the holding-value currency from its header ($ vs ₪).

    Codex zigzag finding #2 (2026-05-29). Unknown headers are
    skipped silently (they may be new Leumi columns we don't care
    about); REQUIRED fields missing from the export produce a
    warning + the parser returns partial positions.

    Returns ``(field_to_idx, value_currency)`` where ``value_currency`` is
    'USD' or 'NIS' (defaults to 'USD' when the holding-value column is absent).
    """
    field_to_idx: dict[str, int] = {}
    value_currency = "USD"
    for i, header in enumerate(header_cells):
        normalized = header.strip()
        field = _HEADER_FIELD_MAP.get(normalized)
        if field is not None and field not in field_to_idx:
            field_to_idx[field] = i
            if field == "holding_value":
                value_currency = _detect_currency_from_label(normalized)
    required = ("security_id", "name_he", "quantity", "last_price",
                "holding_value")
    for r in required:
        if r not in field_to_idx:
            warnings.append(
                f"required column {r!r} not found in header row; "
                "Leumi may have renamed it -- check _HEADER_FIELD_MAP"
            )
    return field_to_idx, value_currency


def _maybe_normalize_pct(
    raw_pct: float | None, warnings: list[str], context: str,
) -> float | None:
    """Normalize a "% of portfolio" value to 0..1 if it's actually on a
    0..100 scale. Codex zigzag finding #4 (2026-05-29): Leumi could
    emit either; the parser detects > 1.5 as a 100-scaled value
    (max valid 0..1 fraction is 1.0; max valid percent is 100) and
    normalizes + warns. None passthrough.
    """
    if raw_pct is None:
        return None
    if raw_pct > 1.5:
        warnings.append(
            f"{context}: pct_of_portfolio looks scaled 0..100 "
            f"(value={raw_pct!r}); normalizing to 0..1"
        )
        return raw_pct / 100.0
    return raw_pct


def _parse(text: str) -> LeumiPortfolioSnapshot:
    rows_xml = _ROW_RE.findall(text)
    rows = [_row_cells(r) for r in rows_xml]

    warnings: list[str] = []

    # ---- Meta rows (1, 2) ------------------------------------------------
    snapshot_date: date | None = None
    portfolio_number: str | None = None
    securities_count = 0
    total_value: float | None = None
    total_value_currency = "USD"

    if len(rows) > 1 and len(rows[1]) >= 2 and rows[1][0] == _META_DATE:
        try:
            snapshot_date = datetime.strptime(rows[1][1], "%d.%m.%y").date()
        except ValueError:
            warnings.append(
                f"meta-row 1: could not parse date {rows[1][1]!r}"
            )
        if len(rows[1]) >= 4 and rows[1][2] == _META_PORTFOLIO:
            portfolio_number = rows[1][3] or None

    if len(rows) > 2 and len(rows[2]) >= 2 and rows[2][0] == _META_SEC_COUNT:
        try:
            securities_count = int(rows[2][1])
        except ValueError:
            warnings.append(
                f"meta-row 2: non-integer securities count {rows[2][1]!r}"
            )
        if len(rows[2]) >= 4:
            total_value = _safe_float(rows[2][3])
            # The total's currency is carried in its own label cell
            # ("שווי תיק עדכני ב$" vs "…ב₪"); detect it the same way as the
            # per-position column so meta + positions stay on one basis.
            total_value_currency = _detect_currency_from_label(rows[2][2])

    # ---- Find the position-table header row + build column map ----------
    header_idx: int | None = None
    for i, cells in enumerate(rows[:20]):
        if len(cells) >= 2 and cells[0] == _HEADER_C0 and cells[1] == _HEADER_C1:
            header_idx = i
            break

    if header_idx is None:
        warnings.append(
            "header row not found; expected Hebrew columns "
            "מספר נייר + שם הנייר in the first 20 rows"
        )
        return LeumiPortfolioSnapshot(
            snapshot_date=snapshot_date,
            portfolio_number=portfolio_number,
            securities_count=securities_count,
            total_value=total_value,
            total_value_currency=total_value_currency,
            positions=[],
            parse_warnings=warnings,
        )

    col_idx, value_currency = _build_column_map(rows[header_idx], warnings)
    # The per-position holding-value column header is the authoritative
    # currency signal; the meta-total label should agree, but if Leumi ever
    # disagrees the position column wins (it's what every row carries).
    total_value_currency = value_currency

    def cell(row: list[str], field: str) -> str | None:
        """Look up a row's cell by field-name (via the header map).

        Returns None when the field isn't present in the header
        (we warned earlier) or when the row is too short. Empty-
        string cells return None too so _safe_float treats them as
        missing instead of as 0.0.
        """
        idx = col_idx.get(field)
        if idx is None or idx >= len(row):
            return None
        val = row[idx]
        return val if val != "" else None

    # ---- Position rows ---------------------------------------------------
    positions: list[LeumiPortfolioPosition] = []
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        sec = cell(row, "security_id")
        if not sec or not sec[0].isdigit():
            # Trailing footer / disclaimer / empty row; skip silently.
            continue

        # Money + count fields: codex zigzag finding #1 (2026-05-29).
        # Use None when missing, not 0.0 -- silent zero-coercion would
        # corrupt a position without surfacing the failure. We then
        # validate required fields below; a missing required field
        # gets a warning + the row is skipped.
        name_he = cell(row, "name_he") or ""
        avg_buy_price = _safe_float(cell(row, "avg_buy_price"))
        quantity = _safe_float(cell(row, "quantity"))
        last_price = _safe_float(cell(row, "last_price"))
        holding_value = _safe_float(cell(row, "holding_value"))
        gain_pct = _safe_float(cell(row, "gain_pct"))
        pct_of_portfolio = _maybe_normalize_pct(
            _safe_float(cell(row, "pct_of_portfolio")),
            warnings, context=f"row {i} (security_id={sec})",
        )

        # Required fields: quantity, last_price, holding_value.
        if quantity is None or last_price is None or holding_value is None:
            warnings.append(
                f"row {i} (security_id={sec}): missing required numeric field "
                f"(quantity={quantity!r}, last_price={last_price!r}, "
                f"holding_value={holding_value!r}); row skipped"
            )
            continue

        positions.append(LeumiPortfolioPosition(
            security_id=sec,
            name_he=name_he,
            ticker=_extract_ticker(name_he),
            avg_buy_price=avg_buy_price,
            quantity=quantity,
            last_price=last_price,
            holding_value=holding_value,
            holding_value_currency=value_currency,
            gain_pct=gain_pct,
            pct_of_portfolio=pct_of_portfolio,
        ))

    return LeumiPortfolioSnapshot(
        snapshot_date=snapshot_date,
        portfolio_number=portfolio_number,
        securities_count=securities_count,
        total_value=total_value,
        total_value_currency=total_value_currency,
        positions=positions,
        parse_warnings=warnings,
    )


__all__ = [
    "PARSER_NAME",
    "PARSER_VERSION",
    "LeumiPortfolioPosition",
    "LeumiPortfolioSnapshot",
    "is_leumi_portfolio_xls",
    "parse_leumi_portfolio_xls",
    "parse_leumi_portfolio_xls_path",
]
