"""Migration 0049 — state_snapshots + monitor_flags.dedup_key + CHECK relax.

Spec B (state-observer) commit #1. Smoke tests:

* upgrade to 0049 creates the ``state_snapshots`` table with all
  columns + the UNIQUE(user_id, snapshot_date) constraint;
* duplicate (user_id, snapshot_date) inserts raise IntegrityError;
* ``state_json`` and ``source_versions_json`` CHECKs reject non-JSON
  payloads (``json_valid``);
* ``monitor_flags.dedup_key`` column is present after upgrade;
* the partial unique index ``ix_monitor_flags_observer_dedup``
  enforces uniqueness on ``(user_id, dedup_key)`` only when the row
  is active (dedup_key NOT NULL AND acknowledged_at IS NULL);
* the relaxed ``monitor_flags.kind`` CHECK accepts the new
  ``state_observer_*`` values AND still accepts the legacy three;
* downgrade reverses all three changes cleanly (table dropped,
  column dropped, CHECK reverts to the legacy three-value enum).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0049.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh per-test DB rooted at ``tmp_path`` (mirrors 0048 fixture)."""
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


def test_0049_creates_state_snapshots_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "state_snapshots" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("state_snapshots")}
    expected = {
        "id",
        "user_id",
        "snapshot_date",
        "state_json",
        "source_versions_json",
        "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

    # The (user_id, snapshot_date) UNIQUE is enforced; declared as
    # constraint, so it shows up either in unique_constraints OR as a
    # unique index depending on dialect introspection.
    uqs = {u["name"] for u in insp.get_unique_constraints("state_snapshots")}
    idx_names = {i["name"] for i in insp.get_indexes("state_snapshots")}
    assert (
        "uq_state_snapshots_user_date" in uqs
        or "uq_state_snapshots_user_date" in idx_names
    )

    # Descending covering index for the "newest snapshot first" query.
    assert "ix_state_snapshots_user_date" in idx_names


def test_0049_unique_user_date_blocks_dup(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO state_snapshots "
                "(user_id, snapshot_date, state_json, "
                " source_versions_json) "
                "VALUES ('ariel', '2026-05-29', '{}', '{}')"
            )
        )

    # Same (user_id, snapshot_date) is rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO state_snapshots "
                    "(user_id, snapshot_date, state_json, "
                    " source_versions_json) "
                    "VALUES ('ariel', '2026-05-29', '{\"a\": 1}', "
                    "'{\"b\": 2}')"
                )
            )

    # Different date for same user OK.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO state_snapshots "
                "(user_id, snapshot_date, state_json, "
                " source_versions_json) "
                "VALUES ('ariel', '2026-05-30', '{}', '{}')"
            )
        )


def test_0049_state_json_check_rejects_non_json(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO state_snapshots "
                    "(user_id, snapshot_date, state_json, "
                    " source_versions_json) "
                    "VALUES ('ariel', '2026-05-29', "
                    "'this is not json', '{}')"
                )
            )


def test_0049_source_versions_check_rejects_non_json(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO state_snapshots "
                    "(user_id, snapshot_date, state_json, "
                    " source_versions_json) "
                    "VALUES ('ariel', '2026-05-29', '{}', "
                    "'also-not-json')"
                )
            )


def test_0049_adds_dedup_key_column(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {c["name"] for c in insp.get_columns("monitor_flags")}
    assert "dedup_key" in cols

    # Partial unique index is in place.
    idx_names = {i["name"] for i in insp.get_indexes("monitor_flags")}
    assert "ix_monitor_flags_observer_dedup" in idx_names

    # And the partial WHERE-clause is materialised in sqlite_master so a
    # future migration that drops + recreates the index doesn't lose the
    # predicate semantics.
    with engine.connect() as conn:
        partial_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_monitor_flags_observer_dedup'"
            )
        ).scalar()
    assert partial_sql is not None
    assert "dedup_key IS NOT NULL" in partial_sql
    assert "acknowledged_at IS NULL" in partial_sql
    # Predicate is STRICT — it does NOT carry an ``expires_at`` clause.
    # SQLite forbids non-deterministic functions
    # (``CURRENT_TIMESTAMP``) in partial-index predicates, so the
    # naive "is this expired?" check cannot live at the DB level. The
    # migration docstring documents the tombstone-then-insert dance
    # that the flag-writer uses to re-fire after expiry without
    # violating the strict uniqueness invariant.
    assert "expires_at" not in partial_sql


