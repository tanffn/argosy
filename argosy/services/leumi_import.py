"""Import a Leumi securities deposit + cash balances into the book snapshot.

Leumi has no API. Once a month the user exports four files from the website:

  * ``leumi_..._protfolio.xls``   — the deposit ("מבט אישי - האחזקות שלי"),
    SpreadsheetML, parsed by ``adapters.brokers.leumi_portfolio_xls``
  * ``תנועות בחשבון ....xls``      — the ILS current account (HTML)
  * ``תנועות בחשבון מטח ....xls``  — one per foreign currency (HTML)

The transaction ROWS of the latter three go through the expense-ingest
pipeline. This module handles what that pipeline throws away: the **closing
balance** printed in each file's summary header, and the deposit's holdings.

Feeds Leumi-located rows ONLY. ``persist_snapshot`` merges per account, so
Schwab / RSU / pension / real-estate rows are carried forward untouched — the
guard that exists for the Jul-13 NVDA-wipe class of failure. Do not "helpfully"
pass the whole book here; feeding only what the file covers is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from argosy.adapters.brokers.leumi_portfolio_xls import (
    LeumiPortfolio,
    parse_portfolio_xls,
)

__all__ = [
    "LeumiCashBalance",
    "LeumiImportReport",
    "read_balance",
    "build_leumi_positions",
    "import_leumi",
]

_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MONEY_RE = re.compile(r"-?[0-9,]+\.[0-9]{2}")

#: Labels that introduce the closing balance, by export type.
BALANCE_LABELS = ("היתרה", 'יתרת עו"ש')

#: Cell text that marks the start of the movements table. Everything at or
#: after it is transaction data, never the summary balance.
_TABLE_MARKERS = ("תנועות בחשבון", 'תנועות בעו"ש')

#: TASE tracking funds carry NO ticker in the export, only a numeric security
#: id and a Hebrew name. Taking the name's first word yields "ATF" / "MTF" /
#: "אי.בי.אי." — the FUND MANAGER, not the instrument — which silently breaks
#: continuity with the symbols the rest of the book already uses (and with
#: LOOKTHROUGH_MAP, whose coverage test caught exactly this). Key on the id.
TASE_SYMBOLS = {
    "5139951": 'ת"א-200',            # ATF מחקה ת"א-200
    "5122569": "MSCI WORLD",         # MTF מחקה MSCI World
    "5130752": "STOXX EUROPE 600",   # אי.בי.אי. מחקה STOXX Europe 600
}

#: ETFs among the user's Leumi holdings. Anything else is labelled "Stock".
#: Only affects the ``asset_type`` display column, never money math.
_ETF_SYMBOLS = frozenset({
    "SCHD", "CSPX", "EXUS", "FWRA", "SGOV", "ACWD", "CNDX", "QQQM", "EIMI",
    "VTV", "IWDP", "SPMO", "IWQU", "XZEW", "IUHC", "SCHG", "SPMV", "VOO",
    "FUSA", "IBTA", "DPYA",
})


@dataclass(frozen=True)
class LeumiCashBalance:
    currency: str
    amount: float
    source: str


@dataclass
class LeumiImportReport:
    as_of: date
    securities: int = 0
    securities_usd: float = 0.0
    balances: list[LeumiCashBalance] = field(default_factory=list)
    leumi_rows_before: int = 0
    leumi_rows_after: int = 0
    other_rows_carried: int = 0
    snapshot_id: int | None = None
    applied: bool = False

    def lines(self) -> list[str]:
        out = [
            f"as of            {self.as_of}",
            f"securities       {self.securities} rows, ${self.securities_usd:,.2f}",
        ]
        out += [f"balance {b.currency:<4}     {b.amount:,.2f}" for b in self.balances]
        out += [
            f"Leumi rows       {self.leumi_rows_before} -> {self.leumi_rows_after}",
            f"carried forward  {self.other_rows_carried} non-Leumi rows",
        ]
        if self.applied:
            out.append(f"WROTE snapshot   id={self.snapshot_id}")
        else:
            out.append("DRY RUN — nothing written")
        return out


def _cells(path: Path) -> list[str]:
    raw = path.read_bytes()
    text = None
    # UTF-8 FIRST, deliberately. cp1255/iso-8859-8 are single-byte codecs that
    # decode almost any byte sequence WITHOUT raising, so putting either first
    # makes them win on a UTF-8 file and silently produce mojibake — the label
    # lookup then finds nothing and the balance "isn't there". Leumi already
    # ships the portfolio export as UTF-8 and the movements exports as cp1255,
    # so both orders occur in practice; only strict-first is safe.
    for enc in ("utf-8", "windows-1255", "iso-8859-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"{path}: undecodable as any known Leumi encoding")
    out = [
        _TAG_RE.sub("", c).replace("&nbsp;", " ").strip()
        for c in _CELL_RE.findall(text)
    ]
    return [c for c in out if c]


def read_balance(path: str | Path) -> float:
    """Closing balance from a Leumi movements export. Raises if absent.

    Two layouts must both work. The ILS export packs label and value into ONE
    multi-line cell (``'היתרה\\n  ₪\\n  47,100.50'``); the FX exports put the
    value in a FOLLOWING cell. An earlier version read "the next cell" only and
    silently returned a transaction's reference number (13104) as the ILS
    balance — hence the hard stop before the movements table, so a balance
    COLUMN HEADER can never be mistaken for the balance itself.
    """
    p = Path(path)
    cells = _cells(p)
    stop = next(
        (i for i, c in enumerate(cells)
         if any(c.startswith(m) for m in _TABLE_MARKERS)),
        len(cells),
    )
    head = cells[:stop]
    for i, c in enumerate(head):
        if not any(lbl in c for lbl in BALANCE_LABELS):
            continue
        nums = _MONEY_RE.findall(c)
        if nums:
            return float(nums[-1].replace(",", ""))
        for nxt in head[i + 1:i + 4]:
            nums = _MONEY_RE.findall(nxt)
            if nums:
                return float(nums[-1].replace(",", ""))
    raise ValueError(
        f"{p.name}: no closing balance found in the summary block "
        f"(looked for {BALANCE_LABELS} before {_TABLE_MARKERS})"
    )


def build_leumi_positions(
    portfolio: LeumiPortfolio,
    balances: list[LeumiCashBalance],
    *,
    usd_ils: float,
    eur_usd: float,
) -> list:
    """Leumi securities + cash as ``PortfolioPosition`` rows."""
    from argosy.ingest.tsv import PortfolioPosition

    if usd_ils <= 0:
        raise ValueError(f"usd_ils must be positive, got {usd_ils}")
    stamp = dict(
        location="Leumi", managed=True, excluded_from_sleeve_math=False,
        observed_as_of=portfolio.as_of, valued_as_of=portfolio.as_of,
        carried_forward=False, mark_stale=None, review_status="",
    )
    rows: list = []
    for h in portfolio.holdings:
        sym = h.symbol or TASE_SYMBOLS.get(h.security_id) or h.name.split()[0]
        rows.append(PortfolioPosition(
            currency="USD",
            asset_type="ETF" if (h.symbol or "").upper() in _ETF_SYMBOLS
            or h.symbol is None else "Stock",
            details=h.name, symbol=sym, shares=h.quantity,
            current_price=h.last_price, avg_price=h.avg_cost,
            current_value_local=h.value_usd,
            usd_value_k=h.value_usd / 1000.0, **stamp,
        ))
    for b in balances:
        if b.currency == "NIS":
            usd_k = b.amount / usd_ils / 1000.0
        elif b.currency == "EUR":
            usd_k = b.amount * eur_usd / 1000.0
        else:
            usd_k = b.amount / 1000.0
        rows.append(PortfolioPosition(
            currency=b.currency, asset_type="Cash", details="", symbol="",
            current_value_local=b.amount, usd_value_k=usd_k, **stamp,
        ))
    return rows


def import_leumi(
    session,
    *,
    user_id: str,
    portfolio_path: str | Path,
    ils_path: str | Path | None = None,
    fx_paths: dict[str, str | Path] | None = None,
    apply: bool = False,
) -> LeumiImportReport:
    """Parse the exports and (optionally) write a merged snapshot.

    ``fx_paths`` maps currency code -> movements export, e.g.
    ``{"USD": ".../מטח (1).xls", "EUR": ".../מטח (2).xls"}``.

    Aborts before writing if the portfolio file does not reconcile against its
    own header totals — a short read must never become a silent position wipe.
    """
    from argosy.services.fx import rate as fx_rate
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        persist_snapshot,
        row_to_snapshot,
    )

    portfolio = parse_portfolio_xls(portfolio_path)
    if not portfolio.reconciles():
        raise ValueError(
            f"{Path(portfolio_path).name}: parsed {len(portfolio.holdings)} rows / "
            f"${portfolio.parsed_value_usd:,.2f} does not match the file's own "
            f"header ({portfolio.declared_count} / "
            f"${portfolio.declared_value_usd:,.2f}) — refusing to import"
        )

    balances: list[LeumiCashBalance] = []
    if ils_path:
        balances.append(LeumiCashBalance(
            "NIS", read_balance(ils_path), Path(ils_path).name))
    for ccy, p in (fx_paths or {}).items():
        balances.append(LeumiCashBalance(ccy, read_balance(p), Path(p).name))

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        raise ValueError(f"no existing snapshot for {user_id} to merge into")
    snap = row_to_snapshot(row)
    is_leumi = lambda p: "leumi" in (getattr(p, "location", "") or "").lower()  # noqa: E731
    before = [p for p in snap.positions if is_leumi(p)]
    others = [p for p in snap.positions if not is_leumi(p)]

    usd_ils = float(row.fx_usd_nis or 0)
    if usd_ils <= 0:
        usd_ils = float(fx_rate(session, "USD", "ILS", date.today()))
    eur_usd = float(fx_rate(session, "EUR", "ILS", date.today())) / usd_ils

    new_rows = build_leumi_positions(
        portfolio, balances, usd_ils=usd_ils, eur_usd=eur_usd)

    report = LeumiImportReport(
        as_of=portfolio.as_of, securities=len(portfolio.holdings),
        securities_usd=portfolio.parsed_value_usd, balances=balances,
        leumi_rows_before=len(before), leumi_rows_after=len(new_rows),
        other_rows_carried=len(others),
    )
    if not apply:
        return report

    snap.positions = new_rows          # feed covers Leumi only — merge does the rest
    snap.snapshot_date = portfolio.as_of
    written = persist_snapshot(
        session, user_id=user_id, snapshot=snap, actor="leumi_import")
    session.commit()
    report.snapshot_id = written.id
    report.applied = True
    return report
