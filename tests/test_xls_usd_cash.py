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


def _pos(security_id, name_he, ticker, qty, value_usd):
    return LeumiPortfolioPosition(
        security_id=security_id, name_he=name_he, ticker=ticker,
        avg_buy_price=None, quantity=qty, last_price=value_usd / max(qty, 1),
        holding_value_usd=value_usd, gain_pct=None, pct_of_portfolio=None,
    )


def _xls(positions):
    return LeumiPortfolioSnapshot(
        snapshot_date=None, portfolio_number="1", securities_count=len(positions),
        total_value_usd=sum(p.holding_value_usd for p in positions), positions=positions,
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

    def test_stoxx_position_symbol_is_name_not_O(self):
        rows = _rows_for([_pos("1100284", "אי בי אי מחקה STOXX Europe 600", None, 12500, 6810.05)],
                         usd_closing=None)
        stoxx = [r.split("\t") for r in rows if "STOXX Europe 600" in r and "Cash" not in r][0]
        assert stoxx[5] == "STOXX Europe 600"    # symbol cell — not "O", not a bare id
