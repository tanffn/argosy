"""Migration 0051 — prediction_outcomes + evaluation_method_registry + predictions FK.

Spec C (predictions ledger) commit #2. Smoke tests cover:

* upgrade applies cleanly and creates both tables;
* the registry is seeded with the five v1 methods (the four §5 scoring
  methods + ``unparseable`` as the method-of-record for unparseable
  predictions);
* ``prediction_outcomes.outcome_kind`` CHECK rejects unknown values
  while accepting all six known ones;
* FK ``prediction_outcomes.prediction_id → predictions(id) ON DELETE
  CASCADE`` is enforced — deleting a parent prediction takes its
  outcomes with it;
* UNIQUE ``(prediction_id, evaluation_method)`` prevents duplicate
  outcome rows for the same (prediction, method) pair (replay-safety
  invariant: re-scoring under v1 is a no-op);
* multiple outcome rows for the SAME prediction under DIFFERENT
  methods coexist (replay path: a new method version inserts a new
  row, leaving the prior one intact);
* FK ``prediction_outcomes.evaluation_method → registry.method_name``
  rejects un-registered method names;
* FK ``predictions.evaluation_method → registry.method_name`` rejects
  un-registered method names (added in migration 0051 via batch_alter);
* downgrade reverses all three changes cleanly (drops both tables,
  removes the predictions FK while preserving the predictions table).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0051.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


# SQLite enforces FKs only when PRAGMA foreign_keys = ON is set per
# connection (it defaults to OFF). The Argosy app session-factory turns
# it on at session-open; the alembic command machinery does NOT (it
# uses its own connection). For the test cases that assert FK behavior
# we wire the pragma via a connection event so every fresh connection
# the test creates has FK enforcement live.
def _enable_fk_enforcement(engine: sa.Engine) -> None:
    @sa.event.listens_for(engine, "connect")
    def _fk_pragma_on_connect(dbapi_conn, _conn_record):  # pragma: no cover
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh per-test DB rooted at ``tmp_path`` (mirrors 0049 fixture)."""
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


def _insert_prediction(
    engine: sa.Engine,
    *,
    user_id: str = "ariel",
    source: str = "discord",
    direction: str = "long",
    evaluation_method: str = "target_stop",
    event_at: str = "2026-05-15 10:23:00+00:00",
    evaluation_due_at: str = "2026-05-22 10:23:00+00:00",
    source_ref: str = '{"channel_id":"c1","message_id":"m1"}',
    message_id: str | None = None,
    ticker: str | None = "NVDA",
) -> int:
    """Insert a predictions row + return its id."""
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO predictions ("
                " user_id, source, source_ref, ticker, direction,"
                " event_at, created_at, evaluation_due_at,"
                " evaluation_method, message_id"
                ") VALUES ("
                " :uid, :src, :sref, :tkr, :dir,"
                " :evt, :cra, :due,"
                " :em, :mid"
                ")"
            ),
            {
                "uid": user_id,
                "src": source,
                "sref": source_ref,
                "tkr": ticker,
                "dir": direction,
                "evt": event_at,
                "cra": "2026-05-15 10:23:00+00:00",
                "due": evaluation_due_at,
                "em": evaluation_method,
                "mid": message_id,
            },
        )
        # SQLite: lastrowid; works because predictions.id is AUTOINCREMENT.
        return int(result.lastrowid)


