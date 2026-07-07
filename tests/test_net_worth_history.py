"""GET /api/portfolio/net-worth-history — snapshot-history series route.

Home-page wealth-trajectory + deconcentration charts read this. Seeds
``portfolio_snapshots`` rows directly (same pattern as
``test_portfolio_snapshot_db_wiring``) and asserts chronology, dedupe-by-
date (freshest import wins), NVDA percentage math, and the months window.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from argosy.state.models import PortfolioSnapshotRow, User


@pytest.fixture(autouse=True)
def _no_backfill_root(monkeypatch):
    """Keep the dev machine's real archived TSVs (Google Drive) from
    leaking reconstructed points into these fixtures."""
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)


def _seed_row(
    session,
    *,
    user_id: str = "ariel",
    snapshot_date: date,
    imported_at: datetime | None = None,
    total_usd_value_k: float = 1000.0,
    nvda_usd_value_k: float = 400.0,
    stored_total_usd_value_k: float | None = None,
) -> int:
    # ``total_usd_value_k`` is the TRADEABLE (securities) sum; a $250K
    # cash row rides along on top of it. nvda_pct must be computed
    # against the tradeable book (canonical ``nvda_concentration_pct``),
    # so cash never dilutes the weight — the same denominator basis as
    # the TargetAllocationDoc glide. The served total is the
    # POSITIONS-SUM = tradeable + cash. ``stored_total_usd_value_k``
    # lets a test store a deliberately stale grand total.
    positions = [
        {"symbol": "NVDA", "asset_type": "stock", "usd_value_k": nvda_usd_value_k},
        {
            "symbol": "SCHD",
            "asset_type": "etf",
            "usd_value_k": total_usd_value_k - nvda_usd_value_k,
        },
        {"symbol": "-", "asset_type": "Cash", "usd_value_k": 250.0},
    ]
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date,
        # Same-date import by default: points label by PRICE VINTAGE
        # (imported_at's date when it differs from snapshot_date), so a
        # fixture importing "now" for a months-old snapshot would relabel
        # every point to today.
        imported_at=imported_at
        or datetime(
            snapshot_date.year, snapshot_date.month, snapshot_date.day, 12, 0
        ),
        source_path="/tmp/family.tsv",
        positions_json=json.dumps(positions),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps(
            {
                "total_usd_value_k": (
                    stored_total_usd_value_k
                    if stored_total_usd_value_k is not None
                    else total_usd_value_k + 250.0
                ),
                "cash_balances_usd_k": 250.0,
            }
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
    # Total = POSITIONS-SUM (tradeable + the $250K cash row).
    assert pts[0]["total_usd"] == 1_250_000.0
    assert pts[0]["nvda_pct"] == 50.0  # 500 / (500+500) tradeable — cash excluded
    assert pts[1]["total_usd"] == 1_450_000.0
    assert pts[1]["nvda_pct"] == 40.0
    # Delta-tooltip decomposition inputs: NVDA position value + cash
    # balances per point (USD), so the UI can attribute a book move to
    # NVDA repricing vs cash flow vs the rest of the book.
    assert pts[0]["nvda_usd"] == 500_000.0
    assert pts[1]["nvda_usd"] == 480_000.0
    assert pts[0]["cash_usd"] == 250_000.0
    assert pts[1]["cash_usd"] == 250_000.0
    # Currency dimension: each point converted at ITS OWN snapshot fx.
    assert pts[0]["fx_usd_nis"] == 3.7
    assert pts[0]["total_nis"] == 1_250_000.0 * 3.7
    assert pts[1]["total_nis"] == 1_450_000.0 * 3.7
    # No NIS-denominated positions in the fixture.
    assert pts[0]["nis_denominated_usd"] == 0.0


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
            imported_at=datetime(
                snap_date.year, snap_date.month, snap_date.day, 10, 0
            ),
            total_usd_value_k=900.0,
        )
        _seed_row(
            s,
            snapshot_date=snap_date,
            imported_at=datetime(
                snap_date.year, snap_date.month, snap_date.day, 12, 0
            ),
            total_usd_value_k=950.0,
        )

    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=ariel")
    assert res.status_code == 200
    pts = res.json()["points"]
    assert len(pts) == 1
    # Positions-sum of the freshest import: 950 tradeable + 250 cash.
    assert pts[0]["total_usd"] == 1_200_000.0


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


def test_history_total_is_positions_sum_never_stored_totals(client_with_db):
    """BASIS RULE: mixed-provenance rows (TSV / self-refresh /
    fills-applied) must all report the POSITIONS-SUM as the total — a
    stale stored grand total (the stale-allocations bug class) must not
    leak into the series."""
    SF = client_with_db.app.state.session_factory
    today = date.today()
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        row_id = _seed_row(
            s,
            snapshot_date=today - timedelta(days=3),
            total_usd_value_k=1000.0,  # positions sum to 1000 + 250 cash
            nvda_usd_value_k=400.0,
            stored_total_usd_value_k=999.0,  # deliberately stale stored total
        )
        # Sanity: the seeded stored total disagrees with the rows.
        row = s.get(PortfolioSnapshotRow, row_id)
        assert json.loads(row.totals_json)["total_usd_value_k"] == 999.0

    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=ariel")
    assert res.status_code == 200
    pt = res.json()["points"][0]
    # NVDA 400 + SCHD 600 + cash 250 = 1250 (positions-sum), not 999.
    assert pt["total_usd"] == 1_250_000.0
    assert pt["total_nis"] == 1_250_000.0 * 3.7


def test_history_labels_by_price_vintage_when_import_postdates_snapshot(
    client_with_db,
):
    """PRICE-VINTAGE labeling: a TSV stamped Jun-29 but exported/ingested
    Jun-30 embeds Jun-30 prices — the point must plot at the vintage
    (imported_at's date) with the stamped snapshot_date carried for
    reference, and the VALUES untouched. Same-date rows keep their
    snapshot_date label with no separate vintage."""
    SF = client_with_db.app.state.session_factory
    today = date.today()
    stamped = today - timedelta(days=8)
    vintage = today - timedelta(days=7)
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_row(
            s,
            snapshot_date=stamped,
            imported_at=datetime(
                vintage.year, vintage.month, vintage.day, 16, 25
            ),
            total_usd_value_k=1000.0,
        )
        _seed_row(s, snapshot_date=today - timedelta(days=2))

    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=ariel")
    assert res.status_code == 200
    pts = res.json()["points"]
    assert [p["date"] for p in pts] == [
        vintage.isoformat(),
        (today - timedelta(days=2)).isoformat(),
    ]
    # The stamped date rides along for the tooltip's "prices as of" note.
    assert pts[0]["snapshot_date"] == stamped.isoformat()
    # Values are NOT adjusted — only the time label moved.
    assert pts[0]["total_usd"] == 1_250_000.0
    # Same-date row: label == snapshot_date, nothing to flag.
    assert pts[1]["snapshot_date"] == pts[1]["date"]


def test_history_empty_for_unknown_user(client_with_db):
    res = client_with_db.get("/api/portfolio/net-worth-history?user_id=nobody")
    assert res.status_code == 200
    assert res.json()["points"] == []
