"""STREAM D — managed/unmanaged book + abstention tests (iteration 2).

Every CRITICAL/HIGH fix has a test that FAILS for the right reason when
reverted. Fixture DB only — never touches live ``db/argosy.db``.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.stock_decision import (
    abstain_insufficient_evidence,
    bundle_has_sufficient_evidence,
    decide_stock,
    evidence_field_is_usable,
)
from argosy.ingest.tsv import PortfolioSnapshot
from argosy.services.allocation_breakdown import build_allocation_breakdown
from argosy.services.holding_books import (
    QUANTITY_STALE_DAYS,
    STALE_VALUATION_DAYS,
    SnapshotIngestRejected,
    TotalBookDegraded,
    assess_snapshot_ingest,
    backfill_unmanaged_from_snapshots,
    books_consistency_check,
    books_consistency_check_positions,
    dedupe_positions_by_symbol_location,
    implied_nvda_weight_frac,
    is_managed_position,
    load_total_and_managed_books,
    load_total_book,
    location_account_key,
    managed_positions,
    merge_total_book_positions,
    parse_explicit_managed_flag,
    positions_for_books,
    quantity_is_stale,
    retire_unmanaged_account,
    sync_unmanaged_from_positions,
    symbol_value_usd_k,
    valuation_is_stale,
)
from argosy.services.plan_numeric_resolver import ResolvedValue
from argosy.services.portfolio_snapshot_store import persist_snapshot
from argosy.services.retirement.fi_shock import (
    PRIMARY_NVDA_SHOCK,
    derive_nvda_shock_inputs,
    primary_nvda_shock_net_worth_nis,
)
from argosy.services.retirement.safety_gates import (
    _us_situs_assets_usd,
    compute_nra_estate_gate,
)
from argosy.services.stock_decision import run_holdings_review
from argosy.services.wealth_dashboard import (
    _estate_exposure,
    _total_book_positions,
    nvda_concentration_pct,
    tradeable_securities_usd_k,
)
from argosy.state.models import (
    Base,
    HoldingReview,
    KvCacheEntry,
    PortfolioSnapshotRow,
    PositionStance,
    UnmanagedHolding,
    UnmanagedSymbolPolicy,
    User,
)


def _pos(
    symbol: str,
    usd_value_k: float,
    *,
    shares: float | None = None,
    price: float | None = None,
    managed: bool | None = None,
    asset_type: str = "Stock",
    details: str = "Stock",
    location: str = "schwab",
    currency: str = "USD",
    review_status: str = "",
) -> dict:
    d = {
        "symbol": symbol,
        "usd_value_k": usd_value_k,
        "shares": shares,
        "current_price": price,
        "asset_type": asset_type,
        "details": details,
        "location": location,
        "currency": currency,
        "review_status": review_status,
    }
    if managed is not None:
        d["managed"] = managed
        d["excluded_from_sleeve_math"] = not managed
    return d


class _FakeUnmanaged:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        if not hasattr(self, "status"):
            self.status = "active"


@pytest.fixture()
def fixture_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stream_d_fixture.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    session.add(User(id="ariel"))
    session.commit()
    # Live reprice is mandatory for durable restores — inject a deterministic
    # quote so tests never hit yfinance / hang on network.
    monkeypatch.setattr(
        "argosy.services.snapshot_refresh.default_quote_fn",
        lambda symbol, **kw: 180.0 if str(symbol).upper() == "NVDA" else 100.0,
    )
    yield session
    session.close()
    engine.dispose()


def _nvda_quote(price: float = 180.0):
    return lambda symbol, **kw: price if str(symbol).upper() == "NVDA" else None


def test_merge_restores_nvda_omitted_from_snapshot():
    snapshot = [_pos("CSPX", 400.0)]
    row = _FakeUnmanaged(
        symbol="NVDA",
        shares=10_000.0,
        current_price=230.0,
        usd_value_k=2300.0,
        currency="USD",
        location="schwab 876",
        asset_type="Stock",
        details="Stock, AI",
        reason="excluded_from_sleeve_math",
        valued_as_of=date.today(),
        observed_as_of=date.today(),
    )
    total = merge_total_book_positions(
        snapshot, unmanaged_rows=[row], quote_fn=_nvda_quote(230.0),
    )
    assert symbol_value_usd_k(total, "NVDA") == 2300.0


# ---------------------------------------------------------------------------
# Finding 1 — quantity durable + live reprice (not a stale-price threshold)
# ---------------------------------------------------------------------------


def test_backfill_persists_observed_as_of(fixture_db):
    _add_snap(
        fixture_db,
        positions=[
            _pos("CSPX", 400.0, location="ibi"),
            _pos("NVDA", 2300.0, shares=10000, price=230, location="schwab 876"),
        ],
        snap_date=date(2026, 7, 11),
    )
    n = backfill_unmanaged_from_snapshots(fixture_db, user_id="ariel")
    assert n >= 1
    row = fixture_db.execute(
        select(UnmanagedHolding).where(
            UnmanagedHolding.user_id == "ariel",
            UnmanagedHolding.symbol == "NVDA",
            UnmanagedHolding.status == "active",
        )
    ).scalar_one()
    assert row.observed_as_of == date(2026, 7, 11)
    assert row.valued_as_of == date(2026, 7, 11)
    assert float(row.shares) == 10000.0


def test_25day_old_quantity_reprices_to_live_valuation(fixture_db):
    """Live-shaped: snap 48 incomplete + NVDA last seen 25 days ago.

    Quantity (10,940) is still trusted; value comes from LIVE quote — never
    the July $2.308M mark. Simulates what a user must see today.
    """
    today = date(2026, 8, 7)
    observed = date(2026, 7, 13)  # 25 days old — within QUANTITY_STALE_DAYS=90
    assert (today - observed).days == 25
    assert quantity_is_stale(observed, today=today) is False

    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=10_940.0, current_price=211.0, usd_value_k=2307.9,
        currency="USD", asset_type="Stock", details="Stock, AI",
        reason="backfill", status="active",
        valued_as_of=observed, observed_as_of=observed,
    ))
    fixture_db.commit()
    # Latest snap = Leumi-only (live id 48 shape).
    incomplete = [_pos("CSPX", 1608.0, location="leumi", details="UCITS ETF")]
    live_px = 180.0
    book = load_total_book(
        fixture_db, "ariel", incomplete, today=today,
        snapshot_date=today,
        quote_fn=_nvda_quote(live_px),
    )
    assert book.degraded is False, book.degrade_reason
    nvda = next(p for p in book.total if p["symbol"] == "NVDA")
    assert float(nvda["shares"]) == 10_940.0
    assert float(nvda["current_price"]) == live_px
    expected_k = 10_940.0 * live_px / 1000.0
    assert float(nvda["usd_value_k"]) == pytest.approx(expected_k)
    # Must NOT be the stale July mark.
    assert float(nvda["usd_value_k"]) != pytest.approx(2307.9)


def test_quote_miss_degrades_loudly(fixture_db):
    today = date(2026, 8, 7)
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=10_940.0, current_price=211.0, usd_value_k=2307.9,
        currency="USD", asset_type="Stock", details="Stock",
        reason="backfill", status="active",
        valued_as_of=date(2026, 7, 13), observed_as_of=date(2026, 7, 13),
    ))
    fixture_db.commit()
    book = load_total_book(
        fixture_db, "ariel",
        [_pos("CSPX", 400.0, shares=10, price=40, location="ibi")],
        today=today,
        snapshot_date=today,
        quote_fn=lambda *a, **k: None,  # infrastructure failure
    )
    assert book.degraded is True
    assert "reprice" in (book.degrade_reason or "").lower()
    assert symbol_value_usd_k(book.total, "NVDA") == 0.0


def test_quantity_older_than_90_days_degrades(fixture_db):
    today = date(2026, 8, 7)
    old = date(2026, 4, 1)  # >90 days
    assert quantity_is_stale(old, today=today) is True
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=10_940.0, current_price=211.0, usd_value_k=2307.9,
        currency="USD", asset_type="Stock", details="Stock",
        reason="backfill", status="active",
        valued_as_of=old, observed_as_of=old,
    ))
    fixture_db.commit()
    book = load_total_book(
        fixture_db, "ariel",
        [_pos("CSPX", 400.0, shares=10, price=40, location="ibi")],
        today=today, snapshot_date=today, quote_fn=_nvda_quote(180.0),
    )
    assert book.degraded is True
    assert "stale" in (book.degrade_reason or "").lower() or "quantity" in (
        book.degrade_reason or ""
    ).lower() or "observed" in (book.degrade_reason or "").lower()


def test_fresh_durable_restores_nvda(fixture_db):
    today = date(2026, 8, 7)
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab 876",
        shares=10000.0, current_price=230.0, usd_value_k=2300.0,
        currency="USD", asset_type="Stock", details="Stock",
        reason="observed", status="active",
        valued_as_of=date(2026, 8, 1), observed_as_of=date(2026, 8, 1),
    ))
    fixture_db.commit()
    book = load_total_book(
        fixture_db, "ariel",
        [_pos("CSPX", 410.0, shares=10, price=41, location="ibi")],
        today=today, snapshot_date=today, quote_fn=_nvda_quote(230.0),
    )
    assert book.degraded is False
    assert symbol_value_usd_k(book.total, "NVDA") == pytest.approx(2300.0)

def _add_snap(session: Session, *, positions, snap_date, fx=3.0, totals_k=None):
    if totals_k is None:
        totals_k = sum(float(p.get("usd_value_k") or 0) for p in positions)
    row = PortfolioSnapshotRow(
        user_id="ariel",
        snapshot_date=snap_date,
        imported_at=datetime.now(timezone.utc),
        source_path=f"fixture-{snap_date}.tsv",
        positions_json=json.dumps(positions),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({"total_usd_value_k": totals_k}),
        fx_usd_nis=fx,
        fx_usd_eur=None,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _pydantic_snap(positions, snap_date, *, fx=3.0):
    from argosy.ingest.tsv import PortfolioPosition

    return PortfolioSnapshot(
        source_path="fixture.tsv",
        snapshot_date=snap_date,
        fx_usd_nis=fx,
        fx_usd_eur=None,
        positions=[PortfolioPosition(**p) for p in positions],
        real_estate=[],
        allocations=[],
        nvda_sales=[],
        pensions=[],
        parse_warnings=[],
    )


# ---------------------------------------------------------------------------
# Book helpers
# ---------------------------------------------------------------------------


def test_nvda_is_unmanaged_by_convention():
    assert is_managed_position(_pos("CSPX", 100.0)) is True
    assert is_managed_position(_pos("NVDA", 2300.0)) is False
    assert is_managed_position(_pos("NVDA", 2300.0, managed=True)) is True
    assert is_managed_position(_pos("FOO", 10.0, managed=False)) is False


def test_tsv_review_status_override_is_reachable():
    assert parse_explicit_managed_flag(
        _pos("NVDA", 1.0, review_status="managed")
    ) is True
    assert parse_explicit_managed_flag(
        _pos("CSPX", 1.0, review_status="unmanaged")
    ) is False


def test_sleeve_excludes_nvda_total_includes_nvda():
    positions = [
        _pos("CSPX", 400.0),
        _pos("NVDA", 2300.0),
        _pos("IBTA", 100.0, asset_type="Cash", details="Cash"),
    ]
    total, managed = positions_for_books(positions)
    assert symbol_value_usd_k(total, "NVDA") == 2300.0
    assert symbol_value_usd_k(managed, "NVDA") == 0.0
    assert tradeable_securities_usd_k(managed) == 400.0


def test_us_situs_includes_unmanaged_nvda():
    positions = [
        _pos("CSPX", 100.0, details="UCITS ETF"),
        _pos("NVDA", 2300.0, details="Stock, AI"),
    ]
    us = _us_situs_assets_usd(positions, exclude_nvda=False)
    assert us == 2300.0 * 1000.0


def test_location_account_key_is_full_identity():
    """Finding 2 — schwab 876 ≠ schwab 999 (not first-token family)."""
    assert location_account_key("schwab 876") == "schwab 876"
    assert location_account_key("Schwab 999") == "schwab 999"
    assert location_account_key("schwab 876") != location_account_key("schwab 999")
# ---------------------------------------------------------------------------
# Finding 2 — lifecycle both directions
# ---------------------------------------------------------------------------


def test_partial_feed_does_not_retire_other_schwab_account(fixture_db):
    """Feed with schwab 999 must NOT retire NVDA still held at schwab 876."""
    session = fixture_db
    session.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    session.commit()
    sync_unmanaged_from_positions(
        session, "ariel",
        [
            _pos("NVDA", 2300.0, shares=100, price=23, location="schwab 876"),
            _pos("CSPX", 100.0, location="schwab 876"),
        ],
        valued_as_of=date(2026, 8, 7),
        commit=True,
    )
    # Partial feed: different Schwab account present, 876 absent entirely.
    sync_unmanaged_from_positions(
        session, "ariel",
        [_pos("AAPL", 50.0, location="schwab 999")],
        valued_as_of=date(2026, 8, 7),
        commit=True,
    )
    active = session.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "active")
    ).scalars().all()
    assert len(active) == 1
    assert active[0].symbol == "NVDA"
    assert "876" in (active[0].location or "")


def test_observed_sale_at_same_account_retires(fixture_db):
    session = fixture_db
    session.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    session.commit()
    sync_unmanaged_from_positions(
        session, "ariel",
        [
            _pos("NVDA", 2300.0, shares=100, price=23, location="schwab 876"),
            _pos("CSPX", 100.0, location="schwab 876"),
        ],
        valued_as_of=date(2026, 8, 7),
        commit=True,
    )
    sync_unmanaged_from_positions(
        session, "ariel",
        [_pos("CSPX", 100.0, location="schwab 876")],
        valued_as_of=date(2026, 8, 7),
        commit=True,
    )
    active = session.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "active")
    ).scalars().all()
    assert active == []
    retired = session.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "retired")
    ).scalars().all()
    assert len(retired) == 1


def test_closed_account_via_explicit_retire(fixture_db):
    """Account closure is account-scoped — not a blunt catastrophic flag."""
    session = fixture_db
    session.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    session.commit()
    sync_unmanaged_from_positions(
        session, "ariel",
        [
            _pos("NVDA", 2300.0, shares=100, price=23, location="schwab 876"),
            _pos("NVDA", 500.0, shares=20, price=25, location="schwab 999"),
        ],
        valued_as_of=date(2026, 8, 7),
        commit=True,
    )
    # Incomplete feed must KEEP both (and must reject blunt retire flag).
    with pytest.raises(ValueError, match="retire_unmanaged_account"):
        sync_unmanaged_from_positions(
            session, "ariel",
            [_pos("CSPX", 100.0, location="ibi")],
            valued_as_of=date(2026, 8, 7),
            retire_absent_accounts=True,
            commit=True,
        )
    # Explicit single-account closure retires only 876.
    result = retire_unmanaged_account(
        session, "ariel",
        account_location="schwab 876",
        reason="account closed at Schwab",
        actor="test",
        commit=True,
    )
    assert result["retired"] == 1
    active = session.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "active")
    ).scalars().all()
    assert len(active) == 1
    assert "999" in (active[0].location or "")


def test_persist_partial_feed_merges_per_account_without_override(fixture_db):
    """Task 1 — Leumi-only feed must carry Schwab/Aborad, not wipe them."""
    from argosy.state.models import AuditLog

    full = [
        _pos("NVDA", 2300.0, shares=10940, price=210, location="schwab"),
        _pos("BMY", 5.8, shares=100, price=58, location="schwab 876"),
        _pos("-", 69.0, shares=3, price=None, location="Aborad", asset_type="Other"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
        _pos("NKE", 6.7, shares=150, price=44, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 7, 13)),
    )
    leumi_only = [
        _pos("CSPX", 410.0, shares=100, price=410, location="Leumi"),
        # NKE sold — genuine disappearance within covered account
    ]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(leumi_only, date(2026, 8, 8)),
    )
    pos = json.loads(row.positions_json)
    totals = json.loads(row.totals_json)
    syms = {(p.get("symbol") or "").upper(): p for p in pos}
    assert "NVDA" in syms
    assert float(syms["NVDA"]["shares"]) == 10940
    assert any((p.get("symbol") or "") == "BMY" for p in pos)
    assert any(
        (p.get("symbol") or "") == "-" and "aborad" in (p.get("location") or "").lower()
        for p in pos
    )
    assert not any((p.get("symbol") or "").upper() == "NKE" for p in pos)
    assert "leumi" in totals.get("accounts_covered", [])
    assert "schwab" in totals.get("accounts_carried", [])
    assert any(p.get("carried_forward") for p in pos if (p.get("symbol") or "").upper() == "NVDA")


def test_persist_rename_not_scored_as_sale(fixture_db):
    """מחקה ת\"א-200 → ת\"א-200 is a rename, not a sale+buy."""
    prior = [
        _pos('מחקה ת"א-200', 39.0, shares=80000, price=147.5, location="Leumi",
             currency="NIS", asset_type="Core Equity"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(prior, date(2026, 7, 13)),
    )
    incoming = [
        _pos('ת"א-200', 40.0, shares=80000, price=150.5, location="Leumi",
             currency="NIS", asset_type="Core Equity"),
        _pos("CSPX", 410.0, shares=100, price=410, location="Leumi"),
    ]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(incoming, date(2026, 8, 8)),
    )
    warns = json.loads(row.parse_warnings_json or "[]")
    assert any("SYMBOL_RENAME" in w for w in warns)
    pos = json.loads(row.positions_json)
    assert sum(1 for p in pos if "ת\"א-200" in str(p.get("symbol") or "")) == 1
    assert not any('מחקה' in str(p.get("symbol") or "") for p in pos)


