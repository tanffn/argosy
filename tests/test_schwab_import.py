"""Schwab import — positions CSV and the equity-award workbook."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argosy.services import schwab_import as si

DRIVE = Path("D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Schwab")
POS = DRIVE / "Individual-Positions-2026-08-22-125202.csv"
EQ = DRIVE / "EquityAwardsCenter_EquityDetails_2026822195554.xlsx"
needs_real = pytest.mark.skipif(not POS.is_file(), reason="Drive exports absent")

TITLE = '"Positions for account Individual ...876 as of 12:52 PM ET, 2026/08/22"'
HDR = ('"Symbol","Description","Qty (Quantity)","Price","Price Chng $","Price Chng %",'
       '"Mkt Val (Market Value)","Day Chng $","Day Chng %","Cost Basis","Gain $",'
       '"Gain %","Reinvest?","Reinvest Capital Gains?","Cost/Share","Asset Type",')


def _csv(*rows: str) -> str:
    return "\n".join([TITLE, "", HDR, *rows])


class TestPositionsCsv:
    def test_reads_the_as_of_date_from_the_title_line(self, tmp_path):
        f = tmp_path / "p.csv"
        f.write_text(_csv('"BMY","BRISTOL MYERS","200","67.01","1.54","2.35%",'
                          '"$13,402.00","$308.00","2.35%","$10,664.32","$2,737.68",'
                          '"25.67%","No","N/A","$53.32","Equity",'), encoding="utf-8")
        as_of, rows = si.parse_individual_positions(f)
        assert as_of == date(2026, 8, 22)
        assert rows[0].symbol == "BMY" and rows[0].quantity == 200
        assert rows[0].market_value == 13_402.00

    def test_cash_becomes_a_symbol_less_position_not_a_dropped_row(self, tmp_path):
        f = tmp_path / "p.csv"
        f.write_text(_csv('"Cash & Cash Investments","--","--","--","--","--",'
                          '"$302.78","$0.00","0%","--","--","--","--","--","--",'
                          '"Cash and Money Market",'), encoding="utf-8")
        _, rows = si.parse_individual_positions(f)
        assert len(rows) == 1
        assert rows[0].symbol == "" and rows[0].asset_type == "Cash"
        assert rows[0].market_value == 302.78

    def test_positions_total_row_is_not_a_holding(self, tmp_path):
        f = tmp_path / "p.csv"
        f.write_text(_csv(
            '"VOO","VANGUARD","10","703.71","2.70","0.39%","$7,037.10","$27.00",'
            '"0.39%","$5,775.69","$1,261.41","21.84%","No","N/A","$577.57","ETFs",',
            '"Positions Total","","--","--","--","--","$60,616.36","$634.35",'
            '"1.05%","$50,376.48","$9,937.10","19.73%","--","--","--","--",'),
            encoding="utf-8")
        _, rows = si.parse_individual_positions(f)
        assert [r.symbol for r in rows] == ["VOO"]

    def test_missing_as_of_raises(self, tmp_path):
        f = tmp_path / "p.csv"
        f.write_text('"Positions for account"\n\n' + HDR, encoding="utf-8")
        with pytest.raises(ValueError, match="no as-of date"):
            si.parse_individual_positions(f)

    def test_no_rows_raises_rather_than_returning_empty(self, tmp_path):
        f = tmp_path / "p.csv"
        f.write_text(_csv(), encoding="utf-8")
        with pytest.raises(ValueError, match="no position rows"):
            si.parse_individual_positions(f)


@needs_real
class TestAgainstTheRealExports:
    def test_individual_account_reconciles_to_its_own_total(self):
        as_of, rows = si.parse_individual_positions(POS)
        assert as_of == date(2026, 8, 22)
        assert sum(r.market_value for r in rows) == pytest.approx(60_616.36, abs=0.01)

    def test_equity_award_split(self):
        n = si.parse_equity_details(EQ, as_of=date(2026, 8, 22))
        assert n.vested_shares == 10_380
        assert n.unvested_shares == 3_378
        assert n.section_102_eligible == 9_573
        assert n.price == pytest.approx(215.38, abs=0.05)

    def test_the_vest_stream_is_surfaced_not_folded_into_the_position(self):
        """Unvested shares must NOT inflate the position — they are not owned —
        but they must be visible, because a glide sized on the vested count
        alone under-delivers by exactly this amount."""
        n = si.parse_equity_details(EQ, as_of=date(2026, 8, 22))
        assert len(n.future_vests) == 46
        assert sum(v for _, v in n.future_vests) == pytest.approx(3_378)
        assert max(d for d, _ in n.future_vests) == date(2030, 3, 15)
        assert all(d > date(2026, 8, 22) for d, _ in n.future_vests)


class TestImportGuards:
    def test_refuses_with_nothing_to_import(self):
        with pytest.raises(ValueError, match="nothing to import"):
            si.import_schwab(None, user_id="ariel")
