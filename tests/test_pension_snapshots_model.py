"""Round-trip + query tests for the `pension_fund_snapshots` table."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import PensionFundSnapshot, User
from argosy.state.queries import get_user_pension_snapshots


@pytest.mark.asyncio
async def test_pension_fund_snapshot_roundtrip(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()

        snap = PensionFundSnapshot(
            user_id="ariel",
            fund_id="1234",
            fund_name="Altshuler Shaham Hishtalmut",
            fund_type="keren_hishtalmut",
            manager="Altshuler Shaham",
            return_pct_12m=12.34,
            benchmark_return_pct_12m=10.00,
            relative_to_benchmark_pct=2.34,
            balance_nis=75000,
            source_url="http://gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx",
        )
        session.add(snap)
        await session.commit()

    async with db_mod.get_session() as session:
        rows = (await session.execute(select(PensionFundSnapshot))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.fund_id == "1234"
        assert float(row.return_pct_12m) == pytest.approx(12.34)
        assert "gemelnet" in (row.source_url or "")


@pytest.mark.asyncio
async def test_get_user_pension_snapshots_returns_latest_per_fund(
    engine: None,
) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()

        now = datetime.now(timezone.utc)
        # Two snapshots for fund 1234, one for fund 5678. Helper should
        # return the latest per fund.
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="1234",
                return_pct_12m=10.0,
                snapshot_at=now - timedelta(days=30),
            )
        )
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="1234",
                return_pct_12m=12.5,
                snapshot_at=now,
            )
        )
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="5678",
                return_pct_12m=-1.5,
                snapshot_at=now - timedelta(days=7),
            )
        )
        await session.commit()

    out = await get_user_pension_snapshots("ariel")
    assert len(out) == 2
    by_fund = {row["fund_id"]: row for row in out}
    assert by_fund["1234"]["return_pct_12m"] == pytest.approx(12.5)
    assert by_fund["5678"]["return_pct_12m"] == pytest.approx(-1.5)


@pytest.mark.asyncio
async def test_get_user_pension_snapshots_full_history(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()
        now = datetime.now(timezone.utc)
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="1234",
                return_pct_12m=10.0,
                snapshot_at=now - timedelta(days=30),
            )
        )
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="1234",
                return_pct_12m=12.5,
                snapshot_at=now,
            )
        )
        await session.commit()

    out = await get_user_pension_snapshots("ariel", only_latest_per_fund=False)
    assert len(out) == 2
    # Newest first
    assert out[0]["return_pct_12m"] == pytest.approx(12.5)