def test_0051_creates_evaluation_method_registry(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "evaluation_method_registry" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("evaluation_method_registry")}
    expected = {
        "method_name",
        "family",
        "method_version",
        "description",
        "is_active",
        "superseded_by",
        "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

    # PK is method_name.
    pks = insp.get_pk_constraint("evaluation_method_registry")
    assert pks["constrained_columns"] == ["method_name"]


def test_0051_seeds_initial_methods(alembic_cfg):
    """The five v1 methods land in the registry on upgrade.

    Per spec §5 (target_stop, fixed_lookahead_7d, fixed_lookahead_30d,
    multi_basket_weighted) plus ``unparseable`` as the method-of-record
    for predictions the writer flagged as structurally unscoreable
    (§1.2 / §3.1). All five must be ``is_active = 1`` on fresh upgrade.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT method_name, family, method_version, is_active "
                "FROM evaluation_method_registry "
                "ORDER BY method_name"
            )
        ).fetchall()

    by_name = {r[0]: (r[1], r[2], r[3]) for r in rows}

    # Exactly five seed rows.
    assert len(rows) == 5, f"expected 5 seeds, got {len(rows)}: {by_name}"

    expected_seeds = {
        "target_stop": ("target_stop", 1, 1),
        "fixed_lookahead_7d": ("fixed_lookahead", 1, 1),
        "fixed_lookahead_30d": ("fixed_lookahead", 1, 1),
        "multi_basket_weighted": ("multi_basket", 1, 1),
        "unparseable": ("unparseable", 1, 1),
    }
    assert by_name == expected_seeds


def test_0051_creates_prediction_outcomes_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "prediction_outcomes" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("prediction_outcomes")}
    expected = {
        "id",
        "prediction_id",
        "outcome_kind",
        "pnl_pct",
        "evaluated_at",
        "evaluation_method",
        "entry_price_used",
        "exit_price_used",
        "exit_trigger_date",
        "evidence_json",
        "notes",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

    idx_names = {i["name"] for i in insp.get_indexes("prediction_outcomes")}
    # UNIQUE (prediction_id, evaluation_method) may appear as
    # unique_constraints or indexes depending on dialect.
    uqs = {
        u["name"] for u in insp.get_unique_constraints("prediction_outcomes")
    }
    assert (
        "ix_outcomes_pred_method" in idx_names
        or "ix_outcomes_pred_method" in uqs
    )
    assert "ix_outcomes_evaluated" in idx_names
    assert "ix_outcomes_kind" in idx_names


def test_0051_outcome_kind_check_rejects_bad_values(alembic_cfg):
    """CHECK enforces the six values from spec §2.4."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)
    pred_id = _insert_prediction(engine)

    # All six valid kinds accepted (one per prediction-method pair —
    # we use distinct methods so the UNIQUE on (pred_id, method)
    # doesn't fire).
    valid_kinds = (
        "hit_target",
        "hit_stop",
        "expired_neutral",
        "expired_positive",
        "expired_negative",
        "unparseable",
    )
    methods = (
        "target_stop",
        "fixed_lookahead_7d",
        "fixed_lookahead_30d",
        "multi_basket_weighted",
        "unparseable",
    )
    # Pair the first 5 valid kinds with the 5 distinct methods; the 6th
    # kind reuses a separate prediction.
    for kind, method in zip(valid_kinds[:5], methods):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, outcome_kind, evaluation_method, "
                    " evaluated_at) "
                    "VALUES (:pid, :k, :m, "
                    "'2026-05-29 10:00:00+00:00')"
                ),
                {"pid": pred_id, "k": kind, "m": method},
            )

    # Use a fresh prediction for the sixth valid kind to avoid the
    # UNIQUE clash on (prediction_id, evaluation_method) — the prior
    # loop already paired target_stop with hit_target on this pred_id.
    pred_id_b = _insert_prediction(
        engine,
        message_id="m2",
        source_ref='{"channel_id":"c1","message_id":"m2"}',
    )
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO prediction_outcomes "
                "(prediction_id, outcome_kind, evaluation_method, "
                " evaluated_at) "
                "VALUES (:pid, 'unparseable', 'unparseable', "
                "'2026-05-29 10:00:00+00:00')"
            ),
            {"pid": pred_id_b},
        )

    # Unknown kind rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, outcome_kind, evaluation_method, "
                    " evaluated_at) "
                    "VALUES (:pid, 'definitely_invalid', 'target_stop', "
                    "'2026-05-29 10:00:00+00:00')"
                ),
                {"pid": pred_id_b},
            )


