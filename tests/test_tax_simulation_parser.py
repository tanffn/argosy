import os

import pytest

from argosy.services.tax_simulation_parser import parse_rows, parse_workbook

_HEADER = [
    "Number of Shares Requested to Sell- כמות מניות למכירה",
    "Simulation Date- תאריך הסימולציה",
    "Grant Award ID- מספר הענקה",
    "Holding Period - תקופת חסימה לצרכי מס",
    "Grant Date - תאריך הענקה ",
    "Grant Stock Price (For Tax)-USD -שער הענקה",
    "Capital Income USD- הכנסה חייבת",
    "Ordinary Income USD - הכנסה חייבת",
    "Amount Wired to Bank in USD - סכום העברה",
]


def test_parse_rows_maps_bilingual_headers_and_eligibility():
    rows = [
        [280, "18/06/2026", "213000", "OK", "08/06/2022", 18.11, 52229.5, 5069.3, 41089.9],
        [71, "18/06/2026", "331375", "Breaking", "10/03/2025", 126.86, 0, 14529.3, 5496.4],
        [1495, None, None, None, None, None, 31059, None, 157777.7],  # totals row -> skipped
    ]
    lots = parse_rows("RSU", rows, _HEADER)
    assert len(lots) == 2  # totals row dropped
    ok, breaking = lots
    assert ok.grant_id == "213000" and ok.eligible is True and ok.shares == 280
    assert ok.cost_basis_usd == 18.11
    assert breaking.grant_id == "331375" and breaking.eligible is False
    assert breaking.ordinary_income_usd == 14529.3


def test_report_aggregates_eligible_shares():
    rows = [
        [100, "x", "213000", "OK", "d", 18, 1, 0, 9],
        [50, "x", "213000", "OK", "d", 18, 1, 0, 9],
        [30, "x", "331375", "Breaking", "d", 126, 0, 1, 9],
    ]
    from argosy.services.tax_simulation_parser import TaxSimReport
    rep = TaxSimReport(simulation_date="x", lots=parse_rows("RSU", rows, _HEADER))
    assert rep.eligible_shares() == 150
    assert rep.breaking_shares() == 30
    assert rep.eligible_by_grant() == {"213000": 150}


_REAL = r"D:\Google Drive\Family\Finances\Portfolio\Resources\2026\Schwab\Nvidia simulation Report.xlsx"


@pytest.mark.skipif(not os.path.exists(_REAL), reason="real report not present")
def test_parses_real_report_eligibility():
    rep = parse_workbook(_REAL)
    assert rep.simulation_date == "18/06/2026"
    # ~9,230 shares already capital-track eligible (OK); the deconcentration unblocker.
    assert 9000 <= rep.eligible_shares() <= 9500
    assert "213000" in rep.eligible_by_grant()
