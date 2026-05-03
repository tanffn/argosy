"""IBKRAdapter tests with a mocked `ib_insync` module."""

from __future__ import annotations

import types
from typing import Any

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.ibkr import IBKRAdapter
from argosy.adapters.brokers.types import ProposedOrder
from argosy.state import db as db_mod
from argosy.state.models import Fill as FillRow, User


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeOrderStatus:
    def __init__(self, status: str = "submitted") -> None:
        self.status = status
        self.filled = 0


class _FakeTrade:
    def __init__(self, contract: Any, order: Any) -> None:
        self.contract = contract
        self.order = order
        self.orderStatus = _FakeOrderStatus("submitted")
        self.fills: list[Any] = []


class _FakeIB:
    def __init__(self) -> None:
        self.connected = False
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[str] = []

    def isConnected(self) -> bool:
        return self.connected

    async def connectAsync(self, host: str, port: int, clientId: int) -> None:
        self.connected = True
        self.host = host
        self.port = port
        self.clientId = clientId

    def placeOrder(self, contract: Any, order: Any) -> _FakeTrade:
        trade = _FakeTrade(contract, order)
        # Synthesize a broker order id for assertions
        if not getattr(order, "orderId", None):
            order.orderId = 42
        self.placed.append((contract, order))
        return trade

    def cancelOrder(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    def positions(self, account_id: str) -> list[Any]:
        return []

    def openOrders(self) -> list[Any]:
        return []


def _fake_module() -> types.ModuleType:
    """Build a tiny fake `ib_insync` module with the constructors we need."""
    mod = types.ModuleType("ib_insync_fake")

    class Stock:
        def __init__(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.secType = "STK"

    class _Order:
        def __init__(self, action: str, qty: float) -> None:
            self.action = action
            self.totalQuantity = qty
            self.orderType = "MKT"
            self.tif = "DAY"
            self.orderId: int | None = None
            self.orderRef = ""

    class MarketOrder(_Order):
        def __init__(self, action: str, qty: float) -> None:
            super().__init__(action, qty)
            self.orderType = "MKT"

    class LimitOrder(_Order):
        def __init__(self, action: str, qty: float, lmtPrice: float) -> None:
            super().__init__(action, qty)
            self.orderType = "LMT"
            self.lmtPrice = lmtPrice

    class StopOrder(_Order):
        def __init__(self, action: str, qty: float, auxPrice: float) -> None:
            super().__init__(action, qty)
            self.orderType = "STP"
            self.auxPrice = auxPrice

    class StopLimitOrder(_Order):
        def __init__(self, action: str, qty: float, lmtPrice: float, stopPrice: float) -> None:
            super().__init__(action, qty)
            self.orderType = "STP LMT"
            self.lmtPrice = lmtPrice
            self.auxPrice = stopPrice

    mod.IB = _FakeIB
    mod.Stock = Stock
    mod.MarketOrder = MarketOrder
    mod.LimitOrder = LimitOrder
    mod.StopOrder = StopOrder
    mod.StopLimitOrder = StopLimitOrder
    return mod


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_mode_writes_paperfill_and_audit(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    fake = _fake_module()
    adapter = IBKRAdapter(user_id="ariel")
    adapter._ib_module_factory = lambda: fake

    order = ProposedOrder(
        account_id="limited",
        ticker="NVDA",
        action="buy",
        order_type="limit",
        quantity=10,
        limit_price=120.0,
        client_order_id="abc123",
        proposal_id=None,
        user_id="ariel",
    )
    result = await adapter.place_order(order, paper=True)
    assert result.status == "paper"
    assert result.broker == "ibkr"
    assert result.paper is True
    # Fake IB must NOT have received an order.
    assert adapter._ib is None or not adapter._ib.placed

    async with db_mod.get_session() as session:
        rows = (await session.execute(select(FillRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].paper is True
        assert rows[0].broker == "ibkr"
        assert rows[0].action == "buy"
        assert float(rows[0].quantity) == 10
        assert float(rows[0].price) == 120.0


@pytest.mark.asyncio
async def test_live_mode_calls_placeOrder(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    fake = _fake_module()
    adapter = IBKRAdapter(user_id="ariel")
    adapter._ib_module_factory = lambda: fake

    order = ProposedOrder(
        account_id="limited",
        ticker="NVDA",
        action="buy",
        order_type="market",
        quantity=5,
        client_order_id="live-1",
        proposal_id=None,
        user_id="ariel",
    )
    result = await adapter.place_order(order, paper=False)
    assert result.status == "submitted"
    assert result.broker == "ibkr"
    assert result.paper is False

    # The fake IB instance should have received the order.
    placed = adapter._ib.placed
    assert len(placed) == 1
    contract, ib_order = placed[0]
    assert contract.symbol == "NVDA"
    assert contract.secType == "STK"
    assert ib_order.action == "BUY"
    assert ib_order.totalQuantity == 5
    assert ib_order.orderType == "MKT"
    assert ib_order.orderRef == "live-1"


@pytest.mark.asyncio
async def test_limit_order_construction(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    fake = _fake_module()
    adapter = IBKRAdapter(user_id="ariel")
    adapter._ib_module_factory = lambda: fake

    order = ProposedOrder(
        account_id="limited",
        ticker="AAPL",
        action="sell",
        order_type="limit",
        quantity=3,
        limit_price=210.0,
        time_in_force="GTC",
        client_order_id="lim-1",
        user_id="ariel",
    )
    result = await adapter.place_order(order, paper=False)
    assert result.status == "submitted"
    contract, ib_order = adapter._ib.placed[0]
    assert ib_order.action == "SELL"
    assert ib_order.orderType == "LMT"
    assert ib_order.lmtPrice == 210.0
    assert ib_order.tif == "GTC"


@pytest.mark.asyncio
async def test_paper_and_live_share_user_id_and_proposal(engine: None) -> None:
    """Symmetry: paper mode writes to fills with proposal_id linkage."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    fake = _fake_module()
    adapter = IBKRAdapter(user_id="ariel")
    adapter._ib_module_factory = lambda: fake

    order = ProposedOrder(
        account_id="limited",
        ticker="MSFT",
        action="buy",
        order_type="limit",
        quantity=2,
        limit_price=400.0,
        client_order_id="sym-1",
        proposal_id=999,
        user_id="ariel",
    )
    await adapter.place_order(order, paper=True)
    async with db_mod.get_session() as session:
        rows = (await session.execute(select(FillRow))).scalars().all()
        assert rows[0].proposal_id == 999
