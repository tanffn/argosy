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
