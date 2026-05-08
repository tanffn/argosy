"""Tests for argosy.services.file_catalog (Wave A — provenance catalog)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.services.file_catalog import (
    UserFileDTO,
    catalog_upload,
)
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, User, UserFile


@pytest.fixture
async def _user(client_with_db):
    """Ensure a 'ariel' User row exists in the DB the catalog will write to."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_catalog_upload_writes_row_and_file(_user, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    bytes_ = b"# My plan\n\nGoal: retire by 2040.\n"
    dto = await catalog_upload(
        user_id="ariel",
        raw_bytes=bytes_,
        original_name="plan.md",
        mime_type="text/markdown",
        kind="plan_markdown",
        source="chat_attachment",
        turn_uuid="abc123",
    )
    assert isinstance(dto, UserFileDTO)
    assert dto.sha256 == hashlib.sha256(bytes_).hexdigest()
    assert dto.size_bytes == len(bytes_)
    assert dto.kind == "plan_markdown"
    assert dto.source == "chat_attachment"
    assert dto.turn_uuid == "abc123"
    assert Path(dto.storage_path).exists()
    assert Path(dto.storage_path).read_bytes() == bytes_


@pytest.mark.asyncio
async def test_catalog_upload_dedups_same_user_same_bytes(_user, tmp_path, monkeypatch):
    """Re-uploading identical bytes for the same user must return the
    existing row without writing a second file on disk.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    bytes_ = b"hello world"
    a = await catalog_upload(
        user_id="ariel", raw_bytes=bytes_,
        original_name="a.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    b = await catalog_upload(
        user_id="ariel", raw_bytes=bytes_,
        original_name="b.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    assert a.id == b.id, "second upload of identical bytes must return same row"
    assert a.storage_path == b.storage_path

    # Only one row in DB for this user.
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(UserFile).where(UserFile.user_id == "ariel")
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_catalog_upload_per_user_dedup(_user, client_with_db, tmp_path, monkeypatch):
    """Two users uploading the same bytes get two rows (dedup is per-user)."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "bob") is None:
            sess.add(User(id="bob", plan="free"))
            sess.commit()
    finally:
        sess.close()

    bytes_ = b"shared content"
    a = await catalog_upload(
        user_id="ariel", raw_bytes=bytes_,
        original_name="x.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    b = await catalog_upload(
        user_id="bob", raw_bytes=bytes_,
        original_name="x.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    assert a.id != b.id
    assert a.sha256 == b.sha256


@pytest.mark.asyncio
async def test_catalog_upload_storage_layout(_user, tmp_path, monkeypatch):
    """Layout: <ARGOSY_HOME>/uploads/<user_id>/<YYYY>/<YYYY-MM-DD>/<HHMMSS>__<sha8>__<sanitized>"""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    dto = await catalog_upload(
        user_id="ariel", raw_bytes=b"layout-test",
        original_name="thing.md", mime_type="text/markdown",
        kind="plan_markdown", source="chat_attachment",
    )
    p = Path(dto.storage_path)
    rel = p.relative_to(tmp_path / "uploads" / "ariel")
    parts = rel.parts
    assert len(parts) == 3, f"expected 3-deep layout, got {parts!r}"
    yyyy, yyyy_mm_dd, fname = parts
    assert re.fullmatch(r"\d{4}", yyyy)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", yyyy_mm_dd)
    # Filename: HHMMSS__sha8__sanitized.ext
    m = re.match(r"^(\d{6})__([0-9a-f]{8})__(.+)$", fname)
    assert m, f"filename pattern mismatch: {fname!r}"
    assert m.group(3) == "thing.md"
    # sha8 is the prefix of the full sha256.
    assert dto.sha256.startswith(m.group(2))


@pytest.mark.asyncio
async def test_catalog_upload_sanitizes_illegal_chars(_user, tmp_path, monkeypatch):
    """NTFS-illegal chars in original_name must not appear in sanitized_name
    or in the on-disk filename.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    dto = await catalog_upload(
        user_id="ariel", raw_bytes=b"y",
        original_name='weird:name<>?.md', mime_type="text/markdown",
        kind="plan_markdown", source="chat_attachment",
    )
    for bad in '<>:"|?*':
        assert bad not in dto.sanitized_name
        assert bad not in Path(dto.storage_path).name
    assert dto.original_name == 'weird:name<>?.md'  # original preserved as metadata


@pytest.mark.asyncio
async def test_catalog_upload_emits_audit_event(_user, tmp_path, monkeypatch):
    """Every successful catalog write must leave an audit_log row with
    event_type='provenance.upload.cataloged'.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    dto = await catalog_upload(
        user_id="ariel", raw_bytes=b"audit-me",
        original_name="audit.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.user_id == "ariel",
                    AuditLog.event_type == "provenance.upload.cataloged",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].entity_type == "user_file"
    assert rows[0].entity_id == str(dto.id)


@pytest.mark.asyncio
async def test_catalog_upload_dedup_does_not_emit_second_audit(
    _user, tmp_path, monkeypatch,
):
    """A dedup hit (second upload, same bytes) returns the existing row but
    must NOT emit a second audit event — otherwise the audit log gets
    polluted with no-op writes.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    bytes_ = b"only-once"
    await catalog_upload(
        user_id="ariel", raw_bytes=bytes_,
        original_name="a.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    await catalog_upload(
        user_id="ariel", raw_bytes=bytes_,
        original_name="b.txt", mime_type="text/plain",
        kind="text", source="chat_attachment",
    )
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.user_id == "ariel",
                    AuditLog.event_type == "provenance.upload.cataloged",
                )
            )
        ).scalars().all()
    assert len(rows) == 1, "second (dedup) call should not emit a second audit event"
