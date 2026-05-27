"""T1.5 — verify ``/api/portfolio/snapshot`` + ``assemble_phase1_inputs``
prefer the DB-backed ``portfolio_snapshots`` row over the filesystem walk,
and that the write-through path is idempotent.

The route now depends on the sync ``get_db`` injector, so we use the
``client_with_db`` fixture (file-backed SQLite, sync session factory
exposed on ``client.app.state.session_factory``) which exercises the
same wiring real deployments use.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    persist_snapshot,
    write_through_if_changed,
)
from argosy.state.models import PortfolioSnapshotRow, User


def _seed_snapshot_row(
    session,
    *,
    user_id: str = "ariel",
    source_path: str = "/tmp/family.tsv",
    snapshot_date: date | None = date(2026, 5, 1),
    positions: list[dict] | None = None,
    total_usd_value_k: float = 1234.5,
) -> int:
    """Insert one PortfolioSnapshotRow directly. Returns the row id."""
    if positions is None:
        positions = [
            {
                "location": "Schwab",
                "currency": "USD",
                "asset_type": "stock",
                "details": "NVIDIA",
                "symbol": "NVDA",
                "shares": 1000.0,
                "current_price": 900.0,
                "current_value_local": 900_000.0,
                "usd_value_k": 900.0,
            },
            {
                "location": "Schwab",
                "currency": "USD",
                "asset_type": "etf",
                "details": "Schwab US Dividend Equity",
                "symbol": "SCHD",
                "shares": 100.0,
                "current_price": 80.0,
                "current_value_local": 8_000.0,
                "usd_value_k": 8.0,
            },
        ]
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date,
        imported_at=datetime.now(timezone.utc),
        source_path=source_path,
        positions_json=json.dumps(positions),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({
            "total_usd_value_k": total_usd_value_k,
            "cash_balances_usd_k": 0.0,
        }),
        fx_usd_nis=3.7,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


def test_portfolio_snapshot_route_serves_db_row_when_present(client_with_db):
    """When a ``portfolio_snapshots`` row exists, the route returns it
    instead of walking the filesystem."""
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_snapshot_row(s)

    res = client_with_db.get("/api/portfolio/snapshot?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    assert body["snapshot_date"] == "2026-05-01"
    assert body["source_path"] == "/tmp/family.tsv"
    assert body["fx_usd_nis"] == 3.7
    symbols = {p["symbol"] for p in body["positions"]}
    assert symbols == {"NVDA", "SCHD"}


def test_portfolio_snapshot_route_falls_back_to_filesystem_when_db_empty(
    client_with_db, tmp_path, monkeypatch,
):
    """When no row exists for the user, the route falls back to walking
    ARGOSY_HOME for the freshest TSV. We seed an ARGOSY_HOME with a
    canonical TSV file so the fallback actually finds something."""
    # Point ARGOSY_HOME at a temp dir with a valid TSV.
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()

    # Minimal TSV with the canonical header marker so _find_latest_tsv
    # accepts it. We don't need real positions for the fallback path —
    # the route just needs `_find_latest_tsv` to return a file and
    # `parse_portfolio_tsv` to produce a snapshot (parse_warnings OK).
    tsv = tmp_path / "Family Finances Status - 2026-05-15.tsv"
    tsv.write_text(
        "Family Finances Status\n"
        "Snapshot date\t2026-05-15\n"
        "FX USD/NIS\t3.65\n"
        "\n"
        "Bank account / funds allocation\n"
        "Schwab\tUSD\tcash\tcash\t-\t\t\t10.0\n",
        encoding="utf-8",
    )

    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()

    res = client_with_db.get("/api/portfolio/snapshot?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    # Either the TSV parsed cleanly (source_path set) or it parsed empty
    # (parse_warnings). Both are valid fallback outcomes; what we're
    # really asserting is that the route didn't crash on the missing-row
    # path and returned a valid shape.
    assert "snapshot_date" in body
    assert "positions" in body

    # Write-through: after the fallback, a row should exist in the DB
    # for next time.
    with SF() as s:
        row = get_latest_snapshot_row(s, "ariel")
        assert row is not None, (
            "Expected /api/portfolio/snapshot to write-through into "
            "portfolio_snapshots after a filesystem fallback."
        )

    reload_settings()


def test_write_through_if_changed_is_idempotent(client_with_db):
    """Repeated write_through calls with the same snapshot must not
    create duplicate rows."""
    SF = client_with_db.app.state.session_factory
    snap = PortfolioSnapshot(
        source_path="/tmp/family.tsv",
        snapshot_date=date(2026, 5, 1),
        fx_usd_nis=3.7,
        fx_usd_eur=4.0,
        positions=[
            PortfolioPosition(
                location="Schwab",
                currency="USD",
                asset_type="stock",
                details="NVIDIA",
                symbol="NVDA",
                shares=1000.0,
                current_price=900.0,
                current_value_local=900_000.0,
                usd_value_k=900.0,
            ),
        ],
    )
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()

        first = write_through_if_changed(s, user_id="ariel", snapshot=snap)
        assert first is not None, "First write_through must persist a row"

        second = write_through_if_changed(s, user_id="ariel", snapshot=snap)
        assert second is None, (
            "Second write_through with identical snapshot must be a no-op"
        )

        # Confirm row count is still 1.
        rows = s.query(PortfolioSnapshotRow).filter_by(user_id="ariel").all()
        assert len(rows) == 1


def test_assemble_phase1_inputs_prefers_db_row_over_filesystem(tmp_path, monkeypatch):
    """When the DB has a portfolio_snapshots row, ``assemble_phase1_inputs``
    must NOT walk the filesystem and must hydrate tickers from the row."""
    # Isolate ARGOSY_HOME to a temp dir with NO TSV files so any
    # filesystem fallback would produce an empty positions_summary.
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    from argosy.config import reload_settings

    reload_settings()

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_snapshot_row(s)

        from argosy.orchestrator.flows.plan_synthesis.inputs import (
            assemble_phase1_inputs,
        )

        out = assemble_phase1_inputs(
            s,
            user_id="ariel",
            baseline=None,
            prior_current=None,
            decision_audit_token="plan-synth-test",
        )
        # The DB row contained NVDA + SCHD positions; tickers must
        # reflect that.
        assert set(out.tickers) == {"NVDA", "SCHD"}
        # positions_summary must be populated (non-sentinel) from the
        # DB-hydrated snapshot.
        assert "NVDA" in out.positions_summary
        assert "SCHD" in out.positions_summary
    finally:
        s.close()
        engine.dispose()

    reload_settings()


@pytest.mark.asyncio
async def test_existing_async_client_route_still_returns_valid_shape(client):
    """Regression guard for the pre-existing test path: even when the
    async ``client`` fixture has no DB seeding and no ARGOSY_HOME TSV,
    the route still returns a valid empty DTO instead of crashing."""
    res = await client.get("/api/portfolio/snapshot?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    for key in (
        "snapshot_date", "fx_usd_nis", "total_usd_value_k",
        "positions", "allocations", "source_path",
    ):
        assert key in body
