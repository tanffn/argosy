"""Migration 0050 — predictions table.

Spec C (predictions-ledger) commit #1. Smoke tests:

* upgrade to 0050 creates the ``predictions`` table with all spec
  Appendix A columns;
* source CHECK accepts the 11 v1 values and rejects unknowns;
* direction CHECK accepts long / short / neutral / multi and rejects
  unknowns;
* ``event_at`` / ``evaluation_due_at`` / ``evaluation_method`` are
  NOT NULL — inserts that omit them fail;
* ``archived`` defaults to 0 and the CHECK enforces (0, 1);
* ``timeframe_days`` CHECK rejects 0 and negatives but allows NULL
  and positives;
* JSON CHECKs reject non-JSON for ``multi_ticker_json`` and
  ``entry_prices_json``;
* the five expected indexes are present (partial-WHERE clauses
  materialised in sqlite_master for the three partial indexes);
* the partial-UNIQUE on (source, message_id) WHERE message_id IS NOT
  NULL only enforces uniqueness on non-NULL rows (two NULL keys
  coexist; two identical non-NULL keys for the same source conflict);
* downgrade drops the table + all indexes cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0050.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh per-test DB rooted at ``tmp_path`` (mirrors 0048 / 0049)."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def _seed_user(engine: sa.Engine, user_id: str = "ariel") -> None:
    """Insert a minimal ``users`` row so FK constraints are satisfied."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:uid, 'free', '2026-05-29 10:00:00+00:00')"
            ),
            {"uid": user_id},
        )


def _insert_valid(engine: sa.Engine, **overrides) -> None:
    """Insert a minimum-valid predictions row, then apply overrides.

    Column overrides may be NULL via ``None`` or a fresh value; pass
    ``__omit__`` to drop a key entirely (for NOT-NULL violation tests).
    """
    defaults = {
        "user_id": "ariel",
        "source": "discord",
        "source_ref": '{"channel_id":1,"message_id":42}',
        "ticker": "NVDA",
        "direction": "long",
        "entry_price": 145.0,
        "target_price": 180.0,
        "stop_price": 135.0,
        "timeframe_days": 7,
        "multi_ticker_json": None,
        "entry_prices_json": None,
        "message_id": "v1|predictions|discord|1.42",
        "raw_text_ref": "news_signals.id:423",
        "unparseable_reason": None,
        "event_at": "2026-05-15 10:23:00+00:00",
        "evaluation_due_at": "2026-05-22 10:23:00+00:00",
        "evaluation_method": "target_stop",
    }
    defaults.update(overrides)
    cols = [k for k, v in defaults.items() if v != "__omit__"]
    placeholders = ", ".join(f":{c}" for c in cols)
    params = {c: defaults[c] for c in cols}
    sql = (
        f"INSERT INTO predictions ({', '.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def test_0050_creates_predictions_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "predictions" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("predictions")}
    expected = {
        "id",
        "user_id",
        "source",
        "source_ref",
        "ticker",
        "direction",
        "entry_price",
        "target_price",
        "stop_price",
        "timeframe_days",
        "multi_ticker_json",
        "entry_prices_json",
        "message_id",
        "raw_text_ref",
        "unparseable_reason",
        "event_at",
        "created_at",
        "evaluation_due_at",
        "evaluation_method",
        "archived",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_0050_indexes_present_with_partial_predicates(alembic_cfg):
    """All five indexes show up; the three partial ones carry their
    WHERE clauses in sqlite_master so a future drop+recreate doesn't
    lose the predicate.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    idx_names = {i["name"] for i in insp.get_indexes("predictions")}
    expected = {
        "ix_predictions_source_event",
        "ix_predictions_ticker_event",
        "ix_predictions_source_messageid",
        "ix_predictions_due_at",
        "ix_predictions_event_at",
    }
    assert expected.issubset(idx_names), (
        f"missing indexes: {expected - idx_names}"
    )

    # Partial-WHERE clauses must be materialised in sqlite_master so
    # the predicate survives schema dumps / future-migration rebuilds.
    with engine.connect() as conn:
        ticker_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_predictions_ticker_event'"
            )
        ).scalar()
        messageid_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_predictions_source_messageid'"
            )
        ).scalar()
        due_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_predictions_due_at'"
            )
        ).scalar()

    assert ticker_sql is not None and "ticker IS NOT NULL" in ticker_sql
    assert (
        messageid_sql is not None
        and "message_id IS NOT NULL" in messageid_sql
    )
    assert due_sql is not None and "archived = 0" in due_sql


def test_0050_valid_row_inserts(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_valid(engine)

    with engine.connect() as conn:
        n = conn.execute(
            sa.text("SELECT COUNT(*) FROM predictions")
        ).scalar()
    assert n == 1


def test_0050_source_check_accepts_all_v1_values(alembic_cfg):
    """All 11 v1 source values per spec §1.2 accepted; an unknown one
    is rejected.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    v1_sources = [
        "discord",
        "news",
        "sec_form_4",
        "tipranks",
        "sec_13f",
        "capitoltrades",
        "internal_per_position_thesis",
        "internal_news_signal_analyst",
        "internal_state_observer",
        "internal_monitor_flags",
        "manual_user",
    ]
    for i, s in enumerate(v1_sources):
        _insert_valid(engine, source=s, message_id=f"key-{i}")

    # Unknown source rejected — proves we kept the enum explicit, not
    # ``source LIKE '%'`` permissive.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, source="bloomberg_terminal", message_id="x")


def test_0050_direction_check_accepts_four_values(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # neutral covers HOLD verdicts (codex BLOCKER 3 fix in §2.4); multi
    # is the basket-rotation case (§5.4).
    for i, d in enumerate(("long", "short", "neutral", "multi")):
        _insert_valid(engine, direction=d, message_id=f"d-{i}")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, direction="sideways", message_id="x")


def test_0050_event_at_not_null(alembic_cfg):
    """Codex IMPORTANT 2 fix in §2.3 — writers MUST pass event_at; the
    DB enforces NOT NULL so backfill paths that forget can't slip
    through with a default-to-CURRENT_TIMESTAMP semantic.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, event_at=None, message_id="ea")


def test_0050_evaluation_due_at_not_null(alembic_cfg):
    """Codex BLOCKER 2 fix — evaluation_due_at is pre-computed at write
    time, so it's NOT NULL. A writer that forgets to compute the
    window can't paper over the bug with a NULL.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, evaluation_due_at=None, message_id="ed")


def test_0050_evaluation_method_not_null(alembic_cfg):
    """Codex BLOCKER 1 fix — evaluation_method is stored at write time
    (single source of truth for which scoring rule applies). NOT NULL
    so a writer that skipped the method-selection step can't insert.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, evaluation_method=None, message_id="em")


def test_0050_archived_defaults_to_zero(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_valid(engine)

    with engine.connect() as conn:
        archived = conn.execute(
            sa.text("SELECT archived FROM predictions")
        ).scalar()
    assert archived == 0


def test_0050_archived_check_enforces_bool(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # archived = 1 is fine.
    _insert_valid(engine, message_id="a1")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE predictions SET archived = 1 "
                "WHERE message_id = 'a1'"
            )
        )

    # archived = 2 rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE predictions SET archived = 2 "
                    "WHERE message_id = 'a1'"
                )
            )


def test_0050_timeframe_days_check_positive(alembic_cfg):
    """NULL allowed (per-source default fallback); positive integer
    allowed; zero and negatives rejected.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    _insert_valid(engine, timeframe_days=None, message_id="tf-null")
    _insert_valid(engine, timeframe_days=1, message_id="tf-1")
    _insert_valid(engine, timeframe_days=90, message_id="tf-90")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, timeframe_days=0, message_id="tf-0")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, timeframe_days=-7, message_id="tf-neg")


