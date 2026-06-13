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

from dataclasses import dataclass, replace as _replace

# --- Canonical vocabularies -------------------------------------------------

# Asset class — matches wealth_dashboard._ASSET_CLASS_ORDER.
ASSET_EQUITY = "Equity"
ASSET_FIXED_INCOME = "Fixed Income"
ASSET_CASH = "Cash"
ASSET_ALTERNATIVES = "Alternatives"
ASSET_REAL_ESTATE = "Real Estate"
ASSET_OTHER = "Other"

# Instrument STRUCTURE — the wrapper, orthogonal to sector/exposure. A single
# stock and a broad-index ETF are both "Equity" asset class but differ in what
# they ARE. This is the level-1 of the per-account "Type" column; sector is
# level-2. Not captured by asset_class (which is Equity/FI/Cash/Alt/RE).
STRUCT_STOCK = "Stock"
STRUCT_ETF = "ETF"
STRUCT_REIT = "REIT"
STRUCT_BOND = "Bond"
STRUCT_CASH = "Cash"

# Level-2 EXPOSURE / STYLE category (``sector`` for historical reasons — the
# attribute name is kept to avoid churning ~70 call sites + the API DTO, but it
# is genuinely a mixed "category" axis: a GICS sector for single stocks, a
# style for funds, a sub-asset for T-bills/crypto). The "ETF" suffix is NOT
# baked in here anymore (``structure`` carries the wrapper), so a value sleeve
# is "Value", not "Value ETF". The user-facing donut is labelled "Exposure &
# style", not "Sector", because Growth / Momentum / T-Bill aren't sectors
# (codex review).
#
# GICS sectors for single stocks (mega-caps are NOT all "Tech": Alphabet/Meta
# are Communication Services, Amazon/Tesla are Consumer Discretionary).
SECTOR_TECH = "Tech"
SECTOR_COMM_SERVICES = "Communication Services"
SECTOR_CONSUMER_DISC = "Consumer Discretionary"
SECTOR_FINANCIALS = "Financials"
SECTOR_HEALTHCARE = "Healthcare"
SECTOR_CONGLOMERATE = "Conglomerate"
SECTOR_REAL_ESTATE = "Real Estate"
# Fund styles / factor tilts.
SECTOR_BROAD_INDEX = "Broad Index"
SECTOR_GROWTH = "Growth"
SECTOR_VALUE = "Value"
SECTOR_DIVIDEND = "Dividend"
SECTOR_MOMENTUM = "Momentum"
SECTOR_LOW_VOL = "Low Volatility"
SECTOR_ISRAELI = "Israeli"
# Sub-asset categories.
SECTOR_TBILL = "T-Bill"
SECTOR_TREASURY = "Treasury 1-3yr"
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
    # Instrument structure (the wrapper): Stock / ETF / REIT / Bond / Cash.
    # Level-1 of the per-account "Type" column; ``sector`` is level-2. Defaults
    # to ETF (most of the book is funds); single names set it to Stock.
    structure: str = STRUCT_ETF
    # Estate-safe = NOT US-situs for a non-US person (no US estate-tax tail).
    # UCITS (Irish/London) + Israeli-domiciled are safe; US-domiciled
    # securities are exposed. Defaults safe; the US-situs set below flips the
    # US-domiciled table entries to False. NVDA is US-situs (exposed) — the one
    # sanctioned exception, but still flagged exposed so the tail is visible.
    estate_safe: bool = True


