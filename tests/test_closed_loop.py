"""Closed-loop expectation verifier tests (argosy/services/closed_loop.py).

Parse fixtures are VERBATIM copies of the armed strings in the dev DB
(portfolio_snapshots rows 10/11, the 2026-07-06 real-money deploy + SGOV
cover-sale) — the parser must handle what the code actually wrote, not an
invented format. Sweep/proposal tests run on the real schema
(alembic_engine_at_head) because the 0055/0077 CHECK constraints are exactly
the class of bug fake DBs hid (green tests, dead sink).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services.closed_loop import (
    collect_armed_expectations,
    parse_warning_entries,
    sweep_unverified_expectations,
    verify_against_positions,
    verify_and_record_on_ingest,
)
from argosy.services.portfolio_snapshot_store import persist_snapshot
from argosy.services.snapshot_refresh import Fill, apply_fills_to_snapshot
from argosy.state.models import ActionProposal, Base, PortfolioSnapshotRow


@pytest.fixture()
def session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'closed_loop.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 1. Parse round-trip on the REAL armed strings (copied from db/argosy.db)
# ---------------------------------------------------------------------------

# Verbatim from portfolio_snapshots row 11 (fills-applied:2026-07-06-sgov-sale)
_REAL_ROW11_WARNINGS = [
    "fill-applied:SPMV:125@114.7",
    "fill-applied:TEM:80@60.46",
    "fill-applied:OKLO:100@51.32",
    "fill-applied:RXRX:1500@3.78",
    "fill-applied:IBTA:2000@5.96",
    "fill-applied:FUSA:800@16.27",
    "fill-applied:EIMI:250@55.07",
    "fill-applied:IWQU:225@88.42",
    "fill-applied:EXUS:800@45.55",
    "fill-applied:CSPX:45@808.04",
    "expectation:next-real-ingest:new positions expected at Leumi/USD: "
    "SPMV 125 sh, TEM 80 sh, OKLO 100 sh, RXRX 1500 sh, IBTA 2000 sh, "
    "FUSA 800 sh, IWQU 225 sh, EXUS 800 sh",
    "expectation:next-real-ingest:CSPX 240 sh total, EIMI 650 sh total",
    "expectation:next-real-ingest:Leumi USD cash reduced by $161,376.10 vs "
    "the 2026-06-29-sourced balance; a mismatch must surface loudly "
    "(deploy executed 2026-07-06)",
    "fill-applied:SELL:SGOV:200@100.45 (price=live-quote estimate; "
    "reconcile vs broker print on next ingest)",
    "funding-gap RESOLVED: Leumi USD cash -16,434.66 + 20,090.00 SGOV "
    "proceeds = 3,655.34 (covers the -16,434.66 single-account execution "
    "gap; part of the standing SGOV US-situs exit)",
    "expectation:next-real-ingest: SGOV@Leumi shares reduced by 200; "
    "Leumi USD cash positive ~3.6k",
    "totals corrected to conservation basis (row-10 totals + sale "
    "price-print delta); the naive full-recompute mis-handled unit "
    "conventions and was discarded",
]

# Verbatim from portfolio_snapshots row 10 (fills-applied:2026-07-06-deploy)
_REAL_ROW10_CASH_GAP = (
    "cash_funding_gap:Leumi:USD:-16434.66 — CAUSE CONFIRMED BY ARIEL: the "
    "deployable pool was multi-currency/multi-account (Leumi USD 144,941 + "
    "Leumi NIS ~20,040 + Schwab 5,893) but all fills executed from Leumi USD "
    "alone. Remedy: convert ~NIS 58,945 -> USD at Leumi before T+2 settlement "
    "(no sale needed)."
)

# The exact format apply_fills_to_snapshot writes for an overdraft.
_OVERDRAFT_LINE = (
    "cash_overdraft:Leumi:USD:-16,434.66 — snapshot cash was stale vs the "
    "real broker balance; next real ingest must reconcile"
)


def test_parse_real_row11_prose_round_trip():
    parsed = parse_warning_entries(_REAL_ROW11_WARNINGS)
    by_sym = {f["symbol"]: f for f in parsed.fills}
    assert len(parsed.fills) == 11  # 10 buys + 1 sell
    assert by_sym["SPMV"]["shares_delta"] == pytest.approx(125.0)
    assert by_sym["SPMV"]["price"] == pytest.approx(114.7)
    assert by_sym["SPMV"]["price_estimated"] is False
    assert by_sym["CSPX"]["shares_delta"] == pytest.approx(45.0)
    # The SELL entry: negative delta + estimated price flag from the tail.
    sgov = by_sym["SGOV"]
    assert sgov["shares_delta"] == pytest.approx(-200.0)
    assert sgov["price"] == pytest.approx(100.45)
    assert sgov["price_estimated"] is True
    # Free-text expectation notes carried as manual (4 of them), the
    # non-expectation prose ("funding-gap RESOLVED", "totals corrected")
    # is neither a fill nor a manual expectation.
    assert len(parsed.manual) == 4
    assert all(m.startswith("expectation:") for m in parsed.manual)
    assert parsed.cash == []


def test_parse_real_cash_entries():
    parsed = parse_warning_entries([_REAL_ROW10_CASH_GAP, _OVERDRAFT_LINE])
    assert len(parsed.cash) == 2
    gap, over = parsed.cash
    assert gap["location"] == "Leumi" and gap["currency"] == "USD"
    assert gap["recorded_after_local"] == pytest.approx(-16434.66)
    assert over["recorded_after_local"] == pytest.approx(-16434.66)  # comma form
    assert over["overdraft"] is True


def test_parse_prefers_machine_blob_over_prose():
    blob = {
        "v": 1,
        "expected_positions": [
            {"symbol": "CSPX", "location": "Leumi", "currency": "USD",
             "shares": 110.0, "price": 810.0},
        ],
        "cash": {"location": "Leumi", "currency": "USD", "after_local": 41_900.0},
        "manual": ["expectation:next-real-ingest:CSPX 110 sh"],
    }
    parsed = parse_warning_entries([
        "fill-applied:CSPX:10@810",
        "closed_loop_expectations:" + json.dumps(blob),
        "expectation:next-real-ingest:CSPX 110 sh",  # duplicate of blob manual
    ])
    assert parsed.fills == []  # prose duplicates the blob → blob wins
    assert parsed.blob is not None
    assert parsed.manual == ["expectation:next-real-ingest:CSPX 110 sh"]


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _persist(session, *, source_path, positions, snapshot_date=date(2026, 7, 6),
             parse_warnings=(), user_id="ariel"):
    snap = PortfolioSnapshot(
        source_path=source_path,
        snapshot_date=snapshot_date,
        fx_usd_nis=3.0,
        fx_usd_eur=0.85,
        positions=positions,
        parse_warnings=list(parse_warnings),
    )
    return persist_snapshot(session, user_id=user_id, snapshot=snap)


def _pos(symbol, shares, price, *, location="Leumi", currency="USD",
         asset_type="Core Equity"):
    return PortfolioPosition(
        location=location, currency=currency, asset_type=asset_type,
        details=f"({symbol}) {symbol} LN", symbol=symbol, shares=shares,
        current_price=price, avg_price=price,
        current_value_local=shares * price, usd_value_k=shares * price / 1000.0,
    )


def _cash(value, *, location="Leumi", currency="USD"):
    return PortfolioPosition(
        location=location, currency=currency, asset_type="Cash", symbol="",
        current_value_local=value, usd_value_k=value / 1000.0,
    )


def _seed_book_then_fills(session):
    """Real ingest → apply_fills (arms expectations, incl. the JSON blob)."""
    _persist(
        session,
        source_path=r"D:\somewhere\Family Finances Status - 26 Jun.tsv",
        snapshot_date=date(2026, 6, 29),
        positions=[_pos("CSPX", 100.0, 800.0), _cash(50_000.0)],
    )
    return apply_fills_to_snapshot(
        session,
        fills=[
            Fill(symbol="CSPX", shares=10.0, price=810.0),
            Fill(symbol="EXUS", shares=100.0, price=45.0,
                 asset_type="International"),
        ],
        source_tag="fills-applied:test-deploy",
        today=date(2026, 7, 6),
    )


# ---------------------------------------------------------------------------
# 2. apply_fills arms a machine-readable blob; collect binds expectations
# ---------------------------------------------------------------------------


def test_apply_fills_writes_machine_blob_and_collect_arms_it(session):
    res = _seed_book_then_fills(session)
    row = session.get(PortfolioSnapshotRow, res.row.id)
    warnings = json.loads(row.parse_warnings_json)
    blob_lines = [w for w in warnings if w.startswith("closed_loop_expectations:")]
    assert len(blob_lines) == 1
    blob = json.loads(blob_lines[0].split(":", 1)[1])
    expected = {e["symbol"]: e["shares"] for e in blob["expected_positions"]}
    assert expected == {"CSPX": 110.0, "EXUS": 100.0}
    assert blob["cash"]["after_local"] == pytest.approx(50_000.0 - 8_100.0 - 4_500.0)
    # prose entries still present for humans
    assert "fill-applied:CSPX:10@810" in warnings

    armed = collect_armed_expectations(session, user_id="ariel")
    assert armed.armed_row_ids == [res.row.id]
    by_sym = {p.symbol: p for p in armed.positions}
    assert by_sym["CSPX"].expected_shares == pytest.approx(110.0)
    assert by_sym["EXUS"].expected_shares == pytest.approx(100.0)
    assert len(armed.cash) == 1 and armed.cash[0].location == "Leumi"


# ---------------------------------------------------------------------------
# 3. Resolve on ingest — expectations disappear, recorded for audit
# ---------------------------------------------------------------------------


def test_resolve_on_ingest_records_and_disarms(session):
    _seed_book_then_fills(session)
    new_row = _persist(
        session,
        source_path=r"D:\somewhere\Family Finances Status - 10 Jul.tsv",
        snapshot_date=date(2026, 7, 10),
        positions=[
            _pos("CSPX", 110.0, 815.0),
            _pos("EXUS", 100.0, 45.6, asset_type="International"),
            _cash(37_400.0),
        ],
    )
    result = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=new_row, commit=True,
    )
    assert result is not None
    assert result.mismatches == []
    assert any("CSPX 110 sh" in r for r in result.resolved)
    assert any("EXUS 100 sh" in r for r in result.resolved)
    assert any("cash reconciled" in r for r in result.resolved)
    # Outcome lines are recorded on the NEW row (audit), history untouched.
    row = session.get(PortfolioSnapshotRow, new_row.id)
    recorded = json.loads(row.parse_warnings_json)
    assert any(w.startswith("closed-loop:resolved:") for w in recorded)
    assert not any(w.startswith("CLOSED-LOOP MISMATCH") for w in recorded)
    # Disarmed: the fills row is now older than the newest real ingest.
    assert collect_armed_expectations(session, user_id="ariel").empty


def test_mismatch_flags_loud_with_expected_vs_actual(session):
    _seed_book_then_fills(session)
    new_row = _persist(
        session,
        source_path=r"D:\somewhere\Family Finances Status - 10 Jul.tsv",
        snapshot_date=date(2026, 7, 10),
        positions=[
            _pos("CSPX", 100.0, 815.0),  # the +10 fill is MISSING at the broker
            _pos("EXUS", 100.0, 45.6, asset_type="International"),
            _cash(37_400.0),
        ],
    )
    result = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=new_row, commit=True,
    )
    assert result is not None
    assert len(result.mismatches) == 1
    m = result.mismatches[0]
    assert "expected CSPX 110 sh" in m and "shows 100 sh" in m
    row = session.get(PortfolioSnapshotRow, new_row.id)
    recorded = json.loads(row.parse_warnings_json)
    assert any(w.startswith("CLOSED-LOOP MISMATCH") for w in recorded)


# ---------------------------------------------------------------------------
# 4. Estimated price (the SGOV case): shares govern, price updates, no fail
# ---------------------------------------------------------------------------


def test_estimated_price_updates_never_fails(session):
    # Legacy prose arming (the already-armed dev-DB rows have no blob).
    _persist(
        session,
        source_path=r"D:\somewhere\Family Finances Status - 26 Jun.tsv",
        snapshot_date=date(2026, 6, 29),
        positions=[_pos("SGOV", 1050.0, 100.4, asset_type="Treasuries"),
                   _cash(1_000.0)],
    )
    _persist(
        session,
        source_path="fills-applied:2026-07-06-sgov-sale",
        positions=[_pos("SGOV", 850.0, 100.45, asset_type="Treasuries"),
                   _cash(21_090.0)],
        parse_warnings=[
            "fill-applied:SELL:SGOV:200@100.45 (price=live-quote estimate; "
            "reconcile vs broker print on next ingest)",
        ],
    )
    # Broker print came in materially different (>2%) — shares still match.
    new_row = _persist(
        session,
        source_path=r"D:\somewhere\Family Finances Status - 15 Jul.tsv",
        snapshot_date=date(2026, 7, 15),
        positions=[_pos("SGOV", 850.0, 103.0, asset_type="Treasuries"),
                   _cash(21_090.0)],
    )
    result = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=new_row, commit=True,
    )
    assert result is not None
    assert result.mismatches == []
    line = next(r for r in result.resolved if "SGOV" in r)
    assert "850 sh" in line
    assert "estimated fill price" in line and "price updated" in line


def test_full_sell_expects_zero_shares(session):
    _persist(
        session,
        source_path=r"D:\x.tsv", snapshot_date=date(2026, 6, 29),
        positions=[_pos("SGOV", 200.0, 100.4), _cash(1_000.0)],
    )
    _persist(
        session,
        source_path="fills-applied:full-exit",
        positions=[_cash(21_090.0)],  # SGOV fully sold — no position row left
        parse_warnings=["fill-applied:SELL:SGOV:200@100.45"],
    )
    armed = collect_armed_expectations(session, user_id="ariel")
    sgov = next(p for p in armed.positions if p.symbol == "SGOV")
    assert sgov.expected_shares == 0.0
    # Ingest confirms the exit.
    result = verify_against_positions(armed, [_cash(21_090.0).model_dump()])
    assert result.mismatches == []
    assert any("SGOV 0 sh" in r for r in result.resolved)


def test_cash_still_negative_after_ingest_is_loud(session):
    _persist(
        session,
        source_path=r"D:\x.tsv", snapshot_date=date(2026, 6, 29),
        positions=[_pos("CSPX", 100.0, 800.0), _cash(1_000.0)],
    )
    _persist(
        session,
        source_path="fills-applied:overdraft",
        positions=[_pos("CSPX", 110.0, 800.0), _cash(-16_434.66)],
        parse_warnings=["fill-applied:CSPX:10@810", _OVERDRAFT_LINE],
    )
    armed = collect_armed_expectations(session, user_id="ariel")
    result = verify_against_positions(
        armed,
        [_pos("CSPX", 110.0, 815.0).model_dump(), _cash(-500.0).model_dump()],
    )
    assert any("STILL NEGATIVE" in m for m in result.mismatches)


# ---------------------------------------------------------------------------
# 5. Daily sweep — writes once (dedup refresh-in-place), supersedes on resolve.
#    Real schema: the 0055 partial-unique dedup index + 0077 kind CHECK.
# ---------------------------------------------------------------------------


@pytest.fixture()
def head_session(alembic_engine_at_head):
    with alembic_engine_at_head.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) "
            "VALUES ('ariel', 'free', :now)"
        ), {"now": datetime.now(UTC).isoformat()})
    s = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.close()


def _age_row(session, row_id, days):
    row = session.get(PortfolioSnapshotRow, row_id)
    row.imported_at = datetime.now(UTC) - timedelta(days=days)
    session.commit()


def _open_proposals(session):
    return (
        session.query(ActionProposal)
        .filter_by(user_id="ariel", status="open")
        .all()
    )


def test_sweep_writes_once_then_refreshes_then_supersedes(head_session):
    session = head_session
    real = _persist(
        session, source_path=r"D:\x.tsv", snapshot_date=date(2026, 6, 29),
        positions=[_pos("CSPX", 100.0, 800.0), _cash(50_000.0)],
    )
    _age_row(session, real.id, 9)
    fills = apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=10.0, price=810.0)],
        source_tag="fills-applied:test-deploy",
        today=date(2026, 7, 6),
    )
    _age_row(session, fills.row.id, 8)  # armed 8 days ago, no ingest since

    out1 = sweep_unverified_expectations(session, user_id="ariel")
    assert out1["armed"] >= 1 and out1["proposal"] is not None
    open_rows = _open_proposals(session)
    assert len(open_rows) == 1
    assert open_rows[0].kind == "note_only"
    assert "unverified" in open_rows[0].summary
    assert "send the current broker export" in open_rows[0].summary

    # Second sweep: refresh-in-place, still exactly ONE open row (dedup).
    out2 = sweep_unverified_expectations(session, user_id="ariel")
    assert out2["proposal"] == out1["proposal"]
    assert len(_open_proposals(session)) == 1

    # A real ingest lands and verifies clean → the proposal is superseded
    # (resolved items LEAVE the client's checklist, stored for audit).
    new_row = _persist(
        session, source_path=r"D:\y.tsv", snapshot_date=date(2026, 7, 14),
        positions=[_pos("CSPX", 110.0, 812.0), _cash(41_900.0)],
    )
    result = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=new_row, commit=True,
    )
    assert result is not None and result.mismatches == []
    assert _open_proposals(session) == []
    audit = (
        session.query(ActionProposal)
        .filter_by(user_id="ariel", status="superseded")
        .all()
    )
    assert len(audit) == 1  # stored for audit, not deleted


def test_sweep_young_expectations_write_nothing(head_session):
    session = head_session
    _persist(
        session, source_path=r"D:\x.tsv", snapshot_date=date(2026, 7, 5),
        positions=[_pos("CSPX", 100.0, 800.0), _cash(50_000.0)],
    )
    apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=10.0, price=810.0)],
        source_tag="fills-applied:test-deploy",
        today=date.today(),
    )
    out = sweep_unverified_expectations(session, user_id="ariel")
    assert out["armed"] >= 1 and out["proposal"] is None
    assert _open_proposals(session) == []


def test_mismatch_writes_then_clean_ingest_supersedes(head_session):
    session = head_session
    _persist(
        session, source_path=r"D:\x.tsv", snapshot_date=date(2026, 6, 29),
        positions=[_pos("CSPX", 100.0, 800.0), _cash(50_000.0)],
    )
    apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=10.0, price=810.0)],
        source_tag="fills-applied:test-deploy",
        today=date(2026, 7, 6),
    )
    bad = _persist(
        session, source_path=r"D:\y.tsv", snapshot_date=date(2026, 7, 10),
        positions=[_pos("CSPX", 100.0, 812.0), _cash(41_900.0)],  # fill missing
    )
    result = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=bad, commit=True,
    )
    assert result is not None and result.mismatches
    open_rows = _open_proposals(session)
    assert len(open_rows) == 1
    assert "MISMATCHED" in open_rows[0].summary
    assert open_rows[0].severity == "warning"

    # A later fills-correction + clean ingest clears the mismatch flag.
    apply_fills_to_snapshot(
        session,
        fills=[Fill(symbol="CSPX", shares=10.0, price=810.0)],
        source_tag="fills-applied:rebook",
        today=date(2026, 7, 11),
    )
    good = _persist(
        session, source_path=r"D:\z.tsv", snapshot_date=date(2026, 7, 12),
        positions=[_pos("CSPX", 110.0, 812.0), _cash(33_800.0)],
    )
    result2 = verify_and_record_on_ingest(
        session, user_id="ariel", new_row=good, commit=True,
    )
    assert result2 is not None and result2.mismatches == []
    assert _open_proposals(session) == []
