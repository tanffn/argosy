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


def test_cash_pension_and_unpriceable_rows_carry_quantities(session):
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
    # Cash LOCAL value carries verbatim; the USD projection is re-derived at
    # the FRESH FX (usd_value_k is derived, never source data).
    assert cash.current_value_local == old_cash.current_value_local
    assert cash.usd_value_k == pytest.approx(6_000.0 / 3.2 / 1000.0)

    heb = [p for p in res.snapshot.positions if p.symbol == 'מחקה ת"א-200'][0]
    assert heb.current_price == 147.53
    assert heb.current_value_local == 118_024.0
    assert heb.usd_value_k == pytest.approx(118_024.0 / 3.2 / 1000.0)
    # Unpriceable (no feed) is NOT a miss — no warning for it.
    assert not any('מחקה' in w for w in res.warnings)

    # No local value to convert → the old projection carries.
    re_row = [p for p in res.snapshot.positions if p.asset_type == "Real estate"][0]
    assert re_row.usd_value_k == 69.0

    assert [pe.model_dump() for pe in res.snapshot.pensions] == [
        pe.model_dump() for pe in old.pensions
    ]


def test_carried_row_heals_stale_usd_projection(session):
    """Live incident (rows 11→12): an upstream writer moved a cash row's
    LOCAL value without recomputing ``usd_value_k``; the self-refresh then
    carried the stale projection verbatim (phantom −$16.4k cash). The
    refresh must re-derive usd_value_k from current_value_local + FX for
    every carried row."""
    snap = PortfolioSnapshot(
        source_path="fills-applied:sgov-sale",
        snapshot_date=date(2026, 7, 6),
        fx_usd_nis=3.0,
        fx_usd_eur=0.85,
        positions=[
            PortfolioPosition(  # cash: local moved to +3,655.34, usd stale
                location="Leumi", currency="USD", asset_type="Cash",
                symbol="", current_value_local=3_655.34,
                usd_value_k=-16.43466,
            ),
            PortfolioPosition(  # quote-missing row with stale usd projection
                location="Leumi", currency="USD", asset_type="Defensive",
                details="(ISH 0-3M TREAS) SGOV", symbol="SGOV",
                shares=850.0, current_price=100.44,
                current_value_local=85_374.0, usd_value_k=105.462,
            ),
        ],
    )
    persist_snapshot(session, user_id="ariel", snapshot=snap)
    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: None,  # miss → carry path
        fx_fn=lambda: {"usd_nis": 3.0, "usd_eur": 0.85},
        today=date(2026, 7, 7),
    )
    cash = [p for p in res.snapshot.positions if p.asset_type == "Cash"][0]
    assert cash.current_value_local == pytest.approx(3_655.34)
    assert cash.usd_value_k == pytest.approx(3.65534)
    sgov = [p for p in res.snapshot.positions if p.symbol == "SGOV"][0]
    assert sgov.shares == 850.0
    assert sgov.usd_value_k == pytest.approx(85.374)
    totals = json.loads(session.get(PortfolioSnapshotRow, res.row.id).totals_json)
    assert totals["cash_balances_usd_k"] == pytest.approx(3.65534)


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


def _persist_minimal(session, source_path: str) -> int:
    snap = PortfolioSnapshot(
        source_path=source_path,
        snapshot_date=date(2026, 7, 6),
        fx_usd_nis=3.0,
        fx_usd_eur=0.85,
        positions=[
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Cash",
                symbol="", current_value_local=1_000.0, usd_value_k=1.0,
            ),
        ],
    )
    return persist_snapshot(session, user_id="ariel", snapshot=snap).id


def _force_imported_at(session, row_id: int, raw_text: str) -> None:
    """Write imported_at as RAW TEXT — reproduces rows written outside the
    SQLAlchemy default (which always emits microseconds)."""
    session.execute(
        sa.text("UPDATE portfolio_snapshots SET imported_at = :t WHERE id = :i"),
        {"t": raw_text, "i": row_id},
    )
    session.commit()
    session.expire_all()


