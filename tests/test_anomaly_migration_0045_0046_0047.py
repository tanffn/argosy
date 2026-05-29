"""Migrations 0045 / 0046 / 0047 — sprint #2 anomaly-detection schema.

Lightweight alembic-driven smoke tests: upgrade to head, exercise the new
tables / columns with a few SQL statements (including CHECK constraint
violations), then downgrade and confirm artifacts disappear.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_anomaly_migration_0045_0046_0047.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


# ---------------------------------------------------------------------------
# Fixture — same pattern as tests/test_migration_0023.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def _seed_user(engine: sa.Engine) -> None:
    """Insert a baseline user so FKs in the new tables can resolve."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', CURRENT_TIMESTAMP)"
            )
        )


# ---------------------------------------------------------------------------
# Migration 0045 — merchant_rolling_stats.
# ---------------------------------------------------------------------------


def test_migration_0045_creates_merchant_rolling_stats(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045_merchant_rolling_stats")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "merchant_rolling_stats" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("merchant_rolling_stats")}
    assert cols["mad_nis"]["nullable"] is True
    assert cols["stdev_nis"]["nullable"] is True
    assert cols["median_nis"]["nullable"] is False
    assert cols["txn_count"]["nullable"] is False
    idx_names = {ix["name"] for ix in insp.get_indexes("merchant_rolling_stats")}
    assert "ix_merchant_rolling_stats_user_merchant" in idx_names


def test_migration_0045_unique_constraint_fires(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045_merchant_rolling_stats")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    insert_sql = sa.text(
        "INSERT INTO merchant_rolling_stats "
        "(user_id, merchant_normalized, category_id, window_start, "
        "window_end, txn_count, median_nis, mean_nis, min_nis, "
        "max_nis, first_seen_at, last_seen_at) "
        "VALUES ('ariel', 'spar', 7, '2026-01-01', '2026-06-30', 5, "
        "100, 105, 80, 130, '2026-01-15', '2026-06-15')"
    )
    with engine.begin() as conn:
        conn.execute(insert_sql)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert_sql)


def test_migration_0045_check_constraint_bad_window(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0045_merchant_rolling_stats")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # window_end < window_start violates ck_merchant_rolling_stats_window_order.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO merchant_rolling_stats "
                "(user_id, merchant_normalized, window_start, window_end, "
                "txn_count, median_nis, mean_nis, min_nis, max_nis, "
                "first_seen_at, last_seen_at) "
                "VALUES ('ariel', 'spar', '2026-06-30', '2026-01-01', "
                "5, 100, 105, 80, 130, '2026-01-15', '2026-06-15')"
            ))


# ---------------------------------------------------------------------------
# Migration 0046 — watchlist_observations + recurring_charge_patterns.
# ---------------------------------------------------------------------------


def test_migration_0046_creates_both_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046_watchlist_recurring")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    names = insp.get_table_names()
    assert "watchlist_observations" in names
    assert "recurring_charge_patterns" in names


def test_migration_0046_watchlist_status_check_constraint(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046_watchlist_recurring")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # Valid status — accepted.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO watchlist_observations "
            "(user_id, watchlist_entry_id, observation_period, status) "
            "VALUES ('ariel', 'fee_waiver_2923', '2026-05-01', 'MATCHED')"
        ))
    # Invalid status — CHECK constraint rejects.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO watchlist_observations "
                "(user_id, watchlist_entry_id, observation_period, status) "
                "VALUES ('ariel', 'fee_waiver_2923', '2026-06-01', 'INVALID_STATE')"
            ))


def test_migration_0046_recurring_min_occurrences_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046_watchlist_recurring")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # occurrence_count < 3 → CHECK rejects (learner requires ≥3 per spec §6).
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO recurring_charge_patterns "
                "(user_id, merchant_normalized, expected_amount_nis, "
                "cadence_days, first_seen, last_seen, occurrence_count) "
                "VALUES ('ariel', 'netflix', 50, 30, "
                "'2026-01-01', '2026-02-01', 2)"
            ))
    # ≥ 3 occurrences accepted.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO recurring_charge_patterns "
            "(user_id, merchant_normalized, expected_amount_nis, "
            "cadence_days, first_seen, last_seen, occurrence_count) "
            "VALUES ('ariel', 'netflix', 50, 30, "
            "'2026-01-01', '2026-03-01', 3)"
        ))


