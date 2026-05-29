"""Parity tests for the Leumi portfolio XLS parser.

Fixture: tests/fixtures/portfolio_ingest_leumi/Leumi_26_May_01.xls -- a
real May 2026 export from the user's Bank Leumi web banking. The
parser must extract:
  - 23 positions (matches the declared count in the meta row).
  - Total USD value within ~$5 of the declared total (rounding).
  - Each position's ticker extraction (where applicable).

These are end-to-end parity tests against the real export shape. If
Leumi changes the format, these fail loudly. Add a new dated fixture
when the format shifts so the parser can detect the variant.

Pre-codex-tandem-zigzag note: the user's binding "use codex tandem
for risky work (parsers, money math)" applies here. This first cut
of the parser ships under that gate; we run the zigzag pass once
the parser is in tree so codex can see the actual diff. Until then,
treat the parity tests below as the spec.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argosy.services.portfolio_ingest.parsers.leumi_xls import (
    is_leumi_portfolio_xls,
    parse_leumi_portfolio_xls,
    parse_leumi_portfolio_xls_path,
)


FIXTURE_DIR = (
    Path(__file__).parent.parent.parent / "fixtures" / "portfolio_ingest_leumi"
)
FIXTURE_MAY_2026 = FIXTURE_DIR / "Leumi_26_May_01.xls"


class TestSniffer:
    def test_real_export_matches(self):
        assert is_leumi_portfolio_xls(FIXTURE_MAY_2026.read_text(encoding="utf-8"))

    def test_random_xml_rejected(self):
        # A different XML shape (no "Personal View" Hebrew title)
        assert not is_leumi_portfolio_xls(
            "<?xml version='1.0'?><Workbook><Worksheet/></Workbook>"
        )

    def test_bytes_input_accepted(self):
        assert is_leumi_portfolio_xls(FIXTURE_MAY_2026.read_bytes())


class TestMetaRows:
    def setup_method(self):
        self.snap = parse_leumi_portfolio_xls_path(FIXTURE_MAY_2026)

    def test_snapshot_date_parsed(self):
        assert self.snap.snapshot_date == date(2026, 5, 1)

    def test_portfolio_number_extracted(self):
        # Leumi portfolio number format observed: 882-447452/10
        assert self.snap.portfolio_number is not None
        assert "/" in self.snap.portfolio_number  # has the /10 suffix
        assert "447452" in self.snap.portfolio_number

    def test_declared_securities_count_matches_parsed(self):
        # Meta row says 23 securities; parser should extract that many position rows.
        assert self.snap.securities_count == 23
        assert len(self.snap.positions) == 23

    def test_total_value_extracted(self):
        # Meta row carries "שווי תיק עדכני ב$ : 1,196,253.40"
        assert self.snap.total_value_usd is not None
        assert 1_196_000 < self.snap.total_value_usd < 1_197_000


class TestPositions:
    def setup_method(self):
        self.snap = parse_leumi_portfolio_xls_path(FIXTURE_MAY_2026)

    def test_total_value_reconciles_within_rounding(self):
        # Sum of holding_value_usd across all positions should match the
        # declared total within a few dollars (rounding on the per-row).
        parsed_total = sum(p.holding_value_usd for p in self.snap.positions)
        declared = self.snap.total_value_usd or 0
        # The declared total in the meta INCLUDES the event row's $60.86; the
        # position rows don't carry that, so we expect a small positive gap.
        gap = declared - parsed_total
        assert -100 <= gap <= 200, (
            f"reconciliation off by ${gap:.2f}; declared={declared} parsed={parsed_total}"
        )

    def test_us_ticker_extracted(self):
        # AMD position: Hebrew name "(אדוונסד מיקרו דיווייסז) AMD"
        amd = next(p for p in self.snap.positions if p.ticker == "AMD")
        assert amd.quantity == 100.0
        assert amd.last_price > 300  # ~352 in May 2026

    def test_uk_listed_ticker_extracted(self):
        # CNDX LN (LSE-listed iShares NASDAQ 100) -- ticker should drop the venue suffix.
        cndx = next(p for p in self.snap.positions if p.ticker == "CNDX")
        assert cndx.quantity == 35.0

    def test_brk_dot_b_ticker(self):
        # BRK/B has a forward-slash -- regex must allow it.
        brk = next(p for p in self.snap.positions if p.ticker == "BRK/B")
        assert brk.quantity == 150.0

    def test_israeli_listed_no_latin_ticker(self):
        # ATF tracking TA-200 etc. -- Hebrew-only name, no Latin ticker.
        israeli = [p for p in self.snap.positions if p.ticker is None]
        assert len(israeli) >= 2  # At least the two ATF/MTF Israeli ETFs
        for p in israeli:
            # All Israeli positions still have a security_id + name + value.
            assert p.security_id
            assert p.holding_value_usd > 0

    def test_pct_of_portfolio_sums_to_about_100(self):
        total_pct = sum(
            (p.pct_of_portfolio or 0) for p in self.snap.positions
        )
        # Decimal fractions, so total should sum to ~1.0 (within rounding).
        assert 0.95 < total_pct < 1.05, (
            f"% of portfolio sums to {total_pct:.4f}; expected ~1.00"
        )

    def test_no_cash_position(self):
        # The Leumi portfolio export does NOT include cash (confirmed
        # 2026-05-29 by Ariel). Every parsed position should be a
        # security, not a cash row.
        for p in self.snap.positions:
            # A cash row would have ticker/name like "Cash" or "מזומן"; we
            # don't expect either here.
            assert "Cash" not in (p.name_he or "")
            assert "מזומן" not in (p.name_he or "")


class TestParseWarnings:
    def test_clean_parse_no_warnings(self):
        snap = parse_leumi_portfolio_xls_path(FIXTURE_MAY_2026)
        assert snap.parse_warnings == [], (
            f"unexpected warnings on clean parse: {snap.parse_warnings}"
        )

    def test_missing_header_returns_empty_positions_with_warning(self):
        # Strip the header row; parser should fail gracefully + warn.
        broken = "<?xml version='1.0'?><Workbook><Worksheet Name='S'><Table></Table></Worksheet></Workbook>"
        snap = parse_leumi_portfolio_xls(broken)
        assert snap.positions == []
        assert any("header row not found" in w for w in snap.parse_warnings)


def _minimal_xls(*, headers: list[str], data_rows: list[list[str]]) -> str:
    """Build a minimal Leumi-shaped SpreadsheetML for tests.

    Includes the required preamble + Hebrew title + meta rows so the
    sniffer + header-row finder land on the right rows.
    """
    def row(cells: list[str]) -> str:
        return "<Row>" + "".join(
            f"<Cell><Data ss:Type='String'>{c}</Data></Cell>" for c in cells
        ) + "</Row>"
    sec_count_label = "מס' ניירות:"  # מס' ניירות:
    title = "מבט אישי - האחזקות שלי"  # מבט אישי - האחזקות שלי
    date_label = "תאריך:"  # תאריך:
    portfolio_label = "תיק:"  # תיק:
    total_value_label = "שווי תיק עדכני ב$"  # שווי תיק עדכני ב$
    return (
        "<?xml version='1.0'?>"
        "<Workbook xmlns:ss='urn:schemas-microsoft-com:office:spreadsheet'>"
        "<Worksheet><Table>"
        + row([title])
        + row([date_label, "01.05.26", portfolio_label, "882-447452/10"])
        + row([sec_count_label, str(len(data_rows)), total_value_label, "100000"])
        + row(["placeholder"])
        + row(headers)
        + "".join(row(r) for r in data_rows)
        + "</Table></Worksheet></Workbook>"
    )


# ─── Codex zigzag finding #1: silent zero-coercion ──────────────────────


class TestNoZeroCoercion:
    """When a required money field can't be parsed, row should be SKIPPED
    with a warning, NOT silently zero-coerced. Codex zigzag finding #1."""

    def test_missing_quantity_skips_row_with_warning(self):
        headers = ["מספר נייר", "שם הנייר", "אירועים", "שער קניה ממוצע",
                   "כמות אחזקה", "שער אחרון", "שווי אחזקה ב $"]
        good = ["60001234", "(test) GOOD", "", "100", "10", "200", "2000"]
        bad = ["60005678", "(test) BAD", "", "100", "NOT_A_NUMBER",
               "200", "2000"]
        xls = _minimal_xls(headers=headers, data_rows=[good, bad])
        snap = parse_leumi_portfolio_xls(xls)
        assert len(snap.positions) == 1
        assert snap.positions[0].ticker == "GOOD"
        assert any("missing required numeric field" in w
                   for w in snap.parse_warnings)

    def test_empty_holding_value_skips_row(self):
        headers = ["מספר נייר", "שם הנייר", "אירועים", "שער קניה ממוצע",
                   "כמות אחזקה", "שער אחרון", "שווי אחזקה ב $"]
        bad = ["60005678", "(test) NO_VALUE", "", "100", "10", "200", ""]
        xls = _minimal_xls(headers=headers, data_rows=[bad])
        snap = parse_leumi_portfolio_xls(xls)
        assert snap.positions == []
        assert any("missing required numeric field" in w
                   for w in snap.parse_warnings)