def test_latest_row_mixed_precision_timestamps_and_provenance(session):
    """Live shape (rows 9-11): ingest → fills-applied (microseconds) →
    fills-applied written by an ad-hoc path WITHOUT microseconds. The
    freshest row by (imported_at, id) must win regardless of the
    timestamp's sub-second precision or provenance tag."""
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    a = _persist_minimal(session, "self-refresh:reprice-of-2026-06-29")
    b = _persist_minimal(session, "fills-applied:2026-07-06-deploy")
    c = _persist_minimal(session, "fills-applied:2026-07-06-sgov-sale")
    _force_imported_at(session, a, "2026-07-06 04:00:05.280869")
    _force_imported_at(session, b, "2026-07-06 13:42:32.018224")
    _force_imported_at(session, c, "2026-07-06 14:04:41")  # second-precision

    latest = get_latest_snapshot_row(session, "ariel")
    assert latest is not None and latest.id == c


def test_latest_row_exact_timestamp_tie_breaks_on_id(session):
    """Two rows sharing the exact imported_at text: the higher id (later
    insert) wins deterministically."""
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    a = _persist_minimal(session, "fills-applied:first")
    b = _persist_minimal(session, "fills-applied:second")
    _force_imported_at(session, a, "2026-07-06 14:04:41")
    _force_imported_at(session, b, "2026-07-06 14:04:41")

    latest = get_latest_snapshot_row(session, "ariel")
    assert latest is not None and latest.id == b


def test_refresh_bases_off_freshest_row_in_a_provenance_chain(session):
    """The self-refresh must reprice the LAST row of a deploy → sale chain,
    never an earlier fills row."""
    a = _persist_minimal(session, "fills-applied:deploy")
    b = _persist_minimal(session, "fills-applied:sale")
    _force_imported_at(session, a, "2026-07-06 13:42:32.018224")
    _force_imported_at(session, b, "2026-07-06 14:04:41")
    # Mark the freshest row's cash distinctly so parentage is observable.
    row = session.get(PortfolioSnapshotRow, b)
    positions = json.loads(row.positions_json)
    positions[0]["current_value_local"] = 2_000.0
    positions[0]["usd_value_k"] = 2.0
    row.positions_json = json.dumps(positions)
    session.commit()

    res = refresh_portfolio_snapshot(
        session,
        user_id="ariel",
        quote_fn=lambda sym, **kw: None,
        fx_fn=lambda: {"usd_nis": 3.0, "usd_eur": 0.85},
        today=date(2026, 7, 7),
    )
    cash = [p for p in res.snapshot.positions if p.asset_type == "Cash"][0]
    assert cash.current_value_local == pytest.approx(2_000.0)


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


def test_us_line_does_not_probe_eur_or_chf_venues():
    """A plain US line must not walk foreign venues.

    Observed 2026-08-23 during plan run 436: SOFI and TEM carry no venue hint
    and are USD, yet the resolver tried SOFI.SW and TEM.AS, each costing
    several 404 round-trips with retries. Worse than wasteful — a same-ticker
    foreign listing that omits its currency passes _currencies_agree (which
    tolerates None for USD positions) and would price the WRONG instrument.
    """
    for details in ("(Sofi Technologies Inc) SOFI", "(Tempus Ai Inc) TEM"):
        chain = _hinted_suffixes(details, "USD")
        assert chain[0] == "", f"{details}: bare US listing must be tried first"
        for bad in (".L", ".AS", ".MI", ".DE", ".SW"):
            assert bad not in chain, f"{details}: must not probe {bad}"


def test_listing_identity_never_falls_back_to_other_exchanges():
    # An explicit hint is authoritative even when the venue's usual currency
    # differs — the venue is stated, not guessed.
    chain = _hinted_suffixes("(ISHR DM PRPTY YD) DPYA SW", "USD")
    assert chain == (".SW",)

    # A EUR position should only ever see EUR venues.
    eur = _hinted_suffixes("some european line", "EUR")
    assert eur == ()  # Currency does not establish instrument identity.

    assert _hinted_suffixes("mystery line", "JPY") == ()


