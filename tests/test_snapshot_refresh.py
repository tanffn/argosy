"""Unit tests for the snapshot self-refresh service (mocked quotes/FX).

Covers the binding rules from the 2026-07-06 decision:
  * quantities carry, prices refresh (reprice math),
  * a quote miss carries the old value + records ``reprice_miss:<symbol>``,
  * cash / pension / unpriceable rows carry unchanged,
  * the persisted total is an INDEPENDENT sum over the new positions,
  * provenance: ``source_path = self-refresh:reprice-of-<old date>``.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.ingest.tsv import (
    PensionEntry,
    PortfolioPosition,
    PortfolioSnapshot,
)
from argosy.services.portfolio_snapshot_store import persist_snapshot
from argosy.services.snapshot_refresh import (
    _currencies_agree,
    _hinted_suffixes,
    refresh_portfolio_snapshot,
)
from argosy.state.models import Base, PortfolioSnapshotRow


@pytest.fixture()
def session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'snap_refresh.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.close()


def _seed(session, *, fx_nis=3.0, fx_eur=0.85) -> PortfolioSnapshot:
    snap = PortfolioSnapshot(
        source_path="/tmp/Family Finances Status - test.tsv",
        snapshot_date=date(2026, 6, 29),
        fx_usd_nis=fx_nis,
        fx_usd_eur=fx_eur,
        positions=[
            PortfolioPosition(
                location="schwab", currency="USD", asset_type="NVIDIA",
                details="RSU", symbol="NVDA", shares=100.0,
                current_price=200.0, current_value_local=20_000.0,
                usd_value_k=20.0,
            ),
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Core Equity",
                details="(ISHR CORE S&P500) CSPX LN", symbol="CSPX",
                shares=10.0, current_price=800.0,
                current_value_local=8_000.0, usd_value_k=8.0,
            ),
            PortfolioPosition(  # NIS-denominated priceable line
                location="Leumi", currency="NIS", asset_type="Growth",
                details="(fake nis etf) FNIS", symbol="FNIS", shares=100.0,
                current_price=30.0, current_value_local=3_000.0,
                usd_value_k=1.0,
            ),
            PortfolioPosition(  # cash — must carry verbatim
                location="Leumi", currency="NIS", asset_type="Cash",
                symbol="", current_value_local=6_000.0, usd_value_k=2.0,
            ),
            PortfolioPosition(  # unpriceable Israeli fund — carry, no warning
                location="Leumi", currency="NIS", asset_type="Core Equity",
                details='ATF מחקה ת"א-200', symbol='מחקה ת"א-200',
                shares=80_000.0, current_price=147.53,
                current_value_local=118_024.0, usd_value_k=39.34,
            ),
            PortfolioPosition(  # real-estate summary row — carry
                location="Aborad", currency="USD", asset_type="Real estate",
                details="Real estate", symbol="-", shares=3.0,
                usd_value_k=69.0,
            ),
        ],
        pensions=[
            PensionEntry(person="Ariel", account_type="Keren Hishtalmut",
                         value=384_000.0, currency="NIS"),
        ],
    )
    persist_snapshot(session, user_id="ariel", snapshot=snap)
    return snap


def test_reprice_math_and_fx_conversion(session):
    _seed(session)
    quotes = {"NVDA": 210.0, "CSPX": 820.0, "FNIS": 33.0}
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: quotes.get(sym),
        fx_fn=lambda: {"usd_nis": 3.2, "usd_eur": 0.9},
        today=date(2026, 7, 6),
    )
    assert res.row is not None
    by_sym = {p.symbol: p for p in res.snapshot.positions}

    nvda = by_sym["NVDA"]
    assert nvda.shares == 100.0  # quantity NEVER changes
    assert nvda.current_price == 210.0
    assert nvda.current_value_local == pytest.approx(21_000.0)
    assert nvda.usd_value_k == pytest.approx(21.0)

    cspx = by_sym["CSPX"]
    assert cspx.usd_value_k == pytest.approx(8.2)

    fnis = by_sym["FNIS"]
    assert fnis.current_value_local == pytest.approx(3_300.0)
    # NIS → USD with the FRESH rate (3.2), not the stored one (3.0).
    assert fnis.usd_value_k == pytest.approx(3_300.0 / 3.2 / 1000.0)

    assert res.snapshot.fx_usd_nis == 3.2
    assert res.snapshot.fx_usd_eur == 0.9
    assert sorted(res.repriced) == ["CSPX", "FNIS", "NVDA"]
    # No misses: warnings empty.
    assert res.warnings == []


def test_quote_miss_carries_old_value_and_warns(session):
    _seed(session)
    quotes = {"NVDA": 210.0}  # CSPX + FNIS miss
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: quotes.get(sym),
        fx_fn=lambda: {"usd_nis": 3.0, "usd_eur": 0.85},
        today=date(2026, 7, 6),
    )
    by_sym = {p.symbol: p for p in res.snapshot.positions}
    assert by_sym["CSPX"].current_price == 800.0  # carried
    assert by_sym["CSPX"].usd_value_k == 8.0
    assert "reprice_miss:CSPX" in res.warnings
    assert "reprice_miss:FNIS" in res.warnings
    # Persisted warnings match.
    row = session.get(PortfolioSnapshotRow, res.row.id)
    assert "reprice_miss:CSPX" in json.loads(row.parse_warnings_json)


def test_out_of_band_quote_is_a_miss_never_fabricated(session):
    _seed(session)
    # CSPX quoted at 8.2 — a pence/wrong-listing artifact (old price 800).
    quotes = {"NVDA": 210.0, "CSPX": 8.2, "FNIS": 33.0}
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: quotes.get(sym),
        fx_fn=lambda: {"usd_nis": 3.0, "usd_eur": 0.85},
        today=date(2026, 7, 6),
    )
    by_sym = {p.symbol: p for p in res.snapshot.positions}
    assert by_sym["CSPX"].current_price == 800.0
    assert by_sym["CSPX"].usd_value_k == 8.0
    assert any(w.startswith("reprice_miss:CSPX") for w in res.warnings)


def test_cash_pension_and_unpriceable_rows_carry_unchanged(session):
    old = _seed(session)
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: 999_999.0,  # would be out-of-band anyway
        fx_fn=lambda: {"usd_nis": 3.2, "usd_eur": 0.9},
        today=date(2026, 7, 6),
    )
    cash = [p for p in res.snapshot.positions if p.asset_type == "Cash"][0]
    old_cash = [p for p in old.positions if p.asset_type == "Cash"][0]
    # Cash carries VERBATIM — value and USD conversion untouched even though
    # FX moved (rule: cash rows carry over unchanged).
    assert cash.current_value_local == old_cash.current_value_local
    assert cash.usd_value_k == old_cash.usd_value_k

    heb = [p for p in res.snapshot.positions if p.symbol == 'מחקה ת"א-200'][0]
    assert heb.current_price == 147.53
    assert heb.usd_value_k == 39.34
    # Unpriceable (no feed) is NOT a miss — no warning for it.
    assert not any('מחקה' in w for w in res.warnings)

    re_row = [p for p in res.snapshot.positions if p.asset_type == "Real estate"][0]
    assert re_row.usd_value_k == 69.0

    assert [pe.model_dump() for pe in res.snapshot.pensions] == [
        pe.model_dump() for pe in old.pensions
    ]


def test_totals_are_independent_sum_over_new_positions(session):
    _seed(session)
    quotes = {"NVDA": 210.0, "CSPX": 820.0, "FNIS": 33.0}
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: quotes.get(sym),
        fx_fn=lambda: {"usd_nis": 3.2, "usd_eur": 0.9},
        today=date(2026, 7, 6),
    )
    row = session.get(PortfolioSnapshotRow, res.row.id)
    totals = json.loads(row.totals_json)
    independent = sum(p.usd_value_k or 0.0 for p in res.snapshot.positions)
    assert totals["total_usd_value_k"] == pytest.approx(independent)
    assert res.new_total_usd_k == pytest.approx(independent)
    # And it is NOT the old total: prices moved.
    assert totals["total_usd_value_k"] != pytest.approx(res.old_total_usd_k)


def test_provenance_marker_and_snapshot_date(session):
    _seed(session)
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: None,
        fx_fn=lambda: {"usd_nis": None, "usd_eur": None},
        today=date(2026, 7, 6),
    )
    row = session.get(PortfolioSnapshotRow, res.row.id)
    assert row.source_path == "self-refresh:reprice-of-2026-06-29"
    assert row.snapshot_date == date(2026, 7, 6)
    # FX miss: stored rates carry, and the miss is recorded.
    assert row.fx_usd_nis == 3.0
    assert "fx_miss:usd_nis" in res.warnings
    assert "fx_miss:usd_eur" in res.warnings


def test_no_prior_snapshot_is_a_noop(session):
    res = refresh_portfolio_snapshot(
        session, user_id="ariel",
        quote_fn=lambda sym, **kw: None,
        fx_fn=lambda: {"usd_nis": None, "usd_eur": None},
    )
    assert res.row is None
    assert res.snapshot is None


def test_currency_agreement_rejects_pence():
    assert _currencies_agree("USD", "USD")
    assert _currencies_agree("USD", None)
    assert not _currencies_agree("USD", "GBp")
    assert not _currencies_agree("USD", "GBX")
    assert not _currencies_agree("NIS", None)
    assert _currencies_agree("NIS", "ILS")


def test_exchange_hint_orders_suffixes():
    assert _hinted_suffixes("(ISH NASDAQ100 $A) CNDX LN")[0] == ".L"
    assert _hinted_suffixes("(ISHR DM PRPTY YD) IWDP SW")[0] == ".SW"
    assert _hinted_suffixes("Stock, Med")[0] == ""
    assert _hinted_suffixes("")[0] == ""
