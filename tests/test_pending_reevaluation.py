"""Tests for the /consult auto-retry queue + daily sweep.

Covers the persist / list / mark APIs in
`argosy/services/pending_reevaluation.py` and the daily loop's
re-fire-and-classify logic in
`argosy/orchestrator/loops/pending_reevaluation_daily.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from argosy.orchestrator.loops.pending_reevaluation_daily import (
    PendingReevaluationDailyLoop,
)
from argosy.services.pending_reevaluation import (
    MAX_REEVAL_ATTEMPTS,
    enqueue_pending_reevaluation,
    list_pending_for_sweep,
    mark_abandoned,
    mark_resolved,
    record_attempt,
)
from argosy.state import db as db_mod
from argosy.state.models import PendingReevaluation, User


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()


# ----------------------------------------------------------------------
# Enqueue / upsert behaviour
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_creates_new_row(engine: None) -> None:
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="user said BUY",
        failure_reason="stale price + incomplete fundamentals",
    )
    assert row_id > 0
    rows = await list_pending_for_sweep(user_id="ariel")
    assert len(rows) == 1
    assert rows[0].ticker == "NOW"
    assert rows[0].status == "pending"
    assert rows[0].attempt_count == 1


@pytest.mark.asyncio
async def test_enqueue_upserts_increments_attempt(engine: None) -> None:
    """Second enqueue for same (user, ticker, mode) increments attempt_count
    + refreshes last_failure_reason — does NOT create a new row."""
    await _seed_user()
    first = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="ctx",
        failure_reason="first failure",
    )
    second = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="ctx",
        failure_reason="second failure (different)",
    )
    assert first == second
    rows = await list_pending_for_sweep(user_id="ariel")
    assert len(rows) == 1
    assert rows[0].attempt_count == 2
    assert "second failure" in (rows[0].last_failure_reason or "")


@pytest.mark.asyncio
async def test_enqueue_reopens_resolved_row(engine: None) -> None:
    """User re-runs the same consult after a resolved retry —
    queue should reopen with fresh retry budget."""
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="ctx",
        failure_reason="first failure",
    )
    await mark_resolved(row_id=row_id, decision_run_id=999)

    second = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="ctx",
        failure_reason="second failure",
    )
    assert second == row_id
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "pending"
    assert row.attempt_count == 1  # reset on reopen
    assert row.resolved_decision_run_id is None


# ----------------------------------------------------------------------
# Mark helpers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_resolved_writes_decision_run_id(engine: None) -> None:
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )
    await mark_resolved(row_id=row_id, decision_run_id=42)
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "resolved"
    assert row.resolved_decision_run_id == 42


@pytest.mark.asyncio
async def test_mark_abandoned_writes_final_reason(engine: None) -> None:
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )
    await mark_abandoned(row_id=row_id, final_reason="hit max attempts")
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "abandoned"
    assert "max attempts" in (row.last_failure_reason or "")


@pytest.mark.asyncio
async def test_record_attempt_increments(engine: None) -> None:
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )
    new = await record_attempt(row_id=row_id, failure_reason="another fail")
    assert new == 2


# ----------------------------------------------------------------------
# Daily loop classifications
# ----------------------------------------------------------------------


@dataclass
class _StubResponse:
    decision_run_id: int = 0
    status: str = "blocked"
    blocked_by: str | None = None
    blocked_reason: str | None = None


@pytest.mark.asyncio
async def test_daily_loop_resolves_on_real_verdict(engine: None) -> None:
    """When the consult retry returns a real verdict (not
    INSUFFICIENT_DATA), the row is marked resolved + linked."""
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )

    async def _stub(_body: Any) -> _StubResponse:
        return _StubResponse(
            decision_run_id=101,
            status="blocked",
            blocked_by="trader_hold",  # real verdict (HOLD), not insufficient
            blocked_reason="hold rationale",
        )

    loop = PendingReevaluationDailyLoop(
        user_id="ariel", consult_runner=_stub,
    )
    summary = await loop.tick()
    assert summary == {
        "swept": 1, "resolved": 1, "still_pending": 0, "abandoned": 0,
        "proposals_expired": 0,
    }
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "resolved"
    assert row.resolved_decision_run_id == 101


@pytest.mark.asyncio
async def test_daily_loop_records_attempt_on_persistent_insufficient_data(
    engine: None,
) -> None:
    """Retry STILL returns INSUFFICIENT_DATA — attempt_count increments,
    row stays pending."""
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )

    async def _stub(_body: Any) -> _StubResponse:
        return _StubResponse(
            decision_run_id=102,
            status="blocked",
            blocked_by="trader_insufficient_data",
            blocked_reason="still no clean data",
        )

    loop = PendingReevaluationDailyLoop(
        user_id="ariel", consult_runner=_stub,
    )
    summary = await loop.tick()
    assert summary["still_pending"] == 1
    assert summary["resolved"] == 0
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "pending"
    assert row.attempt_count == 2


@pytest.mark.asyncio
async def test_daily_loop_abandons_after_max_attempts(engine: None) -> None:
    """When attempt_count hits the cap, mark abandoned."""
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )
    # Bump attempt_count to MAX-1 so the next tick hits the cap.
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
        row.attempt_count = MAX_REEVAL_ATTEMPTS - 1
        await session.commit()

    async def _stub(_body: Any) -> _StubResponse:
        return _StubResponse(
            decision_run_id=103,
            status="blocked",
            blocked_by="trader_insufficient_data",
            blocked_reason="still no clean data",
        )

    loop = PendingReevaluationDailyLoop(
        user_id="ariel", consult_runner=_stub,
    )
    summary = await loop.tick()
    assert summary["abandoned"] == 1
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "abandoned"
    assert row.attempt_count == MAX_REEVAL_ATTEMPTS


@pytest.mark.asyncio
async def test_daily_loop_no_rows_returns_zero_summary(engine: None) -> None:
    """Empty queue → summary with zeros, no errors."""
    await _seed_user()
    loop = PendingReevaluationDailyLoop(user_id="ariel")
    summary = await loop.tick()
    assert summary == {
        "swept": 0, "resolved": 0, "still_pending": 0, "abandoned": 0,
        "proposals_expired": 0,
    }


@pytest.mark.asyncio
async def test_daily_loop_handles_consult_exception(engine: None) -> None:
    """If the consult call raises, treat as an attempt (record_attempt)
    so we don't loop forever on a broken retry path."""
    await _seed_user()
    row_id = await enqueue_pending_reevaluation(
        user_id="ariel", ticker="NOW", tier_value="T2",
        consult_mode="long_hold", user_constraints="",
        failure_reason="x",
    )

    async def _stub(_body: Any) -> _StubResponse:
        raise RuntimeError("consult exploded")

    loop = PendingReevaluationDailyLoop(
        user_id="ariel", consult_runner=_stub,
    )
    summary = await loop.tick()
    assert summary["still_pending"] == 1
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
    assert row.status == "pending"
    assert row.attempt_count == 2
    assert "consult exploded" in (row.last_failure_reason or "")
