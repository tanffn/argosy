"""Proposals API tests via FastAPI TestClient."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from argosy.decisions.proposals import ProposalStatus
from argosy.state import db as db_mod
from argosy.state.models import (
    Approval,
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
    User,
)


async def _seed_user(uid: str = "ariel") -> None:
    async with db_mod.get_session() as session:
        session.add(User(id=uid))
        await session.commit()


async def _seed_proposal(
    *,
    user_id: str = "ariel",
    tier: str = "T2",
    status: str = "awaiting_human",
    ticker: str = "AAPL",
    cooling_until: datetime | None = None,
    shadow: int = 0,
    source: str = "manual",
) -> int:
    async with db_mod.get_session() as session:
        row = ProposalRow(
            user_id=user_id,
            ticker=ticker,
            action="buy",
            size_shares_or_currency=10,
            tier=tier,
            account_class="main",
            status=status,
            rationale_summary="test",
            expected_impact_json="{}",
            confidence="MEDIUM",
            cooling_off_until=cooling_until,
            shadow=shadow,
            source=source,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_list_proposals_excludes_shadow_by_default(client: AsyncClient) -> None:
    await _seed_user()
    visible = await _seed_proposal(ticker="AAPL")
    _hidden = await _seed_proposal(
        ticker="NVDA", shadow=1, source="decision_funnel"
    )
    # Default: shadow proposal is NOT surfaced to the client.
    r = await client.get("/api/proposals?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    ids = {row["id"] for row in body["rows"]}
    assert visible in ids
    assert _hidden not in ids
    assert body["total"] == 1
    # Opt-in (debug/trace) sees both.
    r2 = await client.get("/api/proposals?user_id=ariel&include_shadow=true")
    ids2 = {row["id"] for row in r2.json()["rows"]}
    assert {visible, _hidden} <= ids2


@pytest.mark.asyncio
async def test_list_proposals_empty(client: AsyncClient) -> None:
    await _seed_user()
    r = await client.get("/api/proposals?user_id=ariel")
    assert r.status_code == 200
    assert r.json() == {"rows": [], "total": 0}


@pytest.mark.asyncio
async def test_list_proposals_filters_by_status(client: AsyncClient) -> None:
    await _seed_user()
    pid_a = await _seed_proposal(status="awaiting_human")
    _pid_b = await _seed_proposal(status="approved")
    r = await client.get(
        "/api/proposals?user_id=ariel&status=awaiting_human"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["id"] == pid_a


@pytest.mark.asyncio
async def test_get_proposal_detail(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal()
    r = await client.get(f"/api/proposals/{pid}?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["proposal"]["id"] == pid
    assert body["history"] == []
    assert body["reasoning_trail"] == []


@pytest.mark.asyncio
async def test_approve_proposal_t2(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(status="awaiting_human", tier="T2")
    r = await client.post(
        f"/api/proposals/{pid}/approve",
        json={"user_id": "ariel", "channel": "dashboard"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "approved"
        # An approval row was created.
        from sqlalchemy import select
        approvals = (
            await session.execute(select(Approval).where(Approval.proposal_id == pid))
        ).scalars().all()
        assert len(approvals) == 1


@pytest.mark.asyncio
async def test_approve_t3_requires_second_factor(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(status="awaiting_human", tier="T3")
    r = await client.post(
        f"/api/proposals/{pid}/approve",
        json={"user_id": "ariel", "channel": "dashboard", "second_factor": False},
    )
    assert r.status_code == 400
    # With second factor, OK.
    r2 = await client.post(
        f"/api/proposals/{pid}/approve",
        json={"user_id": "ariel", "channel": "dashboard", "second_factor": True},
    )
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_reject_proposal(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(status="awaiting_human")
    r = await client.post(
        f"/api/proposals/{pid}/reject",
        json={"user_id": "ariel", "note": "no go"},
    )
    assert r.status_code == 200
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "rejected"


@pytest.mark.asyncio
async def test_reject_illegal_transition_returns_409(client: AsyncClient) -> None:
    """Rejecting an already-approved proposal is illegal."""
    await _seed_user()
    pid = await _seed_proposal(status="approved")
    r = await client.post(
        f"/api/proposals/{pid}/reject", json={"user_id": "ariel"}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_escalate_tier(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T1")
    r = await client.post(
        f"/api/proposals/{pid}/escalate-tier",
        json={"user_id": "ariel", "levels": 1},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.tier == "T2"


@pytest.mark.asyncio
async def test_get_proposal_404_for_other_user(client: AsyncClient) -> None:
    await _seed_user("ariel")
    pid = await _seed_proposal(user_id="ariel")
    r = await client.get(f"/api/proposals/{pid}?user_id=other")
    assert r.status_code == 404