@pytest.mark.parametrize("symbol,details,currency,expected", [
    ("CSPX", "Ishares CSPX LN", "USD", ["CSPX.L"]),
    ("CSPX.L", "Ishares CSPX LN", "USD", ["CSPX.L"]),
    ("BRK/B", "Berkshire BRK/B", "USD", ["BRK-B"]),
    ("BRK.B", "Berkshire BRK.B", "USD", ["BRK-B"]),
    ("SOFI", "Sofi SOFI", "USD", ["SOFI"]),
    ("GE", "General Electric GE", "USD", ["GE"]),
    ("DE", "Deere DE", "USD", ["DE"]),
    ("BMY", "BRISTOL MYERS SQUIBB CO", "USD", ["BMY"]),
    ("TEST", "EXAMPLE HOLDINGS AG", "USD", ["TEST"]),
    ("AS", "Company AS AS", "USD", ["AS.AS"]),
    ("AS.L", "Company AS AS", "USD", []),
    ("IWDP.SW", "Ishares IWDP LN", "USD", []),
    ("UNKNOWN", "Unknown", "JPY", []),
    ("UNKNOWN", "Unknown UNKNOWN HK", "USD", []),
    ("UNKNOWN.L", "Unknown UNKNOWN HK", "USD", []),
])
def test_default_quotes_only_the_identified_listing(monkeypatch, symbol, details, currency, expected):
    from argosy.adapters.data.yfinance_adapter import Quote, YFinanceAdapter
    from argosy.services.snapshot_refresh import default_quote_fn

    called = []

    async def miss(self, candidate):
        called.append(candidate)
        return Quote(ticker=candidate, price=None)

    monkeypatch.setattr(YFinanceAdapter, "get_quote", miss)
    assert default_quote_fn(symbol, currency=currency, details=details) is None
    assert called == expected  # A miss never changes the instrument's identity.


# ---------------------------------------------------------------------------
# apply_fills_to_snapshot — executed broker buys folded into a new row
# ---------------------------------------------------------------------------

from argosy.services.snapshot_refresh import Fill, apply_fills_to_snapshot  # noqa: E402


@pytest.mark.parametrize("holding_unit,cash_unit,receipt_unit", [
    ("ILS", "NIS", "NIS"), ("NIS", "ILS", "ILS"), ("ILS", "ILS", "NIS"), ("NIS", "NIS", "ILS")])
def test_fill_currency_aliases_merge_one_holding_and_cash(session, holding_unit, cash_unit, receipt_unit):
    from argosy.execution.settlement import FillSettlement

    facts = dict(tax_withheld=0, net_cash_delta=-300, reference="alias")
    assert FillSettlement(currency="ILS", **facts) == FillSettlement(currency="NIS", **facts)
    persist_snapshot(session, user_id="ariel", snapshot=PortfolioSnapshot(
        source_path="test:broker", snapshot_date=date(2026, 7, 6), fx_usd_nis=3,
        positions=[PortfolioPosition(location="Leumi", currency=holding_unit, symbol="TA35", asset_type="Core Equity",
                     shares=2, current_price=300, current_value_local=600, usd_value_k=.2),
                   PortfolioPosition(location="Leumi", currency=cash_unit, symbol="", asset_type="Cash",
                     current_value_local=3000, usd_value_k=1)]))
    result = apply_fills_to_snapshot(session, user_id="ariel", cash_currency=receipt_unit,
        fills=[Fill(symbol="TA35", location="Leumi", currency=receipt_unit, shares=1, price=300)],
        source_tag="fills-applied:alias-test")
    holdings = [p for p in result.snapshot.positions if p.symbol == "TA35"]
    assert len(holdings) == 1 and holdings[0].shares == 3
    assert next(p for p in result.snapshot.positions if p.asset_type == "Cash").current_value_local == 2700
    assert result.snapshot.total_usd_value_k == pytest.approx(1.2)


def test_stored_fx_survives_fresh_session_and_provider_miss(session):
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot

    _seed(session, fx_nis=3.17, fx_eur=.92)
    with sessionmaker(bind=session.get_bind(), expire_on_commit=False)() as fresh:
        hydrated = row_to_snapshot(get_latest_snapshot_row(fresh, "ariel"))
        assert hydrated.fx_usd_nis == 3.17 and hydrated.fx_usd_eur == .92
        result = refresh_portfolio_snapshot(fresh, user_id="ariel", quote_fn=lambda *a, **kw: None,
                                           fx_fn=lambda: {}, today=date(2026, 7, 6))
        assert result.snapshot.fx_usd_nis == 3.17 and result.snapshot.fx_usd_eur == .92
        assert "fx_miss:usd_nis" in result.warnings and "fx_miss:usd_eur" in result.warnings


