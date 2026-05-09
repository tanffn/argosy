"""Format sniffing — content-based, filename is hint only."""

from pathlib import Path

import pytest

from argosy.services.expense_ingest.sniff import detect_format, UnknownFormatError
from argosy.services.expense_ingest.types import ParserName

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


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
