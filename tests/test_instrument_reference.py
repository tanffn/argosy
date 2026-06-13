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
    assert ref.sector == "Israeli"


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
    assert ref is not None and ref.sector == "Dividend"


def test_unknown_ticker_returns_none():
    assert lookup("ZZZUNKNOWN", "Some Stock") is None


def test_name_keyword_fallback_does_not_apply_to_real_unknown_ticker():
    # A real-but-uncurated US-domiciled ETF whose name contains a fallback
    # keyword ("emerging") must NOT borrow the safe EM ref — it returns None so
    # the fail-loud guard flags it (else VWO would be silently estate-safe).
    assert lookup("VWO", "Vanguard FTSE Emerging Markets ETF") is None
    # The fallback still applies when there is NO symbol at all.
    assert lookup("", "iShares Core MSCI Emerging Markets") is not None


def test_estate_safe_us_domiciled_is_exposed():
    from argosy.services.instrument_reference import estate_safe_for
    assert estate_safe_for("NVDA", "RSU") is False           # US-situs (sanctioned but exposed)
    assert estate_safe_for("AMD", "(...) AMD") is False
    assert estate_safe_for("VOO", "(...) VOO") is False       # US-domiciled ETF
    assert estate_safe_for("QQQM", "(...) QQQM") is False      # US-domiciled (cf. UCITS CNDX)
    assert estate_safe_for("CNDX", "(ISH NASDAQ100 $A) CNDX LN") is True  # UCITS twin
    assert estate_safe_for("SGOV", "(...) SGOV") is False
    assert estate_safe_for("O", "(ריאלטי אינקם) O") is False  # US REIT


def test_estate_safe_ucits_and_israeli_are_safe():
    from argosy.services.instrument_reference import estate_safe_for
    assert estate_safe_for("CSPX", "(ISHR CORE S&P500) CSPX LN") is True   # UCITS
    assert estate_safe_for("IUHC", "(ISH S&P HLTH CR) IUHC LN") is True    # UCITS
    assert estate_safe_for("EIMI", "(ISHR CORE EM IMI) EIMI LN") is True   # UCITS
    assert estate_safe_for('מחקה ת"א-200', 'ATF מחקה ת"א-200') is True     # Israeli
    # The STOXX-as-"O" collision resolves to the (safe) IBI tracker, not Realty.
    assert estate_safe_for("O", "אי בי אי מחקה STOXX Europe 600") is True


def test_estate_safe_unknown_is_none():
    from argosy.services.instrument_reference import estate_safe_for
    assert estate_safe_for("ZZZUNKNOWN", "mystery") is None


# --- 2-level Type taxonomy: structure × exposure (the carry-over redesign) ---

def test_structure_stock_vs_etf_vs_reit():
    assert lookup("NVDA", "RSU").structure == "Stock"
    assert lookup("VOO", "").structure == "ETF"
    assert lookup("O", "(ריאלטי אינקם) O").structure == "REIT"


def test_type_label_is_structure_dot_exposure():
    from argosy.services.instrument_reference import type_label
    assert type_label("NVDA", "RSU") == "Stock · Tech"
    assert type_label("VOO", "") == "ETF · Broad Index"
    assert type_label("O", "(ריאלטי אינקם) O") == "REIT · Real Estate"
    # Un-curated row falls back to the caller's raw asset_type, never blank.
    assert type_label("ZZZ", "mystery", fallback="Growth") == "Growth"


def test_codex_corrections_megacap_gics_sectors():
    # Mega-caps are NOT all "Tech" (codex GICS review).
    assert lookup("GOOG", "x").sector == "Communication Services"
    assert lookup("META", "x").sector == "Communication Services"
    assert lookup("AMZN", "x").sector == "Consumer Discretionary"
    assert lookup("TSLA", "x").sector == "Consumer Discretionary"
    assert lookup("NVDA", "x").sector == "Tech"


def test_codex_corrections_factor_and_treasury():
    # SPMO is momentum, not growth; IBTA is 1-3yr Treasury bonds (Fixed Income,
    # not cash); DPYA is the property-yield ETF sibling of IWDP, not dividend.
    assert lookup("SPMO", "x").sector == "Momentum"
    assert lookup("SPMV", "x").sector == "Low Volatility"
    ibta = lookup("IBTA", "x")
    assert ibta.asset_class == "Fixed Income" and ibta.sector == "Treasury 1-3yr"
    dpya = lookup("DPYA", "x")
    assert dpya.asset_class == "Real Estate" and dpya.estate_safe is True
