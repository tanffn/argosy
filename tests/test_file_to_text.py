"""Tests for argosy.ingest.file_to_text.

Fixtures are generated programmatically (no binary blobs in the repo):
  - PDF: a 2-page document built with pypdf
  - XLSX: a 2-sheet workbook built with openpyxl
  - CSV/TSV/MD/TXT: plain inline strings
"""

from __future__ import annotations

from io import BytesIO

import pytest

from argosy.ingest.file_to_text import (
    FileTooLargeError,
    MAX_BYTES,
    UnsupportedFileTypeError,
    convert_to_text,
)


# ----------------------------------------------------------------------
# Fixture builders
# ----------------------------------------------------------------------


def _build_pdf_bytes(pages: list[str]) -> bytes:
    """Return PDF bytes containing one text-only page per entry in `pages`."""
    from pypdf import PdfWriter  # type: ignore[import-not-found]

    # pypdf's PdfWriter alone can't lay out arbitrary text; use reportlab
    # if available, else fall back to a hand-built minimal PDF.
    try:
        from reportlab.pdfgen import canvas  # type: ignore[import-not-found]
        from reportlab.lib.pagesizes import letter  # type: ignore[import-not-found]
    except ImportError:
        # Fall back to writing a hand-rolled minimal PDF with each text
        # entry as a content stream. Only meant for the test path.
        return _build_minimal_pdf(pages)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for line in pages:
        c.drawString(72, 720, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def _build_minimal_pdf(pages: list[str]) -> bytes:
    """Hand-rolled minimal PDF with one text per page.

    pypdf's `extract_text` understands this layout. Used when reportlab
    is unavailable in the test environment.
    """
    objs: list[bytes] = []

    def _add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    # Reserve obj numbers in build order: catalog, pages, then per page (page+content).
    catalog_id = _add(b"")  # 1
    pages_id = _add(b"")  # 2
    page_kids: list[int] = []
    for txt in pages:
        # Escape parens/backslashes for PDF string literal.
        safe = txt.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        content_obj = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream
            + b"\nendstream"
        )
        content_id = _add(content_obj)
        page_obj = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("latin-1")
        page_id = _add(page_obj)
        page_kids.append(page_id)
    # Now backfill the catalog and pages root.
    objs[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
    )
    kids_str = " ".join(f"{k} 0 R" for k in page_kids)
    objs[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_kids)} >>".encode(
            "latin-1"
        )
    )

    # Serialize.
    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_pos = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode("latin-1")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("latin-1")
    buf += (
        f"trailer << /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(buf)


def _build_xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    """Return XLSX bytes containing the given sheets keyed by name."""
    from openpyxl import Workbook  # type: ignore[import-untyped]

    wb = Workbook()
    # Workbook starts with a default sheet; remove it.
    default = wb.active
    wb.remove(default)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Plain-text variants
# ----------------------------------------------------------------------


def test_md_returns_text_as_is() -> None:
    src = "# Title\n\n**Bold** plus קרן השתלמות line."
    res = convert_to_text(
        filename="notes.md",
        content_type="text/markdown",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src
    assert res.page_or_sheet_count == 0
    assert res.warnings == []
    assert res.filename == "notes.md"


def test_markdown_extension_alias() -> None:
    src = "Plain content."
    res = convert_to_text(
        filename="notes.markdown", content_type="", data=src.encode("utf-8")
    )
    assert res.extracted_text == src


def test_txt_returns_text_as_is() -> None:
    src = "First line.\nSecond line.\n"
    res = convert_to_text(
        filename="notes.txt",
        content_type="text/plain",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src


def test_csv_returns_text_as_is() -> None:
    src = "ticker,shares\nAAPL,100\nMSFT,50\n"
    res = convert_to_text(
        filename="positions.csv",
        content_type="text/csv",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src


def test_tsv_returns_text_as_is() -> None:
    src = "ticker\tshares\nAAPL\t100\n"
    res = convert_to_text(
        filename="positions.tsv",
        content_type="text/tab-separated-values",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src


def test_text_non_utf8_records_warning() -> None:
    # latin-1 byte 0xff is invalid UTF-8.
    res = convert_to_text(
        filename="weird.txt",
        content_type="text/plain",
        data=b"abc\xffdef",
    )
    assert "abc" in res.extracted_text
    assert any("Non-UTF-8" in w for w in res.warnings)


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------


def test_pdf_two_pages_extracts_text() -> None:
    data = _build_pdf_bytes(["Hello from page one", "Second page content"])
    res = convert_to_text(
        filename="doc.pdf", content_type="application/pdf", data=data
    )
    assert res.page_or_sheet_count == 2
    assert "Hello from page one" in res.extracted_text
    assert "Second page content" in res.extracted_text


def test_pdf_empty_page_records_warning() -> None:
    # First page has text, second page is intentionally blank.
    data = _build_pdf_bytes(["Only this page has text", ""])
    res = convert_to_text(
        filename="doc.pdf", content_type="application/pdf", data=data
    )
    assert res.page_or_sheet_count == 2
    assert any("Page 2" in w for w in res.warnings)


# ----------------------------------------------------------------------
# XLSX
# ----------------------------------------------------------------------


def test_xlsx_per_sheet_csv_like_text() -> None:
    data = _build_xlsx_bytes(
        {
            "Positions": [
                ["ticker", "shares"],
                ["AAPL", 100],
                ["MSFT", 50],
            ],
            "Cash": [
                ["currency", "amount"],
                ["USD", 1234.56],
            ],
        }
    )
    res = convert_to_text(
        filename="broker.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=data,
    )
    assert res.page_or_sheet_count == 2
    assert "## Sheet: Positions" in res.extracted_text
    assert "## Sheet: Cash" in res.extracted_text
    assert "AAPL,100" in res.extracted_text
    assert "USD,1234.56" in res.extracted_text


def test_xlsx_empty_sheet_records_warning() -> None:
    data = _build_xlsx_bytes(
        {
            "Filled": [["a", "b"], [1, 2]],
            "Empty": [],
        }
    )
    res = convert_to_text(
        filename="mixed.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=data,
    )
    assert any("Empty" in w for w in res.warnings)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def test_unsupported_extension_raises() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        convert_to_text(
            filename="image.png", content_type="image/png", data=b"fake"
        )


def test_oversized_payload_raises() -> None:
    big = b"x" * (MAX_BYTES + 1)
    with pytest.raises(FileTooLargeError):
        convert_to_text(filename="big.txt", content_type="text/plain", data=big)


def test_extension_wins_over_unknown_content_type() -> None:
    # User-agents often send octet-stream for unknown payloads; the
    # extension should resolve the kind even if the MIME is wrong.
    src = "fallback content"
    res = convert_to_text(
        filename="notes.md",
        content_type="application/octet-stream",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src


def test_content_type_only_when_extension_missing() -> None:
    src = "no-extension content"
    res = convert_to_text(
        filename="upload",
        content_type="text/plain; charset=utf-8",
        data=src.encode("utf-8"),
    )
    assert res.extracted_text == src
