"""Tests for argosy.services.turn_attachments (Wave 5 + provenance Wave A).

Wave A re-routed `save_attachment` through the file_catalog helper, which
moved storage from `<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/` to
`<ARGOSY_HOME>/uploads/<user_id>/<YYYY>/<YYYY-MM-DD>/<HHMMSS>__<sha8>__<name>`.
The `Attachment` shape is preserved so callers don't break, but the
filesystem layout assertions are updated.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from argosy.services.turn_attachments import (
    Attachment,
    AttachmentEncryptedError,
    AttachmentTooLargeError,
    AttachmentUnsupportedError,
    MAX_BYTES_PER_FILE,
    _is_pdf_encrypted,
    save_attachment,
    save_attachments_with_total_cap,
)


def _upload(content: bytes, *, filename: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_save_attachment_classifies_markdown_as_text(argosy_home_db):
    upload = _upload(b"# Hello\n\nMarkdown body.", filename="plan.md", content_type="text/markdown")
    att = await save_attachment(user_id="ariel", turn_uuid="t1", upload=upload)

    assert isinstance(att, Attachment)
    assert att.kind == "text"
    assert att.original_name == "plan.md"
    assert att.size_bytes == len(b"# Hello\n\nMarkdown body.")
    assert Path(att.path).exists()
    assert Path(att.path).read_bytes() == b"# Hello\n\nMarkdown body."


@pytest.mark.asyncio
async def test_save_attachment_classifies_tsv_as_text(argosy_home_db):
    """Tab-separated-values files are tabular text (same shape as CSV).

    Browsers commonly send `application/octet-stream` (no MIME hint) for
    `.tsv` files, so the extension allowlist is the gate that matters.
    """
    upload = _upload(
        b"col1\tcol2\tcol3\nfoo\tbar\tbaz\n",
        filename="data.tsv",
        content_type="application/octet-stream",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="t-tsv", upload=upload)
    assert att.kind == "text"
    assert att.original_name == "data.tsv"
    assert Path(att.path).exists()


@pytest.mark.asyncio
async def test_save_attachment_classifies_png_as_image(argosy_home_db):
    # 1x1 transparent PNG (smallest valid PNG)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    )
    upload = _upload(png, filename="screenshot.png", content_type="image/png")
    att = await save_attachment(user_id="ariel", turn_uuid="t2", upload=upload)

    assert att.kind == "image"
    assert att.mime_type == "image/png"
    assert Path(att.path).exists()


@pytest.mark.asyncio
async def test_save_attachment_classifies_pdf(argosy_home_db):
    """PDFs are accepted as kind='pdf' so the advisor route can forward
    them to the Anthropic API as native ``document`` content blocks.
    Layout / tables / scans survive (the prior text-extraction path
    lost them)."""
    upload = _upload(b"%PDF-1.4\n...", filename="doc.pdf", content_type="application/pdf")
    att = await save_attachment(user_id="ariel", turn_uuid="t3", upload=upload)
    assert att.kind == "pdf"
    assert att.mime_type == "application/pdf"
    assert Path(att.path).exists()


@pytest.mark.asyncio
async def test_save_attachment_rejects_truly_unsupported_type(argosy_home_db):
    """An exec / unknown binary still 415s — the allowlist is closed."""
    upload = _upload(
        b"\x7fELF\x02\x01...", filename="payload.bin", content_type="application/octet-stream",
    )
    with pytest.raises(AttachmentUnsupportedError) as exc_info:
        await save_attachment(user_id="ariel", turn_uuid="t-bad", upload=upload)
    assert exc_info.value.status_code == 415


@pytest.mark.asyncio
async def test_save_attachment_rejects_oversize(argosy_home_db):
    huge = b"x" * (MAX_BYTES_PER_FILE + 1)
    upload = _upload(huge, filename="big.txt", content_type="text/plain")
    with pytest.raises(AttachmentTooLargeError) as exc_info:
        await save_attachment(user_id="ariel", turn_uuid="t4", upload=upload)
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_save_attachment_distinct_paths_for_different_content(argosy_home_db):
    """Wave A: two uploads with different content land at distinct paths.

    Pre-Wave-A layout used `<turn_uuid>/<filename>` with a `-N` suffix
    rule for collisions. New layout uses `<HHMMSS>__<sha8>__<filename>`
    so different bytes produce different sha-prefixes regardless of name.
    """
    a = await save_attachment(
        user_id="ariel", turn_uuid="t5",
        upload=_upload(b"first", filename="note.txt", content_type="text/plain"),
    )
    b = await save_attachment(
        user_id="ariel", turn_uuid="t5",
        upload=_upload(b"second", filename="note.txt", content_type="text/plain"),
    )
    assert a.path != b.path
    assert Path(a.path).read_bytes() == b"first"
    assert Path(b.path).read_bytes() == b"second"


@pytest.mark.asyncio
async def test_save_attachment_dedups_identical_bytes(argosy_home_db):
    """Wave A: re-uploading identical bytes returns the same on-disk file
    (the catalog dedups by sha256). The filename header in the second
    Attachment may carry the second upload's `original_name` but the
    storage path resolves to the same file.
    """
    a = await save_attachment(
        user_id="ariel", turn_uuid="t5a",
        upload=_upload(b"identical", filename="a.txt", content_type="text/plain"),
    )
    b = await save_attachment(
        user_id="ariel", turn_uuid="t5b",
        upload=_upload(b"identical", filename="b.txt", content_type="text/plain"),
    )
    assert a.path == b.path, "dedup should land at the same file"
    assert Path(a.path).read_bytes() == b"identical"


@pytest.mark.asyncio
async def test_save_attachments_total_cap_rolls_back(argosy_home_db):
    """Per-turn total cap: 12 MB chunks each trip the per-FILE 10 MB cap
    first; this test pins that behavior so a future change that reorders
    cap evaluation doesn't silently regress.
    """
    chunk = b"x" * (12 * 1024 * 1024)
    uploads = [
        _upload(chunk, filename=f"f{i}.txt", content_type="text/plain") for i in range(3)
    ]
    with pytest.raises(AttachmentTooLargeError):
        await save_attachments_with_total_cap(
            user_id="ariel", turn_uuid="t6", uploads=uploads,
        )


@pytest.mark.asyncio
async def test_save_attachment_handles_directory_traversal_in_filename(argosy_home_db):
    """Wave A: directory traversal in `original_name` must not escape the
    catalog's user-scoped uploads dir.
    """
    upload = _upload(
        b"benign", filename="../../../etc/passwd", content_type="text/plain",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="t7", upload=upload)
    saved = Path(att.path).resolve()
    # Saved file must be under <ARGOSY_HOME>/uploads/<user_id>/...
    user_root = (argosy_home_db / "uploads" / "ariel").resolve()
    assert str(saved).startswith(str(user_root)), (
        f"saved path {saved} escaped user uploads root {user_root}"
    )


@pytest.mark.asyncio
async def test_save_attachment_sanitizes_windows_illegal_chars(argosy_home_db):
    """A filename with NTFS-illegal chars must not appear unescaped in the
    on-disk filename. Catalog-layer sanitization handles this; original_name
    is preserved as metadata.
    """
    upload = _upload(
        b"hello",
        filename='weird:name<thing>?.md',
        content_type="text/markdown",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="t8", upload=upload)
    saved = Path(att.path)
    for bad in '<>:"|?*':
        assert bad not in saved.name, f"illegal char {bad!r} survived in {saved.name!r}"
    assert saved.exists()
    assert saved.read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_save_attachments_total_cap_actually_exercises_rollback(argosy_home_db):
    """3 distinct 9 MB chunks → each lands on disk individually (under the
    per-file cap), but together they exceed the 20 MB per-turn cap, so
    `save_attachments_with_total_cap` evaluates and triggers the rollback
    branch. Each chunk must be DIFFERENT bytes — otherwise the catalog
    dedups them into one row and the per-turn total never grows.
    """
    uploads = [
        _upload(
            (chr(ord("a") + i).encode()) * (9 * 1024 * 1024),
            filename=f"f{i}.txt", content_type="text/plain",
        )
        for i in range(3)
    ]
    with pytest.raises(AttachmentTooLargeError):
        await save_attachments_with_total_cap(
            user_id="ariel", turn_uuid="t9", uploads=uploads,
        )


# ----------------------------------------------------------------------
# Encrypted-PDF detection (Israeli payslip case)
# ----------------------------------------------------------------------
#
# Confirmed in dev with the user's real תלוש שכר PDFs: claude.exe reports
# "PDF is password protected" via an assistant message, then exits 1; the
# SDK turns that exit code into a fatal ProcessError so even the
# placeholder text is lost. Detecting at upload time gives an actionable
# error instead of an opaque crash.


def test_is_pdf_encrypted_detects_encrypt_in_head():
    """An /Encrypt reference in the leading bytes triggers detection."""
    raw = b"%PDF-1.7\n1 0 obj <</Encrypt 2 0 R /Size 5>> endobj\n%%EOF"
    assert _is_pdf_encrypted(raw) is True


def test_is_pdf_encrypted_detects_encrypt_in_tail():
    """The trailer dict typically lives near the end of large PDFs."""
    raw = b"%PDF-1.7\n" + b"x" * 100_000 + b"\ntrailer<</Encrypt 5 0 R>>\n%%EOF"
    assert _is_pdf_encrypted(raw) is True


def test_is_pdf_encrypted_false_for_plain_pdf():
    """No /Encrypt anywhere → not encrypted."""
    raw = b"%PDF-1.7\n1 0 obj <</Type /Catalog>> endobj\n%%EOF"
    assert _is_pdf_encrypted(raw) is False


@pytest.mark.asyncio
async def test_save_attachment_rejects_encrypted_pdf(argosy_home_db):
    """Uploading a password-locked PDF raises AttachmentEncryptedError
    (HTTP 422) with a message that points the user at the fix."""
    encrypted_pdf = (
        b"%PDF-1.7\n"
        b"1 0 obj\n<</Filter/Standard /V 4 /R 4 /Length 128>>\nendobj\n"
        b"trailer\n<</Encrypt 1 0 R /Size 2>>\n%%EOF\n"
    )
    upload = _upload(
        encrypted_pdf, filename="payslip.pdf", content_type="application/pdf",
    )
    with pytest.raises(AttachmentEncryptedError) as exc_info:
        await save_attachment(user_id="ariel", turn_uuid="enc1", upload=upload)
    assert exc_info.value.status_code == 422
    # Detail should name the file and suggest removing the password.
    detail = exc_info.value.detail
    assert "payslip.pdf" in detail
    assert "password" in detail.lower()


@pytest.mark.asyncio
async def test_save_attachment_accepts_plain_pdf(argosy_home_db):
    """A plain (non-encrypted) PDF still saves normally."""
    plain_pdf = b"%PDF-1.7\n1 0 obj <</Type /Catalog>> endobj\n%%EOF"
    upload = _upload(
        plain_pdf, filename="statement.pdf", content_type="application/pdf",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="enc2", upload=upload)
    assert att.kind == "pdf"
    assert att.size_bytes == len(plain_pdf)


@pytest.mark.asyncio
async def test_save_attachment_decrypts_encrypted_pdf_via_password_config(
    argosy_home_db,
):
    """Encrypted PDF + matching password in
    ${ARGOSY_HOME}/configs/<user_id>/pdf_passwords.json is decrypted
    transparently — the saved file is unencrypted, and the upload
    succeeds without raising AttachmentEncryptedError."""
    import json as _json
    from io import BytesIO as _BytesIO

    from pypdf import PdfReader as _PdfReader
    from pypdf import PdfWriter as _PdfWriter

    # Seed the per-user password config with the matching candidate.
    cfg_dir = Path(argosy_home_db) / "configs" / "ariel"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        _json.dumps({"passwords": ["wrong1", "secret-pw", "wrong2"]}),
        encoding="utf-8",
    )

    # Build a small encrypted PDF using pypdf's own helper.
    writer = _PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="", owner_password="secret-pw")
    buf = _BytesIO()
    writer.write(buf)
    encrypted_bytes = buf.getvalue()

    upload = _upload(
        encrypted_bytes, filename="payslip.pdf", content_type="application/pdf",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="dec1", upload=upload)

    # Upload succeeded — kind detected, file landed on disk.
    assert att.kind == "pdf"
    saved_bytes = Path(att.path).read_bytes()
    # The saved file is the DECRYPTED version, not the original encrypted one.
    saved_reader = _PdfReader(_BytesIO(saved_bytes))
    assert saved_reader.is_encrypted is False
    # Size reflects the re-serialized output, not the original encrypted size.
    assert att.size_bytes == len(saved_bytes)


@pytest.mark.asyncio
async def test_save_attachment_rejects_encrypted_pdf_with_no_matching_password(
    argosy_home_db,
):
    """Encrypted PDF + password config that doesn't contain the right
    password → AttachmentEncryptedError fires as the existing path."""
    import json as _json
    from io import BytesIO as _BytesIO

    from pypdf import PdfWriter as _PdfWriter

    cfg_dir = Path(argosy_home_db) / "configs" / "ariel"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        _json.dumps({"passwords": ["wrong1", "wrong2"]}),
        encoding="utf-8",
    )

    writer = _PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt(user_password="", owner_password="some-other-pw")
    buf = _BytesIO()
    writer.write(buf)

    upload = _upload(
        buf.getvalue(), filename="payslip.pdf", content_type="application/pdf",
    )
    from argosy.services.turn_attachments import AttachmentEncryptedError
    with pytest.raises(AttachmentEncryptedError):
        await save_attachment(user_id="ariel", turn_uuid="dec2", upload=upload)
