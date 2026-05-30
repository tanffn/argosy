"""Migration 0055 — action_proposals + notification_{subscriptions,
preferences,dispatch_ledger}.

Spec E (last-mile delivery layer) commit #1.  Smoke tests:

* upgrade to 0055 creates all four tables with their full column sets;
* CHECK enums enforce on each table (kind / severity / status /
  execution_state / channel / subscription status / dispatch status /
  enabled bool);
* ``json_valid(suggested_payload)`` CHECK on action_proposals rejects
  non-JSON;
* the partial UNIQUE on ``action_proposals(user_id, dedup_key)
  WHERE status='open' AND dedup_key IS NOT NULL`` enforces active-
  only uniqueness (tombstone pattern — closing the row releases the
  key);
* ``notification_subscriptions`` UNIQUE(user_id, channel, endpoint)
  blocks duplicate (user, channel, endpoint) triples;
* ``notification_preferences`` UNIQUE(user_id, channel, severity,
  kind) blocks duplicate cells in the enable matrix;
* ``notification_dispatch_ledger`` UNIQUE(notification_id, channel)
  enforces re-dispatch idempotency;
* FK cascade behaviour: user delete -> all four tables cascade;
  source_flag delete -> action_proposals.source_flag_id SET NULL;
  state_snapshot delete -> action_proposals.source_observation_id SET
  NULL;
  notification_subscription delete -> dispatch_ledger.subscription_id
  SET NULL;
* downgrade reverses all four tables cleanly (no orphaned indexes).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0055.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


# ---------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh per-test DB rooted at ``tmp_path`` (mirrors 0049 / 0050 / 0054)."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def _engine_with_fks(sync_url: str) -> sa.Engine:
    """Engine that enables SQLite foreign-key enforcement per-connection.

    SQLite ships with FK enforcement OFF by default; the cascade /
    SET NULL tests below need the ``PRAGMA foreign_keys = ON`` toggle
    on every new connection.  Use an event listener so each pool
    checkout re-applies the pragma.
    """
    engine = sa.create_engine(sync_url)

    @sa.event.listens_for(engine, "connect")
    def _enable_fks(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    return engine


def _seed_user(engine: sa.Engine, user_id: str = "ariel") -> None:
    """Insert a minimal ``users`` row so FK constraints are satisfied."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:uid, 'free', '2026-05-30 10:00:00+00:00')"
            ),
            {"uid": user_id},
        )


def _seed_monitor_flag(
    engine: sa.Engine, user_id: str = "ariel"
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at) "
                "VALUES (:uid, 'allocation_drift', 'warning', '{}', "
                "        '2026-05-30 11:00:00+00:00') "
                "RETURNING id"
            ),
            {"uid": user_id},
        )
        return int(result.scalar())


def _seed_state_snapshot(
    engine: sa.Engine,
    user_id: str = "ariel",
    snapshot_date: str = "2026-05-30",
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO state_snapshots "
                "(user_id, snapshot_date, state_json, "
                " source_versions_json) "
                "VALUES (:uid, :sd, '{}', '{}') "
                "RETURNING id"
            ),
            {"uid": user_id, "sd": snapshot_date},
        )
        return int(result.scalar())


def _insert_proposal(
    engine: sa.Engine,
    *,
    user_id: str = "ariel",
    kind: str = "allocate",
    severity: str = "warning",
    status: str = "open",
    execution_state: str = "proposed",
    dedup_key: str | None = "v1|action_proposal|ariel|observer_flag|allocate|abc",
    suggested_payload: str = '{"foo": "bar"}',
    expires_at: str = "2026-06-15 10:00:00+00:00",
    surfaced_at: str | None = None,
    source_flag_id: int | None = None,
    source_observation_id: int | None = None,
) -> int:
    cols = [
        "user_id",
        "kind",
        "severity",
        "status",
        "execution_state",
        "summary",
        "rationale_md",
        "suggested_payload",
        "expires_at",
    ]
    params: dict = {
        "user_id": user_id,
        "kind": kind,
        "severity": severity,
        "status": status,
        "execution_state": execution_state,
        "summary": "test summary",
        "rationale_md": "test rationale",
        "suggested_payload": suggested_payload,
        "expires_at": expires_at,
    }
    if dedup_key is not None:
        cols.append("dedup_key")
        params["dedup_key"] = dedup_key
    if source_flag_id is not None:
        cols.append("source_flag_id")
        params["source_flag_id"] = source_flag_id
    if source_observation_id is not None:
        cols.append("source_observation_id")
        params["source_observation_id"] = source_observation_id
    if surfaced_at is not None:
        cols.append("surfaced_at")
        params["surfaced_at"] = surfaced_at

    sql = (
        f"INSERT INTO action_proposals ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)}) "
        "RETURNING id"
    )
    with engine.begin() as conn:
        result = conn.execute(sa.text(sql), params)
        return int(result.scalar())


