"""Leumi XLS auto-refresh: USD cash row + name-derived symbols for TASE
trackers (no Latin ticker). Stops the hand-maintained TSV from owning the
Leumi data — the root cause of the dropped USD cash + the 'O' mislabel."""
from __future__ import annotations

from argosy.ingest.tsv import PortfolioSnapshot
from argosy.services.portfolio_ingest.parsers.leumi_xls import (
    LeumiPortfolioPosition,
    LeumiPortfolioSnapshot,
)
from argosy.services.portfolio_ingest.xls_osh_pair import (
    _build_prior_mappings,
    _display_symbol_from_name,
    _xls_to_tsv_rows,
)


def _pos(security_id, name_he, ticker, qty, value_usd, holding_value_currency="USD"):
    return LeumiPortfolioPosition(
        security_id=security_id, name_he=name_he, ticker=ticker,
        avg_buy_price=None, quantity=qty, last_price=value_usd / max(qty, 1),
        holding_value=value_usd, holding_value_currency=holding_value_currency,
        gain_pct=None, pct_of_portfolio=None,
    )


def _xls(positions, total_value_currency="USD"):
    return LeumiPortfolioSnapshot(
        snapshot_date=None, portfolio_number="1", securities_count=len(positions),
        total_value=sum(p.holding_value for p in positions),
        total_value_currency=total_value_currency, positions=positions,
    )


class TestDisplaySymbolFromName:
    def test_stoxx_europe_derived_not_O(self):
        assert _display_symbol_from_name("אי בי אי מחקה STOXX Europe 600") == "STOXX Europe 600"

    def test_msci_world_derived(self):
        assert _display_symbol_from_name("MTF מחקה MSCI World") == "MSCI World"

    def test_ta200_derived(self):
        assert _display_symbol_from_name('ATF מחקה ת"א-200') == 'ת"א-200'

    def test_no_marker_returns_none(self):
        assert _display_symbol_from_name("(אדוונסד מיקרו דיווייסז) AMD") is None
        assert _display_symbol_from_name("") is None


def _rows_for(positions, *, usd_closing):
    xls = _xls(positions)
    empty = PortfolioSnapshot(source_path="(none)")
    sym, cur, typ = _build_prior_mappings(empty, xls)
    return _xls_to_tsv_rows(
        xls=xls, osh_closing_nis=58944.86, fx_usd_nis=2.94161,
        symbol_map=sym, currency_map=cur, type_map=typ, usd_closing=usd_closing,
    )


class TestUsdCashRow:
    def test_usd_cash_row_emitted_with_both_currencies(self):
        rows = _rows_for([_pos("1100284", "אי בי אי מחקה STOXX Europe 600", None, 12500, 6810.05)],
                         usd_closing=264997.33)
        cells = [r.split("\t") for r in rows]
        nis = [c for c in cells if c[1] == "Leumi" and c[2] == "NIS" and c[3] == "Cash"]
        usd = [c for c in cells if c[1] == "Leumi" and c[2] == "USD" and c[3] == "Cash"]
        assert len(nis) == 1, "NIS cash row present"
        assert len(usd) == 1, "USD cash row present"
        assert usd[0][9] == "264997.33"          # local value
        assert usd[0][10] == "265.00"            # (K) USD value

    def test_no_usd_cash_row_when_balance_absent(self):
        rows = _rows_for([_pos("1100284", "(ואנגארד S&P 500) VOO", "VOO", 20, 13564.6)],
                         usd_closing=None)
        cells = [r.split("\t") for r in rows]
        assert not [c for c in cells if c[1] == "Leumi" and c[2] == "USD" and c[3] == "Cash"]

    def test_no_ticker_does_not_inherit_one_char_prior_symbol(self):
        # A prior "O" (Realty Income) row must NOT substring-match a no-ticker
        # tracker name and re-stamp "O" onto it (the STOXX bug).
        from argosy.ingest.tsv import PortfolioPosition
        prior = PortfolioSnapshot(source_path="x", positions=[
            PortfolioPosition(location="Leumi", symbol="O", asset_type="REIT",
                              details="(ריאלטי אינקם) O"),
        ])
        xls = _xls([_pos("999", "אי בי אי מחקה STOXX Europe 600", None, 12500, 6810.0)])
        sym, _cur, _typ = _build_prior_mappings(prior, xls)
        assert sym["999"] == "STOXX Europe 600"

    def test_stoxx_position_symbol_is_name_not_O(self):
        rows = _rows_for([_pos("1100284", "אי בי אי מחקה STOXX Europe 600", None, 12500, 6810.05)],
                         usd_closing=None)
        stoxx = [r.split("\t") for r in rows if "STOXX Europe 600" in r and "Cash" not in r][0]
        assert stoxx[5] == "STOXX Europe 600"    # symbol cell — not "O", not a bare id


