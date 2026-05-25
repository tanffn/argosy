"""`argosy synthesis ...` — plan-synthesis operator commands.

W1.C-v4 of the gap-closure roadmap. Plan synthesis writes a JSONL
forensic trail of every agent's ``AgentReport`` under
``${ARGOSY_HOME}/logs/synthesis/<decision_audit_token>.jsonl`` during
each phase (file IO, no SQLite lock contention with the orchestrator's
own session). At the END of synthesis the orchestrator ingests the
trail into ``agent_reports`` via its own session.

If synthesis crashes mid-flight, the auto-ingest path doesn't run — but
the JSONL is preserved on disk. ``argosy synthesis ingest-trail
<decision_run_id>`` lets the operator replay that ingest after the
fact: it reconstructs the audit token (``f"plan-synth-{decision_run_id}"``),
opens a fresh sync session against the configured DB, and feeds the
file to ``_ingest_synthesis_trail``.

Exit code is 0 on success (including the no-file case, which is
informational, not an error). Non-zero exits only when the ingest
machinery itself raises.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="synthesis",
    help="Plan-synthesis operator commands (forensic-trail replay, etc.).",
    no_args_is_help=True,
)


@app.command("ingest-trail")
def ingest_trail_cmd(
    decision_run_id: int = typer.Argument(
        ..., help="DecisionRun.id of the synthesis whose trail to ingest.",
    ),
) -> None:
    """Replay a synthesis JSONL trail into ``agent_reports``.

    Reads ``${ARGOSY_HOME}/logs/synthesis/plan-synth-<decision_run_id>.jsonl``
    and writes the rows to the ``agent_reports`` table. Idempotent only
    in the sense that the JSONL file isn't deleted afterwards — but
    re-running this command will insert duplicate rows. Use only when
    the auto-ingest at end of ``run_synthesis`` didn't fire (e.g.
    synthesis crashed before reaching the ingest call).
    """
    # Build the audit token in the exact format the orchestrator uses
    # (orchestrator.py: ``f"plan-synth-{decision_run_id}"``).
    audit_token = f"plan-synth-{decision_run_id}"

    # Mirror the sessionmaker pattern from
    # ``argosy/api/routes/advisor.py::_run_synthesis_background``: a
    # sync engine bound to the configured DB, with check_same_thread=False
    # so the session works regardless of which thread typer dispatches us
    # onto.
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.config import reload_settings, get_settings
    from argosy.orchestrator.flows.plan_synthesis import (
        _ingest_synthesis_trail,
    )
    from argosy.state.models import Base

    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    sync_url = f"sqlite:///{settings.db_file}"
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False},
    )
    # ``create_all`` is a no-op when the schema already exists; included
    # so this command works in a freshly-initialised dev environment.
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    with SessionLocal() as session:
        count = _ingest_synthesis_trail(session, audit_token)

    if count == 0:
        typer.echo(
            f"No rows ingested for {audit_token}: trail file missing or empty.",
        )
    else:
        typer.echo(f"Ingested {count} agent_reports row(s) for {audit_token}.")


__all__ = [
    "app",
    "ingest_trail_cmd",
]
