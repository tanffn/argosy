"""Standalone-agent telemetry must reach the durable audit/cost ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.base import AgentReport
from argosy.services.agent_report_persistence import stage_agent_report
from argosy.state.models import (
    AgentReport as AgentReportRow,
)
from argosy.state.models import AgentReportBlob, Base, User


class _Output(BaseModel):
    ticker: str
    verdict: str


@pytest.mark.real_seam
def test_stage_agent_report_preserves_cost_prompts_and_structured_output() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    created = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    report = AgentReport(
        agent_role="quick_estimator",
        user_id="ariel",
        model="claude-sonnet-4-6",
        response_text='{"ticker":"IONQ","verdict":"BUY"}',
        tokens_in=3,
        tokens_out=120,
        cost_usd=0.042,
        prompt_hash="abc",
        confidence=None,
        output=_Output(ticker="IONQ", verdict="BUY"),
        cache_input_tokens=1000,
        cache_creation_tokens=200,
        thinking_tokens=0,
        sources_json='["radar"]',
        run_correlation_id="00000000-0000-0000-0000-000000000001",
        system_prompt="system",
        user_prompt="user",
        created_at=created,
    )
    with factory() as session:
        session.add(User(id="ariel", plan="free"))
        stage_agent_report(
            session,
            report,
            decision_id="discovery:IONQ",
        )
        session.commit()

        row = session.query(AgentReportRow).one()
        blob = session.query(AgentReportBlob).one()
        assert row.agent_role == "quick_estimator"
        assert row.decision_id == "discovery:IONQ"
        assert float(row.cost_usd) == 0.042
        assert row.cache_input_tokens == 1000
        assert row.system_prompt == "system"
        assert row.user_prompt == "user"
        assert blob.key == "output_json"
        assert '"IONQ"' in blob.value
    engine.dispose()


def test_sync_report_routes_to_explicit_tenant_not_control_database(tmp_path, monkeypatch):
    from argosy.services.agent_report_persistence import persist_agent_report_sync
    from argosy.state import db as db_mod
    from argosy.tenancy import database as tenant_db
    path = tmp_path / "tenant.db"
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel", plan="free"))
        session.commit()
    seen = []
    def tenant_engine(user_id):
        seen.append(user_id)
        return SimpleNamespace(url=f"sqlite+aiosqlite:///{path.as_posix()}")
    def no_control_engine():
        raise AssertionError("Control database must not receive a tenant report")
    monkeypatch.setattr(db_mod, "_per_tenant_mode", lambda: True)
    monkeypatch.setattr(db_mod, "get_engine", no_control_engine)
    monkeypatch.setattr(tenant_db, "get_tenant_engine", tenant_engine)
    report = AgentReport(agent_role="quick_estimator", user_id="ariel", model="test",
        response_text='{"ticker":"XYZ","verdict":"WATCH"}', tokens_in=1, tokens_out=1,
        cost_usd=0.01, prompt_hash="tenant-test", confidence=None, output=_Output(ticker="XYZ", verdict="WATCH"))
    report_id = persist_agent_report_sync(report, decision_id="discovery:XYZ")
    with Session(engine) as session:
        assert session.get(AgentReportRow, report_id).user_id == "ariel"
    assert seen == ["ariel"]
    engine.dispose()