# --- Curated reference table (keyed by resolved ticker, upper-cased) --------
# Every instrument currently in the book. Comment = plain-language identity so
# an adversarial reviewer can reconcile each row.
_REFERENCE: dict[str, InstrumentRef] = {
    # US mega-cap single stocks — GICS sectors (NOT all "Tech": codex review).
    "NVDA": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US, STRUCT_STOCK),
    "AMD": InstrumentRef(ASSET_EQUITY, SECTOR_TECH, REGION_US, STRUCT_STOCK),
    "GOOG": InstrumentRef(ASSET_EQUITY, SECTOR_COMM_SERVICES, REGION_US, STRUCT_STOCK),
    "GOOGL": InstrumentRef(ASSET_EQUITY, SECTOR_COMM_SERVICES, REGION_US, STRUCT_STOCK),
    "META": InstrumentRef(ASSET_EQUITY, SECTOR_COMM_SERVICES, REGION_US, STRUCT_STOCK),
    "AMZN": InstrumentRef(ASSET_EQUITY, SECTOR_CONSUMER_DISC, REGION_US, STRUCT_STOCK),
    "TSLA": InstrumentRef(ASSET_EQUITY, SECTOR_CONSUMER_DISC, REGION_US, STRUCT_STOCK),
    # US single names — financials / healthcare / conglomerate.
    "SOFI": InstrumentRef(ASSET_EQUITY, SECTOR_FINANCIALS, REGION_US, STRUCT_STOCK),
    "RKT": InstrumentRef(ASSET_EQUITY, SECTOR_FINANCIALS, REGION_US, STRUCT_STOCK),
    "BRK/B": InstrumentRef(ASSET_EQUITY, SECTOR_CONGLOMERATE, REGION_US, STRUCT_STOCK),
    "BRK.B": InstrumentRef(ASSET_EQUITY, SECTOR_CONGLOMERATE, REGION_US, STRUCT_STOCK),
    "BMY": InstrumentRef(ASSET_EQUITY, SECTOR_HEALTHCARE, REGION_US, STRUCT_STOCK),
    # US broad-market index ETFs (US- and UCITS-domiciled both track the S&P).
    "VOO": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_US),
    "VTI": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_US),
    "CSPX": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_US),
    "XZEW": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_US),  # equal-weight S&P 500
    # US growth-tilt ETFs (Nasdaq-100 / large-cap growth) — kept distinct from
    # plain broad index so the Type + donut reconcile with the plan's "US
    # growth tilt" class.
    "QQQM": InstrumentRef(ASSET_EQUITY, SECTOR_GROWTH, REGION_US),
    "CNDX": InstrumentRef(ASSET_EQUITY, SECTOR_GROWTH, REGION_US),
    "SCHG": InstrumentRef(ASSET_EQUITY, SECTOR_GROWTH, REGION_US),
    "R1GR": InstrumentRef(ASSET_EQUITY, SECTOR_GROWTH, REGION_US),  # Russell 1000 Growth
    # Factor sleeves — momentum / minimum-volatility are NOT "growth" (codex).
    "SPMO": InstrumentRef(ASSET_EQUITY, SECTOR_MOMENTUM, REGION_US),  # S&P 500 Momentum
    "SPMV": InstrumentRef(ASSET_EQUITY, SECTOR_LOW_VOL, REGION_US),   # S&P 500 Min-Vol UCITS
    # Dividend / value style sleeves.
    "SCHD": InstrumentRef(ASSET_EQUITY, SECTOR_DIVIDEND, REGION_US),
    "FUSA": InstrumentRef(ASSET_EQUITY, SECTOR_DIVIDEND, REGION_US),
    "VTV": InstrumentRef(ASSET_EQUITY, SECTOR_VALUE, REGION_US),
    # Global / developed-world broad-index ETFs.
    "EXUS": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_GLOBAL),
    "FWRA": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_GLOBAL),
    "ACWD": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_GLOBAL),
    "MSCI WORLD": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_GLOBAL),
    # Emerging markets.
    "EIMI": InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_EM),
    # Sector ETFs.
    "IUHC": InstrumentRef(ASSET_EQUITY, SECTOR_HEALTHCARE, REGION_US),
    # Real estate (genuine REIT single name + listed-property ETFs). DPYA is
    # the EUR share class of the iShares Dev-Markets Property Yield ETF (sibling
    # of IWDP), NOT a dividend-equity fund (codex review).
    "O": InstrumentRef(ASSET_REAL_ESTATE, SECTOR_REAL_ESTATE, REGION_US, STRUCT_REIT),
    "IWDP": InstrumentRef(ASSET_REAL_ESTATE, SECTOR_REAL_ESTATE, REGION_GLOBAL),
    "DPYA": InstrumentRef(ASSET_REAL_ESTATE, SECTOR_REAL_ESTATE, REGION_GLOBAL),
    # Cash equivalents — SGOV/IB01 are 0-3m / 0-1y T-bill ETFs treated as cash;
    # IBTA is 1-3yr Treasury BONDS = Fixed Income, not cash (codex review). The
    # wrapper stays ETF; the asset class is what differs.
    "SGOV": InstrumentRef(ASSET_CASH, SECTOR_TBILL, REGION_US),
    "IB01": InstrumentRef(ASSET_CASH, SECTOR_TBILL, REGION_US),
    "IBTA": InstrumentRef(ASSET_FIXED_INCOME, SECTOR_TREASURY, REGION_US),
    # Crypto (spot-BTC ETF wrapper, Alternatives asset class).
    "IBIT": InstrumentRef(ASSET_ALTERNATIVES, SECTOR_CRYPTO, REGION_US),
}

# Name-keyword fallback for rows with no resolvable latin ticker (e.g. the
# IBI STOXX Europe 600 tracker whose Symbol cell is the bogus "O"). Matched
# against the lower-cased details string. First hit wins.
_NAME_KEYWORD_FALLBACK: tuple[tuple[str, InstrumentRef], ...] = (
    ("stoxx europe", InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_EUROPE)),
    ("msci world", InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_GLOBAL)),
    ("emerging", InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_EM)),
)


