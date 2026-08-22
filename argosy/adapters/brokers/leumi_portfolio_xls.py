"""Parser for Leumi's "מבט אישי - האחזקות שלי" portfolio export.

Leumi has no customer API (see ``leumi_tsv``), but the website exports the
securities deposit as a **SpreadsheetML** file with an ``.xls`` extension. It is
neither BIFF nor HTML, so ``expense_ingest.sniff.detect_format`` rejects it — a
different pipeline from the transaction statements, which are HTML tables.

This is the only source that gives per-symbol SHARE COUNTS and average cost for
the Leumi deposit directly from the bank. The whole-book TSV snapshot is
maintained by hand, so this file is what makes it auditable.

Two traps the format sets, both handled below:

* **Tickers are buried in the Hebrew name.** The ``שם הנייר`` column reads
  ``(Alphabet Inc-Cl C) GOOG`` or ``(איי-שארס אג""ח אוצר אמריקאי 0-3 חודשים) SGOV``
  — the symbol trails the parenthesised description, and for the London and
  Swiss lines it carries a venue suffix: ``CSPX LN``, ``IWDP SW``. TASE tracking
  funds have no ticker at all, only a numeric security id and a name like
  ``ATF מחקה ת"א-200``.
* **``שווי אחזקה ב $`` is already USD** for every row including the TASE lines,
  so the file needs no FX conversion — but ``שער אחרון`` for those same TASE
  rows is quoted in **agorot**, not dollars. Never derive value from
  price × quantity here; take the bank's own USD figure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

__all__ = ["LeumiHolding", "LeumiPortfolio", "parse_portfolio_xls"]

_ROW_RE = re.compile(r"<Row[^>]*>(.*?)</Row>", re.S)
_CELL_RE = re.compile(r"<Cell[^>]*>(.*?)</Cell>", re.S)
_DATA_RE = re.compile(r"<Data[^>]*>(.*?)</Data>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

#: Venue suffixes Leumi appends to non-US listings. Stripped for the bare
#: symbol, preserved on ``venue`` so LSE CSPX is never confused with a US line.
_VENUES = ("LN", "SW", "GY", "FP", "NA", "IM", "SE", "SS")

#: Header cell that marks the start of the holdings table.
_HEADER_FIRST = "מספר נייר"


@dataclass(frozen=True)
class LeumiHolding:
    security_id: str          # Leumi/TASE numeric id — the only stable key
    name: str                 # raw name cell, Hebrew and all
    symbol: str | None        # extracted ticker, None for TASE tracking funds
    venue: str | None         # "LN", "SW", ... None for US lines
    quantity: float
    avg_cost: float | None    # in the security's own quote currency
    last_price: float | None  # ditto — agorot for TASE lines, NOT dollars
    value_usd: float          # the bank's own USD figure. Authoritative.
    pnl_usd: float | None
    pct_of_portfolio: float | None


@dataclass(frozen=True)
class LeumiPortfolio:
    as_of: date
    account: str
    declared_count: int       # the "מס' ניירות" header figure
    declared_value_usd: float  # the "שווי תיק עדכני ב$" header figure
    holdings: tuple[LeumiHolding, ...]

    @property
    def parsed_value_usd(self) -> float:
        return round(sum(h.value_usd for h in self.holdings), 2)

    def reconciles(self, tol_usd: float = 1.0) -> bool:
        """Do the parsed rows add up to the total the bank printed?

        The check that matters: a silently dropped row is invisible in a list of
        38 holdings but shows up here immediately.
        """
        return (
            len(self.holdings) == self.declared_count
            and abs(self.parsed_value_usd - self.declared_value_usd) <= tol_usd
        )


def _cells(row_xml: str) -> list[str]:
    out: list[str] = []
    for cell in _CELL_RE.findall(row_xml):
        m = _DATA_RE.search(cell)
        out.append(_TAG_RE.sub("", m.group(1)).strip() if m else "")
    return out


def _num(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _split_symbol(name: str) -> tuple[str | None, str | None]:
    """``"(Ishares Core S&P 500) CSPX LN"`` -> ``("CSPX", "LN")``.

    Returns ``(None, None)`` for TASE tracking funds, which carry no ticker.
    Anything after the final ``)`` is the symbol; a trailing venue token is
    split off. Guarding on ``_VENUES`` rather than "any 2-letter tail" keeps
    ``BRK/B`` and other real symbols intact.
    """
    tail = name.rsplit(")", 1)[-1].strip() if ")" in name else ""
    if not tail:
        return None, None
    parts = tail.split()
    if len(parts) >= 2 and parts[-1].upper() in _VENUES:
        return " ".join(parts[:-1]), parts[-1].upper()
    return tail, None


def parse_portfolio_xls(path: str | Path) -> LeumiPortfolio:
    """Parse a Leumi portfolio export. Raises ``ValueError`` on a bad file.

    Fails loudly rather than returning a short list: a partially-parsed
    portfolio that looks plausible is worse than no portfolio at all.
    """
    text = Path(path).read_bytes().decode("utf-8")
    rows = [_cells(r) for r in _ROW_RE.findall(text)]
    if not rows:
        raise ValueError(f"{path}: no <Row> elements — not a SpreadsheetML export")

    as_of, account, count, total = None, "", 0, 0.0
    header_idx = None
    for i, cs in enumerate(rows):
        if cs and cs[0] == _HEADER_FIRST:
            header_idx = i
            break
        for j, c in enumerate(cs):
            if c == "תאריך:" and j + 1 < len(cs):
                d = cs[j + 1].strip()
                m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})$", d)
                if m:
                    as_of = date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
            elif c == "תיק:" and j + 1 < len(cs):
                account = cs[j + 1].strip()
            elif c.startswith("מס' ניירות") and j + 1 < len(cs):
                count = int(_num(cs[j + 1]) or 0)
            elif c.startswith("שווי תיק עדכני") and j + 1 < len(cs):
                total = _num(cs[j + 1]) or 0.0

    if header_idx is None:
        raise ValueError(f"{path}: holdings header row ({_HEADER_FIRST!r}) not found")
    if as_of is None:
        raise ValueError(f"{path}: could not read the 'תאריך:' header")

    holdings: list[LeumiHolding] = []
    for cs in rows[header_idx + 1:]:
        if len(cs) < 11 or not cs[0] or not cs[0].isdigit():
            continue
        sym, venue = _split_symbol(cs[1])
        value = _num(cs[6])
        qty = _num(cs[4])
        if value is None or qty is None:
            continue
        holdings.append(LeumiHolding(
            security_id=cs[0], name=cs[1], symbol=sym, venue=venue,
            quantity=qty, avg_cost=_num(cs[3]), last_price=_num(cs[5]),
            value_usd=value, pnl_usd=_num(cs[9]), pct_of_portfolio=_num(cs[10]),
        ))

    if not holdings:
        raise ValueError(f"{path}: header found but no holding rows parsed")
    return LeumiPortfolio(
        as_of=as_of, account=account, declared_count=count,
        declared_value_usd=total, holdings=tuple(holdings),
    )
