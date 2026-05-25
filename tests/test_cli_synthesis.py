"""Smoke tests for ``argosy synthesis ingest-trail`` (W1.C-v4)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from argosy.cli.main import app as root_app

runner = CliRunner()


def _seed_user_in_db(db_path) -> None:
    """Create the ``users`` table + the ariel row so AgentReport FKs satisfy.

    ``ingest-trail`` opens its OWN sync engine against ``settings.db_file``
    and calls ``Base.metadata.create_all`` — that gives us the schema —
    but it does NOT seed any rows. AgentReport.user_id is an FK to
    ``users.id``, so we have to add the row before ingest or the commit
    raises an IntegrityError and the helper returns 0.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        if session.get(User, "ariel") is None:
            session.add(User(id="ariel", plan="free"))
            session.commit()
    engine.dispose()


def _write_fake_trail(home, decision_run_id: int, *, n_rows: int = 3) -> None:
    """Write a fake JSONL trail at the expected path."""
    trail_dir = home / "logs" / "synthesis"
    trail_dir.mkdir(parents=True, exist_ok=True)
    trail_path = trail_dir / f"plan-synth-{decision_run_id}.jsonl"
    with trail_path.open("w", encoding="utf-8") as f:
        for i in range(n_rows):
            row = {
                "user_id": "ariel",
                "agent_role": f"stub-agent-{i}",
                "decision_id": f"plan-synth-{decision_run_id}",
                "intake_session_id": None,
                "prompt_hash": "h",
                "response_text": f"resp-{i}",
                "tokens_in": 1,
                "tokens_out": 1,
                "cost_usd": 0.0,
                "cache_input_tokens": 0,
                "cache_creation_tokens": 0,
                "thinking_tokens": 0,
                "citations_json": None,
                "sources_json": None,
                "run_correlation_id": f"corr-{i}",
                "system_prompt": "sys",
                "user_prompt": "usr",
                "model": "stub-model",
                "confidence": None,
            }
            f.write(json.dumps(row) + "\n")


def test_ingest_trail_cli_writes_rows(tmp_path, monkeypatch):
    """CLI smoke: ingest-trail reads a fake JSONL and inserts agent_reports."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings

    reload_settings()
    settings = get_settings()
    # Make sure the DB dir exists for the sync engine the CLI builds.
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    _seed_user_in_db(settings.db_file)
    _write_fake_trail(tmp_path, decision_run_id=42, n_rows=3)

    result = runner.invoke(root_app, ["synthesis", "ingest-trail", "42"])
    assert result.exit_code == 0, result.output
    assert "Ingested 3" in result.output
    assert "plan-synth-42" in result.output

    # Verify the rows actually landed in the DB.
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import AgentReport as AgentReportRow

    engine = sa.create_engine(
        f"sqlite:///{settings.db_file}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as s:
        rows = s.execute(sa.select(AgentReportRow)).scalars().all()
    engine.dispose()
    assert len(rows) == 3
    for r in rows:
        assert r.decision_id == "plan-synth-42"
        assert r.user_id == "ariel"

    reload_settings()


def test_ingest_trail_cli_missing_file_is_zero_exit(tmp_path, monkeypatch):
    """CLI smoke: when the JSONL doesn't exist, exit 0 with informational text."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    result = runner.invoke(root_app, ["synthesis", "ingest-trail", "999"])
    assert result.exit_code == 0, result.output
    assert "No rows ingested" in result.output
    assert "plan-synth-999" in result.output

    reload_settings()
