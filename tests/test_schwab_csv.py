"""SchwabCSVAdapter tests against a fixture CSV."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.schwab_csv import SchwabCSVAdapter
from argosy.adapters.brokers.types import ProposedOrder
from argosy.state import db as db_mod
from argosy.state.models import Lot as LotRow, User


_FIXTURE_CSV = """\
Symbol,Quantity,Open Date,Cost/Share,Cost Basis,Account Number
AAPL,100,2024-01-15,175.20,17520.00,1234-5678
NVDA,50,2023-06-30,428.55,21427.50,1234-5678
NVDA,25,2024-09-01,500.00,12500.00,1234-5678
MSFT,40,2024-02-20,400.00,16000.00,1234-5678
"""


@pytest.fixture
def fixture_csv(tmp_path: Path) -> Path:
    p = tmp_path / "schwab_cost_basis.csv"
    p.write_text(_FIXTURE_CSV, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_import_cost_basis_csv(engine: None, fixture_csv: Path) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    adapter = SchwabCSVAdapter(user_id="ariel")
    n = await adapter.import_cost_basis_csv(fixture_csv)
    assert n == 4

    async with db_mod.get_session() as session:
        rows = (await session.execute(select(LotRow))).scalars().all()
        assert len(rows) == 4
        nvda = [r for r in rows if r.ticker == "NVDA"]
        assert len(nvda) == 2
        total = sum(float(r.cost_basis_usd) for r in nvda)
        assert total == pytest.approx(33927.50)
        for r in rows:
            assert r.source == "schwab_csv"
            assert r.account_id == "1234-5678"


@pytest.mark.asyncio
async def test_get_lots_after_import(engine: None, fixture_csv: Path) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    adapter = SchwabCSVAdapter(user_id="ariel")
    await adapter.import_cost_basis_csv(fixture_csv)

    lots = adapter.get_lots("1234-5678", "NVDA")
    assert len(lots) == 2
    assert all(l.ticker == "NVDA" for l in lots)


@pytest.mark.asyncio
async def test_get_positions_aggregates_lots(engine: None, fixture_csv: Path) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    adapter = SchwabCSVAdapter(user_id="ariel")
    await adapter.import_cost_basis_csv(fixture_csv)

    positions = adapter.get_positions("1234-5678")
    by_ticker = {p.ticker: p for p in positions}
    assert by_ticker["AAPL"].quantity == 100
    assert by_ticker["NVDA"].quantity == 75
    # NVDA avg = (50*428.55 + 25*500) / 75 = (21427.5 + 12500) / 75
    assert by_ticker["NVDA"].avg_cost == pytest.approx(33927.5 / 75)


@pytest.mark.asyncio
async def test_place_order_returns_manual_required() -> None:
    adapter = SchwabCSVAdapter(user_id="ariel")
    order = ProposedOrder(
        account_id="schwab",
        ticker="AAPL",
        action="buy",
        order_type="market",
        quantity=1,
        user_id="ariel",
    )
    result = await adapter.place_order(order, paper=True)
    assert result.status == "manual_required"
    assert result.broker == "schwab_csv"
    assert "schwab" in result.reason.lower()