def test_0051_prediction_fk_cascade_delete(alembic_cfg):
    """Deleting a prediction cascades to its outcomes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)

    pred_id = _insert_prediction(engine)

    # Two outcome rows under different methods — both should cascade.
    for method in ("target_stop", "fixed_lookahead_30d"):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, outcome_kind, evaluation_method, "
                    " evaluated_at) "
                    "VALUES (:pid, 'hit_target', :m, "
                    "'2026-05-29 10:00:00+00:00')"
                ),
                {"pid": pred_id, "m": method},
            )

    # Confirm both outcomes are in place.
    with engine.connect() as conn:
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM prediction_outcomes "
                "WHERE prediction_id = :pid"
            ),
            {"pid": pred_id},
        ).scalar()
    assert count == 2

    # Delete the parent prediction.
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM predictions WHERE id = :pid"),
            {"pid": pred_id},
        )

    # Outcomes are gone too.
    with engine.connect() as conn:
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM prediction_outcomes "
                "WHERE prediction_id = :pid"
            ),
            {"pid": pred_id},
        ).scalar()
    assert count == 0


def test_0051_unique_pred_method_blocks_duplicates(alembic_cfg):
    """(prediction_id, evaluation_method) is the natural key.

    Replay-safety invariant: re-scoring the same prediction with the
    same v1 method is a no-op. The evaluator uses ``ON CONFLICT DO
    NOTHING`` against this index. A different method on the SAME
    prediction is allowed (replay path — see separate test below).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)
    pred_id = _insert_prediction(engine)

    # First insert succeeds.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO prediction_outcomes "
                "(prediction_id, outcome_kind, evaluation_method, "
                " evaluated_at) "
                "VALUES (:pid, 'hit_target', 'target_stop', "
                "'2026-05-29 10:00:00+00:00')"
            ),
            {"pid": pred_id},
        )

    # Same (prediction_id, method) → IntegrityError.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, outcome_kind, evaluation_method, "
                    " evaluated_at) "
                    "VALUES (:pid, 'hit_stop', 'target_stop', "
                    "'2026-05-29 11:00:00+00:00')"
                ),
                {"pid": pred_id},
            )


def test_0051_replay_safe_under_different_method(alembic_cfg):
    """Replay path: a NEW method version inserts a fresh row.

    Spec §3.4 — when a scoring rule is bumped (e.g. ``target_stop`` ->
    ``target_stop_v2``), evaluator with replay=True inserts new
    outcome rows under the new method discriminator. The prior row is
    immutable; both coexist; the registry's ``is_active`` flag decides
    which one the view picks. This test demonstrates the schema-level
    permission (the higher-level view logic is commit #5).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)
    pred_id = _insert_prediction(engine)

    # Two outcomes for the same prediction under different methods.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO prediction_outcomes "
                "(prediction_id, outcome_kind, evaluation_method, "
                " evaluated_at) "
                "VALUES (:pid, 'hit_target', 'target_stop', "
                "'2026-05-29 10:00:00+00:00')"
            ),
            {"pid": pred_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO prediction_outcomes "
                "(prediction_id, outcome_kind, evaluation_method, "
                " evaluated_at) "
                "VALUES (:pid, 'expired_positive', 'fixed_lookahead_30d', "
                "'2026-05-29 11:00:00+00:00')"
            ),
            {"pid": pred_id},
        )

    with engine.connect() as conn:
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM prediction_outcomes "
                "WHERE prediction_id = :pid"
            ),
            {"pid": pred_id},
        ).scalar()
    assert count == 2


def test_0051_outcomes_evaluation_method_fk_rejects_unregistered(
    alembic_cfg,
):
    """Outcome's evaluation_method must reference a registry row."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)
    pred_id = _insert_prediction(engine)

    # Unregistered method → FK violation at outcome insert.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, outcome_kind, evaluation_method, "
                    " evaluated_at) "
                    "VALUES (:pid, 'hit_target', 'method_does_not_exist', "
                    "'2026-05-29 10:00:00+00:00')"
                ),
                {"pid": pred_id},
            )

    # Adding the method to the registry first → insert now succeeds.
    # This is the explicit "no schema migration needed" test of §3.4.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO evaluation_method_registry "
                "(method_name, family, method_version, is_active) "
                "VALUES ('fixed_lookahead_30d_v2', 'fixed_lookahead', "
                "2, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO prediction_outcomes "
                "(prediction_id, outcome_kind, evaluation_method, "
                " evaluated_at) "
                "VALUES (:pid, 'expired_positive', "
                "'fixed_lookahead_30d_v2', "
                "'2026-05-29 12:00:00+00:00')"
            ),
            {"pid": pred_id},
        )