def test_rejected_ingest_writes_audit_log(fixture_db):
    """Task 4 — rejection is durable + carries actor/reason/bypasses."""
    from argosy.state.models import AuditLog

    full = [_pos(f"T{i}", 20.0, location="schwab") for i in range(10)]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    with pytest.raises(SnapshotIngestRejected):
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(full, date(2026, 6, 1)),
            actor="test-actor",
            override_reason=None,
        )
    rows = fixture_db.execute(
        select(AuditLog).where(AuditLog.event_type == "snapshot.ingest.rejected")
    ).scalars().all()
    assert len(rows) >= 1
    payload = json.loads(rows[-1].payload_json)
    assert payload["code"] == "stale_snapshot_date"
    assert payload.get("actor") == "test-actor"
    assert payload.get("allow_stale") is False


def test_override_requires_reason(fixture_db):
    full = [_pos(f"T{i}", 20.0, location="schwab") for i in range(10)]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    with pytest.raises(SnapshotIngestRejected) as ei:
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(full, date(2026, 6, 1)),
            allow_stale=True,
            override_reason="",
        )
    assert ei.value.code == "override_reason_required"


def test_present_stale_nvda_mark_must_reprice_or_degrade(fixture_db):
    """Task 4 CRITICAL — NVDA present in snap with July mark cannot publish as-is."""
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    july = date(2026, 7, 13)
    positions = [
        _pos("CSPX", 400.0, shares=10, price=40, location="ibi"),
        _pos("NVDA", 2307.902, shares=10940, price=210.96, location="schwab"),
    ]
    # Stamp July mark dates explicitly (as a stale allow_stale re-import would).
    for p in positions:
        p["valued_as_of"] = july
        p["observed_as_of"] = july
    # Quote miss for NVDA → must degrade, must NOT publish 2307.902
    book = load_total_book(
        fixture_db, "ariel", positions,
        today=date(2026, 8, 8),
        quote_fn=lambda symbol, **kw: None,
        snapshot_date=july,
    )
    assert book.degraded is True
    nvda = next((p for p in book.total if p.get("symbol") == "NVDA"), None)
    assert nvda is not None
    assert nvda.get("usd_value_k") in (None, 0) or nvda.get("mark_stale") is True
    assert symbol_value_usd_k(book.total, "NVDA") == 0.0


