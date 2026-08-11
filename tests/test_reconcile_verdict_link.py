"""Seam 4: fill ↔ verdict linkage at reconcile time.

A settled fill should record the verdict that recommended it, resolved via
fills.proposal_id → proposals.decision_run_id ↔ verdicts.source_decision_run_id.
Resolution is best-effort: a miss stores NULL and never breaks the fill write.
Also exercises the read helpers and the 0101 migration up/down in-memory.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.execution.reconcile import (
    ReconcileLoop,
    _OrderSnapshot,
    fills_for_verdict,
    verdict_for_fill,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    DecisionRun,
    Fill as FillRow,
    PendingOrder,
    Proposal as ProposalRow,
    User,
    Verdict as VerdictRow,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _seed(
    *,
    user_id: str = "ariel",
    with_decision_run: bool = True,
    with_verdict: bool = True,
    verdict_settled: bool = True,
    ticker: str = "AAPL",
) -> tuple[int, int | None]:
    """Seed user, optional decision_run + verdict, proposal, pending_order.

    Returns (proposal_id, verdict_id_or_None).
    """
    async with db_mod.get_session() as session:
        if await session.get(User, user_id) is None:
            session.add(User(id=user_id))
            await session.flush()

        run_id = None
        verdict_id = None
        if with_decision_run:
            run = DecisionRun(user_id=user_id, ticker=ticker, decision_kind="trade_proposal")
            session.add(run)
            await session.flush()
            run_id = run.id
            if with_verdict:
                v = VerdictRow(
                    user_id=user_id,
                    subject=ticker.upper(),
                    verdict="BUY",
                    conviction="HIGH",
                    source_decision_run_id=run_id,
                    settled=verdict_settled,
                )
                session.add(v)
                await session.flush()
                verdict_id = v.id

        proposal = ProposalRow(
            user_id=user_id,
            ticker=ticker,
            action="buy",
            size_shares_or_currency=10,
            tier="T1",
            account_class="main",
            status="executed_live",
            rationale_summary="r",
            expected_impact_json="{}",
            confidence="MEDIUM",
            decision_run_id=run_id,
        )
        session.add(proposal)
        await session.flush()
        pending = PendingOrder(
            user_id=user_id,
            proposal_id=proposal.id,
            broker="ibkr",
            broker_order_id="brkr-1",
            status="submitted",
        )
        session.add(pending)
        await session.commit()
        return proposal.id, verdict_id


class MockAdapter:
    def __init__(self, snapshot: _OrderSnapshot | None) -> None:
        self.snapshot = snapshot

    def get_open_orders(self, account_id):
        return []

    async def get_order_snapshot(self, broker_order_id: str):
        return self.snapshot


def _fill(pid: int, ticker: str = "AAPL") -> FillModel:
    return FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        ticker=ticker,
        action="buy",
        quantity=10,
        price=180.0,
        commission=1.0,
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_records_resolved_verdict_id(engine: None) -> None:
    """Proposal's decision_run has a settled verdict → fill records it."""
    pid, vid = await _seed()
    assert vid is not None
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(
        _OrderSnapshot(status="filled", fills=[_fill(pid)])
    ))
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].verdict_id == vid


@pytest.mark.asyncio
async def test_fill_no_decision_run_stores_null(engine: None) -> None:
    """Proposal without a decision_run_id → verdict_id NULL, fill still written."""
    pid, vid = await _seed(with_decision_run=False)
    assert vid is None
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(
        _OrderSnapshot(status="filled", fills=[_fill(pid)])
    ))
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].verdict_id is None


@pytest.mark.asyncio
async def test_fill_links_superseded_verdict(engine: None) -> None:
    """FIX 2: a verdict for the run that was later superseded (settled=False)
    still links — the fill was recommended by that run's verdict regardless of
    its current settled state."""
    pid, vid = await _seed(with_verdict=True, verdict_settled=False)
    assert vid is not None
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(
        _OrderSnapshot(status="filled", fills=[_fill(pid)])
    ))
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].verdict_id == vid


