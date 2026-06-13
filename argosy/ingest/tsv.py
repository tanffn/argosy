"""Parse the Leumi+Schwab combined Family Finances Status TSV.

Format (per `LLM_Advisor_Handoff.md` and verified against the May 2026 file):

  Row 1: empty / date in col B (e.g. '24-Mar-26')
  Row 2: empty / 'USD to NIS:' in col B / rate in col C
  Row 3: empty / 'USD to EUR:' in col B / rate in col C
  Row 4: blank
  Row 5: 'Bank account / funds allocation' (section header)
  Row 6: column headers — Review Status, Location, Currency, Type,
         Details, Symbol, # Shares, Current price, Avg Price,
         Current Value, (K) USD Value, % Change, % Yearly
  Rows 7..N: position rows. Some are summary/notes (Aborad real estate
         row, sanity-check rows etc.)
  Then: blank → 'Real estate details:' → real-estate rows → Sum row
  Then: 'Current allocation:' → allocation table
  Then: 'NVDA Sales History:' → quarterly sales rows
  Then: 'Pensions/Saving accounts (as of <month>)' → rows per family
        member, sub-rows per account type

Defensive parsing: the bank can change formats unilaterally (SDD OPEN-3).
We never *assume* a row is parseable; we skip with a warning and continue.

Output: a `PortfolioSnapshot` pydantic model holding everything we
successfully extracted. The plan-critique agent then summarizes this.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _normalize_number(s: str) -> float | None:
    """Parse a TSV cell to float. Handles '12,345', '5%', '-', '' etc."""
    if s is None:
        return None
    t = str(s).strip()
    if not t or t in {"-", "—", "?"}:
        return None
    # Strip percent + commas + thousands separators.
    t = t.replace(",", "").replace("\xa0", "").strip()
    if t.endswith("%"):
        t = t[:-1].strip()
        try:
            return float(t)
        except ValueError:
            return None
    try:
        return float(t)
    except ValueError:
        return None


def _normalize_int(s: str) -> int | None:
    f = _normalize_number(s)
    if f is None:
        return None
    return int(f)


def _parse_snapshot_date(raw: str) -> date | None:
    """Parse 'DD-Mon-YY' style ('24-Mar-26'), tolerant of variations."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # Try month-year fallback ('Mar-26').
    m = re.match(r"^([A-Za-z]{3})-(\d{2,4})$", raw)
    if m:
        try:
            mon = datetime.strptime(m.group(1), "%b").month
            yr = int(m.group(2))
            if yr < 100:
                yr += 2000
            return date(yr, mon, 1)
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------
# Pydantic shapes
# ----------------------------------------------------------------------


class PortfolioPosition(BaseModel):
    """One holding row from the TSV."""

    review_status: str = ""
    location: str = ""  # 'schwab 876', 'Leumi', 'Aborad', etc.
    currency: str = ""
    asset_type: str = ""  # 'Cash', 'Dividend', 'Core Equity', ...
    details: str = ""  # 'ETF', 'Stock, AI', 'Treasuries', ...
    symbol: str = ""
    shares: float | None = None
    current_price: float | None = None
    avg_price: float | None = None
    current_value_local: float | None = None  # in `currency`
    usd_value_k: float | None = None  # column "(K) USD Value"
    pct_change: float | None = None
    pct_yearly: float | None = None
    raw_line: int = 0  # 1-based source line for debugging


class RealEstatePosition(BaseModel):
    location: str = ""
    currency: str = ""
    role: str = ""  # 'Home' | 'Loan'
    value_local: float | None = None
    raw_line: int = 0


class AllocationRow(BaseModel):
    category: str
    pct: float | None = None
    usd_value_k: float | None = None
    target_pct: float | None = None
    target_k: float | None = None
    delta_k: float | None = None


class NVDASale(BaseModel):
    month: str
    shares: int | None = None
    price: float | None = None


class PensionEntry(BaseModel):
    person: str
    account_type: str
    value: float | None = None
    currency: str = "NIS"