def test_present_stale_nvda_mark_reprices_when_quote_available(fixture_db):
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    july = date(2026, 7, 13)
    positions = [
        _pos("NVDA", 2307.902, shares=10940, price=210.96, location="schwab"),
    ]
    for p in positions:
        p["valued_as_of"] = july
        p["observed_as_of"] = july
    live = 180.0
    book = load_total_book(
        fixture_db, "ariel", positions,
        today=date(2026, 8, 8),
        quote_fn=_nvda_quote(live),
        snapshot_date=july,
    )
    assert book.degraded is False
    nvda = next(p for p in book.total if p["symbol"] == "NVDA")
    assert float(nvda["current_price"]) == live
    assert float(nvda["usd_value_k"]) == pytest.approx(10940 * live / 1000.0)
    assert float(nvda["usd_value_k"]) != pytest.approx(2307.902)


def test_upload_ordinary_path_does_not_require_admin(fixture_db, monkeypatch):
    """Task 2 — ordinary upload must not 401 when admin token is unset."""
    from io import BytesIO

    from fastapi import UploadFile

    from argosy.api.routes import portfolio as portfolio_routes

    monkeypatch.setattr(
        portfolio_routes, "_resolve_snapshot_root",
        lambda: __import__("pathlib").Path(str(fixture_db.get_bind().url).replace("sqlite:///", "")).parent,
    )
    monkeypatch.setattr(
        portfolio_routes, "run_windfall_detection_on_snapshot",
        lambda *a, **k: SimpleNamespace(event=None, plan=None, detect_status="skipped"),
    )
    monkeypatch.setattr(portfolio_routes, "_warm_derived_cache", lambda *a, **k: None)
    snap = _pydantic_snap(
        [_pos("CSPX", 400.0, shares=10, price=40, location="Leumi")],
        date(2026, 8, 8),
    )
    monkeypatch.setattr(portfolio_routes, "parse_portfolio_tsv", lambda *a, **k: snap)
    tsv = b"Bank account / funds allocation\nSymbol\tShares\nCSPX\t10\n"
    upload = UploadFile(filename="ok.tsv", file=BytesIO(tsv))
    resp = portfolio_routes.upload_snapshot(
        file=upload, user_id="ariel", fire_detector=False,
        allow_stale=False, allow_catastrophic_drop=False,
        override_reason="", x_argosy_admin=None, db=fixture_db,
    )
    assert resp.tsv_persisted is True


