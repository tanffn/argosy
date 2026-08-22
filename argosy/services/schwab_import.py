"""Import Schwab holdings — the individual account and the equity-award NVDA.

Two Schwab surfaces, two files, ONE book location each:

  * ``Individual-Positions-<date>.csv``  -> location ``schwab 876``
  * ``EquityAwardsCenter_EquityDetails_<stamp>.xlsx`` -> location ``schwab``
    (NVDA only: RSU/award lots plus ESPP, counted as AVAILABLE TO SELL)

Feeds only those two locations; ``persist_snapshot`` merges per account so the
Leumi rows and real estate carry forward untouched.

**Vested shares only.** ``Available to Sell`` is what the household can actually
transact, and it is what the NVDA glide schedule is denominated in. Unvested
RSUs are deliberately NOT added to the position — but they are not nothing
either: 3,378 shares vest through 2030-03-15, and a glide sized against the
vested count alone under-delivers by exactly that amount. ``future_vests``
surfaces the stream so callers can reason about it explicitly rather than
discovering it in a review.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

__all__ = [
    "SchwabPosition",
    "NvdaEquityAward",
    "SchwabImportReport",
    "parse_individual_positions",
    "parse_equity_details",
    "import_schwab",
]

#: Book location for the individual brokerage account.
LOC_INDIVIDUAL = "schwab 876"
#: Book location for the equity-award (RSU/ESPP) NVDA holding.
LOC_EQUITY = "schwab"

#: Section-102 capital track: 24 months from the award/subscription date.
SECTION_102_MONTHS = 24


@dataclass(frozen=True)
class SchwabPosition:
    symbol: str
    description: str
    quantity: float
    market_value: float
    cost_basis: float | None
    asset_type: str


@dataclass(frozen=True)
class NvdaEquityAward:
    vested_shares: float          # available to sell, RSU + ESPP
    vested_market_value: float
    unvested_shares: float
    future_vests: tuple[tuple[date, float], ...]
    section_102_eligible: float   # subset of vested_shares past the 24m clock

    @property
    def price(self) -> float | None:
        if self.vested_shares <= 0:
            return None
        return self.vested_market_value / self.vested_shares


@dataclass
class SchwabImportReport:
    as_of: date
    individual: list[SchwabPosition] = field(default_factory=list)
    nvda: NvdaEquityAward | None = None
    rows_before: int = 0
    rows_after: int = 0
    other_rows_carried: int = 0
    snapshot_id: int | None = None
    applied: bool = False

    def lines(self) -> list[str]:
        out = [f"as of              {self.as_of}"]
        if self.individual:
            tot = sum(p.market_value for p in self.individual)
            out.append(f"individual (876)   {len(self.individual)} rows, ${tot:,.2f}")
        if self.nvda:
            n = self.nvda
            out += [
                f"NVDA vested        {n.vested_shares:,.0f} sh, "
                f"${n.vested_market_value:,.2f}"
                + (f" (${n.price:,.2f}/sh)" if n.price else ""),
                f"NVDA unvested      {n.unvested_shares:,.0f} sh across "
                f"{len(n.future_vests)} future vests",
                f"  §102 eligible    {n.section_102_eligible:,.0f} sh of the vested count",
            ]
        out += [
            f"schwab rows        {self.rows_before} -> {self.rows_after}",
            f"carried forward    {self.other_rows_carried} non-Schwab rows",
        ]
        out.append(
            f"WROTE snapshot     id={self.snapshot_id}" if self.applied
            else "DRY RUN — nothing written")
        return out


def _money(s: str | None) -> float | None:
    if s is None:
        return None
    t = str(s).replace("$", "").replace(",", "").strip()
    if not t or t == "--":
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_individual_positions(path: str | Path) -> tuple[date, list[SchwabPosition]]:
    """Parse ``Individual-Positions-<date>.csv``.

    The file opens with a title line carrying the as-of timestamp, then a blank
    line, then the header. Trailing ``Positions Total`` and cash rows are
    handled explicitly: cash becomes a position with no symbol so the book
    keeps its own Cash row rather than silently dropping the balance.
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig").splitlines()
    if not raw:
        raise ValueError(f"{p.name}: empty file")
    as_of = None
    for token in raw[0].replace('"', "").split():
        try:
            as_of = datetime.strptime(token.strip(","), "%Y/%m/%d").date()
            break
        except ValueError:
            continue
    if as_of is None:
        raise ValueError(f"{p.name}: no as-of date in the title line: {raw[0][:80]!r}")

    hdr_i = next((i for i, ln in enumerate(raw) if ln.startswith('"Symbol"')), None)
    if hdr_i is None:
        raise ValueError(f"{p.name}: no Symbol header row")
    out: list[SchwabPosition] = []
    for r in csv.DictReader(raw[hdr_i:]):
        sym = (r.get("Symbol") or "").strip()
        if not sym or sym == "Positions Total":
            continue
        mv = _money(r.get("Mkt Val (Market Value)"))
        if mv is None:
            continue
        qty = _money(r.get("Qty (Quantity)"))
        is_cash = sym.lower().startswith("cash")
        out.append(SchwabPosition(
            symbol="" if is_cash else sym,
            description=(r.get("Description") or "").strip(),
            quantity=0.0 if is_cash else (qty or 0.0),
            market_value=mv,
            cost_basis=_money(r.get("Cost Basis")),
            asset_type="Cash" if is_cash else (r.get("Asset Type") or "").strip(),
        ))
    if not out:
        raise ValueError(f"{p.name}: header found but no position rows parsed")
    return as_of, out


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for f in ("%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip(), f).date()
        except (ValueError, TypeError):
            continue
    return None


def parse_equity_details(path: str | Path, *, as_of: date) -> NvdaEquityAward:
    """Parse the Equity Awards Center ``EquityDetails`` workbook.

    Vested = ``Available to Sell`` on the *Equity Award Shares* and *Employee
    Stock Purchase Plans* sheets. Unvested and the forward vest schedule come
    from the *Restricted Stock Units* sheet, whose layout is an award header row
    followed by that award's own vest table.
    """
    import openpyxl

    wb = openpyxl.load_workbook(Path(path), data_only=True)
    cutoff = date(as_of.year - SECTION_102_MONTHS // 12, as_of.month, as_of.day)

    vested = mv = eligible = 0.0
    ws = wb["Equity Award Shares"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h).strip() if h else "" for h in rows[0]]
    iA, iAv, iMv = (hdr.index("Award Date"), hdr.index("Available to Sell"),
                    hdr.index("Market Value"))
    for r in rows[1:]:
        if not r or not r[0] or str(r[0]).strip() == "Totals":
            continue
        av = float(r[iAv] or 0)
        if av <= 0:
            continue
        vested += av
        mv += float(r[iMv] or 0)
        d = _as_date(r[iA])
        if d and d <= cutoff:
            eligible += av

    esp = wb["Employee Stock Purchase Plans"]
    for r in list(esp.iter_rows(values_only=True))[1:]:
        if not r or not r[0] or str(r[0]).strip() == "Totals":
            continue
        av = float(r[8] or 0)
        if av <= 0:
            continue
        vested += av
        mv += float(r[3] or 0)
        d = _as_date(r[9])          # subscription date starts the §102 clock
        if d and d <= cutoff:
            eligible += av

    unvested = 0.0
    vests: list[tuple[date, float]] = []
    for r in list(wb["Restricted Stock Units"].iter_rows(values_only=True)):
        c = [None if v is None else str(v).strip() for v in r]
        if len(c) > 10 and c[0] and c[0] != "Award Date" and c[1] == "NVDA":
            unvested += float(c[10] or 0)
            continue
        if len(c) > 2 and c[1] and c[1] not in ("Vesting Schedule", "Vest Date") and c[2]:
            d = _as_date(c[1])
            if d is None:
                continue
            try:
                n = float(c[2])
            except (TypeError, ValueError):
                continue
            if d > as_of:
                vests.append((d, n))

    if vested <= 0:
        raise ValueError(f"{Path(path).name}: no vested NVDA shares parsed")
    return NvdaEquityAward(
        vested_shares=vested, vested_market_value=mv, unvested_shares=unvested,
        future_vests=tuple(sorted(vests)), section_102_eligible=eligible,
    )


def import_schwab(
    session,
    *,
    user_id: str,
    positions_path: str | Path | None = None,
    equity_details_path: str | Path | None = None,
    as_of: date | None = None,
    apply: bool = False,
) -> SchwabImportReport:
    """Parse the Schwab exports and (optionally) write a merged snapshot."""
    from argosy.ingest.tsv import PortfolioPosition
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        persist_snapshot,
        row_to_snapshot,
    )

    if not positions_path and not equity_details_path:
        raise ValueError("nothing to import: pass positions and/or equity details")

    individual: list[SchwabPosition] = []
    if positions_path:
        as_of_file, individual = parse_individual_positions(positions_path)
        as_of = as_of or as_of_file
    if as_of is None:
        raise ValueError("as_of could not be determined; pass it explicitly")
    nvda = parse_equity_details(equity_details_path, as_of=as_of) if equity_details_path else None

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        raise ValueError(f"no existing snapshot for {user_id} to merge into")
    snap = row_to_snapshot(row)
    loc = lambda p: (getattr(p, "location", "") or "").lower()  # noqa: E731
    touched = {LOC_INDIVIDUAL} if individual else set()
    if nvda:
        touched.add(LOC_EQUITY)
    before = [p for p in snap.positions if loc(p) in touched]
    others = [p for p in snap.positions if loc(p) not in touched]

    # Preserve the deliberate NVDA book flags (managed=False /
    # excluded_from_sleeve_math=True). They encode a policy decision about
    # whether the glide may route orders — never something an importer infers.
    prior_nvda = next(
        (p for p in snap.positions
         if (getattr(p, "symbol", "") or "").upper() == "NVDA"), None)

    stamp = dict(observed_as_of=as_of, valued_as_of=as_of,
                 carried_forward=False, mark_stale=None, review_status="")
    new_rows: list = []
    for p in individual:
        new_rows.append(PortfolioPosition(
            location=LOC_INDIVIDUAL, currency="USD",
            asset_type="Cash" if p.asset_type == "Cash" else (
                "ETF" if "ETF" in p.asset_type.upper() else "Stock"),
            details=p.description, symbol=p.symbol,
            shares=p.quantity or None,
            current_value_local=p.market_value,
            usd_value_k=p.market_value / 1000.0,
            avg_price=(p.cost_basis / p.quantity) if p.cost_basis and p.quantity else None,
            managed=True, excluded_from_sleeve_math=False, **stamp))
    if nvda:
        new_rows.append(PortfolioPosition(
            location=LOC_EQUITY, currency="USD",
            asset_type=getattr(prior_nvda, "asset_type", None) or "NVIDIA",
            details=getattr(prior_nvda, "details", None) or "RSU",
            symbol="NVDA", shares=nvda.vested_shares,
            current_price=nvda.price,
            current_value_local=nvda.vested_market_value,
            usd_value_k=nvda.vested_market_value / 1000.0,
            managed=getattr(prior_nvda, "managed", False),
            excluded_from_sleeve_math=getattr(
                prior_nvda, "excluded_from_sleeve_math", True),
            **stamp))

    report = SchwabImportReport(
        as_of=as_of, individual=individual, nvda=nvda,
        rows_before=len(before), rows_after=len(new_rows),
        other_rows_carried=len(others))
    if not apply:
        return report

    snap.positions = new_rows + others
    snap.snapshot_date = as_of
    snap.parse_warnings = []       # stale warnings describe the PRIOR feed
    written = persist_snapshot(
        session, user_id=user_id, snapshot=snap, actor="schwab_import")
    session.commit()
    report.snapshot_id = written.id
    report.applied = True
    return report
