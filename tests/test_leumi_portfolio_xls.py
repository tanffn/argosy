"""Leumi portfolio-export parser, pinned against the real 2026-08-22 file."""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.adapters.brokers.leumi_portfolio_xls import (
    LeumiPortfolio,
    parse_portfolio_xls,
)

REAL = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Leumi/"
    "leumi_2026_Aug_22_protfolio.xls"
)
needs_real = pytest.mark.skipif(not REAL.is_file(), reason="user's Drive export absent")


def _mini(rows: str) -> str:
    return (
        '<?xml version="1.0"?><Workbook><Worksheet><Table>'
        '<Row><Cell><Data>תאריך:</Data></Cell><Cell><Data>22.08.26</Data></Cell>'
        '<Cell><Data>תיק:</Data></Cell><Cell><Data>882-447452/10</Data></Cell></Row>'
        "<Row><Cell><Data>מס' ניירות:</Data></Cell><Cell><Data>1</Data></Cell>"
        '<Cell><Data>שווי תיק עדכני ב$</Data></Cell><Cell><Data>100.00</Data></Cell></Row>'
        '<Row><Cell><Data>מספר נייר</Data></Cell><Cell><Data>שם הנייר</Data></Cell></Row>'
        + rows
        + "</Table></Worksheet></Workbook>"
    )


def _row(*vals: str) -> str:
    return "<Row>" + "".join(f"<Cell><Data>{v}</Data></Cell>" for v in vals) + "</Row>"


class TestSymbolExtraction:
    """The ticker is buried at the end of a Hebrew name, sometimes with a venue."""

    @pytest.mark.parametrize(
        "name,sym,venue",
        [
            ("(Alphabet Inc-Cl C) GOOG", "GOOG", None),
            ("(Ishares Core S&P 500) CSPX LN", "CSPX", "LN"),
            ("(Ishares Dvl Mkt Property Yld) IWDP SW", "IWDP", "SW"),
            ("(ברקשייר האטווי קלס ב) BRK/B", "BRK/B", None),
            ('(איי-שארס אג""ח אוצר אמריקאי 0-3 חודשים) SGOV', "SGOV", None),
            # TASE tracking funds carry no ticker at all.
            ('ATF מחקה ת"א-200', None, None),
            ("MTF מחקה (MSCI World (4D", None, None),
        ],
    )
    def test_splits_symbol_and_venue(self, name, sym, venue, tmp_path):
        f = tmp_path / "p.xls"
        f.write_text(_mini(_row("60000001", name, "", "1", "1", "1", "100.00",
                                "0", "0", "0", "1")), encoding="utf-8")
        h = parse_portfolio_xls(f).holdings[0]
        assert (h.symbol, h.venue) == (sym, venue)

    def test_brk_slash_b_is_not_mistaken_for_a_venue(self, tmp_path):
        """Guarding on a venue whitelist, not "any short tail", keeps BRK/B whole."""
        f = tmp_path / "p.xls"
        f.write_text(_mini(_row("60013471", "(ברקשייר) BRK/B", "", "1", "1", "1",
                                "100.00", "0", "0", "0", "1")), encoding="utf-8")
        assert parse_portfolio_xls(f).holdings[0].symbol == "BRK/B"


class TestFailsLoudly:
    """A partially-parsed portfolio that looks plausible is worse than none."""

    def test_not_spreadsheetml(self, tmp_path):
        f = tmp_path / "x.xls"
        f.write_text("<HTML><body>nope</body></HTML>", encoding="utf-8")
        with pytest.raises(ValueError, match="no <Row>"):
            parse_portfolio_xls(f)

    def test_missing_header_row(self, tmp_path):
        f = tmp_path / "x.xls"
        f.write_text('<Workbook><Row><Cell><Data>hi</Data></Cell></Row></Workbook>',
                     encoding="utf-8")
        with pytest.raises(ValueError, match="header row"):
            parse_portfolio_xls(f)

    def test_header_but_no_holdings(self, tmp_path):
        f = tmp_path / "x.xls"
        f.write_text(_mini(""), encoding="utf-8")
        with pytest.raises(ValueError, match="no holding rows"):
            parse_portfolio_xls(f)


@needs_real
class TestAgainstTheRealExport:
    @pytest.fixture(scope="class")
    def p(self) -> LeumiPortfolio:
        return parse_portfolio_xls(REAL)

    def test_header_metadata(self, p):
        assert p.as_of.isoformat() == "2026-08-22"
        assert p.account == "882-447452/10"
        assert p.declared_count == 38

    def test_every_row_is_parsed_and_the_total_reconciles(self, p):
        """The check that catches a silently dropped row: 38 holdings is
        indistinguishable from 37 by eye, but not by sum."""
        assert len(p.holdings) == 38
        assert p.reconciles()
        assert p.parsed_value_usd == pytest.approx(1_635_586.98, abs=0.05)

    def test_the_three_tase_funds_have_no_ticker(self, p):
        assert sum(1 for h in p.holdings if h.symbol is None) == 3

    def test_largest_holding(self, p):
        schd = next(h for h in p.holdings if h.symbol == "SCHD")
        assert schd.quantity == 7750.0
        assert schd.value_usd == pytest.approx(272_102.50, abs=0.01)

    def test_tase_price_is_agorot_so_value_is_not_price_times_qty(self, p):
        """The trap: 'שער אחרון' for TASE lines is quoted in agorot while
        'שווי אחזקה ב $' is dollars. Deriving value would be ~30x wrong."""
        atf = next(h for h in p.holdings if h.name.startswith("ATF"))
        assert atf.last_price is not None
        assert atf.quantity * atf.last_price > 50 * atf.value_usd