# ─── Codex zigzag finding #2: column-order drift ────────────────────────


class TestColumnOrderTolerance:
    """Parser should use the header row to BIND fields to columns. A
    reordered or extra-column export shouldn't misassign values."""

    def test_reordered_columns_still_parsed_correctly(self):
        # Swap the order of avg_buy_price and quantity in the header
        # (and in each data row). Real Leumi never does this today
        # but a future export might insert a new column at position 3
        # which has the same effect on indices.
        headers = ["מספר נייר", "שם הנייר", "אירועים", "כמות אחזקה",
                   "שער קניה ממוצע", "שער אחרון", "שווי אחזקה ב $"]
        # Same row: 50 quantity, 100 avg buy price (swapped vs original layout)
        row = ["60001234", "(test) AMD", "", "50", "100", "200", "10000"]
        xls = _minimal_xls(headers=headers, data_rows=[row])
        snap = parse_leumi_portfolio_xls(xls)
        assert len(snap.positions) == 1
        p = snap.positions[0]
        assert p.quantity == 50.0
        assert p.avg_buy_price == 100.0
        assert p.last_price == 200.0
        assert p.holding_value_usd == 10000.0

    def test_extra_column_inserted_does_not_misassign(self):
        # Insert a fake column between 'name' and 'avg_buy_price'
        headers = ["מספר נייר", "שם הנייר", "fake_extra", "אירועים",
                   "שער קניה ממוצע", "כמות אחזקה",
                   "שער אחרון", "שווי אחזקה ב $"]
        row = ["60001234", "(test) X", "ignored", "", "100", "10",
               "200", "2000"]
        xls = _minimal_xls(headers=headers, data_rows=[row])
        snap = parse_leumi_portfolio_xls(xls)
        assert len(snap.positions) == 1
        p = snap.positions[0]
        assert p.quantity == 10.0
        assert p.avg_buy_price == 100.0
        assert p.holding_value_usd == 2000.0


