"""Leumi balance extraction + position building."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argosy.adapters.brokers.leumi_portfolio_xls import (
    LeumiHolding,
    LeumiPortfolio,
)
from argosy.services.leumi_import import (
    LeumiCashBalance,
    build_leumi_positions,
    read_balance,
)

DRIVE = Path("D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Leumi")
ILS_FILE = DRIVE / "תנועות בחשבון 22_8_2026.xls"
USD_FILE = DRIVE / "תנועות בחשבון מטח 22-08-2026 (1).xls"
EUR_FILE = DRIVE / "תנועות בחשבון מטח 22-08-2026 (2).xls"


def _html(cells: list[str]) -> str:
    return "<HTML><table>" + "".join(f"<td>{c}</td>" for c in cells) + "</table></HTML>"


class TestReadBalance:
    def test_ils_layout_label_and_value_in_one_cell(self, tmp_path):
        f = tmp_path / "osh.xls"
        f.write_text(_html(["בנק לאומי", "היתרה\n   \n  ₪\n  47,100.50",
                            "מסגרת האשראי\n  ₪\n  0.00"]), encoding="utf-8")
        assert read_balance(f) == 47_100.50

    def test_fx_layout_value_in_the_next_cell(self, tmp_path):
        f = tmp_path / "fx.xls"
        f.write_text(_html(['יתרת עו"ש:', "112,960.99"]), encoding="utf-8")
        assert read_balance(f) == 112_960.99

    def test_does_not_read_past_the_movements_table(self, tmp_path):
        """The regression that motivated the stop marker: 'היתרה בש"ח' is a
        COLUMN HEADER inside the table, and reading on returned a
        transaction's reference number (13104) as the ILS balance."""
        f = tmp_path / "osh.xls"
        f.write_text(_html([
            "היתרה\n  ₪\n  47,100.50",
            "תנועות בחשבון",
            'היתרה בש"ח', "13104", "276.00",
        ]), encoding="utf-8")
        assert read_balance(f) == 47_100.50

    def test_raises_when_there_is_no_balance(self, tmp_path):
        f = tmp_path / "x.xls"
        f.write_text(_html(["תנועות בחשבון", 'היתרה בש"ח', "13104"]), encoding="utf-8")
        with pytest.raises(ValueError, match="no closing balance"):
            read_balance(f)

    @pytest.mark.skipif(not ILS_FILE.is_file(), reason="Drive exports absent")
    def test_against_the_real_exports(self):
        assert read_balance(ILS_FILE) == 47_100.50
        assert read_balance(USD_FILE) == 112_960.99
        assert read_balance(EUR_FILE) == 6_108.31


class TestBuildPositions:
    def _portfolio(self):
        return LeumiPortfolio(
            as_of=date(2026, 8, 22), account="882-447452/10",
            declared_count=2, declared_value_usd=300.0,
            holdings=(
                LeumiHolding("60183896", "(Ishares Core S&P 500) CSPX LN", "CSPX",
                             "LN", 240.0, 737.74, 827.54, 200.0, 10.0, 0.12),
                LeumiHolding("60321790", "(Sofi Technologies Inc) SOFI", "SOFI",
                             None, 2000.0, 12.09, 18.91, 100.0, 5.0, 0.06),
            ),
        )

    def test_every_row_is_stamped_leumi_and_dated_from_the_file(self):
        rows = build_leumi_positions(self._portfolio(), [], usd_ils=3.0, eur_usd=1.16)
        assert len(rows) == 2
        assert {r.location for r in rows} == {"Leumi"}
        assert {r.observed_as_of for r in rows} == {date(2026, 8, 22)}
        assert all(r.carried_forward is False for r in rows)

    def test_etfs_and_stocks_are_labelled(self):
        rows = build_leumi_positions(self._portfolio(), [], usd_ils=3.0, eur_usd=1.16)
        by = {r.symbol: r.asset_type for r in rows}
        assert by == {"CSPX": "ETF", "SOFI": "Stock"}

    def test_cash_is_converted_per_currency(self):
        bal = [
            LeumiCashBalance("NIS", 30_000.0, "ils.xls"),
            LeumiCashBalance("USD", 112_960.99, "usd.xls"),
            LeumiCashBalance("EUR", 6_108.31, "eur.xls"),
        ]
        rows = build_leumi_positions(self._portfolio(), bal, usd_ils=3.0, eur_usd=1.16)
        cash = {r.currency: r for r in rows if r.asset_type == "Cash"}
        assert cash["NIS"].usd_value_k == pytest.approx(10.0)
        assert cash["USD"].usd_value_k == pytest.approx(112.96099)
        assert cash["EUR"].usd_value_k == pytest.approx(7.0856396)
        # local amount is preserved untouched for every currency
        assert cash["NIS"].current_value_local == 30_000.0

    def test_rejects_a_nonsense_fx_rate(self):
        with pytest.raises(ValueError, match="usd_ils must be positive"):
            build_leumi_positions(self._portfolio(), [], usd_ils=0.0, eur_usd=1.16)


class TestRefusesAnUnreconciledFile:
    def test_import_aborts_when_the_file_disagrees_with_its_own_header(self, tmp_path):
        """A short read must never become a silent position wipe."""
        from argosy.services.leumi_import import import_leumi

        f = tmp_path / "p.xls"
        f.write_text(
            '<?xml version="1.0"?><Workbook><Table>'
            "<Row><Cell><Data>תאריך:</Data></Cell><Cell><Data>22.08.26</Data></Cell></Row>"
            "<Row><Cell><Data>מס' ניירות:</Data></Cell><Cell><Data>38</Data></Cell>"
            "<Cell><Data>שווי תיק עדכני ב$</Data></Cell><Cell><Data>999999.00</Data></Cell></Row>"
            "<Row><Cell><Data>מספר נייר</Data></Cell></Row>"
            "<Row><Cell><Data>60183896</Data></Cell><Cell><Data>(X) CSPX LN</Data></Cell>"
            "<Cell><Data></Data></Cell><Cell><Data>1</Data></Cell><Cell><Data>1</Data></Cell>"
            "<Cell><Data>1</Data></Cell><Cell><Data>100.00</Data></Cell>"
            "<Cell><Data>0</Data></Cell><Cell><Data>0</Data></Cell><Cell><Data>0</Data></Cell>"
            "<Cell><Data>1</Data></Cell></Row>"
            "</Table></Workbook>", encoding="utf-8")
        with pytest.raises(ValueError, match="refusing to import"):
            import_leumi(None, user_id="ariel", portfolio_path=f, apply=True)
