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
        [_pos("CSPX", 400.0, location="ibi")],
        today=today,
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
        [_pos("CSPX", 400.0, location="ibi")],
        today=today, quote_fn=_nvda_quote(180.0),
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
        [_pos("CSPX", 410.0, location="ibi")],
        today=today, quote_fn=_nvda_quote(230.0),
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


def test_persist_catastrophic_override_does_not_retire_absent_account(fixture_db):
    full = [
        _pos("NVDA", 2300.0, shares=100, price=23, location="schwab 876"),
        *[_pos(f"T{i}", 20.0, location="ibi") for i in range(10)],
    ]
    persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(full, date(2026, 8, 1)),
    )
    closed = [_pos(f"T{i}", 20.0, location="ibi") for i in range(10)]
    row = persist_snapshot(
        fixture_db, user_id="ariel",
        snapshot=_pydantic_snap(closed, date(2026, 8, 7)),
        allow_catastrophic_drop=True,
        actor="test-admin",
        override_reason="leumi-only reimport after schwab export lag",
    )
    warns = json.loads(row.parse_warnings_json or "[]")
    assert any("INGEST_OVERRIDE" in w and "actor=test-admin" in w for w in warns)
    # NVDA at schwab 876 must still be ACTIVE — closure is explicit-only.
    active = fixture_db.execute(
        select(UnmanagedHolding).where(UnmanagedHolding.status == "active")
    ).scalars().all()
    assert len(active) == 1
    assert active[0].symbol == "NVDA"

# ---------------------------------------------------------------------------
# Finding 3 — degraded never renders as 0.0 money
# ---------------------------------------------------------------------------


def test_load_total_book_degraded_when_empty_and_incomplete(fixture_db):
    incomplete = [_pos("CSPX", 400.0, location="ibi")]
    book = load_total_book(fixture_db, "ariel", incomplete)
    assert book.degraded is True
    assert book.degrade_reason


def test_estate_gate_refuses_degraded_with_none_not_zero(fixture_db):
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
    _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],
        snap_date=date(2026, 8, 7),
        totals_k=400.0,
    )
    from argosy.orchestrator.flows.incremental_plan import _snapshot_positions_fx

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

    # Force parse to a catastrophic pydantic snap without needing perfect TSV.
    bad_snap = _pydantic_snap(
        [_pos("T0", 20.0, location="ibi"), _pos("T1", 20.0, location="ibi")],
        date(2026, 8, 8),
    )
    monkeypatch.setattr(
        portfolio_routes, "parse_portfolio_tsv", lambda *a, **k: bad_snap,
    )

    upload = UploadFile(filename="bad.tsv", file=BytesIO(tsv))
    resp = portfolio_routes.upload_snapshot(
        file=upload, user_id="ariel", fire_detector=False,
        allow_stale=False, allow_catastrophic_drop=False, db=fixture_db,
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
    )
    warns = json.loads(row.parse_warnings_json or "[]")
    assert any("allow_stale=True" in w for w in warns)


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
    book = load_total_book(fixture_db, "ariel", raw, today=date(2026, 8, 7))
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
    fixture_db.add(UnmanagedSymbolPolicy(user_id="ariel", symbol="NVDA"))
    fixture_db.add(UnmanagedHolding(
        user_id="ariel", symbol="NVDA", location="schwab",
        shares=10000.0, current_price=230.0, usd_value_k=2300.0,
        currency="USD", asset_type="Stock", details="Stock",
        reason="observed", status="active",
        valued_as_of=date(2026, 8, 6), observed_as_of=date(2026, 8, 6),
    ))
    incomplete = _add_snap(
        fixture_db,
        positions=[_pos("CSPX", 400.0, location="ibi")],
        snap_date=date(2026, 8, 7),
        totals_k=400.0,
    )
    positions, degrade = _total_book_positions(
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


def test_resolver_sell_shares_use_tradeable_denominator(fixture_db):
    """Finding 5 — excluded NVDA weight → sell shares via tradeable book, not NW."""
    from argosy.services.plan_derivation import derive_nvda_deconcentration
    from argosy.services.plan_numeric_resolver import ResolvedValue as RV
    from argosy.services.holding_books import implied_nvda_weight_frac

    # NVDA $2.3M, tradeable $10M, NW $13.8M — wrong denom changes sell shares.
    nvda_nis = 6_900_000.0
    tradeable = 10_000_000.0
    net_worth = 13_800_000.0
    nvda_sh, nvda_px = 10_000, 230.0
    class _R:
        def __init__(self, d):
            self._d = d
        def get(self, key):
            return self._d.get(key)
    resolved = _R({
        "concentration.nvda_current_pct": RV.excluded(
            "concentration.nvda_current_pct", "pct", "unmanaged",
        ),
        "concentration.nvda_value_nis": RV(
            "concentration.nvda_value_nis", nvda_nis, "nis", "resolved", "t",
        ),
        "portfolio.net_worth_nis": RV(
            "portfolio.net_worth_nis", net_worth, "nis", "resolved", "t",
        ),
    })
    w = implied_nvda_weight_frac(resolved, tradeable_securities_nis=tradeable)
    assert w == pytest.approx(0.69)
    wrong_w = nvda_nis / net_worth
    cap, target = 0.13, 0.12
    right = derive_nvda_deconcentration(
        nvda_sh=nvda_sh, nvda_px_usd=nvda_px, nvda_weight=w,
        target_w=target, cap=cap,
    )
    wrong = derive_nvda_deconcentration(
        nvda_sh=nvda_sh, nvda_px_usd=nvda_px, nvda_weight=wrong_w,
        target_w=target, cap=cap,
    )
    # Fixture assertion: correct sell uses tradeable-implied book.
    assert right["nvda_sell_sh"].value != wrong["nvda_sell_sh"].value
    # book = nvda_usd / w = 2.3M / 0.69 ≈ 3.333M tradeable USD
    # target_sh = floor(0.12 * 3.333M / 230) = floor(1739.13) = 1739
    # sell = 10000 - 1739 = 8261
    assert right["nvda_sell_sh"].value == 8261