# ---------------------------------------------------------------------
# Structural tests — tables + columns + indexes
# ---------------------------------------------------------------------


def test_0055_creates_all_four_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    table_names = set(insp.get_table_names())
    assert {
        "action_proposals",
        "notification_subscriptions",
        "notification_preferences",
        "notification_dispatch_ledger",
    }.issubset(table_names)


def test_0055_action_proposals_columns(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {c["name"] for c in insp.get_columns("action_proposals")}
    expected = {
        "id",
        "user_id",
        "source_flag_id",
        "source_observation_id",
        "source_inferred_event_id",
        "summary",
        "rationale_md",
        "suggested_payload",
        "severity",
        "surfaced_at",
        "expires_at",
        "status",
        "decided_at",
        "decided_by_user_note",
        "kind",
        "dedup_key",
        "execution_state",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_0055_notification_subscriptions_columns(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {
        c["name"]
        for c in insp.get_columns("notification_subscriptions")
    }
    expected = {
        "id",
        "user_id",
        "channel",
        "endpoint",
        "p256dh",
        "auth",
        "subscribed_at",
        "last_seen_at",
        "status",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_0055_notification_preferences_columns(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {
        c["name"]
        for c in insp.get_columns("notification_preferences")
    }
    expected = {
        "id",
        "user_id",
        "channel",
        "severity",
        "kind",
        "enabled",
        "updated_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_0055_notification_dispatch_ledger_columns(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    cols = {
        c["name"]
        for c in insp.get_columns("notification_dispatch_ledger")
    }
    expected = {
        "id",
        "user_id",
        "notification_id",
        "channel",
        "subscription_id",
        "dispatched_at",
        "status",
        "error_message",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_0055_action_proposals_indexes_present(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    idx_names = {i["name"] for i in insp.get_indexes("action_proposals")}
    expected = {
        "ix_action_proposals_user_status_surfaced",
        "ix_action_proposals_user_kind_status",
        "ix_action_proposals_dedup_open",
        "ix_action_proposals_expires_open",
    }
    assert expected.issubset(idx_names), (
        f"missing indexes: {expected - idx_names}"
    )

    # Partial-WHERE clauses materialise in sqlite_master so a future
    # drop+recreate doesn't lose the predicate.
    with engine.connect() as conn:
        dedup_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_action_proposals_dedup_open'"
            )
        ).scalar()
        expires_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND "
                "name='ix_action_proposals_expires_open'"
            )
        ).scalar()

    assert dedup_sql is not None
    assert "status = 'open'" in dedup_sql
    assert "dedup_key IS NOT NULL" in dedup_sql
    assert expires_sql is not None
    assert "status = 'open'" in expires_sql


# ---------------------------------------------------------------------
# action_proposals — CHECK enums + JSON validity
# ---------------------------------------------------------------------


def test_0055_proposal_valid_row_inserts(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_proposal(engine)

    with engine.connect() as conn:
        n = conn.execute(
            sa.text("SELECT COUNT(*) FROM action_proposals")
        ).scalar()
    assert n == 1


def test_0055_proposal_kind_check_all_v1_values(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    v1_kinds = [
        "allocate",
        "repatriate_currency",
        "rebalance",
        "replan_full",
        "add_life_event_phase",
        "update_plan_assumption",
        "set_watchlist",
        "note_only",
    ]
    for i, k in enumerate(v1_kinds):
        _insert_proposal(engine, kind=k, dedup_key=f"k-{i}")

    # Unknown kind rejected.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(engine, kind="rugpull", dedup_key="k-bad")


def test_0055_proposal_severity_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    for i, s in enumerate(("info", "warning", "critical")):
        _insert_proposal(engine, severity=s, dedup_key=f"s-{i}")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(engine, severity="cosmic", dedup_key="s-bad")


def test_0055_proposal_status_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # The five statuses this commit ships (see migration docstring —
    # 'expired' / 'customized_accepted' land in commit #2's
    # housekeeping-loop migration).
    for i, s in enumerate(
        ("open", "accepted", "deferred", "rejected", "superseded")
    ):
        _insert_proposal(engine, status=s, dedup_key=f"st-{i}")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(engine, status="panicking", dedup_key="st-bad")


def test_0055_proposal_execution_state_check(alembic_cfg):
    """Capability-boundary enum (codex BLOCKER #1 / spec §2.2.1)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    for i, es in enumerate(
        ("proposed", "accepted_pending_user_action", "dismissed")
    ):
        _insert_proposal(engine, execution_state=es, dedup_key=f"es-{i}")

    # Reject any value that would imply auto-execution — the column
    # CANNOT express "this proposal is executable" per spec §2.2.1.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(
            engine, execution_state="executed", dedup_key="es-bad"
        )
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(
            engine, execution_state="auto_executable", dedup_key="es-bad2"
        )


def test_0055_proposal_execution_state_defaults_proposed(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Omit execution_state — server_default fires.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO action_proposals "
                "(user_id, kind, severity, summary, rationale_md, "
                " suggested_payload, expires_at) "
                "VALUES ('ariel', 'note_only', 'info', 's', 'r', "
                "        '{}', '2026-06-15 10:00:00+00:00')"
            )
        )
        es = conn.execute(
            sa.text(
                "SELECT execution_state FROM action_proposals"
            )
        ).scalar()
    assert es == "proposed"


def test_0055_proposal_status_defaults_open(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO action_proposals "
                "(user_id, kind, severity, summary, rationale_md, "
                " suggested_payload, expires_at) "
                "VALUES ('ariel', 'note_only', 'info', 's', 'r', "
                "        '{}', '2026-06-15 10:00:00+00:00')"
            )
        )
        st = conn.execute(
            sa.text("SELECT status FROM action_proposals")
        ).scalar()
    assert st == "open"


def test_0055_proposal_suggested_payload_json_valid(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Valid JSON accepted (object + array both valid per json_valid).
    _insert_proposal(
        engine,
        suggested_payload='{"amount_usd": 40000}',
        dedup_key="j-1",
    )
    _insert_proposal(
        engine,
        suggested_payload='[{"ticker": "NVDA"}]',
        dedup_key="j-2",
    )

    # Non-JSON rejected.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(
            engine,
            suggested_payload="this is not json",
            dedup_key="j-bad",
        )


def test_0055_proposal_expires_at_not_null(alembic_cfg):
    """spec §1.2 — expires_at is NOT NULL; writer computes the cushion."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO action_proposals "
                    "(user_id, kind, severity, summary, rationale_md, "
                    " suggested_payload) "
                    "VALUES ('ariel', 'note_only', 'info', 's', 'r', "
                    "        '{}')"
                )
            )


# ---------------------------------------------------------------------
# action_proposals — partial UNIQUE dedup index (tombstone pattern)
# ---------------------------------------------------------------------


def test_0055_dedup_partial_unique_active_only(alembic_cfg):
    """Partial UNIQUE only fires while status='open' AND dedup_key NOT NULL.

    * two NULL dedup_keys for the same user coexist (legacy / manual);
    * two distinct dedup_keys for the same user coexist;
    * two identical OPEN dedup_keys for the same user collide;
    * after the first row transitions out of 'open', the same dedup_key
      can re-fire (tombstone pattern).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Two NULL dedup_keys — fine (out of uniqueness scope).
    _insert_proposal(engine, dedup_key=None)
    _insert_proposal(engine, dedup_key=None)

    # Two distinct dedup_keys for same user — fine.
    _insert_proposal(engine, dedup_key="key-A")
    _insert_proposal(engine, dedup_key="key-B")

    # Same OPEN dedup_key for same user — rejected.
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(engine, dedup_key="key-A")

    # Transition the first 'key-A' row out of 'open' — dedup_key
    # released; re-firing the same key is now legal.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE action_proposals SET status = 'rejected', "
                "       decided_at = '2026-05-30 12:00:00+00:00' "
                "WHERE dedup_key = 'key-A'"
            )
        )

    _insert_proposal(engine, dedup_key="key-A")  # re-fires cleanly


def test_0055_dedup_partial_unique_scoped_to_user(alembic_cfg):
    """Two users may both have an open proposal with the same dedup_key."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, user_id="ariel")
    _seed_user(engine, user_id="noga")

    _insert_proposal(engine, user_id="ariel", dedup_key="shared-key")
    _insert_proposal(engine, user_id="noga", dedup_key="shared-key")


# ---------------------------------------------------------------------
# notification_subscriptions — UNIQUE + CHECKs
# ---------------------------------------------------------------------


def test_0055_subscription_channel_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    for i, ch in enumerate(("web_push", "email", "in_app")):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_subscriptions "
                    "(user_id, channel, endpoint) "
                    "VALUES ('ariel', :ch, :ep)"
                ),
                {"ch": ch, "ep": f"endpoint-{i}"},
            )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_subscriptions "
                    "(user_id, channel, endpoint) "
                    "VALUES ('ariel', 'sms', 'unused')"
                )
            )


