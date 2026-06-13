"""The curated instrument reference is the classification authority keyed off
the resolved ticker — it must override the snapshot's unreliable asset_type."""
from __future__ import annotations

from argosy.services.instrument_reference import (
    REGION_EM,
    REGION_EUROPE,
    REGION_ISRAEL,
    REGION_US,
    lookup,
)


def test_em_etf_is_equity_not_real_estate():
    # EIMI's source row is labeled asset_type=REIT — the reference overrides.
    ref = lookup("EIMI", "(ISHR CORE EM IMI) EIMI LN")
    assert ref is not None
    assert ref.asset_class == "Equity"
    assert ref.region == REGION_EM


def test_blank_type_us_etf_resolves_by_ticker():
    # The $3K Schwab SCHG row has a blank asset_type → would be "Unclassified"
    # without a ticker-keyed authority.
    ref = lookup("SCHG", "")
    assert ref is not None
    assert ref.asset_class == "Equity"
    assert ref.region == REGION_US


def test_tase_ticker_is_israel():
    ref = lookup('מחקה ת"א-200', 'ATF מחקה ת"א-200')
    assert ref is not None
    assert ref.region == REGION_ISRAEL
    assert ref.sector == "Israeli ETF"


def test_us_holding_with_hebrew_description_is_us_not_israel():
    ref = lookup("AMD", "(אדוונסד מיקרו דיווייסז) AMD")
    assert ref is not None
    assert ref.region == REGION_US
    assert ref.sector == "Tech"


def test_name_keyword_fallback_for_untickerable_row():
    # An untickerable row (blank/unknown symbol) routes by a name keyword in
    # details — European equity rather than falling through to a raw heuristic.
    ref = lookup("", "אי בי אי מחקה STOXX Europe 600")
    assert ref is not None
    assert ref.asset_class == "Equity"
    assert ref.region == REGION_EUROPE


def test_stoxx_o_collision_resolved_by_narrow_override():
    # The STOXX row's Symbol cell is the bogus "O" (= Realty Income's ticker).
    # A narrow, instrument-specific override classifies it as European equity
    # rather than letting the table mis-attribute it to Realty Income.
    ref = lookup("O", "אי בי אי מחקה STOXX Europe 600")
    assert ref is not None
    assert ref.asset_class == "Equity"
    assert ref.region == REGION_EUROPE
    # The override is narrow: a genuine Realty Income row is unaffected.
    realty = lookup("O", "(ריאלטי אינקם) O")
    assert realty is not None and realty.asset_class == "Real Estate"


def test_schd_is_dividend_sector():
    ref = lookup("SCHD", "(שוואב ארה\"ב דיבידנד) SCHD")
    assert ref is not None and ref.sector == "Dividend ETF"


def test_unknown_ticker_returns_none():
    assert lookup("ZZZUNKNOWN", "Some Stock") is None
