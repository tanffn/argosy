"""Phase 4 API tests: execute / approve-via-token / lots / fills / audit."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.adapters.brokers.types import ExecutionResult, ProposedOrder
from argosy.channels.email import EmailApprovalLink
from argosy.execution.audit import write_paper_fill
from argosy.execution.router import ExecutionRouter
from argosy.state import db as db_mod
from argosy.state.models import (
    Fill as FillRow,
    Lot as LotRow,
    Proposal as ProposalRow,
    User,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _seed_user_proposal(*, status: str = "approved") -> int:
    async with db_mod.get_session() as session:
        existing = await session.get(User, "ariel")
        if existing is None:
            session.add(User(id="ariel"))
            await session.flush()
        p = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=5,
            tier="T1",
            account_class="main",
            status=status,
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            limit_price=100.0,
            order_type="limit",
            time_in_force="DAY",
        )
        session.add(p)
        await session.commit()
        return int(p.id)


class _MockBroker:
    name = "ibkr"

    def __init__(self):
        self.placed: list[ProposedOrder] = []

    def get_positions(self, account_id):
        return []

    def get_lots(self, account_id, ticker):
        return []

    def get_open_orders(self, account_id):
        return []

    async def place_order(self, order: ProposedOrder, paper: bool = True):
        self.placed.append(order)
        if paper:
            await write_paper_fill(
                user_id=order.user_id,
                broker=self.name,
                ticker=order.ticker,
                action=order.action,
                quantity=order.quantity,
                price=order.limit_price or 0.0,
                proposal_id=order.proposal_id,
                broker_order_id=order.client_order_id,
            )
            return ExecutionResult(
                status="paper",
                broker=self.name,
                paper=True,
                broker_order_id=order.client_order_id,
            )
        return ExecutionResult(status="submitted", broker=self.name, broker_order_id="x")

    async def cancel_order(self, order_id):
        from argosy.adapters.brokers.types import CancellationResult

        return CancellationResult(status="cancelled", broker=self.name)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_endpoint_paper_mode(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    pid = await _seed_user_proposal()

    mock = _MockBroker()
    real_init = ExecutionRouter.__init__

    def patched_init(self, **kw):
        kw["adapter_factories"] = {"ibkr": lambda: mock}
        real_init(self, **kw)

    monkeypatch.setattr(ExecutionRouter, "__init__", patched_init)

    r = await client.post(
        f"/api/proposals/{pid}/execute",
        json={"user_id": "ariel", "cash_available_usd": 100_000.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paper"
    assert body["paper"] is True

    async with db_mod.get_session() as session:
        proposal = await session.get(ProposalRow, pid)
        assert proposal.status == "executed_paper"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1


@pytest.mark.asyncio
async def test_execute_endpoint_404_when_missing(client: AsyncClient) -> None:
    r = await client.post(
        "/api/proposals/9999/execute",
        json={"user_id": "ariel"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_execute_endpoint_409_when_not_approved(client: AsyncClient) -> None:
    pid = await _seed_user_proposal(status="awaiting_human")
    r = await client.post(
        f"/api/proposals/{pid}/execute",
        json={"user_id": "ariel"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_approve_via_token_redirects(client: AsyncClient) -> None:
    pid = await _seed_user_proposal()
    link = EmailApprovalLink(signing_key="kx")
    # Inject the same key into the route's verifier by patching verify result.
    # Simpler: tests construct the same EmailApprovalLink in the route via the
    # OS keychain singleton, so we forge one with the same key.
    import argosy.api.routes.execution as exec_mod

    orig_link_class = exec_mod.EmailApprovalLink

    def factory():
        return EmailApprovalLink(signing_key="kx")

    exec_mod.EmailApprovalLink = factory  # type: ignore[assignment]
    try:
        token = link.issue(proposal_id=pid, user_id="ariel", action="approve")
        r = await client.get(f"/api/proposals/{pid}/approve?token={token}", follow_redirects=False)
        assert r.status_code == 302
        location = r.headers["location"]
        assert f"/proposals?confirm={pid}" in location
        assert "action=approve" in location
        assert "token=" in location
    finally:
        exec_mod.EmailApprovalLink = orig_link_class  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_approve_via_token_400_on_bad_token(client: AsyncClient) -> None:
    pid = await _seed_user_proposal()
    r = await client.get(f"/api/proposals/{pid}/approve?token=garbage")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_lots_route(client: AsyncClient) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()
        session.add(
            LotRow(
                user_id="ariel",
                account_id="schwab-1",
                ticker="AAPL",
                quantity=10,
                cost_basis_usd=1000,
                source="schwab_csv",
            )
        )
        session.add(
            LotRow(
                user_id="ariel",
                account_id="schwab-1",
                ticker="NVDA",
                quantity=5,
                cost_basis_usd=2000,
                source="schwab_csv",
            )
        )
        await session.commit()

    r = await client.get("/api/lots?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2

    r2 = await client.get("/api/lots?user_id=ariel&ticker=NVDA")
    assert r2.status_code == 200
    assert r2.json()["total"] == 1


@pytest.mark.asyncio
async def test_fills_route(client: AsyncClient) -> None:
    pid = await _seed_user_proposal()
    await write_paper_fill(
        user_id="ariel",
        broker="ibkr",
        ticker="AAPL",
        action="buy",
        quantity=5,
        price=100.0,
        proposal_id=pid,
        broker_order_id="ord-1",
    )

    r = await client.get(f"/api/fills?user_id=ariel&proposal_id={pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["paper"] is True


@pytest.mark.asyncio
async def test_audit_route(client: AsyncClient) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()
    from argosy.execution.audit import record_audit_event

    await record_audit_event(
        user_id="ariel",
        event_type="test.event",
        entity_type="thing",
        entity_id="42",
        payload={"x": 1},
    )
    await record_audit_event(
        user_id="ariel",
        event_type="other.event",
        entity_type="thing",
        entity_id="43",
        payload={"x": 2},
    )

    r = await client.get("/api/audit?user_id=ariel")
    assert r.status_code == 200
    assert r.json()["total"] == 2
    r2 = await client.get("/api/audit?user_id=ariel&event_type=test.event")
    assert r2.status_code == 200
    assert r2.json()["total"] == 1