def test_0050_multi_ticker_json_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # NULL fine for non-multi rows.
    _insert_valid(engine, multi_ticker_json=None, message_id="mt-null")

    # Valid JSON list accepted (multi-basket row).
    _insert_valid(
        engine,
        direction="multi",
        ticker=None,
        multi_ticker_json=(
            '[{"ticker":"NVDA","direction":"short","weight":0.4},'
            '{"ticker":"SCHG","direction":"long","weight":0.6}]'
        ),
        entry_prices_json='{"NVDA":145.0,"SCHG":98.0}',
        message_id="mt-valid",
    )

    # Non-JSON rejected by json_valid CHECK.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(
            engine,
            multi_ticker_json="this is not json",
            message_id="mt-bad",
        )


def test_0050_entry_prices_json_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # NULL fine.
    _insert_valid(engine, entry_prices_json=None, message_id="ep-null")

    # Valid JSON map accepted.
    _insert_valid(
        engine,
        entry_prices_json='{"NVDA":145.0}',
        message_id="ep-valid",
    )

    # Non-JSON rejected.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(
            engine,
            entry_prices_json="also-not-json",
            message_id="ep-bad",
        )


def test_0050_source_messageid_partial_unique(alembic_cfg):
    """The UNIQUE(source, message_id) partial index only enforces when
    ``message_id IS NOT NULL``:

    * two NULL message_ids for the same source coexist;
    * identical non-NULL message_ids for the SAME source collide;
    * identical non-NULL message_ids for DIFFERENT sources coexist
      (e.g. discord:abc and news:abc — different sources, same key).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Two NULL message_ids for the same source — fine.
    _insert_valid(engine, source="discord", message_id=None)
    _insert_valid(engine, source="discord", message_id=None)

    # Two non-NULL identical message_ids for the same source — rejected.
    _insert_valid(engine, source="discord", message_id="key-A")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_valid(engine, source="discord", message_id="key-A")

    # Same key value but different source — coexist.
    _insert_valid(engine, source="news", message_id="key-A")


def test_0050_created_at_default_current_timestamp(alembic_cfg):
    """created_at defaults to insertion moment when omitted from
    INSERT. Distinct from event_at (codex IMPORTANT 2 fix in §2.3).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_valid(engine)

    with engine.connect() as conn:
        ts = conn.execute(
            sa.text(
                "SELECT created_at FROM predictions "
                "WHERE message_id = 'v1|predictions|discord|1.42'"
            )
        ).scalar()
    assert ts is not None  # CURRENT_TIMESTAMP fired


def test_0050_downgrade_drops_table_and_indexes(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0050_predictions")
    engine = sa.create_engine(sync_url)
    assert "predictions" in sa.inspect(engine).get_table_names()

    command.downgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine_after = sa.create_engine(sync_url)
    insp_after = sa.inspect(engine_after)

    assert "predictions" not in insp_after.get_table_names()

    # No orphaned indexes left in sqlite_master.
    with engine_after.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'ix_predictions_%'"
            )
        ).fetchall()
    assert rows == []
