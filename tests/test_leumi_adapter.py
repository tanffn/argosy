"""LeumiTSVAdapter wraps the existing TSV ingestor; place_order is manual."""

from __future__ import annotations

import pytest

from argosy.adapters.brokers.leumi_tsv import LeumiTSVAdapter
from argosy.adapters.brokers.types import ProposedOrder


@pytest.mark.asyncio
async def test_place_order_always_manual_required() -> None:
    adapter = LeumiTSVAdapter(user_id="ariel")
    order = ProposedOrder(
        account_id="leumi",
        ticker="VWRA",
        action="buy",
        quantity=10,
        user_id="ariel",
    )
    result = await adapter.place_order(order, paper=True)
    assert result.status == "manual_required"
    assert result.broker == "leumi_tsv"
    assert "Leumi" in result.reason


@pytest.mark.asyncio
async def test_cancel_order_manual() -> None:
    adapter = LeumiTSVAdapter(user_id="ariel")
    result = await adapter.cancel_order("any-id")
    assert result.status == "manual_required"
    assert result.broker == "leumi_tsv"


def test_get_open_orders_empty() -> None:
    adapter = LeumiTSVAdapter(user_id="ariel")
    assert adapter.get_open_orders("leumi") == []


def test_get_lots_empty_no_per_lot_data() -> None:
    adapter = LeumiTSVAdapter(user_id="ariel")
    assert adapter.get_lots("leumi", "AAPL") == []


def test_get_positions_empty_when_no_tsv_path() -> None:
    adapter = LeumiTSVAdapter(user_id="ariel")
    assert adapter.get_positions("leumi") == []