def test_0051_predictions_evaluation_method_fk_rejects_unregistered(
    alembic_cfg,
):
    """Predictions.evaluation_method FK (added by 0051) enforces.

    Codex BLOCKER 1 fix — migration 0051 attaches the FK on
    ``predictions.evaluation_method → evaluation_method_registry.method_name``
    via batch_alter_table. An INSERT with a non-registered method must
    fail.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    engine = sa.create_engine(sync_url)
    _enable_fk_enforcement(engine)
    _seed_user(engine)

    # Insert with a non-registered evaluation_method → FK violation.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_prediction(
            engine, evaluation_method="rogue_method_not_registered"
        )

    # The same insert with a registered method succeeds.
    pred_id = _insert_prediction(
        engine, evaluation_method="target_stop"
    )
    assert pred_id >= 1

    # The NOT-NULL on predictions.evaluation_method is preserved by the
    # batch helper; a NULL insert raises.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO predictions ("
                    " user_id, source, source_ref, ticker, direction,"
                    " event_at, created_at, evaluation_due_at,"
                    " evaluation_method"
                    ") VALUES ("
                    " 'ariel', 'discord', '{}', 'NVDA', 'long',"
                    " '2026-05-15 10:23:00+00:00',"
                    " '2026-05-15 10:23:00+00:00',"
                    " '2026-05-22 10:23:00+00:00',"
                    " NULL"
                    ")"
                )
            )


def test_0051_downgrade_reverses_cleanly(alembic_cfg):
    """Downgrade drops both new tables and removes the predictions FK.

    Three checks:
    * ``prediction_outcomes`` table is gone.
    * ``evaluation_method_registry`` table is gone.
    * ``predictions`` table survives but the FK on evaluation_method is
      removed (i.e. a predictions row with an arbitrary method TEXT
      value can be inserted after the downgrade — the FK no longer
      gates it). The NOT-NULL on evaluation_method is preserved
      (it was set in migration 0050 and our batch_alter must not have
      dropped it).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")

    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "prediction_outcomes" in insp.get_table_names()
    assert "evaluation_method_registry" in insp.get_table_names()
    assert "predictions" in insp.get_table_names()

    # Downgrade back to 0050.
    command.downgrade(cfg, "0050_predictions")

    engine_after = sa.create_engine(sync_url)
    insp_after = sa.inspect(engine_after)
    tables_after = insp_after.get_table_names()
    assert "prediction_outcomes" not in tables_after
    assert "evaluation_method_registry" not in tables_after
    assert "predictions" in tables_after

    # FK on predictions.evaluation_method is gone — an arbitrary TEXT
    # method now inserts cleanly (no FK to enforce against).
    _enable_fk_enforcement(engine_after)
    _seed_user(engine_after)
    with engine_after.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO predictions ("
                " user_id, source, source_ref, ticker, direction,"
                " event_at, created_at, evaluation_due_at,"
                " evaluation_method"
                ") VALUES ("
                " 'ariel', 'discord', '{}', 'NVDA', 'long',"
                " '2026-05-15 10:23:00+00:00',"
                " '2026-05-15 10:23:00+00:00',"
                " '2026-05-22 10:23:00+00:00',"
                " 'some_method_not_in_registry'"
                ")"
            )
        )

    # NOT-NULL on evaluation_method is preserved.
    with pytest.raises(sa.exc.IntegrityError):
        with engine_after.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO predictions ("
                    " user_id, source, source_ref, ticker, direction,"
                    " event_at, created_at, evaluation_due_at,"
                    " evaluation_method"
                    ") VALUES ("
                    " 'ariel', 'discord', '{}', 'AMD', 'long',"
                    " '2026-05-15 10:23:00+00:00',"
                    " '2026-05-15 10:23:00+00:00',"
                    " '2026-05-22 10:23:00+00:00',"
                    " NULL"
                    ")"
                )
            )


def test_0051_upgrade_downgrade_upgrade_round_trip(alembic_cfg):
    """Round-trip safety — upgrade, downgrade, upgrade reseeds correctly.

    Catches a class of bug where the seed bulk_insert is order-sensitive
    or the registry self-FK breaks on second upgrade. After the second
    upgrade the registry must again hold all five v1 rows.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0051_prediction_outcomes")
    command.downgrade(cfg, "0050_predictions")
    command.upgrade(cfg, "0051_prediction_outcomes")

    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        count = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM evaluation_method_registry"
            )
        ).scalar()
    assert count == 5