def _seed_for_fills(session) -> None:
    snap = PortfolioSnapshot(
        source_path="self-refresh:reprice-of-2026-06-29",
        snapshot_date=date(2026, 7, 6),
        fx_usd_nis=3.0,
        fx_usd_eur=0.85,
        positions=[
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Core Equity",
                details="(ISHR CORE S&P500) CSPX LN", symbol="CSPX",
                shares=100.0, current_price=800.0, avg_price=700.0,
                current_value_local=80_000.0, usd_value_k=80.0,
                pct_change=0.1429,  # fraction unit (Leumi convention)
            ),
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Cash",
                symbol="", current_value_local=50_000.0, usd_value_k=50.0,
            ),
            PortfolioPosition(  # NIS cash — must NOT be touched as funding
                location="Leumi", currency="NIS", asset_type="Cash",
                symbol="", current_value_local=6_000.0, usd_value_k=2.0,
            ),
        ],
    )
    persist_snapshot(session, user_id="ariel", snapshot=snap)


def test_fill_merge_blends_avg_and_revalues_at_current_price(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=50.0, price=820.0)],
        source_tag="fills-applied:test",
        today=date(2026, 7, 6),
    )
    cspx = next(p for p in res.snapshot.positions if p.symbol == "CSPX")
    assert cspx.shares == 150.0
    # blended avg: (100*700 + 50*820) / 150 = 740.0
    assert cspx.avg_price == pytest.approx(740.0)
    # revalued at the snapshot's current price, not the fill print
    assert cspx.current_price == 800.0
    assert cspx.current_value_local == pytest.approx(150 * 800.0)
    assert cspx.usd_value_k == pytest.approx(120.0)
    # pct_change recomputed in the row's own unit (fraction): 800/740 - 1
    assert cspx.pct_change == pytest.approx(800.0 / 740.0 - 1.0, abs=1e-4)
    assert res.merged == ["CSPX"]


def test_fill_adds_new_position_and_reduces_cash(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(
        session,
        fills=[
            Fill(symbol="EXUS", shares=100.0, price=45.0,
                 asset_type="International", details="(XTR WLD EXUSA) EXUS LN"),
        ],
        source_tag="fills-applied:test",
        today=date(2026, 7, 6),
    )
    exus = next(p for p in res.snapshot.positions if p.symbol == "EXUS")
    assert exus.avg_price == exus.current_price == 45.0
    assert exus.current_value_local == pytest.approx(4_500.0)
    assert exus.location == "Leumi" and exus.currency == "USD"
    cash = next(
        p for p in res.snapshot.positions
        if p.asset_type == "Cash" and p.currency == "USD"
    )
    assert cash.current_value_local == pytest.approx(45_500.0)
    # NIS cash untouched
    nis = next(
        p for p in res.snapshot.positions
        if p.asset_type == "Cash" and p.currency == "NIS"
    )
    assert nis.current_value_local == 6_000.0
    # conservation: value bought at fill price == cash spent → total unchanged
    assert res.new_total_usd_k == pytest.approx(res.old_total_usd_k, abs=1e-9)
    assert res.added == ["EXUS"]
    assert "fill-applied:EXUS:100@45" in res.snapshot.parse_warnings


@pytest.mark.parametrize("shares", [25, 100])
def test_sell_reduces_shares_and_credits_net_reported_cash(session, shares):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=shares, price=800,
                                                     action="sell", commission=2, tax_withheld=100)],
                                  source_tag="fills-applied:sale", today=date(2026, 7, 6))
    held = next(p for p in res.snapshot.positions if p.symbol == "CSPX")
    assert held.shares == 100 - shares
    assert held.avg_price == 700  # Sale price never rewrites remaining cost basis.
    assert held.current_value_local == (100 - shares) * 800
    assert res.cash_after_local == 50000 + shares * 800 - 102
    assert res.new_total_usd_k == pytest.approx(res.old_total_usd_k - .102)
    assert any(w.startswith("fill-applied:SELL:CSPX:") for w in res.snapshot.parse_warnings)


