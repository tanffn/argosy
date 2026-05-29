"""Migration 0054 — life_events cashflow-shape extension.

Spec D (/life-events cashflow redesign) commit #1.  Smoke tests:

* upgrade applies cleanly; new columns visible on ``life_events`` +
  ``users``;
* ``life_events_migration_log`` table created with FK
  ``ON DELETE CASCADE`` to ``life_events.id``;
* CHECK on ``delta_kind`` enforces the five-value enum;
* CHECK on ``recurring_period_years`` enforces ``> 0`` when present;
* existing-row data conversion of ``retirement_milestone`` with
  ``target_date`` → ``delta_kind='none'`` + target_date serialized into
  description + migration_log row with
  ``conversion_outcome='preserved'``;
* existing-row data conversion of ``other_asset_acquired`` with
  amount_usd → ``delta_kind='one_shot'`` + ``one_shot_amount_usd``
  populated + migration_log row with
  ``conversion_outcome='preserved'``;
* fall-through (unrecognized kind) → ``delta_kind='none'`` + log row
  with ``conversion_outcome='lossy_converted'``;
* ``users.life_events_migration_acknowledged_at`` is added, nullable;
* downgrade reverses all three changes (drop new columns, drop log
  table, drop user column);
* downgrade preflight refuses when ``user_decision`` is populated on
  any log row.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0054.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


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
    """Insert a minimal users row so FK constraints are satisfied."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:uid, 'free', '2026-05-29 10:00:00+00:00')"
            ),
            {"uid": user_id},
        )


def _seed_life_event(
    engine: sa.Engine,
    *,
    user_id: str = "ariel",
    category: str = "retirement_milestone",
    kind: str = "retirement_milestone:target_retire_year_change",
    target_date: str | None = "2030-05-01",
    amount_usd: float | None = None,
    recurring_years: int | None = None,
    description: str | None = None,
) -> int:
    """Insert a single legacy-schema life_events row and return its id.

    Caller must have already upgraded to 0042 (creates ``life_events``)
    but NOT yet to 0054 — this helper seeds the v0042 shape.
    """
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO life_events "
                "  (user_id, category, kind, target_date, amount_usd, "
                "   recurring_years, description) "
                "VALUES (:uid, :cat, :kind, :td, :amt, :ry, :desc) "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "cat": category,
                "kind": kind,
                "td": target_date,
                "amt": amount_usd,
                "ry": recurring_years,
                "desc": description,
            },
        )
        return int(result.scalar_one())


def test_0054_upgrade_adds_columns(alembic_cfg):
    """delta_kind + per-shape columns + fx_at_event are present."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {c["name"] for c in insp.get_columns("life_events")}
    expected_new = {
        "delta_kind",
        "monthly_delta_usd",
        "one_shot_amount_usd",
        "recurring_amount_usd",
        "recurring_period_years",
        "phase_start_date",
        "phase_end_date",
        "fx_at_event",
    }
    assert expected_new.issubset(cols), (
        f"missing cols: {expected_new - cols}"
    )

    # delta_kind is NOT NULL with server_default 'none' — every
    # pre-existing row should have picked up the default.
    delta_col = next(
        c for c in insp.get_columns("life_events")
        if c["name"] == "delta_kind"
    )
    assert delta_col["nullable"] is False


def test_0054_users_column_added(alembic_cfg):
    """users.life_events_migration_acknowledged_at exists + nullable."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {
        c["name"]: c for c in insp.get_columns("users")
    }
    assert "life_events_migration_acknowledged_at" in cols
    assert cols["life_events_migration_acknowledged_at"]["nullable"] is True


