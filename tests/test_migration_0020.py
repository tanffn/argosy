"""Schema assertions after migration 0020 (decision_phases + agent_reports.phase_id)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0020_creates_decision_phases_table(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    assert "decision_phases" in insp.get_table_names()


def test_0020_decision_phases_has_expected_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_phases")
    expected = {
        "id", "decision_run_id", "user_id", "seq", "kind",
        "started_at", "finished_at", "participants_json", "verdict_json",
        "verdict_kind", "tldr_md", "bundle_dir", "created_at",
    }
    assert expected.issubset(set(cols.keys()))
    for required in (
        "decision_run_id", "user_id", "seq", "kind", "started_at",
        "participants_json", "created_at",
    ):
        assert cols[required]["nullable"] is False, f"{required} should be NOT NULL"


def test_0020_creates_run_seq_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    idx_names = {i["name"] for i in insp.get_indexes("decision_phases")}
    assert "ix_decision_phases_run_seq" in idx_names


def test_0020_creates_user_kind_started_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    idx_names = {i["name"] for i in insp.get_indexes("decision_phases")}
    assert "ix_decision_phases_user_kind_started" in idx_names


def test_0020_adds_phase_id_to_agent_reports(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "agent_reports")
    assert "phase_id" in cols
    assert cols["phase_id"]["nullable"] is True