def test_resolver_sell_shares_use_tradeable_denominator(fixture_db):
    """Task 3 — exercise real resolver path; target is live IPS 8%, not 12%.

    Ultimate endpoint (8% of tradeable book) is DISTINCT from this year's
    adjudicated glide quota (``concentration.nvda_quota_tax_year_sh``).
    """
    from argosy.services.allocation_plan import NVDA_TARGET_PCT
    from argosy.services.plan_numeric_resolver import (
        ResolvedValue as RV,
        _apply_nvda_deconcentration,
    )
    from argosy.state.models import UnmanagedHolding

    assert NVDA_TARGET_PCT == 8.0

    # Tradeable book $3.554M at $180 → NVDA 10,940 sh = 55.41% weight.
    # target_sh = floor(0.08 * 3.554e6 / 180) = floor(1579.55) = 1579
    # sell = 10940 - 1579 = 9361
    nvda_sh, nvda_px = 10_940, 180.0
    tradeable_usd = 3_554_000.0
    nvda_usd = nvda_sh * nvda_px
    assert abs(nvda_usd / tradeable_usd - 0.5541) < 1e-3

    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=float(nvda_sh), current_price=nvda_px,
        usd_value_k=nvda_usd / 1000.0, currency="USD",
        asset_type="Stock", details="Stock", reason="observed",
        status="active",
        valued_as_of=date(2026, 8, 8), observed_as_of=date(2026, 8, 8),
    ))
    other_usd_k = (tradeable_usd - nvda_usd) / 1000.0
    _add_snap(
        fixture_db,
        positions=[
            _pos("CSPX", other_usd_k, shares=other_usd_k, price=1.0, location="Leumi"),
        ],
        snap_date=date(2026, 8, 8),
        totals_k=tradeable_usd / 1000.0,
        fx=3.0,
    )

    values = {
        "concentration.nvda_current_pct": RV.excluded(
            "concentration.nvda_current_pct", "pct", "unmanaged",
        ),
        "concentration.nvda_value_nis": RV(
            "concentration.nvda_value_nis", nvda_usd * 3.0, "nis", "resolved", "t",
        ),
        "concentration.nvda_cap_pct": RV(
            "concentration.nvda_cap_pct", 0.13, "pct", "resolved", "t",
        ),
    }
    _apply_nvda_deconcentration(fixture_db, "ariel", values)

    sell = values["concentration.nvda_sell_sh"]
    target = values["concentration.nvda_target_sh"]
    target_pct = values["concentration.nvda_target_pct"]
    assert target_pct.status == "resolved"
    assert float(target_pct.value) == pytest.approx(0.08)
    assert sell.status == "resolved"
    assert target.status == "resolved"
    assert int(target.value) == 1579
    assert int(sell.value) == 9361
    # Quota key is a SEPARATE adjudicated figure — pending here (no verdict).
    assert "concentration.nvda_quota_tax_year_sh" not in values or (
        values.get("concentration.nvda_quota_tax_year_sh") is None
    )


def test_persist_catastrophic_override_does_not_retire_absent_account(fixture_db):
    full = [
        _pos("NVDA", 2300.0, shares=100, price=23, location="schwab 876"),
        *[_pos(f"T{i}", 20.0, shares=1, price=20, location="ibi") for i in range(10)],
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 1)),
    )
    # Partial feed covering only ibi — merge carries schwab; no override needed.
    closed = [_pos(f"T{i}", 20.0, shares=1, price=20, location="ibi") for i in range(10)]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(closed, date(2026, 8, 7)),
    )
    totals = json.loads(row.totals_json)
    assert "schwab 876" in totals.get("accounts_carried", [])
    active = fixture_db.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "active")
    ).scalars().all()
    assert len(active) == 1
    assert active[0].symbol == "NVDA"


def test_same_account_catastrophic_drop_still_rejected(fixture_db):
    """Within a covered account, wiping most names is still catastrophic."""
    full = [_pos(f"T{i}", 50.0, shares=1, price=50, location="Leumi") for i in range(12)]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    tiny = [_pos("T0", 50.0, shares=1, price=50, location="Leumi")]
    with pytest.raises(SnapshotIngestRejected) as ei:
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(tiny, date(2026, 8, 8)),
        )
    assert ei.value.code == "catastrophic_position_drop"

# ---------------------------------------------------------------------------
# Finding 3 — degraded never renders as 0.0 money
# ---------------------------------------------------------------------------


def test_load_total_book_degraded_when_empty_and_incomplete(fixture_db):
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    incomplete = [_pos("CSPX", 400.0, location="ibi")]
    book = load_total_book(fixture_db, "ariel", incomplete)
    assert book.degraded is True
    assert book.degrade_reason


def test_estate_gate_refuses_degraded_with_none_not_zero(fixture_db):
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],
        snap_date=date(2026, 8, 7),
        totals_k=400.0,
    )
    gate = compute_nra_estate_gate(session=fixture_db, user_id="ariel")
    assert gate.status == "FAIL"
    assert gate.value.value is None
    assert gate.value.value != 0.0


def test_estate_surface_unavailable_when_degraded(fixture_db):
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    snap = _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],
        snap_date=date(2026, 8, 7),
        totals_k=400.0,
    )
    block = _estate_exposure(
        snapshot=snap, fx_usd_nis=3.0,
        session=fixture_db, user_id="ariel",
    )
    assert block.us_situs_usd is None
    assert any("unavailable" in r.lower() or "degrad" in r.lower()
               for r in block.missing_reasons)


def test_incremental_graph_raises_on_degraded_not_zero(fixture_db):
    # A GENUINELY stale book (weeks old, unpriceable mark, no live quote) must
    # degrade LOUDLY — the incremental graph raises rather than zeroing net
    # worth. (A normal weekend/holiday-fresh book now degrades gracefully; this
    # test pins the hard-stale end of the graduated rule.)
    _snapshot_positions_fx = __import__(
        "argosy.orchestrator.flows.incremental_plan",
        fromlist=["_snapshot_positions_fx"],
    )._snapshot_positions_fx
    _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],  # no shares -> unpriceable
        snap_date=date.today() - timedelta(days=60),  # weeks-stale -> hard
        totals_k=400.0,
    )

    with pytest.raises(TotalBookDegraded):
        _snapshot_positions_fx(fixture_db, "ariel")


# ---------------------------------------------------------------------------
# Finding 4 — ingest rejection is loud; overrides reachable + audited
# ---------------------------------------------------------------------------


def test_assess_rejects_stale_snapshot_date(fixture_db):
    latest = _add_snap(
        fixture_db,
        positions=[_pos(f"T{i}", 10.0) for i in range(10)],
        snap_date=date(2026, 8, 7),
    )
    with pytest.raises(SnapshotIngestRejected) as ei:
        assess_snapshot_ingest(
            latest_row=latest,
            new_positions=[_pos("T0", 10.0)],
            new_snapshot_date=date(2026, 6, 1),
        )
    assert ei.value.code == "stale_snapshot_date"


def test_persist_snapshot_raises_on_stale(fixture_db):
    full = [_pos(f"T{i}", 20.0, location="schwab") for i in range(10)]
    full.append(_pos("NVDA", 2300.0, shares=1, price=1, location="schwab"))
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    with pytest.raises(SnapshotIngestRejected):
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(
                [_pos("T0", 20.0, location="ibi")], date(2026, 6, 1),
            ),
        )


