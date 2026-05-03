"""Intake API route tests."""

from __future__ import annotations

import io
import json

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.intake import IntakeAgent
from argosy.agents.intake_extractor import IntakeExtractorAgent
from argosy.api.routes.intake import (
    reset_intake_agent_factory,
    reset_intake_extractor_factory,
    set_intake_agent_factory,
    set_intake_extractor_factory,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    PlanVersion,
    User,
    UserContext,
)


_CANNED = {
    "stage": "stage_1",
    "question_for_user": "What is your country of tax residence?",
    "context_updates": [],
    "stage_complete": False,
    "next_stage": None,
    "confidence": "MEDIUM",
    "cited_sources": [],
    "notes_for_orchestrator": "",
}


def _factory(user_id: str):
    class _M(IntakeAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(_CANNED),
                tokens_in=80,
                tokens_out=120,
                model=self.model,
            )
    return _M(user_id=user_id)


@pytest.mark.asyncio
async def test_intake_status_default_stage_1(engine: None, client: AsyncClient) -> None:
    res = await client.get("/api/intake/status", params={"user_id": "ariel"})
    assert res.status_code == 200
    body = res.json()
    assert body["current_stage"] == "stage_1"


@pytest.mark.asyncio
async def test_intake_turn_returns_question(engine: None, client: AsyncClient) -> None:
    set_intake_agent_factory(_factory)
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/intake/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["stage"] == "stage_1"
        assert "tax residence" in body["question_for_user"]
        assert body["stage_complete"] is False
    finally:
        reset_intake_agent_factory()


# ----------------------------------------------------------------------
# /upload
# ----------------------------------------------------------------------


_UPLOAD_FIXTURE_MD = """# Sample Wealth Plan v1.0

## Identity
- Tax residency: Israel
- Family: spouse + two children

## Goals
- Retirement: 2032
- Annual income target: 600k NIS
"""


_CANNED_EXTRACTION = {
    "tax_residency": {
        "value": "israel",
        "source_excerpt": "Tax residency: Israel",
        "confidence": "HIGH",
    },
    "citizenship": None,
    "family": {
        "value": "spouse plus two children",
        "source_excerpt": "spouse + two children",
        "confidence": "HIGH",
    },
    "employment": None,
    "retirement_target_year": {
        "value": "2032",
        "source_excerpt": "Retirement: 2032",
        "confidence": "HIGH",
    },
    "target_annual_income": {
        "value": "600k NIS",
        "source_excerpt": "Annual income target: 600k NIS",
        "confidence": "MEDIUM",
    },
    "near_term_spending": None,
    "primary_brokers": None,
    "bank_diversification_preference": None,
    "risk_tolerance": None,
    "constraints_other": [],
    "identity_yaml": "tax_residency: israel\nfamily: spouse plus two children\n",
    "goals_yaml": "retirement_target_year: 2032\ntarget_annual_income: 600k NIS\n",
    "constraints_yaml": "",
    "fields_extracted": [
        "tax_residency",
        "family",
        "retirement_target_year",
        "target_annual_income",
    ],
    "fields_missing": [
        "citizenship",
        "employment",
        "near_term_spending",
        "primary_brokers",
        "risk_tolerance",
    ],
    "confidence": "HIGH",
    "notes": "Plan v1.0 - clear on identity & top goals.",
}


def _extractor_factory(canned: dict):
    def _make(user_id: str):
        class _E(IntakeExtractorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=1500,
                    tokens_out=800,
                    model=self.model,
                )
        return _E(user_id=user_id)

    return _make


@pytest.mark.asyncio
async def test_upload_creates_plan_version_and_merges_context(
    engine: None, client: AsyncClient
) -> None:
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        # Pre-create a user_context with one user-typed identity field that
        # must NOT be overwritten by the extractor.
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(
                UserContext(
                    user_id="ariel",
                    identity_yaml="tax_residency: united_states\n",
                    current_stage="stage_1",
                )
            )
            await session.commit()

        files = {
            "file": (
                "plan.md",
                io.BytesIO(_UPLOAD_FIXTURE_MD.encode("utf-8")),
                "text/markdown",
            )
        }
        data = {"user_id": "ariel"}
        res = await client.post("/api/intake/upload", data=data, files=files)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["plan_version_id"] >= 1
        assert body["intake_session_id"]
        assert "tax_residency" in body["fields_extracted"]
        assert "citizenship" in body["fields_missing"]
        assert body["confidence"] == "HIGH"
        assert "extracted" in body["summary_for_user"].lower()

        # plan_versions row created with the raw markdown.
        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(PlanVersion.user_id == "ariel")
                )
            ).scalar_one()
            assert pv.raw_markdown == _UPLOAD_FIXTURE_MD
            assert pv.source_path == "plan.md"
            assert pv.version_label.startswith("from_intake_upload_")

            # user_context: identity_yaml was merged additively. The pre-existing
            # tax_residency=united_states must STILL be there (existing wins),
            # while the new family field must have been added.
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "ariel")
                )
            ).scalar_one()
            identity = yaml.safe_load(ctx.identity_yaml)
            assert identity["tax_residency"] == "united_states"  # preserved
            assert identity["family"].startswith("spouse")  # added
            goals = yaml.safe_load(ctx.goals_yaml)
            assert goals["retirement_target_year"] == 2032
            assert ctx.intake_session_id == body["intake_session_id"]

            # agent_reports row stamped with the same intake_session_id.
            ar_rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "intake_extractor",
                    )
                )
            ).scalars().all()
            assert len(ar_rows) == 1
            assert ar_rows[0].intake_session_id == body["intake_session_id"]
            assert ar_rows[0].decision_id is None
    finally:
        reset_intake_extractor_factory()


@pytest.mark.asyncio
async def test_upload_rejects_non_markdown(
    engine: None, client: AsyncClient
) -> None:
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": (
                "plan.pdf",
                io.BytesIO(b"%PDF-1.4 not really markdown"),
                "application/pdf",
            )
        }
        res = await client.post(
            "/api/intake/upload",
            data={"user_id": "ariel"},
            files=files,
        )
        assert res.status_code == 400
        assert "Markdown" in res.json()["detail"]
    finally:
        reset_intake_extractor_factory()


@pytest.mark.asyncio
async def test_upload_creates_user_if_missing(
    engine: None, client: AsyncClient
) -> None:
    """Uploading is the first thing a brand-new user might do — the route
    must auto-create the user + user_context rows."""
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": (
                "plan.md",
                io.BytesIO(_UPLOAD_FIXTURE_MD.encode("utf-8")),
                "text/markdown",
            )
        }
        res = await client.post(
            "/api/intake/upload",
            data={"user_id": "newbie"},
            files=files,
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            user = (
                await session.execute(select(User).where(User.id == "newbie"))
            ).scalar_one_or_none()
            assert user is not None
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "newbie")
                )
            ).scalar_one()
            assert "tax_residency" in ctx.identity_yaml
    finally:
        reset_intake_extractor_factory()