def test_migration_0046_recurring_status_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0046_watchlist_recurring")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO recurring_charge_patterns "
                "(user_id, merchant_normalized, expected_amount_nis, "
                "cadence_days, first_seen, last_seen, occurrence_count, "
                "status) "
                "VALUES ('ariel', 'spotify', 30, 30, "
                "'2026-01-01', '2026-03-01', 3, 'paused')"
            ))


# ---------------------------------------------------------------------------
# Migration 0047 — expense_review_queue extensions.
# ---------------------------------------------------------------------------


def test_migration_0047_adds_extension_columns(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047_expense_review_queue_extensions")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("expense_review_queue")}
    assert {"materiality", "dedup_key", "bucket"}.issubset(cols)
    idx_names = {ix["name"] for ix in insp.get_indexes("expense_review_queue")}
    assert "ix_expense_review_queue_dedup" in idx_names


def test_migration_0047_materiality_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047_expense_review_queue_extensions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # Valid materiality — accepted.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO expense_review_queue "
            "(user_id, kind, status, payload_json, materiality, bucket) "
            "VALUES ('ariel', 'bucket_a_merchant_spike', 'open', '{}', "
            "'critical', 'amount')"
        ))
    # Invalid materiality.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO expense_review_queue "
                "(user_id, kind, status, payload_json, materiality) "
                "VALUES ('ariel', 'bucket_x', 'open', '{}', 'apocalyptic')"
            ))
    # Invalid bucket.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO expense_review_queue "
                "(user_id, kind, status, payload_json, bucket) "
                "VALUES ('ariel', 'bucket_x', 'open', '{}', 'mystery')"
            ))


def test_migration_0047_dedup_partial_unique(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0047_expense_review_queue_extensions")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # Two OPEN rows with the same dedup_key → violation.
    insert = lambda dk, status: sa.text(
        "INSERT INTO expense_review_queue "
        "(user_id, kind, status, payload_json, dedup_key, bucket) "
        f"VALUES ('ariel', 'k', '{status}', '{{}}', '{dk}', 'amount')"
    )
    with engine.begin() as conn:
        conn.execute(insert("v1|a1|tx:1", "open"))
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert("v1|a1|tx:1", "open"))
    # Same dedup_key but resolved status — partial index lets it pass.
    with engine.begin() as conn:
        conn.execute(insert("v1|a1|tx:1", "resolved"))
    # NULL dedup_key has no uniqueness constraint — multiple OK.
    null_insert = sa.text(
        "INSERT INTO expense_review_queue "
        "(user_id, kind, status, payload_json) "
        "VALUES ('ariel', 'k', 'open', '{}')"
    )
    with engine.begin() as conn:
        conn.execute(null_insert)
        conn.execute(null_insert)


# ---------------------------------------------------------------------------
# Downgrade sweep — confirm each migration cleanly reverses.
# ---------------------------------------------------------------------------


def test_downgrade_removes_all_three(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    # Pre-conditions.
    assert "merchant_rolling_stats" in insp.get_table_names()
    assert "watchlist_observations" in insp.get_table_names()
    assert "recurring_charge_patterns" in insp.get_table_names()

    # Step back through each migration.
    command.downgrade(cfg, "0046_watchlist_recurring")
    insp = sa.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("expense_review_queue")}
    assert "materiality" not in cols
    assert "bucket" not in cols
    assert "dedup_key" not in cols

    command.downgrade(cfg, "0045_merchant_rolling_stats")
    insp = sa.inspect(engine)
    assert "watchlist_observations" not in insp.get_table_names()
    assert "recurring_charge_patterns" not in insp.get_table_names()

    command.downgrade(cfg, "0044_rsu_vest_events")
    insp = sa.inspect(engine)
    assert "merchant_rolling_stats" not in insp.get_table_names()