def test_upload_surfaces_ingest_rejection_as_error(fixture_db, monkeypatch):
    """Finding 4 — rejected write-through must return tsv_persisted=False."""
    from fastapi import UploadFile
    from io import BytesIO

    from argosy.api.routes import portfolio as portfolio_routes

    full = [_pos(f"T{i}", 20.0, location="schwab") for i in range(12)]
    full.append(_pos("NVDA", 2300.0, shares=1, price=1, location="schwab"))
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )

    # Minimal bytes that pass the header-marker gate; parse is mocked below.
    tsv = (
        "Bank account / funds allocation\n"
        "Symbol\tShares\tPrice\n"
        "T0\t1\t20\n"
    ).encode("utf-8")

    monkeypatch.setattr(
        portfolio_routes, "_resolve_snapshot_root",
        lambda: __import__("pathlib").Path(str(fixture_db.get_bind().url).replace("sqlite:///", "")).parent,
    )
    monkeypatch.setattr(
        portfolio_routes, "run_windfall_detection_on_snapshot",
        lambda *a, **k: SimpleNamespace(event=None, plan=None, detect_status="skipped"),
    )
    monkeypatch.setattr(portfolio_routes, "_warm_derived_cache", lambda *a, **k: None)

    # Force parse to a same-account catastrophic snap (Leumi wiped).
    bad_snap = _pydantic_snap(
        [_pos("T0", 20.0, location="schwab"), _pos("T1", 20.0, location="schwab")],
        date(2026, 8, 8),
    )
    monkeypatch.setattr(
        portfolio_routes, "parse_portfolio_tsv", lambda *a, **k: bad_snap,
    )

    upload = UploadFile(filename="bad.tsv", file=BytesIO(tsv))
    resp = portfolio_routes.upload_snapshot(
        file=upload, user_id="ariel", fire_detector=False,
        allow_stale=False, allow_catastrophic_drop=False,
        override_reason="", x_argosy_admin=None, db=fixture_db,
    )
    assert resp.tsv_persisted is False
    assert resp.detail is not None
    assert "INGEST REJECTED" in resp.detail
    assert "catastrophic" in resp.detail.lower()


def test_allow_stale_override_audited_on_row(fixture_db):
    full = [_pos(f"T{i}", 20.0, location="schwab") for i in range(10)]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 7, 1)),
        allow_stale=True,
        actor="test-admin",
        override_reason="deliberate re-import of June book for audit",
    )
    warns = json.loads(row.parse_warnings_json or "[]")
    assert any("allow_stale=True" in w for w in warns)
    assert any("reason=deliberate re-import" in w for w in warns)


# ---------------------------------------------------------------------------
# Finding 5 — NVDA weight denominator = tradeable securities
# ---------------------------------------------------------------------------


def test_implied_nvda_weight_uses_tradeable_not_net_worth():
    class _R:
        def __init__(self, d):
            self._d = d

        def get(self, key):
            return self._d.get(key)

    nvda_nis = 6_900_000.0
    tradeable = 10_000_000.0  # securities book
    net_worth = 13_800_000.0  # wrong denom (cash+property)
    resolved = _R({
        "concentration.nvda_current_pct": ResolvedValue.excluded(
            "concentration.nvda_current_pct", "pct", "unmanaged",
        ),
        "concentration.nvda_value_nis": ResolvedValue(
            "concentration.nvda_value_nis", nvda_nis, "nis", "resolved", "t",
        ),
        "portfolio.net_worth_nis": ResolvedValue(
            "portfolio.net_worth_nis", net_worth, "nis", "resolved", "t",
        ),
    })
    # Without explicit tradeable denom → refuse (None), never NW fallback.
    assert implied_nvda_weight_frac(resolved) is None
    w = implied_nvda_weight_frac(resolved, tradeable_securities_nis=tradeable)
    assert w == pytest.approx(nvda_nis / tradeable)
    assert w != pytest.approx(nvda_nis / net_worth)


def test_nvda_concentration_pct_matches_tradeable_book():
    positions = [
        _pos("CSPX", 400.0),
        _pos("NVDA", 2300.0),
        _pos("CASH", 500.0, asset_type="Cash", details="Cash", location="ibi"),
    ]
    pct = nvda_concentration_pct(positions)
    tradeable = tradeable_securities_usd_k(positions)
    assert tradeable == pytest.approx(2700.0)
    assert pct == pytest.approx(2300.0 / 2700.0 * 100.0)


# ---------------------------------------------------------------------------
# Finding 6 — duplicates + live consistency check
# ---------------------------------------------------------------------------


def test_dedupe_keeps_first_does_not_sum():
    raw = [
        _pos("NVDA", 2300.0, shares=10000, location="schwab"),
        _pos("NVDA", 2300.0, shares=10000, location="schwab"),
        _pos("CSPX", 400.0, location="ibi"),
    ]
    deduped = dedupe_positions_by_symbol_location(raw)
    assert symbol_value_usd_k(deduped, "NVDA") == 2300.0  # NOT 4600
    assert len([p for p in deduped if p["symbol"] == "NVDA"]) == 1


def test_consistency_check_catches_duplicates():
    with pytest.raises(AssertionError, match="duplicate"):
        books_consistency_check_positions([
            _pos("NVDA", 2300.0, location="schwab"),
            _pos("NVDA", 2300.0, location="schwab"),
        ])


def test_load_total_book_degrades_on_duplicate_rows(fixture_db):
    """Live path: raw duplicate → degraded; published total is NOT doubled."""
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    raw = [
        _pos("NVDA", 2300.0, shares=10000, location="schwab", details="Stock, AI"),
        _pos("NVDA", 2300.0, shares=10000, location="schwab", details="Stock, AI"),
        _pos("CSPX", 400.0, location="ibi", details="UCITS ETF"),
    ]
    book = load_total_book(
        fixture_db, "ariel", raw,
        today=date(2026, 8, 7), snapshot_date=date(2026, 8, 7),
    )
    assert book.degraded is True
    assert "duplicate" in (book.degrade_reason or "").lower()
    assert symbol_value_usd_k(book.total, "NVDA") == 2300.0  # first only
    with pytest.raises(TotalBookDegraded):
        load_total_and_managed_books(fixture_db, "ariel", raw)

def test_books_consistency_check_partition_still_enforced():
    books_consistency_check(
        total_usd_k=100.0, managed_usd_k=60.0, unmanaged_usd_k=40.0,
    )
    with pytest.raises(AssertionError):
        books_consistency_check(
            total_usd_k=100.0, managed_usd_k=60.0, unmanaged_usd_k=10.0,
        )


# ---------------------------------------------------------------------------
# Finding 7 — abstention must not fake freshness
# ---------------------------------------------------------------------------


def test_latest_reviews_skips_abstained(fixture_db):
    from argosy.services.position_stance import _latest_reviews

    session = fixture_db
    session.add(HoldingReview(
        user_id="ariel", symbol="CSPX", verdict="HOLD", outcome="hold",
        confidence="MED", reason="thesis intact", evidence_json="[]",
        reviewed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    ))
    session.add(HoldingReview(
        user_id="ariel", symbol="CSPX", verdict="ABSTAIN", outcome="abstained",
        confidence="LOW", reason="no data", evidence_json="[]",
        reviewed_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    ))
    session.commit()
    reviews = _latest_reviews(session, "ariel")
    assert reviews["CSPX"].verdict == "HOLD"


