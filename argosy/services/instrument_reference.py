"""Canonical per-instrument classification — the single authority for an
instrument's (asset class, sector, region).

Why this exists: the Leumi/Schwab snapshot's ``asset_type`` and ``symbol``
columns are unreliable (observed: equity ETFs labeled ``REIT``; the literal
``O`` pasted onto three distinct instruments; blank ``asset_type``). Reasoning
from those raw fields alone mis-buckets the book. This module keys off the
*resolved ticker* (see ``tsv._derive_symbol``) and is the primary signal;
the raw ``asset_type`` / name heuristics are only a fallback for instruments
not in the curated table.

These are reference FACTS about known holdings (like
``verified_instruments.yaml`` for the alternatives sleeve), not financial
assumptions — region/domicile are estate-critical and must be deterministic,
never provider-dependent. Add a row when a new instrument enters the book.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- Canonical vocabularies -------------------------------------------------

# Asset class — matches wealth_dashboard._ASSET_CLASS_ORDER.
ASSET_EQUITY = "Equity"
ASSET_FIXED_INCOME = "Fixed Income"
ASSET_CASH = "Cash"
ASSET_ALTERNATIVES = "Alternatives"
ASSET_REAL_ESTATE = "Real Estate"
ASSET_OTHER = "Other"

# Sector — extends the prior wealth_dashboard taxonomy with Financials /
# Healthcare so the big "Other" bucket resolves to something meaningful.
SECTOR_TECH = "Tech"
SECTOR_ETF_INDEX = "ETF/Index"
SECTOR_VALUE_ETF = "Value ETF"
SECTOR_ISRAELI_ETF = "Israeli ETF"
SECTOR_CONGLOMERATE = "Conglomerate"
SECTOR_FINANCIALS = "Financials"
SECTOR_HEALTHCARE = "Healthcare"
SECTOR_REAL_ESTATE = "Real Estate"
SECTOR_CASH_TBILL = "Cash/T-Bill"
SECTOR_CRYPTO = "Crypto"
SECTOR_OTHER = "Other"

# Region.
REGION_US = "US"
REGION_ISRAEL = "Israel"
REGION_EUROPE = "Europe"
REGION_EM = "Emerging Markets"
REGION_GLOBAL = "Global"
REGION_OTHER = "Other"


@dataclass(frozen=True)
class InstrumentRef:
    asset_class: str
    sector: str
    region: str


# --- Curated reference table (keyed by resolved ticker, upper-cased) --------
# Every instrument currently in the book. Comment = plain-language identity so
# an adversarial reviewer can reconcile each row.
_REFERENCE: dict[str, InstrumentRef] = {
    # US mega-cap tech / AI.
    "NVDA": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "AMD": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "GOOG": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "GOOGL": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "AMZN": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "META": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    "TSLA": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US),
    # US single names — financials / healthcare / conglomerate.
    "SOFI": InstrumentRef(ASSET_EQUITY, SECTOR_FINANCIALS, REGION_US),
    "RKT": InstrumentRef(ASSET_EQUITY, SECTOR_FINANCIALS, REGION_US),
    "BRK/B": InstrumentRef(ASSET_EQUITY, SECTOR_CONGLOMERATE, REGION_US),
    "BRK.B": InstrumentRef(ASSET_EQUITY, SECTOR_CONGLOMERATE, REGION_US),
    "BMY": InstrumentRef(ASSET_EQUITY, SECTOR_HEALTHCARE, REGION_US),
    # US broad-market / factor ETFs (US- and UCITS-domiciled both track US).
    "VOO": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "VTI": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "CSPX": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "QQQM": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "CNDX": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "SCHG": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "SCHD": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "SPMO": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "XZEW": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "VTV": InstrumentRef(ASSET_EQUITY, SECTOR_VALUE_ETF, REGION_US),
    # UCITS twins the canonical plan buys (non-US-situs, but track US).
    "FUSA": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "EXUS": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_GLOBAL),
    "R1GR": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "SPMV": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    "DPYA": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_US),
    # Global / developed-world equity ETFs.
    "FWRA": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_GLOBAL),
    "ACWD": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_GLOBAL),
    "MSCI WORLD": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_GLOBAL),
    # Emerging markets.
    "EIMI": InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_EM),
    # Sector ETFs.
    "IUHC": InstrumentRef(ASSET_EQUITY, SECTOR_HEALTHCARE, REGION_US),
    # Real estate (genuine REIT / property ETFs).
    "O": InstrumentRef(ASSET_REAL_ESTATE, SECTOR_REAL_ESTATE, REGION_US),
    "IWDP": InstrumentRef(ASSET_REAL_ESTATE, SECTOR_REAL_ESTATE, REGION_GLOBAL),
    # Cash equivalents / T-bills.
    "SGOV": InstrumentRef(ASSET_CASH, SECTOR_CASH_TBILL, REGION_US),
    "IB01": InstrumentRef(ASSET_CASH, SECTOR_CASH_TBILL, REGION_US),
    "IBTA": InstrumentRef(ASSET_CASH, SECTOR_CASH_TBILL, REGION_US),
    # Crypto.
    "IBIT": InstrumentRef(ASSET_ALTERNATIVES, SECTOR_CRYPTO, REGION_US),
}

# Name-keyword fallback for rows with no resolvable latin ticker (e.g. the
# IBI STOXX Europe 600 tracker whose Symbol cell is the bogus "O"). Matched
# against the lower-cased details string. First hit wins.
_NAME_KEYWORD_FALLBACK: tuple[tuple[str, InstrumentRef], ...] = (
    ("stoxx europe", InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_EUROPE)),
    ("msci world", InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_GLOBAL)),
    ("emerging", InstrumentRef(ASSET_EQUITY, SECTOR_ETF_INDEX, REGION_EM)),
)


def _is_hebrew_ticker(symbol: str) -> bool:
    """A genuinely TASE-listed instrument has a Hebrew/non-latin ticker
    (e.g. ``מחקה ת"א-200``). A Hebrew *description* is not evidence — see
    [[feedback]] on the Israeli-ETF misclassification."""
    return any("֐" <= ch <= "׿" for ch in (symbol or ""))


def lookup(symbol: str, details: str = "") -> InstrumentRef | None:
    """Return the canonical reference for a resolved ticker, or ``None`` when
    the instrument isn't in the curated table and no name-keyword/Israeli
    fallback applies (caller then uses its raw-field heuristic)."""
    sym = (symbol or "").upper().strip()
    if sym in _REFERENCE:
        return _REFERENCE[sym]
    # TASE-listed (Hebrew ticker, no latin symbol) → Israeli equity.
    if _is_hebrew_ticker(symbol):
        return InstrumentRef(ASSET_EQUITY, SECTOR_ISRAELI_ETF, REGION_ISRAEL)
    # No resolvable ticker — fall back to a name keyword in details.
    hay = (details or "").lower()
    for kw, ref in _NAME_KEYWORD_FALLBACK:
        if kw in hay:
            return ref
    return None


__all__ = ["InstrumentRef", "lookup", "REGION_US", "REGION_ISRAEL",
           "REGION_EUROPE", "REGION_EM", "REGION_GLOBAL", "REGION_OTHER"]