def test_buy_fees_are_debited_in_native_currency_without_touching_usd(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(session, fills=[Fill(symbol="LOCAL", shares=10, price=100,
                                                     currency="NIS", commission=5)],
                                  cash_currency="NIS", source_tag="fills-applied:nis",
                                  today=date(2026, 7, 6))
    assert res.cash_after_local == 4995
    assert next(p for p in res.snapshot.positions if p.asset_type == "Cash"
                and p.currency == "USD").current_value_local == 50000
    assert res.new_total_usd_k == pytest.approx(res.old_total_usd_k - 5 / 3 / 1000)


@pytest.mark.parametrize("kwargs,match", [
    ({"action": "sell"}, "explicit broker tax"),
    ({"action": "sell", "shares": 101, "tax_withheld": 0}, "exceeds recorded"),
    ({"action": "sell", "symbol": "NOTHELD", "tax_withheld": 0}, "quantity is unknown"),
    ({"currency": "NIS"}, "custody and currency"),
    ({"location": "schwab"}, "custody and currency"),
    ({"price": float("nan")}, "ledger precision"),
    ({"shares": .00000001}, "ledger precision"),
    ({"tax_withheld": -1}, "cannot be negative"),
])
def test_invalid_fill_book_inputs_do_not_write_a_snapshot(session, kwargs, match):
    _seed_for_fills(session)
    baseline = session.query(PortfolioSnapshotRow).count()
    fields = {"symbol": "CSPX", "shares": 1, "price": 800, **kwargs}
    with pytest.raises(ValueError, match=match):
        apply_fills_to_snapshot(session, fills=[Fill(**fields)], source_tag="fills-applied:invalid",
                                today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == baseline


def test_sell_funded_buy_applies_one_after_withholding_cash_movement(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(session, fills=[
        Fill(symbol="CSPX", shares=10, price=800, action="sell", tax_withheld=500, commission=2),
        Fill(symbol="EXUS", shares=100, price=50, commission=1),
    ], source_tag="fills-applied:switch", today=date(2026, 7, 6))
    assert res.cash_after_local == 50000 + 8000 - 500 - 2 - 5000 - 1
    assert res.new_total_usd_k == pytest.approx(res.old_total_usd_k - .503)
    assert {p.symbol: p.shares for p in res.snapshot.positions if p.symbol} == {"CSPX": 90, "EXUS": 100}


def test_empty_fill_batch_is_noop_and_missing_average_is_not_invented(session):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    next(p for p in ps if p["symbol"] == "CSPX")["avg_price"] = None
    row.positions_json = json.dumps(ps)
    session.commit()
    empty = apply_fills_to_snapshot(session, fills=[], source_tag="fills-applied:empty")
    assert empty.row.id == row.id and session.query(PortfolioSnapshotRow).count() == 1
    result = apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                                     source_tag="fills-applied:no-basis", today=date(2026, 7, 6))
    assert next(p for p in result.snapshot.positions if p.symbol == "CSPX").avg_price is None
    assert next(p for p in result.snapshot.positions if p.symbol == "CSPX").pct_change is None


def test_buy_reopens_zero_position_without_duplicate_or_lost_shares(session):
    _seed_for_fills(session)
    apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=100, price=800,
                                                action="sell", tax_withheld=0)],
                            source_tag="fills-applied:close", today=date(2026, 7, 6))
    result = apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=2, price=810)],
                                     source_tag="fills-applied:reopen", today=date(2026, 7, 6))
    positions = [p for p in result.snapshot.positions if p.symbol == "CSPX"]
    assert len(positions) == 1 and positions[0].shares == 2
    assert positions[0].avg_price == positions[0].current_price == 810
    persisted = json.loads(result.row.positions_json)
    assert next(p for p in persisted if p["symbol"] == "CSPX")["shares"] == 2
    assert result.new_total_usd_k == pytest.approx(result.old_total_usd_k)


