"""Schema assertions after migration 0019 (user_files catalog + plan_versions.source_file_id).

Wave A introduces ``user_files`` (the provenance catalog) and bridges
existing ``plan_versions`` rows to it via ``source_file_id``. The partial
unique index on ``(user_id, sha256) WHERE deleted_at IS NULL`` is the
dedup contract — the helper relies on it for race recovery.
"""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0019_creates_user_files_table(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    assert "user_files" in insp.get_table_names()


def test_0019_user_files_has_expected_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "user_files")
    expected = {
        "id", "user_id", "sha256", "original_name", "sanitized_name",
        "mime_type", "kind", "size_bytes", "storage_path", "source",
        "turn_uuid", "intake_session_id", "plan_version_id",
        "decision_run_id", "created_at", "deleted_at",
    }
    assert expected.issubset(set(cols.keys())), (
        f"missing: {expected - set(cols.keys())}"
    )
    # Required columns must be NOT NULL.
    for required in (
        "user_id", "sha256", "original_name", "sanitized_name",
        "mime_type", "kind", "size_bytes", "storage_path", "source",
        "created_at",
    ):
        assert cols[required]["nullable"] is False, f"{required} should be NOT NULL"
    # Optional context columns nullable.
    for optional in ("turn_uuid", "intake_session_id", "plan_version_id",
                     "decision_run_id", "deleted_at"):
        assert cols[optional]["nullable"] is True, f"{optional} should be nullable"


def test_0019_creates_user_created_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    idx_names = {i["name"] for i in insp.get_indexes("user_files")}
    assert "ix_user_files_user_created" in idx_names


def test_0019_creates_partial_unique_dedup_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    indexes = insp.get_indexes("user_files")
    by_name = {i["name"]: i for i in indexes}
    assert "ix_user_files_user_sha256_active" in by_name
    idx = by_name["ix_user_files_user_sha256_active"]
    assert bool(idx["unique"]), "dedup index must be unique"
    assert set(idx["column_names"]) == {"user_id", "sha256"}


def test_0019_adds_source_file_id_to_plan_versions(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    assert "source_file_id" in cols
    assert cols["source_file_id"]["nullable"] is True


def test_0019_partial_unique_dedup_enforced(tmp_path, monkeypatch):
    """The partial unique index must reject a second active row with the
    same (user_id, sha256). A soft-deleted row (deleted_at IS NOT NULL)
    must NOT count toward the constraint, so re-uploading after delete
    succeeds.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings
    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO users (id, plan, created_at) VALUES "
                "('ariel', 'free', :now)"
            ), {"now": "2026-01-01"})
            conn.execute(sa.text(
                "INSERT INTO user_files "
                "(user_id, sha256, original_name, sanitized_name, "
                " mime_type, kind, size_bytes, storage_path, source, "
                " created_at) VALUES "
                "('ariel', 'a' * 64, 'one.md', 'one.md', 'text/markdown', "
                " 'plan_markdown', 100, '/tmp/one', 'chat_attachment', :now)"
            ), {"now": "2026-02-01"})

        # Second active row with same (user_id, sha256) must fail.
        try:
            with eng.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO user_files "
                    "(user_id, sha256, original_name, sanitized_name, "
                    " mime_type, kind, size_bytes, storage_path, source, "
                    " created_at) VALUES "
                    "('ariel', 'a' * 64, 'two.md', 'two.md', 'text/markdown', "
                    " 'plan_markdown', 100, '/tmp/two', 'chat_attachment', :now)"
                ), {"now": "2026-02-02"})
            failed = False
        except sa.exc.IntegrityError:
            failed = True
        assert failed, "second active row with same (user_id, sha256) should violate unique"

        # But after soft-delete of the first, a fresh row with the same
        # (user_id, sha256) must succeed.
        with eng.begin() as conn:
            conn.execute(sa.text(
                "UPDATE user_files SET deleted_at=:now "
                "WHERE user_id='ariel' AND original_name='one.md'"
            ), {"now": "2026-02-03"})
            conn.execute(sa.text(
                "INSERT INTO user_files "
                "(user_id, sha256, original_name, sanitized_name, "
                " mime_type, kind, size_bytes, storage_path, source, "
                " created_at) VALUES "
                "('ariel', 'a' * 64, 'three.md', 'three.md', 'text/markdown', "
                " 'plan_markdown', 100, '/tmp/three', 'chat_attachment', :now)"
            ), {"now": "2026-02-04"})

        with eng.connect() as conn:
            count = conn.execute(sa.text(
                "SELECT COUNT(*) FROM user_files WHERE user_id='ariel'"
            )).scalar()
        assert count == 2, "should have 1 soft-deleted + 1 active"
    finally:
        eng.dispose()
