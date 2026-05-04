"""Round-trip + query tests for the `pension_fund_snapshots` table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

        now = datetime.now(UTC)
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
        now = datetime.now(UTC)
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


# ----------------------------------------------------------------------
# Cross-user isolation — guards against SQL leakage across tenants.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_pension_snapshots_isolates_by_user(engine: None) -> None:
    """Two users, each with their own snapshots — `get_user_pension_snapshots`
    must return ONLY the queried user's rows."""
    async with db_mod.get_session() as session:
        session.add(User(id="user_a"))
        session.add(User(id="user_b"))
        await session.flush()

        now = datetime.now(UTC)
        session.add(
            PensionFundSnapshot(
                user_id="user_a",
                fund_id="A1",
                fund_name="A's fund",
                return_pct_12m=5.0,
                snapshot_at=now,
            )
        )
        session.add(
            PensionFundSnapshot(
                user_id="user_b",
                fund_id="B1",
                fund_name="B's fund",
                return_pct_12m=7.0,
                snapshot_at=now,
            )
        )
        await session.commit()

    a_rows = await get_user_pension_snapshots("user_a")
    assert len(a_rows) == 1
    assert a_rows[0]["fund_id"] == "A1"
    assert all(row["user_id"] == "user_a" for row in a_rows)

    b_rows = await get_user_pension_snapshots("user_b")
    assert len(b_rows) == 1
    assert b_rows[0]["fund_id"] == "B1"
    assert all(row["user_id"] == "user_b" for row in b_rows)


@pytest.mark.asyncio
async def test_get_user_pension_snapshots_same_fund_id_separate_users(
    engine: None,
) -> None:
    """Two users with the SAME fund_id (perfectly legitimate — Altshuler
    Shaham fund 1234 might appear in both portfolios). User_b's row must
    not leak into user_a's response, and vice versa.

    This is the regression guard for the window-function rewrite: a
    naive ROW_NUMBER() PARTITION BY fund_id without a user_id filter
    would collapse cross-user rows into a single ranked partition and
    return only one user's row. The query must filter by user_id first.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="user_a"))
        session.add(User(id="user_b"))
        await session.flush()

        now = datetime.now(UTC)
        # Same fund_id, different users, different timestamps.
        session.add(
            PensionFundSnapshot(
                user_id="user_a",
                fund_id="SHARED_FUND",
                fund_name="Altshuler Shaham Hishtalmut",
                return_pct_12m=10.0,
                snapshot_at=now - timedelta(days=5),
            )
        )
        session.add(
            PensionFundSnapshot(
                user_id="user_b",
                fund_id="SHARED_FUND",
                fund_name="Altshuler Shaham Hishtalmut",
                return_pct_12m=20.0,
                snapshot_at=now,  # newer — would win a global PARTITION
            )
        )
        await session.commit()

    a_rows = await get_user_pension_snapshots("user_a")
    assert len(a_rows) == 1
    assert a_rows[0]["user_id"] == "user_a"
    assert a_rows[0]["return_pct_12m"] == pytest.approx(10.0)

    b_rows = await get_user_pension_snapshots("user_b")
    assert len(b_rows) == 1
    assert b_rows[0]["user_id"] == "user_b"
    assert b_rows[0]["return_pct_12m"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_get_user_pension_snapshots_full_history_isolates_by_user(
    engine: None,
) -> None:
    """The `only_latest_per_fund=False` path must also be user-scoped."""
    async with db_mod.get_session() as session:
        session.add(User(id="user_a"))
        session.add(User(id="user_b"))
        await session.flush()

        now = datetime.now(UTC)
        for offset in (1, 5, 10):
            session.add(
                PensionFundSnapshot(
                    user_id="user_a",
                    fund_id="A1",
                    return_pct_12m=float(offset),
                    snapshot_at=now - timedelta(days=offset),
                )
            )
        session.add(
            PensionFundSnapshot(
                user_id="user_b",
                fund_id="B1",
                return_pct_12m=99.0,
                snapshot_at=now,
            )
        )
        await session.commit()

    a_rows = await get_user_pension_snapshots("user_a", only_latest_per_fund=False)
    assert len(a_rows) == 3
    assert all(row["user_id"] == "user_a" for row in a_rows)

    b_rows = await get_user_pension_snapshots("user_b", only_latest_per_fund=False)
    assert len(b_rows) == 1
    assert b_rows[0]["user_id"] == "user_b"