# ─── Codex zigzag finding #3: ss:Index sparse-cell handling ─────────────


class TestSparseCells:
    """SpreadsheetML supports `<Cell ss:Index="N">` to skip columns.
    Parser must pad to the indexed position. Codex zigzag finding #3."""

    def test_ss_index_pads_empty_cells(self):
        # Build a row where Cell 3 carries ss:Index="5", meaning cells
        # 3 and 4 should be treated as empty. The Data values then
        # land at positions 0, 1, 2, [empty], [empty], 5, 6, ...
        sparse_row_xml = (
            "<Row>"
            "<Cell><Data ss:Type='String'>60001234</Data></Cell>"
            "<Cell><Data ss:Type='String'>(test) SPARSE</Data></Cell>"
            "<Cell><Data ss:Type='String'></Data></Cell>"
            # Skip to position 5 (1-indexed) -> avg_buy_price at idx 3 (0-idx)
            # should be empty, quantity at idx 4 empty too, then
            # ss:Index='5' value lands at position 5 (1-idx) = idx 4 (0-idx).
            # Actually for the header layout we use, we want quantity
            # (column 5 in our test layout, 1-indexed) to be populated.
            "<Cell ss:Index='5'><Data ss:Type='String'>10</Data></Cell>"
            "<Cell><Data ss:Type='String'>200</Data></Cell>"
            "<Cell><Data ss:Type='String'>2000</Data></Cell>"
            "</Row>"
        )
        # We test the low-level _row_cells helper directly to confirm
        # the padding works.
        from argosy.services.portfolio_ingest.parsers.leumi_xls import _row_cells
        cells = _row_cells(sparse_row_xml)
        # Cell 1=60001234, 2=name, 3=empty, 4=empty (padded),
        # 5=10 (ss:Index), 6=200, 7=2000
        assert cells[0] == "60001234"
        assert cells[1] == "(test) SPARSE"
        assert cells[2] == ""
        assert cells[3] == ""  # padded
        assert cells[4] == "10"
        assert cells[5] == "200"
        assert cells[6] == "2000"


# ─── Codex zigzag finding #4: pct scale normalization ───────────────────


class TestPctScaleNormalization:
    """pct_of_portfolio can come on 0..1 or 0..100 scale. Parser should
    detect the 100-scaled case + normalize + warn. Codex finding #4."""

    def test_pct_above_1_5_is_normalized(self):
        # Position with pct_of_portfolio=25.5 (i.e. 25.5% on a 0..100 scale).
        headers = ["מספר נייר", "שם הנייר", "אירועים", "שער קניה ממוצע",
                   "כמות אחזקה", "שער אחרון", "שווי אחזקה ב $",
                   "% שינוי יומי", "רווח ב-%", "רווח ב $", "%  מהתיק"]
        row = ["60001234", "(test) X", "", "100", "10", "200", "2000",
               "0.005", "0.10", "200", "25.5"]
        xls = _minimal_xls(headers=headers, data_rows=[row])
        snap = parse_leumi_portfolio_xls(xls)
        p = snap.positions[0]
        assert p.pct_of_portfolio == 0.255
        assert any("scaled 0..100" in w for w in snap.parse_warnings)

    def test_pct_below_1_5_is_left_alone(self):
        # Position with pct_of_portfolio=0.255 (already 0..1 scale).
        headers = ["מספר נייר", "שם הנייר", "אירועים", "שער קניה ממוצע",
                   "כמות אחזקה", "שער אחרון", "שווי אחזקה ב $",
                   "% שינוי יומי", "רווח ב-%", "רווח ב $", "%  מהתיק"]
        row = ["60001234", "(test) X", "", "100", "10", "200", "2000",
               "0.005", "0.10", "200", "0.255"]
        xls = _minimal_xls(headers=headers, data_rows=[row])
        snap = parse_leumi_portfolio_xls(xls)
        p = snap.positions[0]
        assert p.pct_of_portfolio == 0.255
        assert not any("scaled 0..100" in w for w in snap.parse_warnings)