class PortfolioSnapshot(BaseModel):
    """All structured data extracted from one TSV file."""

    source_path: str
    snapshot_date: date | None = None
    fx_usd_nis: float | None = None
    fx_usd_eur: float | None = None
    positions: list[PortfolioPosition] = Field(default_factory=list)
    real_estate: list[RealEstatePosition] = Field(default_factory=list)
    allocations: list[AllocationRow] = Field(default_factory=list)
    nvda_sales: list[NVDASale] = Field(default_factory=list)
    pensions: list[PensionEntry] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)

    # Convenience aggregates
    @property
    def total_usd_value_k(self) -> float:
        return sum(p.usd_value_k or 0.0 for p in self.positions)

    def cash_balances_usd_k(self) -> float:
        return sum(
            p.usd_value_k or 0.0 for p in self.positions if (p.asset_type or "").lower() == "cash"
        )

    def summary_text(self) -> str:
        """Human-readable summary suitable as plan-critique input."""
        lines: list[str] = []
        lines.append(f"Snapshot date: {self.snapshot_date or 'unknown'}")
        if self.fx_usd_nis is not None:
            lines.append(f"FX USD/NIS: {self.fx_usd_nis}")
        if self.fx_usd_eur is not None:
            lines.append(f"FX USD/EUR: {self.fx_usd_eur}")
        lines.append(f"Total positions parsed: {len(self.positions)}")
        lines.append(f"Total liquid USD value (K): {self.total_usd_value_k:,.0f}")
        # Top 10 positions by USD value (K)
        top = sorted(
            (p for p in self.positions if p.usd_value_k),
            key=lambda p: -(p.usd_value_k or 0),
        )[:10]
        if top:
            lines.append("Top positions:")
            for p in top:
                lines.append(
                    f"  - {p.symbol or p.details or p.asset_type or '(unnamed)'}: "
                    f"${(p.usd_value_k or 0):,.0f}K @ {p.location} ({p.currency})"
                )
        if self.allocations:
            lines.append("Allocation vs target (top 8):")
            for a in self.allocations[:8]:
                lines.append(
                    f"  - {a.category}: current pct={a.pct}, target pct={a.target_pct}, "
                    f"delta_k={a.delta_k}"
                )
        if self.nvda_sales:
            lines.append("NVDA sales history (this snapshot):")
            for s in self.nvda_sales:
                lines.append(f"  - {s.month}: {s.shares} sh @ ${s.price}")
        if self.pensions:
            lines.append("Pensions/savings (self-reported):")
            for pn in self.pensions:
                lines.append(
                    f"  - {pn.person} / {pn.account_type}: {pn.value} {pn.currency}"
                )
        if self.parse_warnings:
            lines.append(f"Parse warnings ({len(self.parse_warnings)}):")
            for w in self.parse_warnings[:10]:
                lines.append(f"  - {w}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


# Column indices in the position table (0-based).
COL_REVIEW = 0
COL_LOCATION = 1
COL_CURRENCY = 2
COL_TYPE = 3
COL_DETAILS = 4
COL_SYMBOL = 5
COL_SHARES = 6
COL_PRICE = 7
COL_AVG = 8
COL_VALUE_LOCAL = 9
COL_USD_K = 10
COL_PCT_CHANGE = 11
COL_PCT_YEAR = 12

POSITION_HEADER_TOKENS = ("Review Status", "Location", "Currency", "Type", "Symbol")


def parse_portfolio_tsv(path: str | Path) -> PortfolioSnapshot:
    """Parse one Family Finances Status TSV into a `PortfolioSnapshot`.

    Defensive: bad rows yield warnings, never exceptions.
    """
    p = Path(path)
    rows: list[list[str]] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            rows.append(row)

    snap = PortfolioSnapshot(source_path=str(p.resolve()))

    # ------------------------------------------------------------------
    # Header rows (1-indexed line numbers used in warnings).
    # ------------------------------------------------------------------
    if rows:
        # Row 1 (index 0): date in col B.
        snap.snapshot_date = _parse_snapshot_date(rows[0][1] if len(rows[0]) > 1 else "")
    # Rows 2 + 3: FX rates ('USD to NIS:' / 'USD to EUR:' label in col B,
    # value in col C).
    for i in (1, 2):
        if i >= len(rows):
            break
        r = rows[i]
        if len(r) < 3:
            continue
        label = (r[1] or "").strip().lower()
        val = _normalize_number(r[2] or "")
        if "usd to nis" in label:
            snap.fx_usd_nis = val
        elif "usd to eur" in label:
            snap.fx_usd_eur = val

    # ------------------------------------------------------------------
    # Locate the position-table header row.
    # ------------------------------------------------------------------
    pos_header_idx: int | None = None
    for i, r in enumerate(rows):
        if any(tok in (cell or "") for tok in POSITION_HEADER_TOKENS for cell in r[:6]):
            pos_header_idx = i
            break
    if pos_header_idx is None:
        snap.parse_warnings.append("Could not locate position-table header row")
        return snap

    # ------------------------------------------------------------------
    # Parse position rows until we hit a non-position section header.
    # ------------------------------------------------------------------
    section_terminators = (
        "real estate details",
        "current allocation",
        "nvda sales history",
        "pensions",
    )

    i = pos_header_idx + 1
    while i < len(rows):
        r = rows[i]
        joined_lower = " ".join((c or "").strip().lower() for c in r)
        # End of position table when we hit a known section header.
        if any(term in joined_lower for term in section_terminators):
            break
        # Treat purely-empty rows as separators between sections; if the
        # *next* non-empty row is a section header, stop.
        if not any((c or "").strip() for c in r):
            i += 1
            continue
        try:
            pos = _parse_position_row(r, line_no=i + 1)
            if pos is not None:
                snap.positions.append(pos)
        except Exception as exc:  # defensive
            snap.parse_warnings.append(f"Row {i + 1}: {exc}")
        i += 1

    # ------------------------------------------------------------------
    # Parse remaining sections (real estate, allocation, NVDA sales, pensions).
    # We do a simple section-machine: scan rows once, switch state on
    # known headers.
    # ------------------------------------------------------------------
    state: str | None = None
    current_person: str | None = None
    for i in range(pos_header_idx + 1, len(rows)):
        r = rows[i]
        joined_lower = " ".join((c or "").strip().lower() for c in r)

        if "real estate details" in joined_lower:
            state = "real_estate"
            continue
        if "current allocation" in joined_lower:
            state = "allocation"
            continue
        if "nvda sales history" in joined_lower:
            state = "nvda"
            continue
        if "pensions" in joined_lower or "saving accounts" in joined_lower:
            state = "pensions"
            current_person = None
            continue

        # Skip empty rows.
        if not any((c or "").strip() for c in r):
            continue

        if state == "real_estate":
            entry = _parse_real_estate_row(r, line_no=i + 1)
            if entry is not None:
                snap.real_estate.append(entry)
        elif state == "allocation":
            entry = _parse_allocation_row(r)
            if entry is not None:
                snap.allocations.append(entry)
        elif state == "nvda":
            entry = _parse_nvda_row(r)
            if entry is not None:
                snap.nvda_sales.append(entry)
        elif state == "pensions":
            new_person, pension_entry = _parse_pension_row(r, current_person)
            if new_person is not None:
                current_person = new_person
            if pension_entry is not None:
                snap.pensions.append(pension_entry)

    return snap


# Matches the Leumi Details pattern "(<name>) TICKER [EXCHANGE]" and captures
# the trailing ticker (first token after the closing paren). The ticker must
# start with a latin letter so Hebrew-only names don't yield a bogus symbol.
_TICKER_AFTER_PAREN = re.compile(r"\)\s*([A-Za-z][A-Za-z0-9./]*)")


def _derive_symbol(details: str, raw_symbol: str) -> str:
    """Return the canonical ticker for a position row.

    The Leumi export's Symbol column is unreliable: the same literal 'O' was
    observed pasted onto the STOXX Europe 600 and EIMI rows. The Details
    column, however, reliably carries '(<name>) TICKER [EXCHANGE]' for Leumi
    holdings, and that trailing ticker is authoritative. When Details has no
    such latin ticker (Schwab rows whose Details is a plain category like
    'ETF'/'RSU', or a Hebrew-only TASE name), keep the cell symbol verbatim.
    """
    if details:
        m = _TICKER_AFTER_PAREN.search(details)
        if m:
            return m.group(1).strip()
    return (raw_symbol or "").strip()


def _parse_position_row(row: list[str], *, line_no: int) -> PortfolioPosition | None:
    """Parse one row of the position table. Return None for non-positions."""
    # Defensive: ensure at least the canonical column count.
    cells = [c.strip() if isinstance(c, str) else "" for c in row]
    while len(cells) < 13:
        cells.append("")

    location = cells[COL_LOCATION]
    if not location:
        # Allocation summary lines and similar floaters; skip silently.
        return None
    # The 'Sum:' row at the end of the bank-account block.
    if "sum" in location.lower():
        return None

    # Real-estate "Aborad" row in the May 2026 TSV is intentionally part of
    # the position list; classify it as a position with asset_type=Real estate.
    return PortfolioPosition(
        review_status=cells[COL_REVIEW],
        location=location,
        currency=cells[COL_CURRENCY],
        asset_type=cells[COL_TYPE],
        details=cells[COL_DETAILS],
        symbol=_derive_symbol(cells[COL_DETAILS], cells[COL_SYMBOL]),
        shares=_normalize_number(cells[COL_SHARES]),
        current_price=_normalize_number(cells[COL_PRICE]),
        avg_price=_normalize_number(cells[COL_AVG]),
        current_value_local=_normalize_number(cells[COL_VALUE_LOCAL]),
        usd_value_k=_normalize_number(cells[COL_USD_K]),
        pct_change=_normalize_number(cells[COL_PCT_CHANGE]),
        pct_yearly=_normalize_number(cells[COL_PCT_YEAR]),
        raw_line=line_no,
    )


def _parse_real_estate_row(row: list[str], *, line_no: int) -> RealEstatePosition | None:
    cells = [c.strip() if isinstance(c, str) else "" for c in row]
    while len(cells) < 13:
        cells.append("")
    location = cells[COL_LOCATION]
    if not location:
        return None
    role = cells[COL_DETAILS]  # 'Home' | 'Loan'
    if not role:
        return None
    # Read the value from COL_PRICE (c7), not COL_VALUE_LOCAL (c9): the
    # "Current Value" column is unreliable for real estate (Atlanta's c9 is 0
    # while c7 holds the actual $318k), per the codex net-equity review. c7 is
    # the home value (Home row) / outstanding loan principal (Loan row).
    return RealEstatePosition(
        location=location,
        currency=cells[COL_CURRENCY],
        role=role,
        value_local=_normalize_number(cells[COL_PRICE]),
        raw_line=line_no,
    )


def _parse_allocation_row(row: list[str]) -> AllocationRow | None:
    cells = [c.strip() if isinstance(c, str) else "" for c in row]
    while len(cells) < 8:
        cells.append("")
    # Allocation row format (indices align with column letters in the TSV):
    #   col 1: Category  (e.g. 'Alternative')
    #   col 2: SUM of (K) USD Value as percent  (e.g. '0.00%')
    #   col 3: SUM of (K) USD Value             (e.g. '0' or '188')
    #   col 4: TargetPct                        (e.g. '3%')
    #   col 5: TargetK                          (e.g. '43.6')
    #   col 6: Delta (K) USD                    (e.g. '43.6')
    category = cells[1]
    if not category:
        return None
    # Filter out the meta header row repeated at top of allocation section.
    if category.lower() in {"type", "category"}:
        return None
    if "additions" in category.lower():
        return None
    return AllocationRow(
        category=category,
        pct=_normalize_number(cells[2]),
        usd_value_k=_normalize_number(cells[3]),
        target_pct=_normalize_number(cells[4]),
        target_k=_normalize_number(cells[5]),
        delta_k=_normalize_number(cells[6]),
    )


def _parse_nvda_row(row: list[str]) -> NVDASale | None:
    cells = [c.strip() if isinstance(c, str) else "" for c in row]
    while len(cells) < 5:
        cells.append("")
    month = cells[1]
    if not month or month.lower() in {"month", "total"}:
        # Total row carries no per-month info but it's not a per-sale entry.
        # If it's 'Total', we still skip because the model captures per-event sales.
        return None
    shares = _normalize_int(cells[2])
    price = _normalize_number(cells[3])
    if shares is None and price is None:
        return None
    return NVDASale(month=month, shares=shares, price=price)


def _parse_pension_row(row: list[str], current_person: str | None) -> tuple[str | None, PensionEntry | None]:
    """Returns (new_person_or_None, pension_entry_or_None).

    Person rows look like: ['', 'Ariel Jacob']
    Account rows look like: ['', '', 'Keren Hishtalmut', '384,000', 'NIS']
    """
    cells = [c.strip() if isinstance(c, str) else "" for c in row]
    while len(cells) < 6:
        cells.append("")
    # Person header: col B has a name and col C is empty.
    if cells[1] and not cells[2]:
        return cells[1], None
    # Account row: col C = account type, col D = value, col E = currency.
    if cells[2]:
        if current_person is None:
            return None, None
        value = _normalize_number(cells[3])
        currency = cells[4] or "NIS"
        return None, PensionEntry(
            person=current_person,
            account_type=cells[2],
            value=value,
            currency=currency,
        )
    return None, None


__all__ = [
    "AllocationRow",
    "NVDASale",
    "PensionEntry",
    "PortfolioPosition",
    "PortfolioSnapshot",
    "RealEstatePosition",
    "parse_portfolio_tsv",
]
