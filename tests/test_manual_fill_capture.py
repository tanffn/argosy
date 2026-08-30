"""Real DB/API seam for read-only-broker fill receipts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.execution.manual_fills import record_manual_fill
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Fill,
    PendingOrder,
    Prediction,
    Proposal,
    ProposalHistory,
    User,
)


async def _seed_proposal(*, quantity: float = 10.0) -> tuple[int, int]:
    now = datetime.now(UTC) - timedelta(days=1)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        proposal = Proposal(
            user_id="ariel",
            ticker="NVDA",
            action="sell",
            size_shares_or_currency=quantity,
            size_units="shares",
            instrument="stock",
            order_type="limit",
            limit_price=190,
            tier="T2",
            account_class="main",
            account_id="schwab_rsu",
            status="approved",
            rationale_summary="fund a higher-conviction order-sheet line",
            expected_impact_json="{}",
            confidence="MEDIUM",
            source="order_sheet",
        )
        session.add(proposal)
        await session.flush()
        prediction = Prediction(
            user_id="ariel",
            source="signal_stream:order_sheet",
            source_ref=json.dumps(
                {"proposal_id": proposal.id, "action": "TRIM"}, sort_keys=True
            ),
            ticker="NVDA",
            direction="short",
            entry_price=Decimal("190"),
            timeframe_days=30,
            message_id=f"manual-fill-test-{proposal.id}",
            event_at=now,
            evaluation_due_at=now + timedelta(days=30),
            evaluation_method="order_sheet_due_date_v1",
        )
        session.add(prediction)
        await session.commit()
        return proposal.id, prediction.id


@pytest.mark.asyncio
@pytest.mark.real_seam
async def test_manual_fill_closes_execution_and_calibration_loop(
    engine: None, client: AsyncClient
) -> None:
    proposal_id, prediction_id = await _seed_proposal()
    filled_at = datetime.now(UTC).replace(microsecond=0)

    response = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill",
        json={
            "user_id": "ariel",
            "broker_order_id": "SCHWAB-ORDER-71",
            "external_fill_id": "SCHWAB-EXEC-71",
            "quantity": 10,
            "price": 187.25,
            "commission": 1.5,
            "filled_at": filled_at.isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["broker"] == "schwab_csv"
    assert body["account_id"] == "schwab_rsu"
    assert body["proposal_status"] == "executed_live"
    assert body["pending_status"] == "filled"

    async with db_mod.get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        prediction = await session.get(Prediction, prediction_id)
        fills = (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all()
        pending = (
            await session.execute(
                select(PendingOrder).where(PendingOrder.proposal_id == proposal_id)
            )
        ).scalar_one()
        histories = (
            await session.execute(
                select(ProposalHistory).where(
                    ProposalHistory.proposal_id == proposal_id
                )
            )
        ).scalars().all()
        audit_types = set(
            (
                await session.execute(
                    select(AuditLog.event_type).where(
                        AuditLog.entity_id == str(proposal_id)
                    )
                )
            ).scalars().all()
        )

        assert proposal is not None and proposal.status == "executed_live"
        assert len(fills) == 1
        assert fills[0].ticker == "NVDA"
        assert fills[0].action == "sell"
        assert fills[0].account_id == "schwab_rsu"
        assert fills[0].paper is False
        assert pending.status == "filled"
        assert histories[-1].transitioned_by == "manual_fill_capture"
        assert {"fill.received", "proposal.transition"} <= audit_types
        assert prediction is not None
        assert float(prediction.entry_price) == 187.25
        telemetry = json.loads(prediction.source_ref)["fill_telemetry"]
        assert telemetry["quantity"] == 10
        assert telemetry["vwap"] == 187.25
        assert telemetry["commission"] == 1.5


@pytest.mark.asyncio
async def test_manual_partial_fills_are_idempotent_and_update_vwap(
    engine: None, client: AsyncClient
) -> None:
    proposal_id, prediction_id = await _seed_proposal(quantity=10)
    first_payload = {
        "user_id": "ariel",
        "broker_order_id": "SCHWAB-ORDER-72",
        "external_fill_id": "SCHWAB-EXEC-72A",
        "quantity": 4,
        "price": 180,
    }
    first = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill", json=first_payload
    )
    replay = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill", json=first_payload
    )
    second = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill",
        json={
            **first_payload,
            "external_fill_id": "SCHWAB-EXEC-72B",
            "quantity": 6,
            "price": 200,
        },
    )

    assert first.status_code == 200 and first.json()["pending_status"] == "partial"
    assert replay.status_code == 200 and replay.json()["created"] is False
    assert second.status_code == 200 and second.json()["pending_status"] == "filled"
    assert second.json()["filled_quantity"] == 10

    async with db_mod.get_session() as session:
        fills = (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all()
        prediction = await session.get(Prediction, prediction_id)
        assert len(fills) == 2
        assert prediction is not None
        assert float(prediction.entry_price) == 192.0


@pytest.mark.asyncio
async def test_manual_fill_rejects_unapproved_or_overfilled_proposal(
    engine: None,
) -> None:
    proposal_id, _ = await _seed_proposal(quantity=5)
    async with db_mod.get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "awaiting_human"
        await session.commit()

    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="must be approved"):
            await record_manual_fill(
                session,
                user_id="ariel",
                proposal_id=proposal_id,
                broker_order_id="SCHWAB-ORDER-73",
                external_fill_id="SCHWAB-EXEC-73",
                quantity=6,
                price=100,
            )
        await session.rollback()
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "approved"
        await session.commit()

    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="exceeds approved quantity"):
            await record_manual_fill(
                session,
                user_id="ariel",
                proposal_id=proposal_id,
                broker_order_id="SCHWAB-ORDER-73",
                external_fill_id="SCHWAB-EXEC-73",
                quantity=6,
                price=100,
            )
        await session.rollback()

    async with db_mod.get_session() as session:
        assert (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all() == []