# US-domiciled (US-situs) tickers in the book — estate-exposed for a non-US
# person. Everything else in the table is UCITS (Irish/London) or Israeli =
# estate-safe. NVDA is here too: it IS US-situs (the sanctioned exception).
_US_SITUS_TICKERS: frozenset[str] = frozenset({
    "NVDA", "AMD", "GOOG", "GOOGL", "AMZN", "META", "TSLA", "SOFI", "RKT",
    "BRK/B", "BRK.B", "BMY", "VOO", "VTI", "SCHD", "SCHG", "SPMO", "VTV",
    "QQQM", "SGOV", "O", "IBIT",
})

# Flip the US-domiciled entries to estate-exposed (the table defaults safe).
_REFERENCE = {
    k: (_replace(v, estate_safe=False) if k in _US_SITUS_TICKERS else v)
    for k, v in _REFERENCE.items()
}


def _is_hebrew_ticker(symbol: str) -> bool:
    """A genuinely TASE-listed instrument has a Hebrew/non-latin ticker
    (e.g. ``מחקה ת"א-200``). A Hebrew *description* is not evidence — see
    [[feedback]] on the Israeli-ETF misclassification."""
    return any("֐" <= ch <= "׿" for ch in (symbol or ""))


# Narrow, instrument-specific overrides for rows whose Symbol cell is a wrong
# but VALID ticker (so the table would mis-attribute them). Keyed on a details
# substring + the bad symbol. Deliberately NOT a general name-keyword-before-
# table rule — generic words ("emerging") could then override real tickers
# (codex review). Only the IBI STOXX Europe 600 tracker (Symbol cell "O", the
# Realty Income ticker) qualifies today.
_COLLISION_OVERRIDES: tuple[tuple[str, str, InstrumentRef], ...] = (
    ("O", "stoxx europe", InstrumentRef(ASSET_EQUITY, SECTOR_BROAD_INDEX, REGION_EUROPE)),
)


def lookup(symbol: str, details: str = "") -> InstrumentRef | None:
    """Return the canonical reference for a resolved ticker, or ``None`` when
    the instrument isn't in the curated table and no name-keyword/Israeli
    fallback applies (caller then uses its raw-field heuristic)."""
    sym = (symbol or "").upper().strip()
    hay_all = (details or "").lower()
    for bad_sym, kw, ref in _COLLISION_OVERRIDES:
        if sym == bad_sym and kw in hay_all:
            return ref
    if sym in _REFERENCE:
        return _REFERENCE[sym]
    # TASE-listed (Hebrew ticker, no latin symbol) → Israeli equity.
    if _is_hebrew_ticker(symbol):
        return InstrumentRef(ASSET_EQUITY, SECTOR_ISRAELI, REGION_ISRAEL)
    # No resolvable ticker — fall back to a name keyword in details.
    hay = (details or "").lower()
    for kw, ref in _NAME_KEYWORD_FALLBACK:
        if kw in hay:
            return ref
    return None


def estate_safe_for(symbol: str, details: str = "") -> bool | None:
    """True = estate-safe (non-US-situs), False = US-situs exposed, None =
    unknown (instrument not in the reference). Travels with the resolved
    instrument, so the STOXX-as-"O" collision resolves to the (safe) IBI
    tracker, not Realty Income."""
    ref = lookup(symbol, details)
    return ref.estate_safe if ref is not None else None


def type_label(symbol: str, details: str = "", fallback: str = "") -> str:
    """The canonical per-account "Type" label: ``"<structure> · <sector>"``
    (e.g. ``"Stock · Tech"``, ``"ETF · Broad Index"``, ``"REIT · Real Estate"``).

    This is the single authority for the Type column, the sector donut, and any
    other "what is this instrument" surface — derived from the reference, never
    from the unreliable source ``asset_type`` free-text. A Cash-class instrument
    collapses to just its structure (no redundant ``"Cash · T-Bill"``-style
    doubling for a money-market row). When the instrument isn't in the reference
    (physical cash, an untyped row), returns ``fallback`` (the caller's raw
    ``asset_type``) so the column is never blank."""
    ref = lookup(symbol, details)
    if ref is None:
        return (fallback or "").strip()
    if ref.asset_class == ASSET_CASH and ref.structure == STRUCT_CASH:
        return ref.structure
    return f"{ref.structure} · {ref.sector}"


__all__ = ["InstrumentRef", "lookup", "estate_safe_for", "type_label",
           "REGION_US", "REGION_ISRAEL", "REGION_EUROPE", "REGION_EM",
           "REGION_GLOBAL", "REGION_OTHER"]