def test_0049_dedup_key_partial_unique_active_only(alembic_cfg):
    """The partial unique index only fires when the row is active.

    "Active" = dedup_key NOT NULL AND acknowledged_at IS NULL. So:

    * two NULL dedup_keys are fine (legacy rows from migration 0043);
    * two distinct dedup_keys for the same user are fine;
    * two identical dedup_keys for the same user where BOTH are active
      should raise;
    * two identical dedup_keys for the same user where one is
      acknowledged should be fine (the user already saw the first,
      dismissing it; the second is a fresh fire).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Two NULL dedup_keys — fine (legacy rows).
    with engine.begin() as conn:
        for i in range(2):
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at) "
                    "VALUES ('ariel', 'allocation_drift', 'warning', "
                    "'{}', '2026-05-29 10:0" + str(i)
                    + ":00+00:00')"
                )
            )

    # Two distinct dedup_keys for same user — fine.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, "
                " dedup_key) "
                "VALUES ('ariel', 'state_observer_fx_observation', "
                "'critical', '{}', '2026-05-29 11:00:00+00:00', "
                "'v1|state_observer|ariel|fx_observation|"
                "macro.fx_usd_nis_spot|large')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, "
                " dedup_key) "
                "VALUES ('ariel', 'state_observer_rates_observation', "
                "'info', '{}', '2026-05-29 11:01:00+00:00', "
                "'v1|state_observer|ariel|rates_observation|"
                "macro.fed_funds_rate_pct|small')"
            )
        )

    # Same active dedup_key for same user — rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at, "
                    " dedup_key) "
                    "VALUES ('ariel', "
                    "'state_observer_fx_observation', 'critical', "
                    "'{}', '2026-05-29 12:00:00+00:00', "
                    "'v1|state_observer|ariel|fx_observation|"
                    "macro.fx_usd_nis_spot|large')"
                )
            )

    # Acknowledge the first FX-observation row. Now the same dedup_key
    # should be insertable as a fresh fire.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE monitor_flags "
                "SET acknowledged_at = '2026-05-29 13:00:00+00:00' "
                "WHERE kind = 'state_observer_fx_observation'"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, "
                " dedup_key) "
                "VALUES ('ariel', "
                "'state_observer_fx_observation', 'critical', "
                "'{}', '2026-05-29 14:00:00+00:00', "
                "'v1|state_observer|ariel|fx_observation|"
                "macro.fx_usd_nis_spot|large')"
            )
        )


def test_0049_dedup_key_index_strict_under_expires_at(alembic_cfg):
    """The partial unique index is STRICT — it enforces uniqueness
    regardless of whether ``expires_at`` is populated. Two active
    (unacknowledged) rows with the same dedup_key conflict even when
    both have future ``expires_at`` values.

    Spec §4.3 branch (b) — "expired flag → write a new row" — is
    handled by the flag-writer via TOMBSTONING (UPDATE the expired
    peer's ``acknowledged_at`` to a timestamp BEFORE the INSERT). The
    tombstoned row falls out of the index scope, leaving room for the
    fresh insert.

    Codex Spec-B-1 review round 2 BLOCKER #1: the earlier predicate
    variant that excluded rows with ``expires_at IS NOT NULL`` was
    too weak — it allowed two unexpired-but-expiring rows with the
    same dedup_key to coexist, violating §4.3(a).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    dedup_key = (
        "v1|state_observer|ariel|fx_observation|"
        "macro.fx_usd_nis_spot|large"
    )

    # Insert a flag WITH future expires_at.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, "
                " expires_at, dedup_key) "
                "VALUES ('ariel', 'state_observer_fx_observation', "
                "'critical', '{}', '2026-05-29 10:00:00+00:00', "
                "'2026-06-30 10:00:00+00:00', :k)"
            ),
            {"k": dedup_key},
        )

    # Second active row with the same dedup_key — even with future
    # expires_at — IS BLOCKED. Strict uniqueness.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at, "
                    " expires_at, dedup_key) "
                    "VALUES ('ariel', "
                    "'state_observer_fx_observation', 'critical', "
                    "'{}', '2026-05-29 11:00:00+00:00', "
                    "'2026-06-30 10:00:00+00:00', :k)"
                ),
                {"k": dedup_key},
            )

    # Spec §4.3 branch (b) escape hatch — tombstone the expired peer
    # (writer simulation: UPDATE acknowledged_at to the moment the
    # writer noticed expiry), THEN insert. The tombstoned row exits
    # the index scope, and the fresh insert proceeds.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE monitor_flags "
                "SET acknowledged_at = '2026-06-30 11:00:00+00:00' "
                "WHERE dedup_key = :k AND acknowledged_at IS NULL"
            ),
            {"k": dedup_key},
        )
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, "
                " expires_at, dedup_key) "
                "VALUES ('ariel', 'state_observer_fx_observation', "
                "'critical', '{}', '2026-06-30 12:00:00+00:00', "
                "'2026-07-30 12:00:00+00:00', :k)"
            ),
            {"k": dedup_key},
        )


def test_0049_kind_check_accepts_state_observer_values(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Legacy three still accepted.
    for legacy in ("allocation_drift", "mc_regression", "macro_shift"):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at) "
                    "VALUES ('ariel', :k, 'info', '{}', "
                    "'2026-05-29 10:00:00+00:00')"
                ),
                {"k": legacy},
            )

    # All twelve state_observer_* values accepted.
    observer_kinds = [
        "state_observer_fx_observation",
        "state_observer_rates_observation",
        "state_observer_equity_observation",
        "state_observer_volatility_observation",
        "state_observer_allocation_observation",
        "state_observer_position_observation",
        "state_observer_concentration_observation",
        "state_observer_cash_observation",
        "state_observer_cashflow_observation",
        "state_observer_tax_observation",
        "state_observer_plan_assumption_observation",
        "state_observer_other_observation",
    ]
    for i, k in enumerate(observer_kinds):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at) "
                    "VALUES ('ariel', :k, 'info', '{}', :ts)"
                ),
                {
                    "k": k,
                    "ts": f"2026-05-29 11:{i:02d}:00+00:00",
                },
            )

    # An unknown observer subtype is still rejected — proves we kept
    # the enum explicit (not LIKE 'state_observer_%').
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at) "
                    "VALUES ('ariel', "
                    "'state_observer_typo_observation', 'info', "
                    "'{}', '2026-05-29 15:00:00+00:00')"
                )
            )