def test_buy_cannot_invent_prior_quantity(session):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[0]["shares"] = None
    row.positions_json = json.dumps(ps)
    session.commit()
    with pytest.raises(ValueError, match="quantity is unknown"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                                source_tag="fills-applied:unknown", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


@pytest.mark.parametrize("duplicate", ["holding", "cash"])
def test_ambiguous_book_rows_fail_before_writing(session, duplicate):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps.append(dict(ps[0 if duplicate == "holding" else 1]))
    row.positions_json = json.dumps(ps)
    session.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                                source_tag="fills-applied:ambiguous", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


@pytest.mark.parametrize("action", ["buy", "sell"])
@pytest.mark.parametrize("factor", [1.1, 100])
def test_book_application_checks_exact_unit_consistency(session, action, factor):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[0]["current_price"] *= factor
    row.positions_json = json.dumps(ps)
    session.commit()
    with pytest.raises(ValueError, match="price/value units"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800,
                                                    action=action, tax_withheld=0)],
                                source_tag="fills-applied:wrong-units", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


@pytest.mark.parametrize("action", ["buy", "sell"])
@pytest.mark.parametrize("rate", [None, 0, -1])
def test_native_currency_application_requires_fx_before_writing(session, action, rate):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[0]["currency"] = "NIS"
    row.positions_json = json.dumps(ps)
    row.fx_usd_nis = rate
    session.commit()
    with pytest.raises(ValueError, match="positive FX"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800, currency="NIS",
                                                    action=action, tax_withheld=0)], cash_currency="NIS",
                                source_tag="fills-applied:no-fx", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


def test_fill_application_cannot_freshen_undated_old_marks(session):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    for position in ps:
        position["valued_as_of"] = position["observed_as_of"] = None
    row.positions_json = json.dumps(ps)
    session.commit()
    applied = apply_fills_to_snapshot(session, fills=[Fill(symbol="EXUS", shares=1, price=50)],
                                      source_tag="fills-applied:later", today=date(2026, 7, 20))
    held = next(p for p in applied.snapshot.positions if p.symbol == "CSPX")
    assert held.valued_as_of == held.observed_as_of == date(2026, 7, 6)
    refreshed = refresh_portfolio_snapshot(session, user_id="ariel", today=date(2026, 7, 21),
                                           quote_fn=lambda *a, **kw: None, fx_fn=lambda *a, **kw: None)
    held = next(p for p in refreshed.snapshot.positions if p.symbol == "CSPX")
    assert held.valued_as_of == date(2026, 7, 6) and held.mark_stale


def test_same_batch_close_and_reopen_uses_one_holding(session):
    _seed_for_fills(session)
    result = apply_fills_to_snapshot(session, fills=[
        Fill(symbol="CSPX", shares=100, price=800, action="sell", tax_withheld=0),
        Fill(symbol="CSPX", shares=1, price=800),
    ], source_tag="fills-applied:roundtrip", today=date(2026, 7, 6))
    rows = [p for p in result.snapshot.positions if p.symbol == "CSPX"]
    assert len(rows) == 1 and rows[0].shares == 1
    assert result.new_total_usd_k == pytest.approx(result.old_total_usd_k)


def test_normalized_fill_amounts_conserve_new_holding_and_cash(session):
    _seed_for_fills(session)
    result = apply_fills_to_snapshot(session, fills=[
        Fill(symbol="NEW", shares=1.000000009, price=10_000_000),
    ], source_tag="fills-applied:near-grid", today=date(2026, 7, 6))
    position = next(p for p in result.snapshot.positions if p.symbol == "NEW")
    assert position.shares == 1
    assert position.current_value_local == 10_000_000
    assert result.cash_after_local == result.cash_before_local - 10_000_000
    assert result.new_total_usd_k == pytest.approx(result.old_total_usd_k, abs=1e-10)
    blob = next(w for w in result.snapshot.parse_warnings if w.startswith("closed_loop_expectations:"))
    receipt = json.loads(blob.split(":", 1)[1])
    assert receipt["fills"][0]["shares"] == receipt["expected_positions"][0]["shares_delta"] == 1