def test_0055_subscription_status_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # active + gone accepted.
    for i, st in enumerate(("active", "gone")):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_subscriptions "
                    "(user_id, channel, endpoint, status) "
                    "VALUES ('ariel', 'web_push', :ep, :st)"
                ),
                {"ep": f"ep-{i}", "st": st},
            )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_subscriptions "
                    "(user_id, channel, endpoint, status) "
                    "VALUES ('ariel', 'web_push', 'ep-x', 'expired')"
                )
            )


def test_0055_subscription_unique_user_channel_endpoint(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_subscriptions "
                "(user_id, channel, endpoint, p256dh, auth) "
                "VALUES ('ariel', 'web_push', "
                "        'https://fcm.googleapis.com/abc', "
                "        'pkey', 'akey')"
            )
        )

    # Duplicate (user, channel, endpoint) — rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_subscriptions "
                    "(user_id, channel, endpoint) "
                    "VALUES ('ariel', 'web_push', "
                    "        'https://fcm.googleapis.com/abc')"
                )
            )

    # Same endpoint but different channel — fine.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_subscriptions "
                "(user_id, channel, endpoint) "
                "VALUES ('ariel', 'email', "
                "        'https://fcm.googleapis.com/abc')"
            )
        )


def test_0055_subscription_p256dh_auth_nullable_for_non_web_push(
    alembic_cfg,
):
    """p256dh + auth are nullable — validate-via-app, not DB constraint.

    Spec §3.4 / codex review: too many edge cases (subscription
    re-issuance, vendor URL evolution) to pin at DDL.  An email
    subscription with NULL crypto material must insert cleanly.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_subscriptions "
                "(user_id, channel, endpoint) "
                "VALUES ('ariel', 'email', 'arielj@nvidia.com')"
            )
        )
        n = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM notification_subscriptions "
                "WHERE channel = 'email' AND p256dh IS NULL "
                "  AND auth IS NULL"
            )
        ).scalar()
    assert n == 1


# ---------------------------------------------------------------------
# notification_preferences — UNIQUE + CHECKs
# ---------------------------------------------------------------------


def test_0055_preferences_enabled_bool_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    for i, en in enumerate((0, 1)):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_preferences "
                    "(user_id, channel, severity, kind, enabled) "
                    "VALUES ('ariel', 'in_app', 'info', :k, :en)"
                ),
                {"k": f"kind-{i}", "en": en},
            )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_preferences "
                    "(user_id, channel, severity, kind, enabled) "
                    "VALUES ('ariel', 'in_app', 'info', 'kbad', 2)"
                )
            )


def test_0055_preferences_unique_cell(alembic_cfg):
    """UNIQUE(user_id, channel, severity, kind) — one row per cell."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_preferences "
                "(user_id, channel, severity, kind, enabled) "
                "VALUES ('ariel', 'web_push', 'critical', "
                "        'state_observer_fx_observation', 1)"
            )
        )

    # Same cell — rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_preferences "
                    "(user_id, channel, severity, kind, enabled) "
                    "VALUES ('ariel', 'web_push', 'critical', "
                    "        'state_observer_fx_observation', 0)"
                )
            )

    # Different kind, same other axes — fine (a different cell).
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_preferences "
                "(user_id, channel, severity, kind, enabled) "
                "VALUES ('ariel', 'web_push', 'critical', "
                "        'allocation_drift', 1)"
            )
        )


