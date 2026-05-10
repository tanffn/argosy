"""Format sniffing — content-based, filename is hint only."""

import os
from pathlib import Path

import pytest

from argosy.services.expense_ingest.sniff import detect_format, UnknownFormatError
from argosy.services.expense_ingest.types import ParserName

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"

_SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")


def test_sniff_leumi_html_xls():
    assert detect_format(FIXTURES / "leumi_osh_minimal.xls") == ParserName.LEUMI_OSH


def test_sniff_isracard_xlsx():
    assert detect_format(FIXTURES / "isracard_minimal.xlsx") == ParserName.ISRACARD


def test_sniff_max_xlsx():
    assert detect_format(FIXTURES / "max_minimal.xlsx") == ParserName.MAX


def test_sniff_unknown_xlsx_raises(tmp_path):
    import openpyxl
    p = tmp_path / "unknown.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Bogus Sheet Name"
    wb.save(p)
    with pytest.raises(UnknownFormatError):
        detect_format(p)


def test_sniff_unknown_binary_raises(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(UnknownFormatError):
        detect_format(p)


# ---------------------------------------------------------------------------
# Leumi NIS vs USD disambiguation — gated on the live samples since we do
# not ship a synthetic USD fixture (the HTML wrapper carries the currency
# marker only on the real export, not in our minimal hand-rolled file).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset")
def test_sniff_leumi_usd_live_fixture():
    """USD ('פמ"ח') exports must be classified LEUMI_USD, not LEUMI_OSH."""
    samples = Path(_SAMPLES)
    candidates = sorted(samples.glob("**/Leumi/usd.xls"))
    if not candidates:
        pytest.skip("no Leumi USD samples present")
    assert detect_format(candidates[0]) == ParserName.LEUMI_USD


@pytest.mark.skipif(not _SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset")
def test_sniff_leumi_nis_live_fixture_stays_osh():
    """A live NIS Osh export must remain LEUMI_OSH (regression guard for
    the USD detection branch — make sure we didn't over-match).
    """
    samples = Path(_SAMPLES)
    # Filter to Osh files (exclude usd.xls)
    candidates = [
        p for p in samples.glob("**/Leumi/leumi_*.xls")
        if p.is_file() and p.name != "usd.xls"
    ]
    if not candidates:
        pytest.skip("no Leumi NIS samples present")
    for p in candidates:
        assert detect_format(p) == ParserName.LEUMI_OSH, (
            f"{p.name} misclassified as USD"
        )


def test_sniff_leumi_synthetic_minimal_stays_osh():
    """The synthetic NIS fixture must remain LEUMI_OSH (no דולר marker)."""
    assert detect_format(FIXTURES / "leumi_osh_minimal.xls") == ParserName.LEUMI_OSH
