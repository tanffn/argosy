"""Provenance instrument classes (Stream A Option C).

Fiscal-vintage rules are class-specific. A fund has no issuer fiscal
quarter; cash/T-bills have none either. Applying the equity quarter gate
to those classes can only ever block them incorrectly — or, worse, let
them silently inherit an equity pass. Classification is therefore
explicit and fail-closed on unknowns.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ProvenanceClass(str, Enum):
    SINGLE_NAME_EQUITY = "single_name_equity"
    FUND_ETF_INDEX = "fund_etf_index"
    CASH_TBILL = "cash_tbill"
    UNKNOWN = "unknown"


class FiscalVintageRule(str, Enum):
    """Named per-class rule — never an accidental default."""

    # US-listed single names: SEC EDGAR reported period vs data period.
    EQUITY_SEC_REPORTED_PERIOD = "equity_sec_reported_period"
    # Funds/ETFs/index vehicles have no issuer fiscal quarter.
    FUND_NO_ISSUER_FISCAL_QUARTER = "fund_no_issuer_fiscal_quarter"
    # Cash and T-bill vehicles — no fiscal statements to gate on.
    CASH_OR_TBILL_NO_FISCAL_VINTAGE = "cash_or_tbill_no_fiscal_vintage"
    # Unclassifiable — must block visibly, never inherit another class.
    UNKNOWN_CLASS_FAIL_CLOSED = "unknown_class_fail_closed"


# Curated from the live held book (snapshot 2026-08-08) plus known
# Schwab-omitted names (NVDA, BMY) that belong to the real book.
_SINGLE_NAME_EQUITIES: frozenset[str] = frozenset(
    {
        # Held book (Leumi snapshot 2026-08-08) + Schwab-omitted real book.
        "AMD",
        "AMZN",
        "BMY",
        "BRK/B",
        "BRK-B",
        "BRK.B",
        "CRM",
        "GOOG",
        "GOOGL",
        "META",
        "NOW",
        "NVDA",
        "O",  # Realty Income — REIT issuer with 10-Q/10-K
        "OKLO",
        "RXRX",
        "SOFI",
        "TEM",
        "TSLA",
        # Decision-universe / regression fixtures (not held today).
        "AAPL",
        "ACN",
        "IOVA",
        "MSFT",
        "TRLV",
    }
)

_CASH_TBILL: frozenset[str] = frozenset(
    {
        "SGOV",
        "IBTA",
        "BIL",
        "SHV",
        "TBILL",
        "USD",
        "NIS",
        "CASH",
    }
)

_FUND_ETF_INDEX: frozenset[str] = frozenset(
    {
        "ACWD",
        "CNDX",
        "CSPX",
        "EIMI",
        "EXUS",
        "FUSA",
        "FWRA",
        "IUHC",
        "IWDP",
        "IWQU",
        "MSCI WORLD",
        "QQQM",
        "SCHD",
        "SCHG",
        "SPMO",
        "SPMV",
        "STOXX EUROPE 600",
        "VOO",
        "VTV",
        "XZEW",
        # Hebrew TA-125/TA-200 tracker label as held in Leumi book.
        'ת"א-200',
        "ת\"א-200",
    }
)

_INDEX_NAME_MARKERS: tuple[str, ...] = (
    "MSCI",
    "STOXX",
    "S&P",
    "FTSE",
    "NASDAQ",
    "INDEX",
    "WORLD",
    'ת"א',
    "תא-",
)


def normalize_symbol(symbol: str | None) -> str:
    return (symbol or "").strip().upper()


def classify_instrument(
    symbol: str | None,
    *,
    asset_type: str | None = None,
    details: str | None = None,
    what_it_is: str | None = None,
) -> ProvenanceClass:
    """Classify one instrument into a provenance class.

    Order: cash/T-bill → curated equity → curated fund → heuristics →
    UNKNOWN (never silently defaults to equity).
    """
    raw = (symbol or "").strip()
    sym = normalize_symbol(symbol)
    at = (asset_type or "").strip().lower()
    blob = " ".join(
        x for x in (details or "", what_it_is or "", raw) if x
    ).upper()

    if not sym or at == "cash":
        return ProvenanceClass.CASH_TBILL
    if sym in _CASH_TBILL or at.startswith("treasury"):
        return ProvenanceClass.CASH_TBILL
    if "T-BILL" in blob or "TBILL" in blob or "TREASURY" in blob:
        # SGOV-style descriptions in Hebrew/English.
        if sym in _CASH_TBILL or "0-3" in blob or "1-3" in blob:
            return ProvenanceClass.CASH_TBILL

    if sym in _SINGLE_NAME_EQUITIES:
        return ProvenanceClass.SINGLE_NAME_EQUITY

    if sym in _FUND_ETF_INDEX or raw in _FUND_ETF_INDEX:
        return ProvenanceClass.FUND_ETF_INDEX

    # UCITS / London-listed fund share-class markers in Leumi details.
    if any(m in blob for m in (" LN", " SW", "UCITS", "ACC)", " ETF", "ETF")):
        if sym not in _SINGLE_NAME_EQUITIES:
            return ProvenanceClass.FUND_ETF_INDEX

    if any(m in blob for m in _INDEX_NAME_MARKERS) and sym not in _SINGLE_NAME_EQUITIES:
        return ProvenanceClass.FUND_ETF_INDEX

    # מחקה = Israeli tracker/index fund.
    if "מחקה" in (details or "") or "מחקה" in (what_it_is or ""):
        return ProvenanceClass.FUND_ETF_INDEX

    return ProvenanceClass.UNKNOWN


def rule_for_class(cls: ProvenanceClass) -> FiscalVintageRule:
    return {
        ProvenanceClass.SINGLE_NAME_EQUITY: FiscalVintageRule.EQUITY_SEC_REPORTED_PERIOD,
        ProvenanceClass.FUND_ETF_INDEX: FiscalVintageRule.FUND_NO_ISSUER_FISCAL_QUARTER,
        ProvenanceClass.CASH_TBILL: FiscalVintageRule.CASH_OR_TBILL_NO_FISCAL_VINTAGE,
        ProvenanceClass.UNKNOWN: FiscalVintageRule.UNKNOWN_CLASS_FAIL_CLOSED,
    }[cls]


def classify_from_fields(
    ticker: str,
    fields: dict[str, Any] | None = None,
) -> ProvenanceClass:
    """Resolve class from explicit payload sidecar, else classify symbol."""
    payload = fields if isinstance(fields, dict) else {}
    raw = payload.get("provenance_class")
    if raw:
        try:
            return ProvenanceClass(str(raw))
        except ValueError:
            return ProvenanceClass.UNKNOWN
    return classify_instrument(
        ticker,
        asset_type=str(payload.get("asset_type") or "") or None,
        details=str(payload.get("details") or "") or None,
        what_it_is=str(payload.get("what_it_is") or "") or None,
    )


def annotate_provenance_class(
    ticker: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Stamp class + named rule onto a fundamentals sidecar (in place + return)."""
    cls = classify_from_fields(ticker, fields)
    fields["provenance_class"] = cls.value
    fields["fiscal_vintage_rule"] = rule_for_class(cls).value
    return fields


__all__ = [
    "FiscalVintageRule",
    "ProvenanceClass",
    "annotate_provenance_class",
    "classify_from_fields",
    "classify_instrument",
    "normalize_symbol",
    "rule_for_class",
]
