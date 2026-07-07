"""GET /api/portfolio/net-worth-history — snapshot-history series route.

Home-page wealth-trajectory + deconcentration charts read this. Seeds
``portfolio_snapshots`` rows directly (same pattern as
``test_portfolio_snapshot_db_wiring``) and asserts chronology, dedupe-by-
date (freshest import wins), NVDA percentage math, and the months window.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from argosy.state.models import PortfolioSnapshotRow, User


def _seed_row(
    session,
    *,
    user_id: str = "ariel",
    snapshot_date: date,
    imported_at: datetime | None = None,
    total_usd_value_k: float = 1000.0,
    nvda_usd_value_k: float = 400.0,
) -> int:
    positions = [
        {"symbol": "NVDA", "usd_value_k": nvda_usd_value_k},
        {"symbol": "SCHD", "usd_value_k": total_usd_value_k - nvda_usd_value_k},
    ]
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date,
        imported_at=imported_at or datetime.now(timezone.utc),
        source_path="/tmp/family.tsv",
        positions_json=json.dumps(positions),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps(
            {"total_usd_value_k": total_usd_value_k, "cash_balances_usd_k": 0.0}
        ),
        fx_usd_nis=3.7,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


def test_history_returns_chronological_points_with_nvda_pct(client_with_db):
    SF = client_with_db.app.state.session_factory
    today = date.today()
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_row(
            s,
            snapshot_date=today - timedelta(days=60),
            total_usd_value_k=1000.0,
            nvda_usd_value_k=500.0,
        )
        _seed_row(
            s,
            snapshot_date=today - timedelta(days=30),
            total_usd_value_k=1200.0,
            nvda_usd_value_k=480.0,
        )

    res = client_with_db.get(
        "/api/portfolio/net-worth-history?user_id=ariel&months=12"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == "ariel"
    pts = body["points"]
    assert [p["date"] for p in pts] == [
        (today - timedelta(days=60)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
    ]
    assert pts[0]["total_usd"] == 1_000_000.0
    assert pts[0]["nvda_pct"] == 50.0
    assert pts[1]["total_usd"] == 1_200_000.0
    assert pts[1]["nvda_pct"] == 40.0


def test_history_dedupes_same_date_keeping_freshest_import(client_with_db):
    SF = client_with_db.app.state.session_factory
    today = date.today()
    snap_date = today - timedelta(days=10)
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_row(
            s,
            snapshot_date=snap_date,
            imported_at=datetime.now(timezone.utc) - timedelta(hours=2),
            total_usd_value_k=900.0,
        )
        _seed_row(
            s,
            snapshot_date=snap_date,
            imported_at=datetime.now(timezone.utc),
            total_usd_value_k=950.0,
        )

    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=ariel")
    assert res.status_code == 200
    pts = res.json()["points"]
    assert len(pts) == 1
    assert pts[0]["total_usd"] == 950_000.0


def test_history_window_excludes_old_snapshots(client_with_db):
    SF = client_with_db.app.state.session_factory
    today = date.today()
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_row(s, snapshot_date=today - timedelta(days=400))
        _seed_row(s, snapshot_date=today - timedelta(days=5))

    res = client_with_db.get(
        "/api/portfolio/net-worth-history?user_id=ariel&months=12"
    )
    assert res.status_code == 200
    pts = res.json()["points"]
    assert [p["date"] for p in pts] == [(today - timedelta(days=5)).isoformat()]


def test_history_empty_for_unknown_user(client_with_db):
    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=nobody")
    assert res.status_code == 200
    assert res.json()["points"] == []
