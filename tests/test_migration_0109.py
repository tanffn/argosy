"""0109 repairs accepted-plan lifecycle metadata and its stale alert chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from argosy.state.models import ActionProposal, MonitorFlag, PlanVersion, User


def test_0109_normalizes_current_plan_and_closes_only_sourced_alert(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0108_fill_telemetry_identity")
    engine = create_engine(settings.database_url.replace("+aiosqlite", ""))
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(User(id="ariel", plan="free"))
        session.add(
            PlanVersion(
                user_id="ariel",
                version_label="synth-2026-08-24-1949-fm-rejected",
                role="current",
                accepted_at=now,
                accepted_by_user_id="ariel",
            )
        )
        flag = MonitorFlag(
            user_id="ariel",
            kind="state_observer_plan_assumption_observation",
            severity="critical",
            payload='{"plan_version_label":"synth-2026-08-24-1949-fm-rejected"}',
            status="active",
        )
        session.add(flag)
        session.flush()
        proposal = ActionProposal(
            user_id="ariel",
            source_flag_id=flag.id,
            summary="historical rejection presented as current authority",
            rationale_md="stale",
            suggested_payload="{}",
            severity="warning",
            expires_at=now + timedelta(days=30),
            status="open",
            kind="note_only",
            execution_state="proposed",
        )
        session.add(proposal)
        inactive_flag = MonitorFlag(
            user_id="ariel",
            kind="state_observer_plan_assumption_observation",
            severity="warning",
            payload='{"rationale_md":"source observation already resolved"}',
            status="superseded",
        )
        session.add(inactive_flag)
        session.flush()
        inactive_source_proposal = ActionProposal(
            user_id="ariel",
            source_flag_id=inactive_flag.id,
            summary="source already inactive",
            rationale_md="stale",
            suggested_payload="{}",
            severity="warning",
            expires_at=now + timedelta(days=30),
            status="open",
            kind="note_only",
            execution_state="proposed",
        )
        session.add(inactive_source_proposal)
        session.commit()
        flag_id = flag.id
        proposal_id = proposal.id
        inactive_source_proposal_id = inactive_source_proposal.id

    command.upgrade(cfg, "head")
    with Session(engine) as session:
        current = session.execute(
            select(PlanVersion).where(PlanVersion.role == "current")
        ).scalar_one()
        assert current.version_label == "synth-2026-08-24-1949"
        assert session.get(MonitorFlag, flag_id).status == "superseded"
        repaired = session.get(ActionProposal, proposal_id)
        assert repaired.status == "superseded"
        assert "accepted-plan normalization" in repaired.decided_by_user_note
        inactive_repaired = session.get(ActionProposal, inactive_source_proposal_id)
        assert inactive_repaired.status == "superseded"
        assert (
            inactive_repaired.decided_by_user_note
            == "source monitor flag is no longer active"
        )
    engine.dispose()
    reload_settings()