def test_zero_quantity_with_nonzero_source_value_cannot_be_reopened(session):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[0]["shares"] = 0
    row.positions_json = json.dumps(ps)
    session.commit()
    with pytest.raises(ValueError, match="price/value units"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                                source_tag="fills-applied:bad-zero", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("value", [None, 1])
def test_fill_cannot_silently_repair_prior_usd_projection(session, index, value):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[index]["usd_value_k"] = value
    row.positions_json = json.dumps(ps)
    session.commit()
    with pytest.raises(ValueError, match="USD projection"):
        apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                                source_tag="fills-applied:bad-projection", today=date(2026, 7, 6))
    assert session.query(PortfolioSnapshotRow).count() == 1


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_existing_zero_mark_is_not_replaced_by_fill_print(session, action):
    _seed_for_fills(session)
    row = session.query(PortfolioSnapshotRow).one()
    ps = json.loads(row.positions_json)
    ps[0]["current_price"] = ps[0]["current_value_local"] = ps[0]["usd_value_k"] = 0
    row.positions_json = json.dumps(ps)
    session.commit()
    result = apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800,
                                                         action=action, tax_withheld=0)],
                                    source_tag="fills-applied:zero-mark", today=date(2026, 7, 6))
    held = next(p for p in result.snapshot.positions if p.symbol == "CSPX")
    assert held.current_price == held.current_value_local == held.usd_value_k == 0
    assert held.shares == (101 if action == "buy" else 99)
    delta = -800 if action == "buy" else 800
    assert result.cash_after_local == result.cash_before_local + delta
    assert result.new_total_usd_k == pytest.approx(result.old_total_usd_k + delta / 1000)


def test_stale_refresh_cannot_overwrite_a_fill_applied_during_quote_collection(session):
    _seed_for_fills(session)
    applied_ids = []

    def quote(*args, **kwargs):
        if not applied_ids:
            result = apply_fills_to_snapshot(session, fills=[Fill(symbol="CSPX", shares=1, price=800)],
                source_tag="fills-applied:arrived-during-refresh", today=date(2026, 7, 6))
            applied_ids.append(result.row.id)
        return 800

    with pytest.raises(ValueError, match="portfolio changed"):
        refresh_portfolio_snapshot(session, quote_fn=quote, fx_fn=lambda: {}, today=date(2026, 7, 6))
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot
    current = get_latest_snapshot_row(session, "ariel")
    assert current.id == applied_ids[0]
    assert next(p for p in row_to_snapshot(current).positions if p.symbol == "CSPX").shares == 101


def test_fill_overdraft_warns_loudly_but_applies(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="EXUS", shares=2_000.0, price=45.0,
                    asset_type="International")],
        source_tag="fills-applied:test",
        today=date(2026, 7, 6),
    )
    assert res.cash_after_local == pytest.approx(50_000.0 - 90_000.0)
    assert any(w.startswith("cash_overdraft:Leumi:USD:") for w in res.warnings)


def test_fill_without_cash_position_fails_loud(session):
    _seed_for_fills(session)
    with pytest.raises(ValueError, match="no cash position"):
        apply_fills_to_snapshot(
            session,
            fills=[Fill(symbol="EXUS", shares=1.0, price=45.0, location="Schwab")],
            source_tag="fills-applied:test",
            cash_location="Schwab",  # no cash row there in the seed
        )


def test_fills_persist_new_row_with_source_tag_and_extra_warnings(session):
    _seed_for_fills(session)
    res = apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=10.0, price=810.0)],
        source_tag="fills-applied:2026-07-06-deploy",
        extra_warnings=["expectation:next-real-ingest:CSPX 110 sh"],
        today=date(2026, 7, 6),
    )
    row = session.get(PortfolioSnapshotRow, res.row.id)
    assert row.source_path == "fills-applied:2026-07-06-deploy"
    warnings = json.loads(row.parse_warnings_json)
    assert "fill-applied:CSPX:10@810" in warnings
    assert "expectation:next-real-ingest:CSPX 110 sh" in warnings
    # totals_json is the independent sum over the persisted positions
    totals = json.loads(row.totals_json)
    assert totals["total_usd_value_k"] == pytest.approx(res.new_total_usd_k)


