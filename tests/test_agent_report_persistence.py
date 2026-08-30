"""Standalone-agent telemetry must reach the durable audit/cost ledger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

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
