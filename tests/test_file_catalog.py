"""Tests for argosy.services.file_catalog (Wave A — provenance catalog)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from unittest.mock import patch

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


# ----------------------------------------------------------------------
# EX2 — Discount Bank statement ingest triggers the anomaly runner.
# ----------------------------------------------------------------------


def test_discount_bank_statement_triggers_anomaly_check(
    expense_client, monkeypatch
):
    """Uploading a Discount Bank statement must fire ``schedule_anomaly_check``
    with ``triggered_by='event'`` so a same-day fee-waiver disappearance
    surfaces within seconds.

    Uses the canonical Discount fixture (``discount_minimal.xlsx``). We
    patch ``schedule_anomaly_check`` at its import site inside the
    expenses route so we can assert without spinning a real LLM thread
    (the gate would skip it under pytest anyway).
    """
    captured: list[dict] = []

    def _spy(**kwargs):
        captured.append(kwargs)

    # The route imports schedule_anomaly_check at call time, so we
    # patch the module's exported attribute.
    monkeypatch.setattr(
        "argosy.services.anomaly_runner.schedule_anomaly_check",
        _spy,
    )
    # Avoid touching the real category-resolver LLM call.
    with patch(
        "argosy.services.expense_ingest.category_resolver._categorize_via_llm",
        return_value=[],
    ):
        with open(
            Path(__file__).parent / "fixtures" / "expenses"
            / "discount_minimal.xlsx",
            "rb",
        ) as f:
            files = {"files": (
                "discount_minimal.xlsx", f.read(),
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet",
            )}
            resp = expense_client.post(
                "/api/expenses/upload",
                files=files,
                data={"user_id": "ariel"},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["status"] == "parsed", body
    assert body["results"][0]["parser_name"] == "discount", body

    # Anomaly runner was fired with the right shape.
    assert len(captured) == 1, captured
    kwargs = captured[0]
    assert kwargs["user_id"] == "ariel"
    assert kwargs["triggered_by"] == "event"
    assert kwargs["source_statement_id"] == body["results"][0]["statement_id"]
    assert kwargs["triggering_source_file_id"] is not None


def test_non_discount_upload_does_not_trigger_anomaly_check(
    expense_client, monkeypatch
):
    """Uploading a non-Discount statement (Isracard / Max / Leumi) must
    NOT fire the anomaly runner — the event-driven path is scoped to
    the issuer whose accounts have a watchlist entry."""
    captured: list[dict] = []

    def _spy(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(
        "argosy.services.anomaly_runner.schedule_anomaly_check",
        _spy,
    )
    with patch(
        "argosy.services.expense_ingest.category_resolver._categorize_via_llm",
        return_value=[],
    ):
        with open(
            Path(__file__).parent / "fixtures" / "expenses"
            / "isracard_minimal.xlsx",
            "rb",
        ) as f:
            files = {"files": (
                "isracard_minimal.xlsx", f.read(),
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet",
            )}
            resp = expense_client.post(
                "/api/expenses/upload",
                files=files,
                data={"user_id": "ariel"},
            )
    assert resp.status_code == 200, resp.text
    assert captured == [], (
        "anomaly runner must only fire on Discount Bank ingest"
    )