@pytest.mark.asyncio
async def test_resolution_never_poisons_fill_write_session(engine: None) -> None:
    """DECISIVE (Sol repro): the caller has a dirtied pending_order whose flush
    WOULD fail (NOT NULL violation). Resolution must NOT autoflush that dirty
    row (no_autoflush) — so it still resolves correctly AND leaves the session
    usable, letting the fill commit with NO PendingRollbackError.

    Without no_autoflush the session.get()/execute() would autoflush the invalid
    po, flip the session rollback-only, and the fill commit would raise → the
    fill would be DROPPED. This asserts that path is closed.
    """
    from argosy.execution.reconcile import _resolve_verdict_id

    pid, vid = await _seed()
    assert vid is not None

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().first()
        # Dirty the po with a NOT-NULL-violating value: any autoflush now raises.
        po.status = None  # type: ignore[assignment]

        got = await _resolve_verdict_id(
            session, user_id="ariel", proposal_id=pid, ticker="AAPL"
        )
        # Resolution succeeded WITHOUT flushing the poisoned po.
        assert got == vid

        # Session is still usable: repair po and commit a fill — must not raise.
        po.status = "filled"  # type: ignore[assignment]
        session.add(
            FillRow(
                user_id="ariel",
                proposal_id=pid,
                verdict_id=None,
                broker="ibkr",
                broker_order_id="brkr-1",
                ticker="AAPL",
                action="buy",
                quantity=1,
                price=1.0,
            )
        )
        await session.commit()  # would raise PendingRollbackError if poisoned

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].verdict_id is None


@pytest.mark.asyncio
async def test_fill_subject_mismatch_stores_null(engine: None) -> None:
    """Verdict exists for the run but for a different subject → no link."""
    pid, vid = await _seed(ticker="AAPL")
    assert vid is not None
    # Fill for a different ticker than the verdict's subject.
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(
        _OrderSnapshot(status="filled", fills=[_fill(pid, ticker="MSFT")])
    ))
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].verdict_id is None


@pytest.mark.asyncio
async def test_read_helpers_roundtrip(engine: None) -> None:
    pid, vid = await _seed()
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(
        _OrderSnapshot(status="filled", fills=[_fill(pid)])
    ))
    await loop.tick()

    async with db_mod.get_session() as session:
        by_verdict = await fills_for_verdict(session, verdict_id=vid)
        assert len(by_verdict) == 1
        fill = by_verdict[0]
        assert fill.verdict_id == vid

        v = await verdict_for_fill(session, fill)
        assert v is not None
        assert v.id == vid
        assert v.subject == "AAPL"

        # An unlinked fill resolves to no verdict.
        fill.verdict_id = None
        assert await verdict_for_fill(session, fill) is None
        # And a verdict with no fills returns [].
        assert await fills_for_verdict(session, verdict_id=999999) == []


# ----------------------------------------------------------------------
# Migration 0101 up/down (in-memory only — never touches live db)
# ----------------------------------------------------------------------


def _import_migration():
    import importlib.util
    from pathlib import Path

    from argosy.config import resolve_home

    path = (
        Path(resolve_home())
        / "alembic"
        / "versions"
        / "0101_fill_verdict_link.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0101", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0101_upgrade_downgrade_in_memory() -> None:
    """0100→0101 adds nullable verdict_id + index; downgrade removes both and
    preserves existing rows/behavior. Uses a throwaway in-memory engine and
    alembic's op context — never runs alembic against the live DB."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = _import_migration()
    assert mod.revision == "0101_fill_verdict_link"
    assert mod.down_revision == "0100_observed_decision_run_id"

    eng = sa.create_engine("sqlite://")  # in-memory
    with eng.connect() as conn:
        # Minimal fills table standing in for the pre-0101 schema.
        conn.exec_driver_sql(
            "CREATE TABLE fills ("
            "id INTEGER PRIMARY KEY, user_id TEXT, proposal_id INTEGER, "
            "ticker TEXT, quantity NUMERIC)"
        )
        conn.exec_driver_sql(
            "INSERT INTO fills (id, user_id, proposal_id, ticker, quantity) "
            "VALUES (1, 'ariel', 7, 'AAPL', 10)"
        )
        ctx = MigrationContext.configure(conn)

        # Bind alembic's global `op` proxy to our migration context.
        with Operations.context(ctx):
            mod.upgrade()
            cols = {c["name"] for c in sa_inspect(conn).get_columns("fills")}
            assert "verdict_id" in cols
            idx = {i["name"] for i in sa_inspect(conn).get_indexes("fills")}
            assert "ix_fills_verdict_id" in idx
            # Existing row preserved; new column defaults to NULL.
            row = conn.exec_driver_sql(
                "SELECT ticker, verdict_id FROM fills WHERE id = 1"
            ).fetchone()
            assert row[0] == "AAPL"
            assert row[1] is None

            mod.downgrade()
            cols = {c["name"] for c in sa_inspect(conn).get_columns("fills")}
            assert "verdict_id" not in cols
            row = conn.exec_driver_sql(
                "SELECT ticker FROM fills WHERE id = 1"
            ).fetchone()
            assert row[0] == "AAPL"  # data preserved through down-migration