def test_0049_downgrade_reverses_cleanly(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)

    assert "state_snapshots" in sa.inspect(engine).get_table_names()
    cols = {c["name"] for c in sa.inspect(engine).get_columns("monitor_flags")}
    assert "dedup_key" in cols

    # Downgrade back to 0048.
    command.downgrade(cfg, "0048_job_runs")
    engine_after = sa.create_engine(sync_url)

    insp_after = sa.inspect(engine_after)
    assert "state_snapshots" not in insp_after.get_table_names()

    cols_after = {c["name"] for c in insp_after.get_columns("monitor_flags")}
    assert "dedup_key" not in cols_after

    idx_after = {i["name"] for i in insp_after.get_indexes("monitor_flags")}
    assert "ix_monitor_flags_observer_dedup" not in idx_after

    # Legacy CHECK back in force — new observer kinds now rejected, but
    # legacy three accepted.
    _seed_user(engine_after)
    with engine_after.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at) "
                "VALUES ('ariel', 'allocation_drift', 'info', '{}', "
                "'2026-05-29 10:00:00+00:00')"
            )
        )
    with pytest.raises(sa.exc.IntegrityError):
        with engine_after.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO monitor_flags "
                    "(user_id, kind, severity, payload, surfaced_at) "
                    "VALUES ('ariel', "
                    "'state_observer_fx_observation', 'info', '{}', "
                    "'2026-05-29 11:00:00+00:00')"
                )
            )


def test_0049_downgrade_preflight_rejects_observer_rows(alembic_cfg):
    """Downgrade must refuse if observer-era rows exist (codex
    Spec-B-1 BLOCKER #2). Without the guard the legacy CHECK rebuild
    would silently drop those rows.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Plant an observer-era row.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at) "
                "VALUES ('ariel', 'state_observer_fx_observation', "
                "'critical', '{}', '2026-05-29 10:00:00+00:00')"
            )
        )

    # Downgrade refuses.
    with pytest.raises(Exception) as excinfo:
        command.downgrade(cfg, "0048_job_runs")
    # The inner RuntimeError gets wrapped by alembic.command in some
    # paths; either way the message about the offending kind must
    # surface.
    assert "state_observer_fx_observation" in str(excinfo.value)

    # Observer row is still there — downgrade did NOT proceed.
    engine.dispose()
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "state_snapshots" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("monitor_flags")}
    assert "dedup_key" in cols

    # After remediation (DELETE the observer rows) the downgrade
    # succeeds.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM monitor_flags "
                "WHERE kind LIKE 'state_observer_%'"
            )
        )
    command.downgrade(cfg, "0048_job_runs")
    engine.dispose()
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "state_snapshots" not in insp.get_table_names()


def test_0049_preflight_rejects_unknown_legacy_kind(alembic_cfg):
    """If monitor_flags contains an out-of-band kind, upgrade halts.

    Without this preflight the SQLite batch-rebuild would silently
    drop those rows when copying to the new CHECK-constrained table
    — codex BLOCKER #6 of the spec.
    """
    cfg, sync_url = alembic_cfg
    # Bring schema up to 0048 only (monitor_flags exists with the
    # legacy three-value CHECK).
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Insert an out-of-band kind by temporarily disabling the CHECK via
    # PRAGMA writable_schema. SQLite-specific surgery — but this is
    # exactly the failure mode the preflight defends against (a hot-
    # fix script or a future-migration fixture leaving an unknown
    # value behind).
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA writable_schema = 1"))
        conn.execute(
            sa.text(
                "UPDATE sqlite_master SET sql = "
                "REPLACE(sql, "
                "\"kind IN ('allocation_drift', 'mc_regression', "
                "'macro_shift')\", "
                "\"kind IN ('allocation_drift', 'mc_regression', "
                "'macro_shift', 'rogue_kind')\") "
                "WHERE type='table' AND name='monitor_flags'"
            )
        )
        conn.execute(sa.text("PRAGMA writable_schema = 0"))

    # Reconnect so SQLite re-reads the patched schema.
    engine.dispose()
    engine = sa.create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at) "
                "VALUES ('ariel', 'rogue_kind', 'info', '{}', "
                "'2026-05-29 10:00:00+00:00')"
            )
        )

    # Now the preflight should refuse to relax the CHECK because
    # 'rogue_kind' wouldn't survive the rebuild.
    with pytest.raises(RuntimeError, match="rogue_kind"):
        command.upgrade(cfg, "0049_state_snapshots_and_monitor_flags")
