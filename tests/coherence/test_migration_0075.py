# tests/coherence/test_migration_0075.py
"""Migration 0075 — decision-funnel observability tables + proposals columns."""
import importlib.util
from pathlib import Path

import sqlalchemy as sa


def test_migration_0075_header_and_chains_from_head():
    path = Path("alembic/versions/0075_decision_funnel.py")
    assert path.exists()
    spec = importlib.util.spec_from_file_location("m0075", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0075_decision_funnel"
    assert mod.down_revision == "0074_payslip_facts"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_migration_0075_creates_funnel_tables_and_columns(alembic_engine_at_head):
    insp = sa.inspect(alembic_engine_at_head)
    tables = set(insp.get_table_names())
    assert {"funnel_runs", "decision_snapshots", "funnel_stage_rows"} <= tables

    # funnel_runs shape
    fr_cols = {c["name"] for c in insp.get_columns("funnel_runs")}
    assert {
        "id", "user_id", "trigger", "shadow", "status", "policy_version",
        "ips_version", "plan_version_id", "macro_read_json", "totals_json",
        "error_message", "started_at", "finished_at", "idempotency_key",
    } <= fr_cols

    # immutable per-decision snapshot shape
    ds_cols = {c["name"] for c in insp.get_columns("decision_snapshots")}
    assert {
        "id", "run_id", "user_id", "ticker", "dedup_key", "decision_json",
        "model_name", "prompt_template_hash", "temperature", "seed",
        "portfolio_snapshot_json", "market_snapshot_json", "policy_version",
        "policy_json", "why_not_act", "execution_drift_json", "human_action_state",
    } <= ds_cols

    # Replay-critical snapshot columns must be NOT NULL (codex hardening).
    ds_by_name = {c["name"]: c for c in insp.get_columns("decision_snapshots")}
    for required in (
        "run_id", "decision_json", "model_name", "prompt_template_hash",
        "portfolio_snapshot_json", "market_snapshot_json", "policy_version",
        "policy_json",
    ):
        assert ds_by_name[required]["nullable"] is False, f"{required} must be NOT NULL"

    # per-stage audit shape
    sr_cols = {c["name"] for c in insp.get_columns("funnel_stage_rows")}
    assert {
        "id", "run_id", "stage", "subject", "subject_type", "decision",
        "reason", "signal_or_rule", "inputs_json", "model", "snapshot_id",
        "proposal_id",
    } <= sr_cols

    # proposals lifecycle columns
    p_cols = {c["name"] for c in insp.get_columns("proposals")}
    assert {"source", "shadow", "expires_at", "funnel_run_id"} <= p_cols


def test_migration_0075_unique_keys_enforced(alembic_engine_at_head):
    eng = alembic_engine_at_head
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) VALUES ('ariel','free',CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO funnel_runs (user_id, started_at, idempotency_key) "
            "VALUES ('ariel', CURRENT_TIMESTAMP, 'funnel|ariel|2026-06-22')"
        ))
    # Duplicate idempotency_key must be rejected.
    import pytest
    with pytest.raises(sa.exc.IntegrityError):
        with eng.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO funnel_runs (user_id, started_at, idempotency_key) "
                "VALUES ('ariel', CURRENT_TIMESTAMP, 'funnel|ariel|2026-06-22')"
            ))
