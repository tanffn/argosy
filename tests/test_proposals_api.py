"""Proposals API tests via FastAPI TestClient."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from argosy.decisions.proposals import ProposalStatus
from argosy.state import db as db_mod
from argosy.state.models import (
    Approval,
    ProposalHistory,
    User,
)
from argosy.state.models import (
    Proposal as ProposalRow,
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


# ----------------------------------------------------------------------
# Inbox "Dismiss" on a beta funnel proposal → the reject transition.
# Regression: the SOFI card's Dismiss was a silent client no-op; the
# backend path it now maps to must work for shadow funnel rows.
# ----------------------------------------------------------------------


async def _seed_snapshot(*, proposal_id: int, user_id: str = "ariel") -> int:
    from datetime import datetime

    from argosy.state.models import DecisionSnapshot, FunnelRun

    async with db_mod.get_session() as session:
        run = FunnelRun(
            user_id=user_id,
            started_at=datetime.now(UTC),
            idempotency_key=f"test-run-{proposal_id}",
        )
        session.add(run)
        await session.flush()
        snap = DecisionSnapshot(
            run_id=run.id,
            user_id=user_id,
            ticker="SOFI",
            dedup_key=f"test-dedup-{proposal_id}",
            decision_json="{}",
            model_name="test-model",
            prompt_template_hash="hash",
            portfolio_snapshot_json="{}",
            market_snapshot_json="{}",
            policy_version="v1",
            policy_json="{}",
            proposal_id=proposal_id,
        )
        session.add(snap)
        await session.commit()
        return snap.id


@pytest.mark.asyncio
async def test_reject_shadow_funnel_proposal_and_grade_snapshot(
    client: AsyncClient,
) -> None:
    """Dismiss (reject) works on a shadow decision_funnel row AND grades the
    immutable decision snapshot 'rejected' so the calibrating funnel learns."""
    from argosy.state.models import DecisionSnapshot

    await _seed_user()
    pid = await _seed_proposal(
        ticker="SOFI", shadow=1, source="decision_funnel", status="awaiting_human"
    )
    sid = await _seed_snapshot(proposal_id=pid)
    r = await client.post(
        f"/api/proposals/{pid}/reject",
        json={"user_id": "ariel", "note": "Dismissed from inbox"},
    )
    assert r.status_code == 200
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "rejected"
        snap = await session.get(DecisionSnapshot, sid)
        assert snap.human_action_state == "rejected"


@pytest.mark.asyncio
async def test_approve_grades_snapshot_accepted(client: AsyncClient) -> None:
    from argosy.state.models import DecisionSnapshot

    await _seed_user()
    pid = await _seed_proposal(status="awaiting_human", tier="T2")
    sid = await _seed_snapshot(proposal_id=pid)
    r = await client.post(
        f"/api/proposals/{pid}/approve",
        json={"user_id": "ariel", "channel": "dashboard"},
    )
    assert r.status_code == 200
    async with db_mod.get_session() as session:
        snap = await session.get(DecisionSnapshot, sid)
        assert snap.human_action_state == "accepted"


# ----------------------------------------------------------------------
# Defer — the proposals-table twin of defer_action_proposal.
# ----------------------------------------------------------------------


async def _seed_proposal_full(**kw) -> int:
    """_seed_proposal + extra column overrides (expires_at, account_class)."""
    expires_at = kw.pop("expires_at", None)
    account_class = kw.pop("account_class", "main")
    pid = await _seed_proposal(**kw)
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        row.expires_at = expires_at
        row.account_class = account_class
        await session.commit()
    return pid


@pytest.mark.asyncio
async def test_defer_proposal_parks_in_cooling_with_history(client: AsyncClient) -> None:
    from sqlalchemy import select

    await _seed_user()
    # Short-dated expiry (the RKT shape): defer must push it past resurface.
    pid = await _seed_proposal_full(
        ticker="RKT",
        status="awaiting_human",
        expires_at=datetime.now(UTC) + timedelta(days=2),
    )
    r = await client.post(
        f"/api/proposals/{pid}/defer",
        json={
            "user_id": "ariel",
            "defer_until_date": "2099-01-15",
            "note": "held pending evaluation",
        },
    )
    assert r.status_code == 200
    assert "deferred until 2099-01-15" in r.json()["message"]
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "cooling"
        assert row.cooling_off_until is not None
        assert row.cooling_off_until.date().isoformat() == "2099-01-15"
        # expires_at pushed past the resurface date + review TTL.
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        assert exp > datetime(2099, 1, 15, tzinfo=UTC)
        hist = (
            (
                await session.execute(
                    select(ProposalHistory).where(ProposalHistory.proposal_id == pid)
                )
            )
            .scalars()
            .all()
        )
        notes = [h.note for h in hist]
        assert any(
            "deferred: defer_until=2099-01-15; held pending evaluation" == n for n in notes
        )
        assert any(h.transitioned_by == "user:ariel" for h in hist)


@pytest.mark.asyncio
async def test_defer_resurfaces_via_process_cooling_state_machine() -> None:
    """The park-and-resurface round trip is legal in the state machine."""
    from argosy.decisions.proposals import is_legal_transition

    assert is_legal_transition(ProposalStatus.AWAITING_HUMAN, ProposalStatus.COOLING)
    assert is_legal_transition(ProposalStatus.COOLING, ProposalStatus.AWAITING_HUMAN)


@pytest.mark.asyncio
async def test_defer_rejects_non_awaiting_status(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(status="approved")
    r = await client.post(
        f"/api/proposals/{pid}/defer",
        json={"user_id": "ariel", "defer_until_date": "2099-01-15"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_defer_rejects_limited_account(client: AsyncClient) -> None:
    """A limited-account row's cooling path auto-executes — defer must refuse."""
    await _seed_user()
    pid = await _seed_proposal_full(status="awaiting_human", account_class="limited")
    r = await client.post(
        f"/api/proposals/{pid}/defer",
        json={"user_id": "ariel", "defer_until_date": "2099-01-15"},
    )
    assert r.status_code == 400
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "awaiting_human"  # untouched


@pytest.mark.asyncio
async def test_defer_bad_date_400_and_wrong_user_404(client: AsyncClient) -> None:
    await _seed_user()
    pid = await _seed_proposal(status="awaiting_human")
    r = await client.post(
        f"/api/proposals/{pid}/defer",
        json={"user_id": "ariel", "defer_until_date": "not-a-date"},
    )
    assert r.status_code == 400
    r2 = await client.post(
        f"/api/proposals/{pid}/defer",
        json={"user_id": "other", "defer_until_date": "2099-01-15"},
    )
    assert r2.status_code == 404