def test_abstention_does_not_update_last_fleet_check_at(fixture_db):
    from argosy.services.verdict_registry import provenance_for_subjects

    session = fixture_db
    old = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    session.add(HoldingReview(
        user_id="ariel", symbol="CSPX", verdict="HOLD", outcome="hold",
        confidence="MED", reason="ok", evidence_json="[]",
        reviewed_at=old,
    ))
    session.add(HoldingReview(
        user_id="ariel", symbol="CSPX", verdict="ABSTAIN", outcome="abstained",
        confidence="LOW", reason="outage", evidence_json="[]",
        reviewed_at=datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc),
    ))
    # Fresh stance rebuild (what abstention waves trigger) — must not feed freshness.
    session.add(PositionStance(
        user_id="ariel", symbol="CSPX", stance="HOLD", stance_source="plan",
        conviction="MED", divergence=False, plan_verdict="HOLD",
        falsifiers_json="[]",
        built_at=datetime(2026, 8, 7, 18, 5, tzinfo=timezone.utc),
        plan_version_id=None, snapshot_key=None,
    ))
    session.commit()

    prov = provenance_for_subjects(session, user_id="ariel", subjects=["CSPX"])
    last = prov["CSPX"].last_fleet_check_at
    assert last is not None
    assert last.startswith("2026-07-01")
    assert "2026-08-07" not in last


# ---------------------------------------------------------------------------
# Finding 8 — sync cache hits a real fixture row (no mock of cache methods)
# ---------------------------------------------------------------------------


def test_sync_kv_cache_hit_from_fixture_row(tmp_path, monkeypatch):
    """Real ORM Session + real KvCacheEntry row — mocks of _sync_kv_* forbidden."""
    from argosy.services.stock_decision import fetchers as F

    db_path = tmp_path / "cache_fixture.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    payload = [{"headline": "FROM_FIXTURE_CACHE"}]
    with SessionLocal() as session:
        session.add(KvCacheEntry(
            provider="finnhub",
            key="news:TESTTICKER",
            payload_json=json.dumps(payload),
            retrieved_at=now,
            expires_at=now + timedelta(hours=1),
            payload_hash="abc",
        ))
        session.commit()
    engine.dispose()

    class _Settings:
        database_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(
        "argosy.config.get_settings", lambda: _Settings(),
    )
    # Prove we are NOT mocking the cache methods under test.
    assert callable(F._sync_kv_get)

    hit = F._sync_kv_get("finnhub", "news:TESTTICKER")
    assert hit == payload

    miss = F._sync_kv_get("finnhub", "news:OTHER")
    assert miss is None


def test_fetchers_mem_cache_avoids_second_live_hit(monkeypatch):
    from argosy.services.stock_decision import fetchers as F

    calls = {"n": 0}

    class _Client:
        def company_news(self, *a, **k):
            calls["n"] += 1
            return [{"headline": "H1"}]

    class _Adapter:
        def _resolve_client(self):
            return _Client()

    monkeypatch.setattr(F, "_shared_adapter", _Adapter())
    monkeypatch.setattr(F, "_throttle_finnhub", lambda: None)
    # Empty DB cache — force live path once; mem-cache covers the second call.
    monkeypatch.setattr(F, "_sync_kv_get", lambda *a, **k: None)
    monkeypatch.setattr(F, "_sync_kv_put", lambda *a, **k: None)
    F._mem_cache.clear()

    a = F.news_fetcher("AAA")
    b = F.news_fetcher("AAA")
    assert a and "H1" in a
    assert b and "H1" in b
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Savepoint / allocation / abstention evidence (kept)
# ---------------------------------------------------------------------------


def test_sync_savepoint_does_not_rollback_caller(fixture_db, monkeypatch):
    session = fixture_db
    session.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    session.add(PortfolioSnapshotRow(
        user_id="ariel",
        snapshot_date=date(2026, 8, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="pre",
        positions_json="[]",
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json="{}",
        fx_usd_nis=3.0,
        parse_warnings_json="[]",
    ))
    session.flush()
    snap_id = session.execute(select(PortfolioSnapshotRow.id)).scalar_one()

    def _boom(*a, **k):
        raise RuntimeError("forced upsert failure")

    monkeypatch.setattr("argosy.services.holding_books._upsert_one", _boom)
    counts = sync_unmanaged_from_positions(
        session, "ariel",
        [_pos("NVDA", 1.0, location="schwab")],
        commit=False,
    )
    assert counts["errors"] >= 1
    assert session.get(PortfolioSnapshotRow, snap_id) is not None
    session.rollback()


def test_allocation_breakdown_exclude_nvda_uses_managed_flag():
    class _Snap:
        positions = [
            type("P", (), {
                "symbol": "CSPX", "usd_value_k": 100.0, "asset_type": "Core Equity",
                "details": "UCITS", "managed": True,
                "excluded_from_sleeve_math": False,
            })(),
            type("P", (), {
                "symbol": "NVDA", "usd_value_k": 900.0, "asset_type": "Stock",
                "details": "Stock", "managed": False,
                "excluded_from_sleeve_math": True,
            })(),
        ]

    class _Doc:
        classes = []

    rows = build_allocation_breakdown(_Snap(), _Doc(), exclude_nvda=True)
    symbols = {h.symbol for r in rows for h in r.holdings}
    assert "NVDA" not in symbols
    assert "CSPX" in symbols


def test_fi_shock_uses_absolute_nvda_value_not_zero_pct():
    class _R:
        def __init__(self, d):
            self._d = d

        def get(self, key):
            return self._d.get(key)

    net_worth = 4_844_426.74
    nvda_value = 2_300_000.0 * 3.0
    resolved = _R({
        "portfolio.net_worth_nis": ResolvedValue(
            "portfolio.net_worth_nis", net_worth, "nis", "resolved", "t",
        ),
        "retirement.fi_target_nis": ResolvedValue(
            "retirement.fi_target_nis", 3_000_000.0, "nis", "resolved", "t",
        ),
        "retirement.fi_total_capital_nis": ResolvedValue(
            "retirement.fi_total_capital_nis", 4_000_000.0, "nis", "resolved", "t",
        ),
        "concentration.nvda_current_pct": ResolvedValue.excluded(
            "concentration.nvda_current_pct", "pct", "unmanaged",
        ),
        "concentration.nvda_value_nis": ResolvedValue(
            "concentration.nvda_value_nis", nvda_value, "nis", "resolved", "t",
        ),
    })
    inputs = derive_nvda_shock_inputs(resolved)
    assert inputs is not None
    shocked = primary_nvda_shock_net_worth_nis(
        net_worth_nis=inputs["net_worth_nis"],
        nvda_value_nis=inputs["nvda_value_nis"],
        shock=PRIMARY_NVDA_SHOCK,
    )
    assert shocked < net_worth


def test_empty_bundle_is_insufficient():
    assert bundle_has_sufficient_evidence({}) is False
    assert bundle_has_sufficient_evidence({"news": "headline"}) is True


def test_hollow_sentiment_is_not_usable_evidence():
    hollow = "social mentions=12 (scores unavailable)"
    assert evidence_field_is_usable(hollow) is False
    v = decide_stock(
        "CSPX", context="held $10k",
        bundle={"sentiment": hollow, "price": "last price 1"},
    )
    assert v.verdict == "ABSTAIN"


def test_empty_bundle_produces_abstention_not_hold():
    v = decide_stock("CSPX", context="held $10k", bundle={})
    assert v.verdict == "ABSTAIN"


def test_price_only_bundle_abstains():
    v = decide_stock(
        "CSPX", context="held $10k",
        bundle={"price": "last price 28.66", "thesis": "sleeve target 13%"},
    )
    assert v.verdict == "ABSTAIN"


def test_run_holdings_review_records_abstained_outcome():
    recorded = []

    def _record(v, **kw):
        recorded.append((v.verdict, kw.get("outcome")))

    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=1.0,
        holdings={"CSPX": 50_000.0},
        fetchers={},
        decide=decide_stock,
        sink=lambda v: None,
        verify=False,
        record=_record,
        elevated_flags={},
        always_review=frozenset(),
    )
    assert summary["abstained"] == 1
    assert recorded == [("ABSTAIN", "abstained")]