class TestCurrencyReferenceOverride:
    """Regression for the ת"א-200 mis-tag: a TASE-listed, NIS-priced
    instrument was defaulting to currency='USD' in the position ledger
    because the file-level default/carry-forward never consulted
    instrument_reference. Per-instrument reference truth must win over the
    file default/header when the two disagree; must stay silent (never
    guess) where reference data doesn't apply."""

    def test_tase_instrument_in_usd_header_file_classified_nis(self):
        # A $-header Leumi file (holding_value_currency="USD" — the whole-
        # file default) still must classify the TASE-listed, no-Latin-
        # ticker ת"א-200 tracker as NIS, because instrument_reference knows
        # it's Israel-listed.
        xls = _xls(
            [_pos("1234567", 'ATF מחקה ת"א-200', None, 80_000, 40_100.0,
                  holding_value_currency="USD")],
            total_value_currency="USD",
        )
        empty = PortfolioSnapshot(source_path="(none)")
        _sym, curr, _typ = _build_prior_mappings(empty, xls)
        assert curr["1234567"] == "NIS"

    def test_genuine_usd_instrument_in_same_file_stays_usd(self):
        # A real foreign-listed (Latin-ticker) instrument in the SAME file
        # must NOT be flipped to NIS by the TASE override above.
        xls = _xls(
            [
                _pos("1234567", 'ATF מחקה ת"א-200', None, 80_000, 40_100.0),
                _pos("7654321", "(אדוונסד מיקרו דיווייסז) AMD", "AMD", 100, 20_000.0),
            ],
        )
        empty = PortfolioSnapshot(source_path="(none)")
        _sym, curr, _typ = _build_prior_mappings(empty, xls)
        assert curr["1234567"] == "NIS"
        assert curr["7654321"] == "USD"

    def test_israel_incorporated_but_foreign_listed_stays_usd(self):
        # INVZ (Innoviz): reference region is Israel (estate/sector
        # classification) but it trades on Nasdaq with a real Latin ticker
        # -- must NOT be flipped to NIS. This is the false-positive class
        # the fix must avoid.
        xls = _xls([_pos("9999999", "(אינוביז) INVZ", "INVZ", 500, 5_000.0)])
        empty = PortfolioSnapshot(source_path="(none)")
        _sym, curr, _typ = _build_prior_mappings(empty, xls)
        assert curr["9999999"] == "USD"

    def test_unknown_instrument_falls_back_to_default(self):
        # No reference entry -> instrument_reference is silent -> the
        # existing default/carry-forward behaviour is kept unchanged
        # (never guess a currency).
        xls = _xls([_pos("1111111", "(לא ידוע) ZZZZ", "ZZZZ", 10, 1_000.0)])
        empty = PortfolioSnapshot(source_path="(none)")
        _sym, curr, _typ = _build_prior_mappings(empty, xls)
        assert curr["1111111"] == "USD"

    def test_prior_currency_overridden_when_reference_disagrees(self):
        # Even a position matched to a (wrongly) prior-tagged USD row must
        # be corrected once instrument_reference is decisive -- fixing the
        # classification path so a re-ingest self-heals rather than
        # perpetuating the mis-tag via carry-forward.
        from argosy.ingest.tsv import PortfolioPosition
        prior = PortfolioSnapshot(source_path="x", positions=[
            PortfolioPosition(location="Leumi", symbol='ת"א-200', currency="USD",
                               asset_type="Equity", details='ATF מחקה ת"א-200'),
        ])
        xls = _xls([_pos("1234567", 'ATF מחקה ת"א-200', None, 80_000, 40_100.0)])
        _sym, curr, _typ = _build_prior_mappings(prior, xls)
        assert curr["1234567"] == "NIS"
