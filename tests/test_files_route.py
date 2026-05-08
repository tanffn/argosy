"""Tests for argosy/api/routes/files.py (Wave A — provenance REST surface)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from argosy.services.file_catalog import catalog_upload
from argosy.state.models import User


@pytest.fixture
def _seed_users(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        for uid in ("ariel", "bob"):
            if sess.get(User, uid) is None:
                sess.add(User(id=uid, plan="free"))
        sess.commit()
    finally:
        sess.close()


def _ingest(user_id: str, content: bytes, **kw) -> int:
    """Helper: synchronously catalog a blob and return its row id.

    Uses ``asyncio.run`` to spin up a fresh event loop per call. We can't
    use ``asyncio.get_event_loop()`` — when this test file runs after
    others in the suite, the prior loop is closed and `get_event_loop`
    returns a stale, unusable handle.
    """
    return asyncio.run(
        catalog_upload(
            user_id=user_id,
            raw_bytes=content,
            original_name=kw.get("original_name", "x.txt"),
            mime_type=kw.get("mime_type", "text/plain"),
            kind=kw.get("kind", "text"),
            source=kw.get("source", "chat_attachment"),
        )
    ).id


def test_list_files_empty(client_with_db, _seed_users):
    r = client_with_db.get("/api/files?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_files_returns_users_uploads(client_with_db, _seed_users, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    _ingest("ariel", b"a", original_name="a.txt")
    _ingest("ariel", b"b", original_name="b.md", kind="plan_markdown", mime_type="text/markdown")
    _ingest("bob", b"c", original_name="c.txt")

    r = client_with_db.get("/api/files?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    names = {item["original_name"] for item in body["items"]}
    assert names == {"a.txt", "b.md"}


def test_list_files_kind_filter(client_with_db, _seed_users, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    _ingest("ariel", b"a", original_name="a.txt", kind="text")
    _ingest("ariel", b"b", original_name="b.md", kind="plan_markdown", mime_type="text/markdown")

    r = client_with_db.get("/api/files?user_id=ariel&kind=plan_markdown")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["original_name"] == "b.md"


def test_list_files_source_filter(client_with_db, _seed_users, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    _ingest("ariel", b"a", original_name="a.txt", source="chat_attachment")
    _ingest("ariel", b"b", original_name="b.txt", source="intake_upload")

    r = client_with_db.get("/api/files?user_id=ariel&source=intake_upload")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["original_name"] == "b.txt"


def test_list_files_pagination(client_with_db, _seed_users, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    for i in range(5):
        _ingest("ariel", f"content-{i}".encode(), original_name=f"f{i}.txt")

    r = client_with_db.get("/api/files?user_id=ariel&limit=2&offset=1")
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_get_file_content_streams_bytes(client_with_db, _seed_users, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    fid = _ingest("ariel", b"hello catalog", original_name="x.txt")

    r = client_with_db.get(f"/api/files/{fid}/content?user_id=ariel")
    assert r.status_code == 200
    assert r.content == b"hello catalog"


def test_get_file_content_acl_blocks_other_user(client_with_db, _seed_users, monkeypatch, tmp_path):
    """Bob asking for ariel's file gets a 404 (don't leak existence)."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    fid = _ingest("ariel", b"private", original_name="secret.txt")

    r = client_with_db.get(f"/api/files/{fid}/content?user_id=bob")
    assert r.status_code == 404


def test_get_file_content_404_on_missing_id(client_with_db, _seed_users):
    r = client_with_db.get("/api/files/9999/content?user_id=ariel")
    assert r.status_code == 404


def test_get_file_content_410_on_missing_disk_file(
    client_with_db, _seed_users, monkeypatch, tmp_path,
):
    """A row whose storage_path no longer exists on disk returns 410."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    fid = _ingest("ariel", b"vanish-me", original_name="vanish.txt")
    # Look up the row's storage_path and delete the file.
    sess = client_with_db.app.state.session_factory()
    try:
        from argosy.state.models import UserFile
        row = sess.get(UserFile, fid)
        Path(row.storage_path).unlink()
    finally:
        sess.close()

    r = client_with_db.get(f"/api/files/{fid}/content?user_id=ariel")
    assert r.status_code == 410


def test_list_files_excludes_deleted_by_default(
    client_with_db, _seed_users, monkeypatch, tmp_path,
):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    fid = _ingest("ariel", b"soft-delete-me", original_name="rm.txt")
    sess = client_with_db.app.state.session_factory()
    try:
        from datetime import datetime, timezone
        from argosy.state.models import UserFile
        row = sess.get(UserFile, fid)
        row.deleted_at = datetime.now(timezone.utc)
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/files?user_id=ariel")
    assert r.json()["total"] == 0
    r = client_with_db.get("/api/files?user_id=ariel&include_deleted=true")
    assert r.json()["total"] == 1
