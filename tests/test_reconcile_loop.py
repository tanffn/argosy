"""ReconcileLoop tests: pending_orders → fills + status transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.execution.reconcile import ReconcileLoop, _OrderSnapshot
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    PendingOrder,
    Prediction,
    User,
)
from argosy.state.models import (
    Fill as FillRow,
)
from argosy.state.models import (
    Proposal as ProposalRow,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _seed(*, user_id: str = "ariel") -> tuple[int, int]:
    """Create user, proposal (executed_live), pending_order. Return ids."""
    async with db_mod.get_session() as session:
        existing = await session.get(User, user_id)
        if existing is None:
            session.add(User(id=user_id))
            await session.flush()
        proposal = ProposalRow(
            user_id=user_id,
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=10,
            tier="T1",
            account_class="main",
            account_id="ibkr_main",
            status="executed_live",
            rationale_summary="r",
            expected_impact_json="{}",
            confidence="MEDIUM",
        )
        session.add(proposal)
        await session.flush()
        pending = PendingOrder(
            user_id=user_id,
            proposal_id=proposal.id,
            broker="ibkr",
            broker_order_id="brkr-1",
            account_id="ibkr_main",
            status="submitted",
        )
        session.add(pending)
        await session.commit()
        return proposal.id, pending.id


def test_broker_fill_omitted_commission_is_not_confirmed_zero():
    args = dict(broker="ibkr", broker_order_id="1", ticker="AAPL", action="buy", quantity=1, price=100)
    assert not FillModel(**args).commission_confirmed
    assert not FillModel(**args, commission_confirmed=True).commission_confirmed
    assert FillModel(**args, commission=0).commission_confirmed


def test_ibkr_parser_requires_raw_order_client_side_and_execution_fee_identity():
    from types import SimpleNamespace

    from argosy.adapters.brokers.ibkr import IBKRAdapter

    adapter = IBKRAdapter(user_id="test")
    raw = SimpleNamespace(execution=SimpleNamespace(execId="execution", acctNumber="ibkr_main",
        orderId=42, clientId=1, side="BOT", shares=1, price=100),
        contract=SimpleNamespace(symbol="AAPL"), commissionReport=SimpleNamespace(commission=0),
        time=datetime.now(UTC))
    assert not adapter._parse_execution_fill(raw, "42", "ibkr_main").commission_confirmed
    raw.commissionReport.execId = "execution"
    assert adapter._parse_execution_fill(raw, "42", "ibkr_main").commission_confirmed
    for field, bad, message in [("orderId", 43, "order identity"), ("orderId", None, "order identity"),
                                ("clientId", 9, "client identity"), ("side", "", "side")]:
        prior = getattr(raw.execution, field)
        setattr(raw.execution, field, bad)
        with pytest.raises(ValueError, match=message):
            adapter._parse_execution_fill(raw, "42", "ibkr_main")
        setattr(raw.execution, field, prior)
    raw.commissionReport.execId = "another-execution"
    with pytest.raises(ValueError, match="different execution"):
        adapter._parse_execution_fill(raw, "42", "ibkr_main")


@pytest.mark.asyncio
@pytest.mark.parametrize("fee_currency", ["USD", "EUR"])
async def test_ibkr_authoritative_execution_time_not_callback_time_and_paper_not_live(engine, fee_currency):
    from types import SimpleNamespace

    from argosy.adapters.brokers.ibkr import IBKRAccountConfig, IBKRAdapter, IBKRSettings
    from argosy.execution.reconcile import persist_broker_fill
    from argosy.execution.settlement import FillSettlement
    from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot
    from argosy.state.models import FillBookApplication

    pid, _ = await _seed()
    now = datetime.now(UTC)
    actual = now-timedelta(days=4)
    raw = SimpleNamespace(execution=SimpleNamespace(execId="late-callback", acctNumber="ibkr_main",
        orderId="brkr-1", clientId=1, side="BOT", shares=1, price=100, time=actual),
        contract=SimpleNamespace(symbol="AAPL", currency="USD"), commissionReport=SimpleNamespace(execId="late-callback", commission=0, currency=fee_currency), time=now)
    paper_adapter = IBKRAdapter(user_id="test", settings=IBKRSettings())
    paper_receipt = paper_adapter._parse_execution_fill(raw, "brkr-1", "ibkr_main")
    assert paper_receipt.paper
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="Paper execution"):
            await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=paper_receipt)
        await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=PortfolioSnapshot(
            source_path="broker:test", snapshot_date=(now-timedelta(days=2)).date(), positions=[
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Core Equity", symbol="AAPL",
                    shares=100, current_price=100, current_value_local=10000, usd_value_k=10),
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Cash", symbol="",
                    current_value_local=5000, usd_value_k=5)])))
        live = IBKRAdapter(user_id="test", settings=IBKRSettings(accounts={
            "ibkr_main": IBKRAccountConfig(account_id="ibkr_main", mode="live")}))
        receipt = live._parse_execution_fill(raw, "brkr-1", "ibkr_main")
        assert receipt.execution_time_confirmed and receipt.filled_at == actual
        receipt.settlement = FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-100, reference="execution-report")
        assert await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=receipt)
        await session.commit()
        application = await session.scalar(select(FillBookApplication))
        assert application.status == "needs_reconciliation"
        assert ("already be included" if fee_currency == "USD" else "currencies differ") in application.reason
        row = await session.scalar(select(FillRow))
        assert row.price_currency == "USD" and row.commission_currency == fee_currency
        raw.time = now+timedelta(hours=1)
        history = live._parse_execution_fill(raw, "brkr-1", "ibkr_main")
        assert not await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=history)
        del raw.execution.time
        assert not live._parse_execution_fill(raw, "brkr-1", "ibkr_main").execution_time_confirmed
        raw.execution.time = actual.replace(tzinfo=None)
        assert not live._parse_execution_fill(raw, "brkr-1", "ibkr_main").execution_time_confirmed


def test_default_adapter_uses_each_users_native_account_mapping(monkeypatch):
    from types import SimpleNamespace

    from argosy.adapters.brokers.ibkr import IBKRAccountConfig, IBKRSettings

    mappings = {"ariel": "U111", "other": "U222"}
    monkeypatch.setattr(IBKRSettings, "load", classmethod(lambda cls, user_id: cls(accounts={
        "ibkr_main": IBKRAccountConfig(account_id="ibkr_main", broker_account_id=mappings[user_id])})))
    for user_id, native in mappings.items():
        adapter = ReconcileLoop(user_id=user_id).adapter_factory("ibkr")
        assert adapter.user_id == user_id
        raw = SimpleNamespace(execution=SimpleNamespace(execId="same-id", acctNumber=native, orderId="same-order", clientId=1,
            side="BOT", shares=1, price=100), contract=SimpleNamespace(symbol="AAPL"),
            commissionReport=SimpleNamespace(execId="same-id", commission=0), time=datetime.now(UTC))
        receipt = adapter._parse_execution_fill(raw, "same-order", "ibkr_main")
        assert receipt.native_account_id == native and receipt.account_id == "ibkr_main"
        raw.execution.acctNumber = next(value for value in mappings.values() if value != native)
        with pytest.raises(ValueError, match="native account mapping"):
            adapter._parse_execution_fill(raw, "same-order", "ibkr_main")


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, " "])
async def test_terminal_order_repolls_missing_currency_evidence(engine, unknown):
    from argosy.execution.settlement import FillSettlement

    pid, pending_id = await _seed()
    receipt = FillModel(broker="ibkr", broker_order_id="brkr-1", external_fill_id="units",
        account_id="ibkr_main", ticker="AAPL", action="buy", quantity=10, price=100, commission=0,
        filled_at=datetime.now(UTC), price_currency=unknown, commission_currency=unknown,
        settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-1000, reference="units"))
    adapter = MockAdapter(_OrderSnapshot(status="filled", filled_quantity=10, fills=[receipt]))
    loop = ReconcileLoop(adapter_factory=lambda broker: adapter)
    assert (await loop.tick())["fills_recorded"] == 1
    async with db_mod.get_session() as session:
        pending = await session.get(PendingOrder, pending_id)
        assert pending.status == "filled"
        pending.last_polled_at = datetime.now(UTC)-timedelta(hours=7)
        await session.commit()
    adapter.snapshot.fills = [receipt.model_copy(update={"price_currency": "USD", "commission_currency": "USD"})]
    recovered = await loop.tick()
    assert recovered["pending"] == 1 and recovered["errors"] == 0 and recovered["fills_deduped"] == 1
    async with db_mod.get_session() as session:
        saved = await session.scalar(select(FillRow))
        assert saved.price_currency == "USD" and saved.commission_currency == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_currency", [None, "EUR"])
async def test_monetary_telemetry_requires_compatible_known_currency_units(engine, second_currency):
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        proposal = await session.get(ProposalRow, pid)
        proposal.expected_impact_json = json.dumps({"order_sheet_fingerprint": "unit-test", "order_line": {"action": "BUY"}})
        forecast = Prediction(user_id="ariel", source="signal_stream:order_sheet", ticker="AAPL", direction="long",
            source_ref=json.dumps({"order_sheet_fingerprint": "unit-test", "action": "BUY"}),
            entry_price=100, timeframe_days=180, event_at=now, evaluation_due_at=now+timedelta(days=180),
            evaluation_method="order_sheet_due_date_v1", archived=0, provenance_weights_applied=0)
        session.add(forecast)
        await session.commit()
        for index, unit in enumerate(("USD", second_currency)):
            await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=FillModel(
                broker="ibkr", broker_order_id="brkr-1", external_fill_id=f"unit-{index}", account_id="ibkr_main",
                ticker="AAPL", action="buy", quantity=5, price=100, commission=1, filled_at=now,
                price_currency=unit, commission_currency=unit))
        await session.commit()
        telemetry = json.loads(forecast.source_ref)["fill_telemetry"]
        assert telemetry["quantity"] == 10 and telemetry["complete"]
        assert telemetry["vwap"] is None and telemetry["commission"] is None
        assert telemetry["status"] == "execution_details_incomplete"
        assert float(forecast.entry_price) == 100


class MockAdapter:
    """Adapter that exposes `get_order_snapshot` with canned outcomes."""

    def __init__(self, snapshot: _OrderSnapshot | None) -> None:
        self.snapshot = snapshot

    def get_open_orders(self, account_id):
        return []

    async def get_order_snapshot(self, broker_order_id: str):
        return self.snapshot


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.real_seam
async def test_reconcile_filled_writes_fill_row(engine: None) -> None:
    pid, _ = await _seed()
    fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        external_fill_id="exec-filled",
        ticker="AAPL",
        action="buy",
        quantity=10,
        price=180.0,
        commission=1.0,
    )
    snap = _OrderSnapshot(status="filled", fills=[fill])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].quantity == 10
        assert fills[0].price == 180.0
        assert fills[0].paper is False
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "filled"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "fill.received")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_partial_keeps_status_partial(engine: None) -> None:
    pid, _ = await _seed()
    partial_fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        ticker="AAPL",
        action="buy",
        quantity=4,
        price=180.0,
    )
    snap = _OrderSnapshot(status="partial", fills=[partial_fill])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "partial"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].quantity == 4


@pytest.mark.asyncio
async def test_reconcile_repeated_partial_fill_is_idempotent(engine: None) -> None:
    pid, _ = await _seed()
    partial_fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        external_fill_id="exec-1",
        account_id="ibkr_main",
        ticker="AAPL",
        action="buy",
        quantity=4,
        price=180.0,
    )
    adapter = MockAdapter(_OrderSnapshot(status="partial", fills=[partial_fill]))
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    first = await loop.tick()
    second = await loop.tick()

    assert first["fills_recorded"] == 1
    assert second["fills_deduped"] == 1
    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].external_fill_id == "exec-1"
        assert fills[0].account_id == "ibkr_main"


@pytest.mark.asyncio
async def test_filled_without_execution_details_stays_pollable(engine: None) -> None:
    _, po_id = await _seed()
    loop = ReconcileLoop(
        adapter_factory=lambda b: MockAdapter(
            _OrderSnapshot(status="filled", fills=[], reason="details lagging")
        )
    )
    summary = await loop.tick()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        assert po.status == "submitted"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert fills == []
    assert summary["errors"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["confirmed", "missing", "naive"])
async def test_fill_vwap_keeps_recommendation_clock_and_adds_execution_telemetry(engine: None, clock) -> None:
    from types import SimpleNamespace

    from argosy.adapters.brokers.ibkr import IBKRAccountConfig, IBKRAdapter, IBKRSettings
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    authored_at = datetime(2026, 8, 1, tzinfo=UTC)
    filled_at = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    async with db_mod.get_session() as session:
        proposal = await session.get(ProposalRow, pid)
        proposal.account_id = "ibkr_main"
        proposal.expected_impact_json = json.dumps({"order_sheet_fingerprint": "a" * 64,
                                                    "order_line": {"action": "BUY"}})
        session.add(
            Prediction(
                user_id="ariel",
                source="signal_stream:order_sheet",
                source_ref=json.dumps({"proposal_id": pid, "order_sheet_fingerprint": "a" * 64, "action": "BUY"}),
                ticker="AAPL",
                direction="long",
                entry_price=175.0,
                timeframe_days=90,
                event_at=authored_at,
                evaluation_due_at=authored_at + timedelta(days=90),
                evaluation_method="order_sheet_due_date_v1",
                archived=0,
                provenance_weights_applied=0,
            )
        )
        await session.commit()
    raw = SimpleNamespace(execution=SimpleNamespace(orderId="brkr-1", execId="exec-vwap-1", acctNumber="ibkr_main",
        clientId=1, side="BOT", shares=10, price=180.25,
        time=filled_at if clock == "confirmed" else filled_at.replace(tzinfo=None) if clock == "naive" else None),
        contract=SimpleNamespace(symbol="AAPL", currency="USD"),
        commissionReport=SimpleNamespace(execId="exec-vwap-1", commission=1, currency="USD"), time=datetime.now(UTC))
    parser = IBKRAdapter(user_id="ariel", settings=IBKRSettings(accounts={
        "ibkr_main": IBKRAccountConfig(account_id="ibkr_main", mode="live")}))
    fill = parser._parse_execution_fill(raw, "brkr-1", "ibkr_main")
    await ReconcileLoop(
        adapter_factory=lambda b: MockAdapter(
            _OrderSnapshot(status="filled", fills=[fill])
        )
    ).tick()
    async with db_mod.get_session() as session:
        prediction = (
            await session.execute(select(Prediction))
        ).scalars().one()
        assert float(prediction.entry_price) == 175.0
        assert prediction.event_at == authored_at.replace(tzinfo=None)
        telemetry = json.loads(prediction.source_ref)["fill_telemetry"]
        assert telemetry["quantity"] == 10
        assert telemetry["external_fill_ids"] == ["exec-vwap-1"]
        assert telemetry["execution_time_confirmed"] == (clock == "confirmed")
        if clock != "confirmed":
            assert telemetry["first_fill_at"] is None and telemetry["last_fill_at"] is None
            assert telemetry["status"] == "execution_details_incomplete"
            raw.execution.time = filled_at
            confirmed = parser._parse_execution_fill(raw, "brkr-1", "ibkr_main")
            assert not await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=confirmed)
            await session.commit()
            updated = json.loads(prediction.source_ref)["fill_telemetry"]
            assert updated["execution_time_confirmed"] and updated["first_fill_at"] == filled_at.isoformat()
            assert updated["status"] == "verified"
            assert prediction.event_at == authored_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_paper_broker_receipt_cannot_be_relabelled_live(engine):
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="Paper execution"):
            await persist_broker_fill(session, user_id="ariel", proposal_id=pid,
                                      account_id="ibkr_main", fill=FillModel(
                                          broker="ibkr", broker_order_id="paper", ticker="AAPL",
                                          action="buy", quantity=10, price=100, paper=True))
        assert not (await session.execute(select(FillRow))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"ticker": "MSFT"}, {"action": "sell"},
                                    {"account_id": "schwab_main"}, {"broker": "schwab_csv"},
                                    {"proposal_id": 999}, {"price": float("inf")}, {"quantity": -1}])
async def test_receipt_identity_cannot_be_overwritten_to_fit_order(engine, change):
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    receipt = FillModel(broker="ibkr", broker_order_id="brkr-1", account_id="ibkr_main",
                        ticker="AAPL", action="buy", quantity=10, price=100).model_copy(update=change)
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError):
            await persist_broker_fill(session, user_id="ariel", proposal_id=pid,
                                      account_id="ibkr_main", fill=receipt)
        assert not (await session.execute(select(FillRow))).scalars().all()


@pytest.mark.asyncio
async def test_duplicate_execution_id_must_keep_all_receipt_facts(engine):
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    receipt = FillModel(broker="ibkr", broker_order_id="brkr-1", external_fill_id="exec-1",
                        account_id="ibkr_main", ticker="AAPL", action="buy", quantity=10,
                        price=100, commission=1)
    async with db_mod.get_session() as session:
        kwargs = dict(user_id="ariel", proposal_id=pid, account_id="ibkr_main")
        assert await persist_broker_fill(session, **kwargs, fill=receipt)
        await session.commit()
        assert not await persist_broker_fill(session, **kwargs, fill=receipt)
        with pytest.raises(ValueError, match="different receipt facts"):
            await persist_broker_fill(session, **kwargs, fill=receipt.model_copy(update={"commission": 2}))


@pytest.mark.asyncio
async def test_derived_execution_identity_is_stored_but_not_proof(engine):
    from argosy.execution.fill_evidence import reconcile_fill_evidence
    from argosy.execution.reconcile import persist_broker_fill

    pid, _ = await _seed()
    async with db_mod.get_session() as session:
        proposal = await session.get(ProposalRow, pid)
        proposal.account_id = "ibkr_main"
        await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main",
                                  fill=FillModel(broker="ibkr", broker_order_id="brkr-1",
                                                 ticker="AAPL", action="buy", quantity=10, price=100))
        await session.commit()
        rows = (await session.execute(select(FillRow))).scalars().all()
        assert rows[0].external_fill_id.startswith("derived:")
        evidence = reconcile_fill_evidence(proposal, rows)
        assert not evidence.complete and evidence.quantity == 0
        assert "derived identity" in evidence.errors[0]


@pytest.mark.asyncio
async def test_snapshot_cannot_attribute_another_orders_execution(engine):
    pid, _ = await _seed()
    receipt = FillModel(broker="ibkr", broker_order_id="different-order", external_fill_id="exec-other",
                        account_id="ibkr_main", ticker="AAPL", action="buy", quantity=10, price=100)
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(_OrderSnapshot(status="filled", fills=[receipt])))
    summary = await loop.tick()
    assert summary["errors"] == 1 and summary["fills_recorded"] == 0
    async with db_mod.get_session() as session:
        assert not (await session.execute(select(FillRow))).scalars().all()
        assert (await session.execute(select(PendingOrder))).scalars().one().status == "submitted"
        error = await session.scalar(select(AuditLog).where(AuditLog.event_type == "reconcile.order_failed"))
        assert "different order" in error.payload_json


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_trailing", [False, True])
@pytest.mark.parametrize("separate_orders", [False, True])
async def test_entire_broker_batch_is_saved_before_any_book_application(engine, unknown_trailing, separate_orders):
    from argosy.execution.settlement import FillSettlement
    from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot
    from argosy.state.models import FillBookApplication

    pid, _ = await _seed()
    second_pid = None
    if separate_orders:
        second_pid, second_pending = await _seed()
        async with db_mod.get_session() as session:
            (await session.get(PendingOrder, second_pending)).broker_order_id = "brkr-2"
            (await session.get(ProposalRow, second_pid)).size_shares_or_currency = 4
            (await session.get(ProposalRow, pid)).size_shares_or_currency = 6
            await session.commit()
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=PortfolioSnapshot(
            source_path="broker:test", snapshot_date=(now-timedelta(days=3)).date(), positions=[
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Core Equity", symbol="AAPL",
                    shares=100, current_price=180, current_value_local=18000, usd_value_k=18),
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Cash", symbol="",
                    current_value_local=2000, usd_value_k=2)])))
    common = dict(proposal_id=pid, broker="ibkr", broker_order_id="brkr-1", account_id="ibkr_main",
                  ticker="AAPL", action="buy", price=180, commission=0, price_currency="USD", commission_currency="USD")
    newer = FillModel(**common, external_fill_id="newer", quantity=6, filled_at=now,
        settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-1080, reference="newer"))
    if separate_orders:
        common = {**common, "proposal_id": second_pid, "broker_order_id": "brkr-2"}
    older = (FillModel(**common, external_fill_id="older", quantity=4) if unknown_trailing else
        FillModel(**common, external_fill_id="older", quantity=4, filled_at=now-timedelta(minutes=1),
            settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-720, reference="older")))
    class BatchAdapter:
        async def get_order_snapshot(self, order_id):
            fills = ([newer] if order_id == "brkr-1" else [older]) if separate_orders else [newer, older]
            return _OrderSnapshot(status="filled", fills=fills)

    loop = ReconcileLoop(adapter_factory=lambda b: BatchAdapter())
    summary = await loop.tick()
    assert summary["errors"] == 0 and summary["fills_recorded"] == 2
    async with db_mod.get_session() as session:
        applications = list(await session.scalars(select(FillBookApplication)))
        if unknown_trailing:
            assert {a.status for a in applications} == {"needs_settlement", "needs_reconciliation"}
            assert not any(a.applied_snapshot_id for a in applications)
        else:
            assert all(a.status == "applied" for a in applications)


@pytest.mark.asyncio
async def test_invalid_order_does_not_rollback_another_orders_receipt(engine):
    first, first_pending = await _seed()
    second, second_pending = await _seed()
    async with db_mod.get_session() as session:
        (await session.get(PendingOrder, second_pending)).broker_order_id = "brkr-2"
        await session.commit()

    class MixedAdapter:
        async def get_order_snapshot(self, order_id):
            return _OrderSnapshot(status="filled", fills=[FillModel(broker="ibkr",
                broker_order_id=order_id if order_id == "brkr-1" else "wrong-order", external_fill_id=order_id,
                account_id="ibkr_main", ticker="AAPL", action="buy", quantity=10, price=180)])

    result = await ReconcileLoop(adapter_factory=lambda b: MixedAdapter()).tick()
    assert result["fills_recorded"] == 1 and result["errors"] == 1
    async with db_mod.get_session() as session:
        assert [r.proposal_id for r in await session.scalars(select(FillRow))] == [first]
        assert (await session.get(PendingOrder, first_pending)).status == "filled"
        assert (await session.get(PendingOrder, second_pending)).status == "submitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["conflict", "empty", "incomplete", "partial_empty", "cancelled_bad", "rejected_bad", "wrong_account", "wrong_client", "paper"])
async def test_failed_staging_pauses_account_until_successful_repoll(engine, fault):
    from argosy.execution.settlement import FillSettlement
    from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot
    from argosy.state.models import FillBookApplication

    newer_pid, _ = await _seed()
    older_pid, older_pending = await _seed()
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        (await session.get(PendingOrder, older_pending)).broker_order_id = "brkr-2"
        if fault == "wrong_account":
            (await session.get(PendingOrder, older_pending)).account_id = "ibkr_other"
        await session.commit()
        await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=PortfolioSnapshot(
            source_path="broker:test", snapshot_date=(now-timedelta(days=3)).date(), positions=[
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Core Equity", symbol="AAPL",
                    shares=100, current_price=180, current_value_local=18000, usd_value_k=18),
                PortfolioPosition(location="ibkr_main", currency="USD", asset_type="Cash", symbol="",
                    current_value_local=5000, usd_value_k=5)])))

    class FailingBatch:
        broken = True

        async def get_order_snapshot(self, order_id):
            receipt = FillModel(proposal_id=newer_pid if order_id == "brkr-1" else older_pid,
                broker="ibkr", broker_order_id=order_id, account_id="ibkr_main", external_fill_id=order_id,
                ticker="AAPL", action="buy", quantity=10, price=180, commission=0,
                price_currency="USD", commission_currency="USD",
                filled_at=now if order_id == "brkr-1" else now-timedelta(minutes=1),
                settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-1800, reference=order_id))
            fills = [receipt]
            if order_id == "brkr-2" and fault in {"cancelled_bad", "rejected_bad"}:
                changed = {"filled_at": now + timedelta(minutes=1)}
                if fault == "cancelled_bad":
                    changed["external_fill_id"] = ""  # Legacy derived identity is not proof.
                else:
                    changed.update(quantity=11, settlement=FillSettlement(currency="USD", tax_withheld=0,
                        net_cash_delta=-1980, reference="overfill"))
                return _OrderSnapshot(status="cancelled" if fault == "cancelled_bad" else "rejected",
                                      fills=[receipt.model_copy(update=changed)])
            if order_id == "brkr-2" and self.broken:
                if fault in {"wrong_client", "paper"}:
                    from types import SimpleNamespace

                    from argosy.adapters.brokers.ibkr import (
                        IBKRAccountConfig,
                        IBKRAdapter,
                        IBKRSettings,
                    )

                    raw_adapter = IBKRAdapter(user_id="test", settings=IBKRSettings(accounts={
                        "ibkr_main": IBKRAccountConfig(account_id="ibkr_main", mode="paper" if fault == "paper" else "live")}))
                    trade = SimpleNamespace(order=SimpleNamespace(orderId=order_id, account="ibkr_main", clientId=99),
                                            orderStatus=SimpleNamespace(status="Cancelled", filled=0), fills=[])

                    async def connect(account):
                        return SimpleNamespace(trades=lambda: [trade], fills=lambda: [])

                    raw_adapter.connect = connect
                    return await raw_adapter.get_order_snapshot(order_id, account_id="ibkr_main")
                if fault == "conflict":
                    fills.append(receipt.model_copy(update={"broker_order_id": "wrong-order"}))
                elif fault in {"empty", "partial_empty"}:
                    return _OrderSnapshot(status="partial" if fault == "partial_empty" else "filled", fills=[])
            if order_id == "brkr-2" and fault == "incomplete":
                fills = [receipt.model_copy(update={"external_fill_id": f"older-part-{index}", "quantity": 5,
                    "settlement": FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-900, reference=f"part-{index}")})
                    for index in range(1 if self.broken else 2)]
            return _OrderSnapshot(status="filled", fills=fills)

    adapter = FailingBatch()
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    first = await loop.tick()
    assert first["errors"] == 1 and first["book_applied"] == 0
    async with db_mod.get_session() as session:
        assert (await session.get(PendingOrder, older_pending)).receipt_sync_error
        assert not any(a.applied_snapshot_id for a in await session.scalars(select(FillBookApplication)))
    if fault in {"cancelled_bad", "rejected_bad"}:
        return  # Contradictory receipts require correction evidence, not a blind retry.
    adapter.broken = False
    if fault == "wrong_account":
        still_broken = await loop.tick()
        assert still_broken["errors"] == 1 and still_broken["book_applied"] == 0
        async with db_mod.get_session() as session:
            (await session.get(PendingOrder, older_pending)).account_id = "ibkr_main"
            await session.commit()
    second = await loop.tick()
    assert second["errors"] == 0 and second["book_applied"] == (3 if fault == "incomplete" else 2)


@pytest.mark.asyncio
async def test_default_reconcile_reuses_and_closes_real_adapter_connections(engine, monkeypatch):
    from types import SimpleNamespace

    from argosy.adapters.brokers.ibkr import IBKRAccountConfig, IBKRAdapter, IBKRSettings

    await _seed()
    _, pending_id = await _seed()
    async with db_mod.get_session() as session:
        (await session.get(PendingOrder, pending_id)).broker_order_id = "brkr-2"
        await session.commit()
    active, connections = set(), []

    class ConnectionAwareIB:
        connected = False

        def isConnected(self):
            return self.connected

        async def connectAsync(self, host, port, clientId):
            key = (host, port, clientId)
            if key in active:
                raise RuntimeError("client ID already connected")
            active.add(key)
            connections.append(key)
            self.key, self.connected = key, True

        def disconnect(self):
            active.remove(self.key)
            self.connected = False

        def trades(self):
            return [SimpleNamespace(order=SimpleNamespace(orderId=identity, clientId=1, account="ibkr_main"),
                    orderStatus=SimpleNamespace(status="Submitted", filled=0), fills=[])
                    for identity in ("brkr-1", "brkr-2")]

        def fills(self):
            return []

    monkeypatch.setattr(IBKRSettings, "load", classmethod(lambda cls, user_id: cls(accounts={
        "ibkr_main": IBKRAccountConfig(account_id="ibkr_main", mode="live")})))
    monkeypatch.setattr(IBKRAdapter, "_ib_module_factory", staticmethod(lambda: SimpleNamespace(IB=ConnectionAwareIB)))
    monkeypatch.setattr(IBKRAdapter, "MAX_RETRIES", 1)
    monkeypatch.setattr("argosy.adapters.brokers.ibkr.get_secret", lambda key: None)
    loop = ReconcileLoop()
    for _ in range(2):
        summary = await loop.tick()
        assert summary["pending"] == 2 and summary["errors"] == 0
        assert not active
    assert len(connections) == 2  # one reused connection per tick, always closed


@pytest.mark.asyncio
async def test_zero_receipt_response_cannot_clear_wrong_pending_custody(engine):
    _, pending_id = await _seed()
    async with db_mod.get_session() as session:
        pending = await session.get(PendingOrder, pending_id)
        pending.account_id, pending.receipt_sync_error = "ibkr_other", "unresolved account staging"
        await session.commit()
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(_OrderSnapshot(status="cancelled", filled_quantity=0)))
    result = await loop.tick()
    assert result["errors"] == 1
    async with db_mod.get_session() as session:
        pending = await session.get(PendingOrder, pending_id)
        assert "custody" in pending.receipt_sync_error and pending.status == "submitted"


@pytest.mark.asyncio
async def test_ibkr_native_mapping_and_delayed_commission_enrich_one_receipt(engine):
    import types

    from argosy.adapters.brokers.ibkr import IBKRAccountConfig, IBKRAdapter, IBKRSettings
    from argosy.execution.reconcile import persist_broker_fill
    from argosy.state.models import FillBookApplication

    pid, _ = await _seed()
    raw = types.SimpleNamespace(
        execution=types.SimpleNamespace(execId="native-fee", acctNumber="U123456", orderId="brkr-1", clientId=1, side="BOT", shares=2, price=201.25),
        contract=types.SimpleNamespace(symbol="AAPL"), commissionReport=None, time=datetime.now(UTC))
    settings = IBKRSettings(accounts={"ibkr_main": IBKRAccountConfig(account_id="ibkr_main", broker_account_id="U123456", mode="live")})
    adapter = IBKRAdapter(user_id="ariel", settings=settings)
    first = adapter._parse_execution_fill(raw, "brkr-1", "ibkr_main")
    assert first.account_id == "ibkr_main" and first.native_account_id == "U123456" and not first.commission_confirmed
    async with db_mod.get_session() as session:
        assert await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=first)
        await session.commit()
        row = await session.scalar(select(FillRow))
        assert not row.commission_confirmed
        raw.commissionReport = types.SimpleNamespace(execId="native-fee", commission=.75)
        reported = adapter._parse_execution_fill(raw, "brkr-1", "ibkr_main")
        assert not await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="ibkr_main", fill=reported)
        await session.commit()
        assert row.commission_confirmed and float(row.commission) == .75 and row.native_account_id == "U123456"
        assert len(list(await session.scalars(select(FillRow)))) == 1
        assert (await session.scalar(select(FillBookApplication))).status == "needs_settlement"
        assert await session.scalar(select(AuditLog).where(AuditLog.event_type == "fill.broker_metadata_confirmed"))
    raw.execution.acctNumber = "U999999"
    with pytest.raises(ValueError, match="native account mapping"):
        adapter._parse_execution_fill(raw, "brkr-1", "ibkr_main")


@pytest.mark.asyncio
@pytest.mark.parametrize("external_id,quantity", [("", 10), ("native-id", 9.9999), ("native-id", 11)])
async def test_broker_filled_status_does_not_override_receipt_evidence(engine, external_id, quantity):
    from argosy.services.jobs.summary_status import derive_run_status

    pid, _ = await _seed()
    receipt = FillModel(broker="ibkr", broker_order_id="brkr-1", external_fill_id=external_id,
                        account_id="ibkr_main", ticker="AAPL", action="buy", quantity=quantity, price=100)
    loop = ReconcileLoop(adapter_factory=lambda b: MockAdapter(_OrderSnapshot(status="filled", fills=[receipt])))
    summary = await loop.tick()
    assert summary["terminal"] == 0 and summary["errors"] == 1
    assert derive_run_status(summary)[0] == "error"
    async with db_mod.get_session() as session:
        assert (await session.execute(select(PendingOrder))).scalars().one().status == "submitted"
        assert len((await session.execute(select(FillRow))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_reconcile_cancelled_records_audit(engine: None) -> None:
    pid, _ = await _seed()
    snap = _OrderSnapshot(status="cancelled", fills=[], reason="user requested")
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "cancelled"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "order.cancelled")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_rejected_records_audit(engine: None) -> None:
    pid, _ = await _seed()
    snap = _OrderSnapshot(status="rejected", fills=[], reason="margin")
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "rejected"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "order.rejected")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_skips_already_terminal_pending(engine: None) -> None:
    pid, po_id = await _seed()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        po.status = "filled"
        await session.commit()
    snap = _OrderSnapshot(status="rejected", fills=[])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        assert po.status == "filled"  # unchanged