# ----------------------------------------------------------------------
# Allocation-block recompute — a derived table carried forward verbatim
# went stale on fills (live incident: the post-deploy row still showed
# the pre-deploy Cash 170.98k / delta -98.28k, the cash detector read it,
# and the directive fleet authored a deploy of already-deployed money).
# ----------------------------------------------------------------------


def _seed_with_allocations(session) -> None:
    from argosy.ingest.tsv import AllocationRow

    snap = PortfolioSnapshot(
        source_path="self-refresh:reprice-of-2026-06-29",
        snapshot_date=date(2026, 7, 6),
        fx_usd_nis=3.0,
        fx_usd_eur=0.85,
        positions=[
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Core Equity",
                details="(ISHR CORE S&P500) CSPX LN", symbol="CSPX",
                shares=100.0, current_price=800.0, avg_price=700.0,
                current_value_local=80_000.0, usd_value_k=80.0,
                pct_change=0.1429,
            ),
            PortfolioPosition(
                location="Leumi", currency="USD", asset_type="Cash",
                symbol="", current_value_local=50_000.0, usd_value_k=50.0,
            ),
        ],
        allocations=[
            AllocationRow(category="Core Equity", pct=61.54, usd_value_k=80.0,
                          target_pct=70.0, target_k=91.0, delta_k=11.0),
            AllocationRow(category="Cash", pct=38.46, usd_value_k=50.0,
                          target_pct=5.0, target_k=6.5, delta_k=-43.5),
            AllocationRow(category="Grand Total", pct=100.0, usd_value_k=130.0),
        ],
    )
    persist_snapshot(session, user_id="ariel", snapshot=snap)


def test_fills_recompute_allocation_block(session):
    """Buying $40k of CSPX from cash must move the allocation table's
    Cash current DOWN and Core Equity UP — never carry the stale table."""
    from argosy.services.snapshot_refresh import Fill, apply_fills_to_snapshot

    _seed_with_allocations(session)
    result = apply_fills_to_snapshot(
        session, user_id="ariel", source_tag="fills-applied:test",
        fills=[Fill(symbol="CSPX", shares=50.0, price=800.0, currency="USD",
                    location="Leumi")],
    )
    snap = result.snapshot
    alloc = {a.category: a for a in snap.allocations}
    # Cash: 50k - 40k = 10k; Core Equity: 150sh @ 800 = 120k
    assert alloc["Cash"].usd_value_k == 10.0
    assert alloc["Core Equity"].usd_value_k == 120.0
    # delta_k re-derived against carried targets
    assert alloc["Cash"].delta_k == 6.5 - 10.0
    assert alloc["Core Equity"].delta_k == 91.0 - 120.0
    # Grand Total re-summed over ALL positions (130k book, conserved)
    assert alloc["Grand Total"].usd_value_k == 130.0
    # targets carried verbatim
    assert alloc["Cash"].target_pct == 5.0
    assert alloc["Core Equity"].target_k == 91.0


def test_refresh_recomputes_allocation_block(session):
    """Self-refresh repricing must also re-derive the table currents."""
    from argosy.services.snapshot_refresh import refresh_portfolio_snapshot

    _seed_with_allocations(session)
    result = refresh_portfolio_snapshot(
        session, user_id="ariel",
        quote_fn=lambda *a, **k: 1000.0,  # CSPX reprices 800 -> 1000
        fx_fn=lambda: {"usd_nis": 3.0, "usd_eur": 0.85},
    )
    alloc = {a.category: a for a in result.snapshot.allocations}
    assert alloc["Core Equity"].usd_value_k == 100.0  # 100sh @ 1000
    assert alloc["Cash"].usd_value_k == 50.0
    assert alloc["Grand Total"].usd_value_k == 150.0


def test_empty_prior_allocations_stay_empty(session):
    from argosy.services.snapshot_refresh import Fill, apply_fills_to_snapshot

    _seed_for_fills(session)  # no allocations block
    result = apply_fills_to_snapshot(
        session, user_id="ariel", source_tag="fills-applied:test",
        fills=[Fill(symbol="CSPX", shares=10.0, price=800.0, currency="USD",
                    location="Leumi")],
    )
    assert result.snapshot.allocations == []
