"""`argosy gemelnet ...` CLI tests using Typer's CliRunner.

Patches the adapter constructor so no network access happens and we
can assert on stdout/stderr directly.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from argosy.adapters import MissingDataSourceError
from argosy.cli import gemelnet as gemelnet_cli
from argosy.state import db as db_mod
from argosy.state.models import PensionFundSnapshot, User, UserContext

_SAMPLE_FUNDS = [
    {
        "fund_id": "1234",
        "name": "Altshuler Shaham Hishtalmut",
        "manager": "Altshuler Shaham",
        "type": "keren_hishtalmut",
        "type_hebrew": "קרן השתלמות",
        "return_pct_12m": "12.34",
        "benchmark_return_pct_12m": "10.00",
        "last_updated": "2026-04-30",
    },
    {
        "fund_id": "5678",
        "name": "Harel Gemel Equity",
        "manager": "Harel",
        "type": "kupat_gemel",
        "type_hebrew": "קופת גמל",
        "return_pct_12m": "-2.50",
        "benchmark_return_pct_12m": "-1.00",
        "last_updated": "2026-04-30",
    },
]


_RETURNS_1234 = {
    "fund_id": "1234",
    "fund_name": "Altshuler Shaham Hishtalmut",
    "fund_type": "keren_hishtalmut",
    "manager": "Altshuler Shaham",
    "period": "12m",
    "return_pct": 12.34,
    "benchmark_return_pct": 10.00,
    "relative_to_benchmark_pct": 2.34,
    "last_updated": "2026-04-30",
    "source_url": "http://gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx",
}


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    async def list_funds(self, *, fund_type: str | None = None,
                         ttl_seconds: int = 0) -> list[dict[str, Any]]:
        self.calls.append(f"list:{fund_type}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        if fund_type:
            return [f for f in _SAMPLE_FUNDS if f["type"] == fund_type]
        return list(_SAMPLE_FUNDS)

    async def get_fund_returns(self, fund_id: str, *, period: str = "12m",
                               ttl_seconds: int = 0) -> dict[str, Any]:
        self.calls.append(f"returns:{fund_id}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        if fund_id == "1234":
            return dict(_RETURNS_1234)
        raise MissingDataSourceError(f"unknown fund {fund_id}")

    async def search_funds(self, query: str, *, ttl_seconds: int = 0,
                           limit: int = 25) -> list[dict[str, Any]]:
        self.calls.append(f"search:{query}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        ql = query.lower()
        return [
            f for f in _SAMPLE_FUNDS
            if ql in (f.get("name") or "").lower()
            or ql in (f.get("manager") or "").lower()
        ][:limit]


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> _FakeAdapter:
    fake = _FakeAdapter()
    monkeypatch.setattr(gemelnet_cli, "_adapter", lambda: fake)
    return fake


def test_list_command_prints_rows(cli_runner: CliRunner, fake_adapter: _FakeAdapter) -> None:
    result = cli_runner.invoke(gemelnet_cli.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "1234" in result.output
    assert "5678" in result.output
    assert "(2 fund(s))" in result.output


def test_list_command_filter_by_type(
    cli_runner: CliRunner, fake_adapter: _FakeAdapter
) -> None:
    result = cli_runner.invoke(gemelnet_cli.app, ["list", "--type", "kupat_gemel"])
    assert result.exit_code == 0
    assert "5678" in result.output
    assert "1234" not in result.output


def test_list_command_handles_outage(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = _FakeAdapter(fail=True)
    monkeypatch.setattr(gemelnet_cli, "_adapter", lambda: failing)
    result = cli_runner.invoke(gemelnet_cli.app, ["list"])
    assert result.exit_code == 2
    # Typer's CliRunner merges stderr into output by default
    assert "gemelnet unavailable" in result.output or "simulated outage" in result.output


def test_returns_command(cli_runner: CliRunner, fake_adapter: _FakeAdapter) -> None:
    result = cli_runner.invoke(gemelnet_cli.app, ["returns", "1234"])
    assert result.exit_code == 0
    assert "12.34" in result.output
    assert "Altshuler" in result.output


def test_returns_command_unknown_id(
    cli_runner: CliRunner, fake_adapter: _FakeAdapter
) -> None:
    result = cli_runner.invoke(gemelnet_cli.app, ["returns", "0000"])
    assert result.exit_code == 2


def test_search_command(cli_runner: CliRunner, fake_adapter: _FakeAdapter) -> None:
    result = cli_runner.invoke(gemelnet_cli.app, ["search", "Harel"])
    assert result.exit_code == 0
    assert "5678" in result.output


def test_refresh_user_persists_snapshots(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Sync test: CLI commands call asyncio.run, so we can't be inside
    an already-running event loop. We bring up our own engine + schema
    via asyncio.run rather than the async-fixture path."""
    import asyncio

    from argosy.state.models import Base

    db_path = tmp_path / "argosy_test_gemelnet_cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    async def _setup_db() -> None:
        eng = db_mod.init_engine(url)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Seed in the LEGACY list shape so the CLI must migrate to the
        # canonical dict-keyed-by-vehicle shape on first refresh. This
        # exercises both the migration path AND the dict-shape persist.
        identity = {
            "name": "Ariel",
            "pensions": [
                {
                    "fund_id": "1234",
                    "fund_name": "Altshuler",
                    "type": "keren_hishtalmut",
                    "balance_nis": 75000,
                },
                {
                    # legacy / free-form: no fund_id → must be skipped
                    "fund_name": "Some old account",
                    "type": "keren_hishtalmut",
                    "balance_nis": 10000,
                },
            ],
        }
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(
                UserContext(
                    user_id="ariel",
                    identity_yaml=yaml.safe_dump(identity, allow_unicode=True),
                    goals_yaml="",
                    constraints_yaml="",
                )
            )
            await session.commit()

    asyncio.run(_setup_db())

    fake = _FakeAdapter()
    monkeypatch.setattr(gemelnet_cli, "_adapter", lambda: fake)

    try:
        result = cli_runner.invoke(
            gemelnet_cli.app, ["refresh-user", "--user-id", "ariel"]
        )
        assert result.exit_code == 0, result.output
        assert "refreshed 1 fund(s); skipped 1." in result.output

        async def _verify() -> None:
            async with db_mod.get_session() as session:
                rows = (
                    await session.execute(select(PensionFundSnapshot))
                ).scalars().all()
                assert len(rows) == 1
                row = rows[0]
                assert row.user_id == "ariel"
                assert row.fund_id == "1234"
                assert float(row.return_pct_12m) == pytest.approx(12.34)
                # Snapshot uses the vehicle's AGGREGATE balance across all
                # funds of that vehicle (75000 + 10000 = 85000) — the
                # legacy entry without fund_id still contributes its
                # balance to the bucket even though it's skipped from
                # adapter calls.
                assert float(row.balance_nis) == pytest.approx(85000.0)

                ctx = (
                    await session.execute(
                        select(UserContext).where(UserContext.user_id == "ariel")
                    )
                ).scalar_one()
                identity_after = yaml.safe_load(ctx.identity_yaml)
                # CLI now persists pensions in dict-keyed-by-vehicle shape.
                pensions_after = identity_after["pensions"]
                assert isinstance(pensions_after, dict), pensions_after
                assert "keren_hishtalmut" in pensions_after
                hishtalmut = pensions_after["keren_hishtalmut"]
                # Aggregated balance across both list entries (75000 + 10000).
                assert float(hishtalmut.get("balance_nis", 0)) == pytest.approx(85000)
                # Refreshed-fund must carry a `last_refreshed_at` timestamp.
                refreshed_funds = [
                    f for f in hishtalmut["funds"] if f.get("last_refreshed_at")
                ]
                assert len(refreshed_funds) == 1
                assert refreshed_funds[0]["fund_id"] == "1234"

        asyncio.run(_verify())
    finally:
        asyncio.run(db_mod.dispose_engine())