def test_0055_preferences_kind_is_permissive(alembic_cfg):
    """``kind`` is plain TEXT — no CHECK enum; writer-contract validates."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    # Arbitrary strings accepted — proves no DB-level enum prevents
    # forward extension when Spec B / Spec E add new flag / proposal
    # kinds.
    for i, k in enumerate(
        (
            "state_observer_fx_observation",
            "state_observer_volatility_observation",
            "allocation_drift",
            "action_proposal_repatriate_currency",
            "totally_new_future_kind",
        )
    ):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_preferences "
                    "(user_id, channel, severity, kind, enabled) "
                    "VALUES ('ariel', 'in_app', 'warning', :k, 1)"
                ),
                {"k": k},
            )


# ---------------------------------------------------------------------
# notification_dispatch_ledger — UNIQUE + CHECKs
# ---------------------------------------------------------------------


def test_0055_dispatch_status_check(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)

    for i, st in enumerate(("sent", "failed", "skipped")):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_dispatch_ledger "
                    "(user_id, notification_id, channel, status) "
                    "VALUES ('ariel', :nid, 'web_push', :st)"
                ),
                {"nid": f"notif-{i}", "st": st},
            )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_dispatch_ledger "
                    "(user_id, notification_id, channel, status) "
                    "VALUES ('ariel', 'notif-x', 'web_push', "
                    "        'delivered_kinda')"
                )
            )


def test_0055_dispatch_unique_user_notification_channel(alembic_cfg):
    """UNIQUE(user_id, notification_id, channel) — re-dispatch idempotency.

    Codex BLOCKER (Spec E #1 review) integration: the uniqueness scope
    INCLUDES user_id because the writer's deterministic notification_id
    is NOT user-namespaced (just ``kind|ref_id|channel|severity|day``);
    without the user scope two tenants would collide on identical ids
    and one tenant's audit row would be lost.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, user_id="ariel")
    _seed_user(engine, user_id="noga")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_dispatch_ledger "
                "(user_id, notification_id, channel, status) "
                "VALUES ('ariel', "
                "        'state_observer_fx|42|web_push|critical|2026-05-30', "
                "        'web_push', 'sent')"
            )
        )

    # Same (user, notification_id, channel) — rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO notification_dispatch_ledger "
                    "(user_id, notification_id, channel, status) "
                    "VALUES ('ariel', "
                    "        'state_observer_fx|42|web_push|critical|2026-05-30', "
                    "        'web_push', 'failed')"
                )
            )

    # Same notification_id but different channel — fine (a fan-out
    # of one logical notification across channels).
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_dispatch_ledger "
                "(user_id, notification_id, channel, status) "
                "VALUES ('ariel', "
                "        'state_observer_fx|42|web_push|critical|2026-05-30', "
                "        'email', 'sent')"
            )
        )

    # Different user, same (notification_id, channel) — fine (multi-
    # tenant correctness; codex BLOCKER fix).
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_dispatch_ledger "
                "(user_id, notification_id, channel, status) "
                "VALUES ('noga', "
                "        'state_observer_fx|42|web_push|critical|2026-05-30', "
                "        'web_push', 'sent')"
            )
        )


