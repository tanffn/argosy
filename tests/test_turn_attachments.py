"""Tests for argosy.services.turn_attachments (Wave 5)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from argosy.services.turn_attachments import (
    Attachment,
    AttachmentTooLargeError,
    AttachmentUnsupportedError,
    MAX_BYTES_PER_FILE,
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
async def test_save_attachment_classifies_markdown_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    upload = _upload(b"# Hello\n\nMarkdown body.", filename="plan.md", content_type="text/markdown")
    att = await save_attachment(user_id="ariel", turn_uuid="t1", upload=upload)

    assert isinstance(att, Attachment)
    assert att.kind == "text"
    assert att.original_name == "plan.md"
    assert att.size_bytes == len(b"# Hello\n\nMarkdown body.")
    assert Path(att.path).exists()
    assert Path(att.path).read_bytes() == b"# Hello\n\nMarkdown body."


@pytest.mark.asyncio
async def test_save_attachment_classifies_png_as_image(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

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
async def test_save_attachment_rejects_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    upload = _upload(b"%PDF-1.4\n...", filename="doc.pdf", content_type="application/pdf")
    with pytest.raises(AttachmentUnsupportedError) as exc_info:
        await save_attachment(user_id="ariel", turn_uuid="t3", upload=upload)
    assert exc_info.value.status_code == 415


@pytest.mark.asyncio
async def test_save_attachment_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    huge = b"x" * (MAX_BYTES_PER_FILE + 1)
    upload = _upload(huge, filename="big.txt", content_type="text/plain")
    with pytest.raises(AttachmentTooLargeError) as exc_info:
        await save_attachment(user_id="ariel", turn_uuid="t4", upload=upload)
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_save_attachment_dedup_filename_collisions(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    a = await save_attachment(
        user_id="ariel", turn_uuid="t5",
        upload=_upload(b"first", filename="note.txt", content_type="text/plain"),
    )
    b = await save_attachment(
        user_id="ariel", turn_uuid="t5",
        upload=_upload(b"second", filename="note.txt", content_type="text/plain"),
    )
    assert Path(a.path).name == "note.txt"
    assert Path(b.path).name == "note-1.txt"
    assert Path(a.path).read_bytes() == b"first"
    assert Path(b.path).read_bytes() == b"second"


@pytest.mark.asyncio
async def test_save_attachments_total_cap_rolls_back(tmp_path, monkeypatch):
    """Per-turn total cap: oversized batch rolls back partial files."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    from argosy.services.turn_attachments import MAX_BYTES_PER_TURN

    reload_settings()

    # 3 files each at ~12MB → total exceeds 20MB cap
    chunk = b"x" * (12 * 1024 * 1024)
    uploads = [
        _upload(chunk, filename=f"f{i}.txt", content_type="text/plain") for i in range(3)
    ]
    with pytest.raises(AttachmentTooLargeError):
        await save_attachments_with_total_cap(
            user_id="ariel", turn_uuid="t6", uploads=uploads,
        )

    # No files should remain on disk after rollback
    upload_dir = tmp_path / "uploads" / "ariel" / "t6"
    if upload_dir.exists():
        files = list(upload_dir.iterdir())
        assert files == [], f"rollback failed; leftover files: {files}"


@pytest.mark.asyncio
async def test_save_attachment_handles_directory_traversal_in_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    upload = _upload(
        b"benign", filename="../../../etc/passwd", content_type="text/plain",
    )
    att = await save_attachment(user_id="ariel", turn_uuid="t7", upload=upload)
    # Saved file should be under the turn's upload dir, not at a parent path
    saved = Path(att.path)
    expected_parent = (tmp_path / "uploads" / "ariel" / "t7").resolve()
    assert saved.resolve().parent == expected_parent
