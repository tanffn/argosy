"""ArgonautAccount tests (Phase 5)."""

from __future__ import annotations

import os
from datetime import date as _date_cls

import pytest

from argosy.accounts.argonaut import ArgonautAccount
from argosy.accounts.persistence import list_snapshots
from argosy.adapters.brokers.types import Position
from argosy.agent_settings import (
    AgentSettings,
    ExecutionBlock,
    LimitedAccountBlock,
)
from argosy.state import db as db_mod
from argosy.state.models import User


async def _seed_user(user_id: str = "ariel") -> None:
    async with db_mod.get_session() as session:
        if await session.get(User, user_id) is None:
            session.add(User(id=user_id))
            await session.commit()


def _settings(
    *,
    size_usd: float = 1000.0,
    mode: str = "paper",
    account_id: str = "argonaut-1",
) -> AgentSettings:
    return AgentSettings(
        execution=ExecutionBlock(default_mode="paper"),
        limited_account=LimitedAccountBlock(
            size_usd=size_usd, account_id=account_id, execution_mode=mode  # type: ignore[arg-type]
        ),
    )


class _StubAdapter:
    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    def get_positions(self, account_id: str) -> list[Position]:
        return list(self._positions)


def test_argonaut_loads_config_from_settings() -> None:
    settings = _settings(size_usd=2500.0, mode="live", account_id="my-ibkr")
    acct = ArgonautAccount(user_id="ariel", settings=settings)
    assert acct.account_id == "my-ibkr"
    assert acct.configured_size_usd == 2500.0
    assert acct.current_execution_mode() == "live"
    assert acct.is_autonomy_enabled() is True


def test_argonaut_default_account_id_when_blank() -> None:
    settings = _settings(account_id="")
    acct = ArgonautAccount(user_id="ariel", settings=settings)
    assert acct.account_id == "argonaut"


def test_argonaut_kill_switch_disables_autonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(mode="live")
    acct = ArgonautAccount(user_id="ariel", settings=settings)
    assert acct.is_autonomy_enabled() is True
    monkeypatch.setenv("ARGOSY_KILL", "1")
    assert acct.is_autonomy_enabled() is False


def test_argonaut_queue_only_disables_autonomy() -> None:
    settings = _settings(mode="queue_only")
    acct = ArgonautAccount(user_id="ariel", settings=settings)
    assert acct.is_autonomy_enabled() is False


def test_argonaut_value_uses_configured_size_when_no_positions() -> None:
    acct = ArgonautAccount(user_id="ariel", settings=_settings(size_usd=1500))
    assert acct.get_value_usd() == 1500.0


def test_argonaut_value_with_positions_and_prices() -> None:
    pos = Position(
        account_id="argonaut-1",
        ticker="AAPL",
        quantity=5.0,
        avg_cost=100.0,
        currency="USD",
        asset_class="stock",
    )
    acct = ArgonautAccount(
        user_id="ariel",
        settings=_settings(size_usd=2000.0),
        adapter=_StubAdapter([pos]),
    )
    # last_prices override avg_cost
    val = acct.get_value_usd(last_prices={"AAPL": 110.0})
    # positions_value = 5*110 = 550; cash = 2000-550 = 1450; total = 2000
    assert val == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_argonaut_persist_daily_snapshot(engine: None) -> None:
    await _seed_user()
    acct = ArgonautAccount(user_id="ariel", settings=_settings(size_usd=1000))
    payload = await acct.persist_daily_snapshot(on_date=_date_cls(2026, 5, 1))
    assert payload.date == "2026-05-01"
    assert payload.total_value_usd == 1000.0

    # Same date is idempotent (upsert).
    payload2 = await acct.persist_daily_snapshot(on_date=_date_cls(2026, 5, 1))
    assert payload2.date == "2026-05-01"

    rows = await list_snapshots(user_id="ariel")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_argonaut_snapshot_day_pnl(engine: None) -> None:
    """day_pnl_usd is delta vs previous snapshot."""
    await _seed_user()
    acct = ArgonautAccount(user_id="ariel", settings=_settings(size_usd=1000))
    await acct.persist_daily_snapshot(on_date=_date_cls(2026, 5, 1))
    # Manually update size to simulate a value change next day, then persist.
    acct.settings.limited_account.size_usd = 1100.0
    p2 = await acct.persist_daily_snapshot(on_date=_date_cls(2026, 5, 2))
    assert p2.day_pnl_usd == pytest.approx(100.0)