def test_abstain_helper_shape():
    v = abstain_insufficient_evidence("RKT", bundle={"price": "1"})
    assert v.verdict == "ABSTAIN"
    assert v.confidence == "LOW"


def test_dashboard_total_book_includes_fresh_durable_nvda(fixture_db):
    today = date.today()
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=10000.0, current_price=230.0, usd_value_k=2300.0,
        currency="USD", asset_type="Stock", details="Stock",
        reason="observed", status="active",
        valued_as_of=today, observed_as_of=today,
    ))
    incomplete = _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, shares=10, price=40, location="ibi")],
        snap_date=today,
        totals_k=400.0,
    )
    positions, degrade, _stale = _total_book_positions(
        snapshot=incomplete, session=fixture_db, user_id="ariel",
    )
    assert degrade is None
    # Repriced via fixture quote (180) — not the stored 230 mark.
    assert symbol_value_usd_k(positions, "NVDA") == pytest.approx(1800.0)
    pct = nvda_concentration_pct(positions)
    assert pct is not None and pct > 0


def test_compositions_unavailable_not_empty_when_degraded(fixture_db):
    """Finding 3 — UI must not see 'no positions' when the book is degraded."""
    from argosy.services.wealth_dashboard import _compositions

    # Explicit policy is required for the NVDA-must-restore integrity gate —
    # DEFAULT alone must not invent it (that degraded every partial seed).
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    snap = _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],
        snap_date=date(2026, 8, 7),
        totals_k=400.0,
    )
    asset, sector, region, reason = _compositions(
        snapshot=snap, fx_usd_nis=3.0,
        session=fixture_db, user_id="ariel",
    )
    assert asset == [] and sector == [] and region == []
    assert reason is not None
    assert "unavailable" in reason.lower() or "degrad" in reason.lower()
    assert "no positions" not in reason.lower()

# ---------------------------------------------------------------------------
# Round-5 adversarial blockers — each test FAILS when its fix is reverted
# ---------------------------------------------------------------------------


def test_blocker1_merge_uses_last_coverage_not_truncated_latest(fixture_db):
    """BLOCKER 1 — after a truncated latest, Leumi feed must still restore Schwab.

    Revert detector: if persist merges only against the globally latest row
    (already Leumi-only), carried accounts stay empty and NVDA disappears.
    """
    from datetime import timedelta

    full = [
        _pos("NVDA", 2307.9, shares=10940, price=210, location="schwab"),
        _pos("BMY", 5.8, shares=100, price=58, location="schwab 876"),
        _pos("SCHD", 13.0, shares=400, price=32, location="schwab 876"),
        _pos("-", 69.0, shares=3, price=None, location="Aborad", asset_type="Other"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
        _pos("NKE", 6.7, shares=150, price=44, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 7, 13)),
    )
    # Simulate the wipe: insert a truncated Leumi-only row OUTSIDE merge
    # (how history actually looks after the bug landed).
    wiped = [
        _pos("CSPX", 410.0, shares=100, price=410, location="Leumi"),
    ]
    _add_snap(
        fixture_db, positions=wiped, snap_date=date(2026, 8, 1), totals_k=410.0,
    )
    # Bump imported_at so wiped is globally latest.
    latest = fixture_db.execute(
        select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.id.desc())
    ).scalars().first()
    latest.imported_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    fixture_db.commit()

    leumi_only = [
        _pos("CSPX", 420.0, shares=100, price=420, location="Leumi"),
    ]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(leumi_only, date(2026, 8, 8)),
    )
    pos = json.loads(row.positions_json)
    totals = json.loads(row.totals_json)
    assert any((p.get("symbol") or "").upper() == "NVDA" for p in pos)
    assert any((p.get("symbol") or "").upper() == "BMY" for p in pos)
    assert any(
        (p.get("symbol") or "") == "-" and "aborad" in (p.get("location") or "").lower()
        for p in pos
    )
    assert "schwab" in totals.get("accounts_carried", [])
    assert len(pos) >= 5  # leumi CSPX + carried schwab/876/aborad


def test_blocker1_backfill_restores_and_is_idempotent(fixture_db):
    """BLOCKER 1b — operator backfill restores then no-ops on second run."""
    from argosy.services.holding_books import (
        backfill_restored_holdings_book,
        resolve_prior_positions_by_account_coverage,
    )

    full = [
        _pos("NVDA", 2307.9, shares=10940, price=210, location="schwab"),
        _pos("SGOV", 20.1, shares=200, price=100, location="schwab 876"),
        _pos("SCHD", 13.0, shares=400, price=32, location="schwab 876"),
        _pos("VOO", 6.9, shares=10, price=690, location="schwab 876"),
        _pos("-", 5.9, shares=5893, price=1, location="schwab 876", asset_type="Other"),
        _pos("BMY", 5.8, shares=100, price=58, location="schwab 876"),
        _pos("SCHG", 3.5, shares=100, price=35, location="schwab 876"),
        _pos("-", 69.0, shares=3, price=None, location="Aborad", asset_type="Other"),
        _pos("CSPX", 1600.0, shares=100, price=400, location="Leumi"),
    ]
    for i in range(29):
        full.append(_pos(f"L{i}", 0.5, shares=1, price=0.5, location="Leumi"))
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 7, 13)),
    )
    wiped_rows = []
    for p in full:
        if (p.get("location") or "").lower() != "leumi":
            continue
        d = dict(p)
        if (d.get("symbol") or "").upper() == "CSPX":
            d["usd_value_k"] = 1615.6 - (29 * 0.5)
        wiped_rows.append(d)
    _add_snap(fixture_db, positions=wiped_rows, snap_date=date(2026, 8, 8))

    expected = resolve_prior_positions_by_account_coverage(fixture_db, "ariel")
    n_exp = len(expected)
    total_exp = sum(float(p.get("usd_value_k") or 0) for p in expected)
    # Must restore the wiped non-Leumi accounts (8) onto the Leumi book.
    assert n_exp == len(wiped_rows) + 8
    first = backfill_restored_holdings_book(
        fixture_db, user_id="ariel",
        expected_position_count=n_exp,
        expected_usd_k=total_exp,
        expected_usd_k_tol=1.0,
    )
    assert first["status"] == "restored"
    assert first["position_count"] == n_exp
    second = backfill_restored_holdings_book(
        fixture_db, user_id="ariel",
        expected_position_count=n_exp,
        expected_usd_k=total_exp,
        expected_usd_k_tol=1.0,
    )
    assert second["status"] == "noop"
    rows = fixture_db.execute(
        select(PortfolioSnapshotRow).where(
            PortfolioSnapshotRow.source_path == "backfill:last_coverage_restore"
        )
    ).scalars().all()
    assert len(rows) == 1


def test_blocker2_unpriceable_stale_hyphen_does_not_publish_money(fixture_db):
    """BLOCKER 2 — July `-` marks must not publish as current money."""
    july = date(2026, 7, 13)
    positions = [
        _pos("CSPX", 400.0, shares=10, price=40, location="Leumi"),
        _pos("-", 69.0, shares=3, price=None, location="Aborad", asset_type="Other"),
        _pos("-", 5.9, shares=5893, price=1, location="schwab 876", asset_type="Other"),
    ]
    for p in positions:
        p["valued_as_of"] = july
        p["observed_as_of"] = july
    book = load_total_book(
        fixture_db, "ariel", positions,
        today=date(2026, 8, 8), snapshot_date=july,
    )
    hyphens = [p for p in book.total if (p.get("symbol") or "") == "-"]
    assert len(hyphens) == 2
    for h in hyphens:
        assert h.get("usd_value_k") in (None, 0) or h.get("mark_stale") is True
        # Must not publish the July dollars as current.
        assert h.get("usd_value_k") in (None, 0)
    assert book.degraded is True
    assert book.degrade_reason and "unpriceable" in book.degrade_reason.lower()