def test_0054_migration_log_table_created(alembic_cfg):
    """life_events_migration_log table + index + CASCADE FK."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "life_events_migration_log" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("life_events_migration_log")}
    expected = {
        "id",
        "original_life_event_id",
        "original_kind",
        "original_amount_usd",
        "target_delta_kind",
        "conversion_outcome",
        "user_decision",
        "notes",
        "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

    # Covering index for lookup by life_event id.
    idx_names = {
        i["name"]
        for i in insp.get_indexes("life_events_migration_log")
    }
    assert "ix_life_events_migration_log_event" in idx_names

    # Confirm FK is CASCADE.
    fks = insp.get_foreign_keys("life_events_migration_log")
    le_fk = next(
        f
        for f in fks
        if f["referred_table"] == "life_events"
    )
    assert le_fk["options"].get("ondelete", "").upper() == "CASCADE"


def test_0054_delta_kind_check_enforces_enum(alembic_cfg):
    """CHECK rejects values outside the five-value delta_kind enum."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # All five legal values accepted.
    for dk in (
        "one_shot",
        "recurring_every_n_years",
        "phase_change_start",
        "phase_change_end",
        "none",
    ):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO life_events "
                    "  (user_id, category, kind, delta_kind) "
                    "VALUES ('ariel', 'asset_event', 'home_purchase', :dk)"
                ),
                {"dk": dk},
            )

    # Unknown value rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO life_events "
                    "  (user_id, category, kind, delta_kind) "
                    "VALUES ('ariel', 'asset_event', 'home_purchase', "
                    "'bogus_value')"
                )
            )


