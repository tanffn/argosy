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
  6. holding_value_usd    decimal (the canonical position value)
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
    holding_value_usd: float
    gain_pct: float | None
    pct_of_portfolio: float | None


@dataclass
class LeumiPortfolioSnapshot:
    """Parsed Leumi portfolio export."""

    snapshot_date: date | None
    portfolio_number: str | None
    securities_count: int
    """Declared count from the meta row. Compare against
    `len(positions)` to spot parse losses."""
    total_value_usd: float | None
    """Declared total from the meta row. Compare against
    `sum(p.holding_value_usd for p in positions)` for the reconciliation
    check (the two should agree within rounding)."""
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

_ROW_RE = re.compile(r"<(?:ss:)?Row[^>]*>(.*?)</(?:ss:)?Row>", re.DOTALL)
_CELL_RE = re.compile(r"<(?:ss:)?Data[^>]*>(.*?)</(?:ss:)?Data>", re.DOTALL)

# Header cell markers (Hebrew) -- two cells we expect to find in the header row.
_HEADER_C0 = "מספר נייר"   # מספר נייר
_HEADER_C1 = "שם הנייר"          # שם הנייר

# Meta-row labels we extract values from.
_META_DATE = "תאריך:"                     # תאריך:
_META_PORTFOLIO = "תיק:"                            # תיק:
_META_SEC_COUNT = "מס' ניירות:"  # מס' ניירות:

# Latin ticker at the end of the Hebrew name, sometimes followed by a
# venue suffix (e.g. "CNDX LN" for LSE-listed). The ticker can contain
# letters, digits, '.', '/' (e.g. "BRK/B").
_LATIN_TICKER_RE = re.compile(
    r"\)\s*([A-Z][A-Z0-9./]{0,8})(?:\s+[A-Z]{2})?\s*$"
)


def _row_cells(row_xml: str) -> list[str]:
    """Extract Cell Data text strings, in row order."""
    return [c.strip() for c in _CELL_RE.findall(row_xml)]


def _safe_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _extract_ticker(name_he: str) -> str | None:
    """Extract the Latin ticker (if any) from a Leumi position name."""
    m = _LATIN_TICKER_RE.search(name_he)
    return m.group(1) if m else None


def _parse(text: str) -> LeumiPortfolioSnapshot:
    rows_xml = _ROW_RE.findall(text)
    rows = [_row_cells(r) for r in rows_xml]

    warnings: list[str] = []

    # ---- Meta rows (1, 2) ------------------------------------------------
    snapshot_date: date | None = None
    portfolio_number: str | None = None
    securities_count = 0
    total_value_usd: float | None = None

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
            total_value_usd = _safe_float(rows[2][3])

    # ---- Find the position-table header row ------------------------------
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
            total_value_usd=total_value_usd,
            positions=[],
            parse_warnings=warnings,
        )

    # ---- Position rows ---------------------------------------------------
    positions: list[LeumiPortfolioPosition] = []
    for i in range(header_idx + 1, len(rows)):
        cells = rows[i]
        if len(cells) < 7:
            # Probably a trailing footer / disclaimer row; skip silently.
            continue
        # Sanity: a position row's first cell is a numeric security id.
        if not cells[0] or not cells[0][0].isdigit():
            continue
        try:
            positions.append(LeumiPortfolioPosition(
                security_id=cells[0],
                name_he=cells[1],
                ticker=_extract_ticker(cells[1]),
                avg_buy_price=_safe_float(cells[3]) if len(cells) > 3 else None,
                quantity=_safe_float(cells[4]) or 0.0,
                last_price=_safe_float(cells[5]) or 0.0,
                holding_value_usd=_safe_float(cells[6]) or 0.0,
                gain_pct=_safe_float(cells[8]) if len(cells) > 8 else None,
                pct_of_portfolio=(
                    _safe_float(cells[10]) if len(cells) > 10 else None
                ),
            ))
        except (ValueError, IndexError) as exc:
            warnings.append(f"row {i}: parse failed -- {exc!r}")
            continue

    return LeumiPortfolioSnapshot(
        snapshot_date=snapshot_date,
        portfolio_number=portfolio_number,
        securities_count=securities_count,
        total_value_usd=total_value_usd,
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