def test_blocker2_snapshot_route_goes_through_load_total_book(fixture_db):
    """BLOCKER 2 — /portfolio/snapshot must not serve raw stale stored marks."""
    from argosy.api.routes.portfolio import (
        _apply_total_book_to_snap,
        _snapshot_to_dto,
    )
    from argosy.services.portfolio_snapshot_store import row_to_snapshot

    july = date(2026, 7, 13)
    positions = [
        _pos("CSPX", 400.0, shares=10, price=40, location="Leumi"),
        _pos("-", 69.0, shares=3, price=None, location="Aborad", asset_type="Other"),
    ]
    for p in positions:
        p["valued_as_of"] = july.isoformat()
        p["observed_as_of"] = july.isoformat()
    row = _add_snap(fixture_db, positions=positions, snap_date=july, totals_k=469.0)
    snap = _apply_total_book_to_snap(row_to_snapshot(row), fixture_db, "ariel")
    dto = _snapshot_to_dto(snap)
    hyphens = [p for p in dto.positions if (p.symbol or "") == "-"]
    assert hyphens
    for h in hyphens:
        assert h.usd_value_k in (None, 0)
    assert dto.book_degraded is True


def test_blocker3_carried_quantity_keeps_original_observed_as_of(fixture_db):
    """BLOCKER 3 — Leumi feed must NOT re-date carried Schwab NVDA observation."""
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.commit()
    july = date(2026, 7, 13)
    full = [
        _pos("NVDA", 2307.9, shares=10940, price=210, location="schwab"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, july),
    )
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(
            [_pos("CSPX", 410.0, shares=100, price=410, location="Leumi")],
            date(2026, 8, 8),
        ),
    )
    pos = json.loads(row.positions_json)
    nvda = next(p for p in pos if (p.get("symbol") or "").upper() == "NVDA")
    assert nvda.get("carried_forward") is True
    obs = nvda.get("observed_as_of")
    assert str(obs)[:10] == "2026-07-13", obs

    # Durable unmanaged row must also keep July — not Aug 8.
    uh = fixture_db.execute(
        select(UnmanagedHolding).where(
            UnmanagedHolding.user_id == "ariel",
            UnmanagedHolding.symbol == "NVDA",
            UnmanagedHolding.status == "active",
        )
    ).scalars().one()
    assert uh.observed_as_of == july


def test_blocker4_coverage_reaches_dto_and_api(fixture_db):
    """BLOCKER 4 — accounts_covered/carried survive row_to_snapshot → DTO."""
    from argosy.api.routes.portfolio import _snapshot_to_dto
    from argosy.services.portfolio_snapshot_store import row_to_snapshot

    full = [
        _pos("NVDA", 100.0, shares=10, price=10, location="schwab"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 7, 13)),
    )
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(
            [_pos("CSPX", 410.0, shares=100, price=410, location="Leumi")],
            date(2026, 8, 8),
        ),
    )
    snap = row_to_snapshot(row)
    assert "leumi" in snap.accounts_covered
    assert "schwab" in snap.accounts_carried
    dto = _snapshot_to_dto(snap)
    assert "leumi" in dto.accounts_covered
    assert "schwab" in dto.accounts_carried


def test_blocker5_dual_alias_in_same_feed_refuses_not_drops_money(fixture_db):
    """BLOCKER 5 — both rename aliases in one feed is a conflict, not a pick-one."""
    prior = [
        _pos('מחקה ת"א-200', 100.0, shares=80000, price=100, location="Leumi",
             currency="NIS", asset_type="Core Equity"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(prior, date(2026, 7, 13)),
    )
    both = [
        _pos('מחקה ת"א-200', 100.0, shares=80000, price=100, location="Leumi",
             currency="NIS", asset_type="Core Equity"),
        _pos('ת"א-200', 200.0, shares=80000, price=200, location="Leumi",
             currency="NIS", asset_type="Core Equity"),
    ]
    with pytest.raises(SnapshotIngestRejected) as ei:
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(both, date(2026, 8, 8)),
        )
    assert ei.value.code == "alias_conflict"


def test_blocker5_two_hyphen_symbols_in_different_accounts_survive(fixture_db):
    """BLOCKER 5 — two `-` lots in different accounts never collide."""
    from argosy.services.holding_books import merge_positions_per_account

    prior = [
        _pos("-", 69.0, shares=3, location="Aborad", asset_type="Other"),
        _pos("-", 5.9, shares=5893, location="schwab 876", asset_type="Other"),
        _pos("CSPX", 400.0, shares=100, location="Leumi"),
    ]
    merge = merge_positions_per_account(
        prior_positions=prior,
        incoming_positions=[
            _pos("CSPX", 410.0, shares=100, location="Leumi"),
        ],
        incoming_snapshot_date=date(2026, 8, 8),
        prior_snapshot_date=date(2026, 7, 13),
    )
    hyphens = [p for p in merge.positions if (p.get("symbol") or "") == "-"]
    assert len(hyphens) == 2
    locs = {(p.get("location") or "").lower() for p in hyphens}
    assert "aborad" in locs and "schwab 876" in locs
    total = sum(float(p.get("usd_value_k") or 0) for p in hyphens)
    assert total == pytest.approx(74.9)


def test_blocker5_rename_mechanism_is_general_not_hebrew_only(fixture_db):
    """BLOCKER 5 — 1:1 same-shares unmatched pair is a rename for any symbols."""
    prior = [
        _pos("OLDETF", 50.0, shares=100, price=50, location="Leumi"),
        _pos("CSPX", 400.0, shares=100, price=400, location="Leumi"),
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(prior, date(2026, 7, 13)),
    )
    incoming = [
        _pos("NEWETF", 51.0, shares=100, price=51, location="Leumi"),
        _pos("CSPX", 410.0, shares=100, price=410, location="Leumi"),
    ]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(incoming, date(2026, 8, 8)),
    )
    warns = json.loads(row.parse_warnings_json or "[]")
    assert any("SYMBOL_RENAME" in w and "OLDETF" in w and "NEWETF" in w for w in warns)
    pos = json.loads(row.positions_json)
    assert any((p.get("symbol") or "").upper() == "NEWETF" for p in pos)
    assert not any((p.get("symbol") or "").upper() == "OLDETF" for p in pos)


def test_blocker6_undated_feed_rejected_after_dated_book(fixture_db):
    """BLOCKER 6 — undated feed must not supersede a dated book."""
    full = [_pos(f"T{i}", 40.0, location="schwab") for i in range(10)]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 7)),
    )
    with pytest.raises(SnapshotIngestRejected) as ei:
        persist_snapshot(
            fixture_db, user_id="ariel",
            snapshot=_pydantic_snap(full, None),  # undated
        )
    assert ei.value.code == "undated_snapshot"


def test_blocker6_mark_is_stale_treats_null_as_stale():
    """BLOCKER 6 — null valued_as_of is never read as current."""
    from argosy.services.holding_books import mark_is_stale

    assert mark_is_stale(None, today=date(2026, 8, 8)) is True
    assert mark_is_stale(date(2026, 8, 8), today=date(2026, 8, 8)) is False