def test_0054_recurring_period_check_enforces_positive(alembic_cfg):
    """recurring_period_years > 0 when present."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # NULL is fine.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO life_events "
                "  (user_id, category, kind, delta_kind, "
                "   recurring_period_years) "
                "VALUES ('ariel', 'asset_event', 'home_purchase', "
                "        'none', NULL)"
            )
        )

    # Positive is fine.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO life_events "
                "  (user_id, category, kind, delta_kind, "
                "   recurring_period_years) "
                "VALUES ('ariel', 'recurring_expense', 'new_car', "
                "        'recurring_every_n_years', 5)"
            )
        )

    # Zero rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO life_events "
                    "  (user_id, category, kind, delta_kind, "
                    "   recurring_period_years) "
                    "VALUES ('ariel', 'recurring_expense', 'new_car', "
                    "        'recurring_every_n_years', 0)"
                )
            )

    # Negative rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO life_events "
                    "  (user_id, category, kind, delta_kind, "
                    "   recurring_period_years) "
                    "VALUES ('ariel', 'recurring_expense', 'new_car', "
                    "        'recurring_every_n_years', -1)"
                )
            )


def test_0054_conversion_retirement_milestone(alembic_cfg):
    """retirement_milestone with target_date → delta_kind=none, preserved.

    The row stays in life_events with delta_kind='none', the target_date
    is serialized into the description column, and a migration_log row
    with conversion_outcome='preserved' is written.
    """
    cfg, sync_url = alembic_cfg
    # Upgrade to 0042 first so we can seed v0042-shaped rows.
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    event_id = _seed_life_event(
        engine,
        category="retirement_milestone",
        kind="retirement_milestone:target_retire_year_change",
        target_date="2030-05-01",
        description="my plan",
    )

    # Now apply 0054 — the data conversion runs.
    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT delta_kind, description FROM life_events "
                "WHERE id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert row[0] == "none"
    # Description carries the legacy target_date marker.
    assert "2030-05-01" in (row[1] or "")
    assert "retirement milestone" in (row[1] or "").lower()

    # Migration_log row created with the right outcome.
    with engine.connect() as conn:
        log_row = conn.execute(
            sa.text(
                "SELECT target_delta_kind, conversion_outcome, "
                "       original_kind "
                "FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert log_row[0] == "none"
    assert log_row[1] == "preserved"
    assert "retirement_milestone" in log_row[2]


def test_0054_conversion_other_asset_acquired(alembic_cfg):
    """other_asset_acquired with amount → delta_kind=one_shot, preserved.

    The row gets delta_kind='one_shot' AND one_shot_amount_usd populated
    from amount_usd; the migration_log row records 'preserved'.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    event_id = _seed_life_event(
        engine,
        category="asset_event",
        kind="asset_event:other_asset_acquired",
        target_date="2026-09-15",
        amount_usd=120000.0,
        description="RSU vest from grant ABC",
    )

    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT delta_kind, one_shot_amount_usd "
                "FROM life_events WHERE id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert row[0] == "one_shot"
    assert row[1] == pytest.approx(120000.0)

    with engine.connect() as conn:
        log_row = conn.execute(
            sa.text(
                "SELECT target_delta_kind, conversion_outcome, "
                "       original_amount_usd "
                "FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert log_row[0] == "one_shot"
    assert log_row[1] == "preserved"
    assert log_row[2] == pytest.approx(120000.0)


def test_0054_conversion_college_flagged_for_review(alembic_cfg):
    """expense_event:college → delta_kind=none, flagged_review."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    event_id = _seed_life_event(
        engine,
        category="expense_event",
        kind="expense_event:college",
        target_date="2030-09-01",
        amount_usd=40000.0,
    )

    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT delta_kind FROM life_events WHERE id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert row[0] == "none"

    with engine.connect() as conn:
        log_row = conn.execute(
            sa.text(
                "SELECT target_delta_kind, conversion_outcome "
                "FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert log_row[0] == "none"
    assert log_row[1] == "flagged_review"


def test_0054_conversion_fall_through_lossy(alembic_cfg):
    """Anything unrecognized → delta_kind=none, lossy_converted."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # A career_event with no amount + no target_date — falls through.
    event_id = _seed_life_event(
        engine,
        category="career_event",
        kind="career_event:promotion",
        target_date=None,
        amount_usd=None,
    )

    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT delta_kind FROM life_events WHERE id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert row[0] == "none"

    with engine.connect() as conn:
        log_row = conn.execute(
            sa.text(
                "SELECT target_delta_kind, conversion_outcome "
                "FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).one()
    assert log_row[0] == "none"
    assert log_row[1] == "lossy_converted"


def test_0054_users_with_no_legacy_rows_auto_acknowledged(alembic_cfg):
    """Per spec §1.5: users with no migration_log rows auto-acknowledged.

    A fresh DB where the user has no legacy life_events rows should
    have ``life_events_migration_acknowledged_at`` auto-populated by
    the migration so the banner never appears for them.
    """
    cfg, sync_url = alembic_cfg
    # Up to 0042 + seed a user.
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, user_id="bystander")

    # No life_events rows for bystander.

    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        ack_ts = conn.execute(
            sa.text(
                "SELECT life_events_migration_acknowledged_at "
                "FROM users WHERE id = 'bystander'"
            )
        ).scalar()
    assert ack_ts is not None, (
        "users with no life_events should have ack_ts auto-set"
    )


def test_0054_users_with_legacy_rows_left_unacknowledged(alembic_cfg):
    """Users WITH a converted life_event should have NULL ack_ts.

    The banner should appear for them.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, user_id="reviewer")

    # Seed a row that will land in the log.
    _seed_life_event(
        engine,
        user_id="reviewer",
        category="retirement_milestone",
        kind="retirement_milestone:target_retire_year_change",
        target_date="2030-01-01",
    )

    command.upgrade(cfg, "0054_life_events_cashflow_shape")

    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        ack_ts = conn.execute(
            sa.text(
                "SELECT life_events_migration_acknowledged_at "
                "FROM users WHERE id = 'reviewer'"
            )
        ).scalar()
    assert ack_ts is None, (
        "users with converted life_events should have NULL ack_ts "
        "until they click 'I've reviewed'"
    )


def test_0054_migration_log_cascade_on_event_delete(alembic_cfg):
    """Deleting a life_event also deletes its migration_log row (CASCADE)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    event_id = _seed_life_event(
        engine,
        category="asset_event",
        kind="asset_event:other_asset_acquired",
        target_date="2026-09-15",
        amount_usd=50000.0,
    )

    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine.dispose()
    engine = sa.create_engine(sync_url)

    # Confirm the log row exists.
    with engine.connect() as conn:
        cnt = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).scalar()
    assert cnt == 1

    # Delete the life_event — SQLite needs FK enforcement enabled
    # per-connection.
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(
            sa.text("DELETE FROM life_events WHERE id = :eid"),
            {"eid": event_id},
        )

    with engine.connect() as conn:
        cnt = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM life_events_migration_log "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        ).scalar()
    assert cnt == 0, "CASCADE should have removed the log row"


def test_0054_migration_log_unique_per_event(alembic_cfg):
    """Codex IMPORTANT — at most one log row per source life_event.id.

    The UNIQUE constraint is the DB-level idempotency floor — even
    though alembic never re-runs a migration on a clean cycle (each
    downgrade drops the log table outright before re-upgrade), this
    constraint prevents an operator footgun (e.g. a manual SQL re-run
    that bypasses alembic's version tracking) from silently inflating
    the log.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    event_id = _seed_life_event(
        engine,
        category="asset_event",
        kind="asset_event:other_asset_acquired",
        target_date="2026-09-15",
        amount_usd=120000.0,
    )
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine.dispose()
    engine = sa.create_engine(sync_url)

    # One log row already inserted by the migration.  A second INSERT
    # for the same source event_id must be refused.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO life_events_migration_log "
                    "  (original_life_event_id, original_kind, "
                    "   target_delta_kind, conversion_outcome, notes) "
                    "VALUES (:eid, 'asset_event:other_asset_acquired', "
                    "        'one_shot', 'preserved', 'dup')"
                ),
                {"eid": event_id},
            )


def test_0054_downgrade_reverses_cleanly(alembic_cfg):
    """Downgrade drops new columns + log table + user column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine = sa.create_engine(sync_url)

    # Pre-downgrade sanity: changes are present.
    insp = sa.inspect(engine)
    assert "life_events_migration_log" in insp.get_table_names()
    le_cols = {c["name"] for c in insp.get_columns("life_events")}
    assert "delta_kind" in le_cols
    assert "fx_at_event" in le_cols
    u_cols = {c["name"] for c in insp.get_columns("users")}
    assert "life_events_migration_acknowledged_at" in u_cols

    engine.dispose()
    command.downgrade(cfg, "0053_predictions_provenance")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "life_events_migration_log" not in insp.get_table_names()

    le_cols = {c["name"] for c in insp.get_columns("life_events")}
    for dropped in (
        "delta_kind",
        "monthly_delta_usd",
        "one_shot_amount_usd",
        "recurring_amount_usd",
        "recurring_period_years",
        "phase_start_date",
        "phase_end_date",
        "fx_at_event",
    ):
        assert dropped not in le_cols, (
            f"downgrade left {dropped!r} on life_events"
        )

    u_cols = {c["name"] for c in insp.get_columns("users")}
    assert "life_events_migration_acknowledged_at" not in u_cols


def test_0054_downgrade_preflight_blocks_user_decision(alembic_cfg):
    """Downgrade refuses when user_decision is populated on any log row.

    Symmetric mirror of the 0049 preflight pattern — if the user has
    acted on the conversion-assistant UI, dropping the log table would
    lose that UX history.  The operator can remediate (DELETE or set
    to NULL) and re-try.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0042_life_events")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    event_id = _seed_life_event(
        engine,
        category="expense_event",
        kind="expense_event:college",
        target_date="2030-09-01",
        amount_usd=40000.0,
    )
    command.upgrade(cfg, "0054_life_events_cashflow_shape")
    engine.dispose()
    engine = sa.create_engine(sync_url)

    # Simulate the user clicking through the conversion assistant.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE life_events_migration_log "
                "SET user_decision = 'upgraded_to_recurring' "
                "WHERE original_life_event_id = :eid"
            ),
            {"eid": event_id},
        )

    # Downgrade now refuses.
    engine.dispose()
    with pytest.raises(Exception) as excinfo:
        command.downgrade(cfg, "0053_predictions_provenance")
    assert "user_decision" in str(excinfo.value)

    # Tables / columns are still present — the downgrade halted.
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "life_events_migration_log" in insp.get_table_names()
    le_cols = {c["name"] for c in insp.get_columns("life_events")}
    assert "delta_kind" in le_cols

    # After remediation (NULL the user_decision), downgrade succeeds.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE life_events_migration_log "
                "SET user_decision = NULL"
            )
        )
    engine.dispose()
    command.downgrade(cfg, "0053_predictions_provenance")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "life_events_migration_log" not in insp.get_table_names()