# ---------------------------------------------------------------------
# FK cascade / SET NULL behaviour
# ---------------------------------------------------------------------


def test_0055_user_delete_cascades_all_four_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = _engine_with_fks(sync_url)
    _seed_user(engine)

    # Seed one row in each of the four tables for ariel.
    _insert_proposal(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_subscriptions "
                "(user_id, channel, endpoint) "
                "VALUES ('ariel', 'email', 'a@b.c')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO notification_preferences "
                "(user_id, channel, severity, kind, enabled) "
                "VALUES ('ariel', 'in_app', 'info', 'kx', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO notification_dispatch_ledger "
                "(user_id, notification_id, channel, status) "
                "VALUES ('ariel', 'nid-x', 'in_app', 'sent')"
            )
        )

    # Delete the user — all four tables should cascade.
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM users WHERE id = 'ariel'")
        )

    with engine.connect() as conn:
        for table in (
            "action_proposals",
            "notification_subscriptions",
            "notification_preferences",
            "notification_dispatch_ledger",
        ):
            n = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM {table}")
            ).scalar()
            assert n == 0, (
                f"{table} did NOT cascade on user delete (rows={n})"
            )


def test_0055_source_flag_delete_sets_null(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = _engine_with_fks(sync_url)
    _seed_user(engine)

    flag_id = _seed_monitor_flag(engine)
    proposal_id = _insert_proposal(engine, source_flag_id=flag_id)

    # Confirm seed.
    with engine.connect() as conn:
        sfi = conn.execute(
            sa.text(
                "SELECT source_flag_id FROM action_proposals "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        ).scalar()
    assert sfi == flag_id

    # Delete the flag — the proposal must survive with source_flag_id=NULL.
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM monitor_flags WHERE id = :fid"),
            {"fid": flag_id},
        )

    with engine.connect() as conn:
        sfi_after = conn.execute(
            sa.text(
                "SELECT source_flag_id FROM action_proposals "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        ).scalar()
        n = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM action_proposals "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        ).scalar()

    assert sfi_after is None
    assert n == 1, "proposal must NOT cascade-delete with the flag"


def test_0055_source_observation_delete_sets_null(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = _engine_with_fks(sync_url)
    _seed_user(engine)

    snap_id = _seed_state_snapshot(engine)
    proposal_id = _insert_proposal(
        engine, source_observation_id=snap_id
    )

    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM state_snapshots WHERE id = :sid"),
            {"sid": snap_id},
        )

    with engine.connect() as conn:
        soi_after = conn.execute(
            sa.text(
                "SELECT source_observation_id FROM action_proposals "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        ).scalar()
    assert soi_after is None


def test_0055_subscription_delete_sets_null_on_ledger(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = _engine_with_fks(sync_url)
    _seed_user(engine)

    with engine.begin() as conn:
        sub_id = conn.execute(
            sa.text(
                "INSERT INTO notification_subscriptions "
                "(user_id, channel, endpoint) "
                "VALUES ('ariel', 'web_push', 'https://x/y') "
                "RETURNING id"
            )
        ).scalar()
        ledger_id = conn.execute(
            sa.text(
                "INSERT INTO notification_dispatch_ledger "
                "(user_id, notification_id, channel, "
                " subscription_id, status) "
                "VALUES ('ariel', 'nid-xyz', 'web_push', "
                "        :sid, 'sent') RETURNING id"
            ),
            {"sid": int(sub_id)},
        ).scalar()

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM notification_subscriptions "
                "WHERE id = :sid"
            ),
            {"sid": int(sub_id)},
        )

    with engine.connect() as conn:
        sid_after = conn.execute(
            sa.text(
                "SELECT subscription_id FROM "
                "notification_dispatch_ledger WHERE id = :lid"
            ),
            {"lid": int(ledger_id)},
        ).scalar()
        n = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM notification_dispatch_ledger "
                "WHERE id = :lid"
            ),
            {"lid": int(ledger_id)},
        ).scalar()

    assert sid_after is None
    assert n == 1, "ledger row must NOT cascade-delete with the sub"


# ---------------------------------------------------------------------
# Downgrade — reverses all four tables cleanly
# ---------------------------------------------------------------------


def test_0055_downgrade_drops_all_four_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0055_action_proposals_and_notifications")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert {
        "action_proposals",
        "notification_subscriptions",
        "notification_preferences",
        "notification_dispatch_ledger",
    }.issubset(insp.get_table_names())

    command.downgrade(cfg, "0054_life_events_cashflow_shape")

    engine_after = sa.create_engine(sync_url)
    insp_after = sa.inspect(engine_after)
    remaining = set(insp_after.get_table_names())
    for t in (
        "action_proposals",
        "notification_subscriptions",
        "notification_preferences",
        "notification_dispatch_ledger",
    ):
        assert t not in remaining, f"{t} survived downgrade"

    # No orphaned indexes left in sqlite_master.
    with engine_after.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND ("
                "  name LIKE 'ix_action_proposals_%' OR "
                "  name LIKE 'ix_notification_%' OR "
                "  name LIKE 'uq_notification_%'"
                ")"
            )
        ).fetchall()
    assert rows == [], f"orphaned indexes after downgrade: {rows}"
