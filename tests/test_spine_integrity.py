"""PHASE 1 spine — integrity floor (spec §2A/§3), incl. the 6 Sol defects.

Uses an in-memory SQLite session with FOREIGN KEYS ENFORCED (so the composite
head FK is active) — NEVER the live DB.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import math

from argosy.services.spine.integrity import (
    CrossTenantVerdict,
    IntegrityHeadRaced,
    RESULT_FAIL,
    RESULT_PASS,
    THRESHOLD_POLICY_VERSION,
    assess_snapshot_integrity,
    compute_snapshot_content_hash,
    record_integrity_verdict,
)
from argosy.services.spine.validated_snapshot import read_validated_snapshot
from argosy.state.models import (
    Base,
    IntegrityVerdict,
    IntegrityVerdictHead,
    PortfolioSnapshotRow,
    User,
)

USER = "u-test"
OTHER = "u-other"
TODAY = date.today().isoformat()


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # enforce composite head FK (defect 4)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id=USER, plan="free", created_at=datetime.now(timezone.utc)))
    sess.add(User(id=OTHER, plan="free", created_at=datetime.now(timezone.utc)))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _pos(symbol, shares, price, *, location="schwab", currency="USD",
         asset_type="equity"):
    value_local = shares * price
    return {
        "symbol": symbol,
        "location": location,
        "shares": shares,
        "current_price": price,
        "current_value_local": value_local,
        "usd_value_k": round(value_local / 1000.0, 4),
        "currency": currency,
        "asset_type": asset_type,
        "valued_as_of": TODAY,
        "observed_as_of": TODAY,
    }


def _cash(usd_value_k, *, location="leumi", currency="ILS"):
    return {
        "symbol": "",
        "location": location,
        "asset_type": "Cash",
        "currency": currency,
        "usd_value_k": usd_value_k,
        "valued_as_of": TODAY,
        "observed_as_of": TODAY,
    }


def _add_snapshot(session, positions, *, imported_at=None, snapshot_date=None,
                  user_id=USER, totals=None):
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot_date or date.today(),
        imported_at=imported_at or datetime.now(timezone.utc),
        positions_json=positions if isinstance(positions, str) else json.dumps(positions),
        totals_json=json.dumps(totals) if totals is not None else "{}",
    )
    session.add(row)
    session.commit()
    return row


def _clean_positions():
    return [
        _pos("AAPL", 100, 200.0),
        _pos("MSFT", 50, 400.0),
        _pos("GOOG", 20, 150.0, location="ibkr"),
    ]


# ---------------------------------------------------------------------------
# content hash
# ---------------------------------------------------------------------------
def test_content_hash_stable_across_reorder():
    a = _clean_positions()
    b = list(reversed(a))
    assert compute_snapshot_content_hash(a) == compute_snapshot_content_hash(b)


def test_content_hash_changes_on_value_change():
    a = _clean_positions()
    b = _clean_positions()
    b[0]["shares"] = 101
    assert compute_snapshot_content_hash(a) != compute_snapshot_content_hash(b)


def test_content_hash_distinguishes_close_share_counts():
    # Lossless numeric repr: 6dp rounding must NOT collide distinct share counts.
    a = [_pos("AAPL", 1.0000001, 10.0)]
    b = [_pos("AAPL", 1.0000002, 10.0)]
    assert compute_snapshot_content_hash(a) != compute_snapshot_content_hash(b)


# ---------------------------------------------------------------------------
# defect 1 — corrupt/incomplete books must NOT pass
# ---------------------------------------------------------------------------
def test_unparseable_book_fails_not_empty_pass(session):
    row = _add_snapshot(session, "{not-json")  # non-empty, unparseable, no prior
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "unparseable" in (res.reason or "").lower()
    v = record_integrity_verdict(session, USER, row)
    assert v.result == RESULT_FAIL


def test_cash_account_drop_fails(session):
    prior = [_pos("AAPL", 100, 200.0), _cash(1_000.0)]  # $20k securities + $1M cash
    _add_snapshot(session, prior,
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = [_pos("AAPL", 100, 200.0)]  # $1M cash account removed
    row = _add_snapshot(session, new,
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    r = (res.reason or "").lower()
    assert "total value" in r or "account" in r  # cash drop caught (not securities-only)


def _val(symbol, usd_k, *, location="schwab", currency="USD"):
    # A value-only position (no shares/price) -> skips per-item/corrupt checks;
    # used to drive the VALUE-weighted coverage logic precisely.
    return {"symbol": symbol, "location": location, "asset_type": "equity",
            "currency": currency, "usd_value_k": usd_k,
            "valued_as_of": TODAY, "observed_as_of": TODAY}


def test_dropped_account_ge20pct_fails_even_when_total_flat(session):
    # A >=20% account vanishes but growth elsewhere holds the total flat -> FAIL
    # (measured on the ABSENT account's value, not the net total).
    prior = [_val("AAPL", 500.0, location="schwab"),
             _val("VOO", 500.0, location="leumi")]  # leumi = 50% of $1000
    _add_snapshot(session, prior,
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = [_val("AAPL", 1000.0, location="schwab")]  # total still $1000, leumi gone
    row = _add_snapshot(session, new,
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert res.detail["checks"]["account_coverage_drop"] == RESULT_FAIL
    assert res.detail["checks"]["total_value_drop"] == RESULT_PASS  # total flat


def test_small_account_closure_lt20pct_passes(session):
    # 1 of 6 accounts (~10% of value) legitimately closes -> PASS.
    prior = [_val(f"S{i}", 180.0, location=f"acct{i}") for i in range(5)]
    prior.append(_val("SMALL", 100.0, location="acct-small"))  # 100 / 1000 = 10%
    _add_snapshot(session, prior,
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = [_val(f"S{i}", 180.0, location=f"acct{i}") for i in range(5)]  # small gone
    row = _add_snapshot(session, new,
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_PASS, res.reason
    assert res.detail["checks"]["account_coverage_drop"] == RESULT_PASS


def test_small_currency_drop_lt20pct_passes(session):
    prior = [_val("US", 900.0, location="schwab", currency="USD"),
             _val("IL", 100.0, location="schwab", currency="ILS")]  # ILS = 10%
    _add_snapshot(session, prior,
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = [_val("US", 900.0, location="schwab", currency="USD")]  # ILS gone
    row = _add_snapshot(session, new,
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_PASS, res.reason
    assert res.detail["checks"]["currency_coverage_drop"] == RESULT_PASS


def test_currency_drop_ge20pct_fails_even_when_total_flat(session):
    prior = [_val("US", 700.0, location="schwab", currency="USD"),
             _val("IL", 300.0, location="schwab", currency="ILS")]  # ILS = 30%
    _add_snapshot(session, prior,
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = [_val("US", 1000.0, location="schwab", currency="USD")]  # total flat, ILS gone
    row = _add_snapshot(session, new,
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert res.detail["checks"]["currency_coverage_drop"] == RESULT_FAIL
    assert res.detail["checks"]["total_value_drop"] == RESULT_PASS  # total flat


# ---------------------------------------------------------------------------
# defect 2 — hash commits to money-critical fields + corrupt-typed field fails
# ---------------------------------------------------------------------------
def test_mutated_value_local_refused_by_gate(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    assert read_validated_snapshot(session, USER, row) is not None
    mutated = _clean_positions()
    mutated[0]["current_value_local"] = 999999.0  # money mutated post-verdict
    row.positions_json = json.dumps(mutated)
    session.commit()
    assert read_validated_snapshot(session, USER, row) is None


def test_mutated_snapshot_date_refused_by_gate(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    row.snapshot_date = date(date.today().year - 1, 1, 1)  # backdated a year
    session.commit()
    assert read_validated_snapshot(session, USER, row) is None


def test_mutated_totals_refused_by_gate(session):
    row = _add_snapshot(session, _clean_positions(),
                        totals={"total_usd_value_k": 40.0})
    record_integrity_verdict(session, USER, row)
    row.totals_json = json.dumps({"total_usd_value_k": 999999.0})
    session.commit()
    assert read_validated_snapshot(session, USER, row) is None


def test_corrupt_typed_field_fails(session):
    bad = _pos("AAPL", 100, 200.0)
    bad["shares"] = "corrupt"  # present but non-numeric money field
    row = _add_snapshot(session, [bad, _pos("MSFT", 50, 400.0)])
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "corrupt" in (res.reason or "").lower()
    assert res.detail["checks"]["corrupt_typed_field"] == RESULT_FAIL


# ---------------------------------------------------------------------------
# defect 3 — prior is the PRECEDING row, never a later one
# ---------------------------------------------------------------------------
def test_prior_selection_uses_preceding_row(session):
    jan = _add_snapshot(
        session, [_pos(f"S{i}", 100, 100.0) for i in range(10)],  # 10 pos, $1000
        imported_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        snapshot_date=date(2026, 1, 10),
    )
    feb = _add_snapshot(
        session, [_pos(f"S{i}", 100, 100.0) for i in range(8)],  # 8 pos, $800 (=20% drop)
        imported_at=datetime(2026, 2, 8, tzinfo=timezone.utc),
        snapshot_date=date(2026, 2, 8),
    )
    _add_snapshot(  # a LATER (March) snapshot must NOT be chosen as Feb's prior
        session, [_pos(f"S{i}", 100, 100.0) for i in range(10)],
        imported_at=datetime(2026, 3, 8, tzinfo=timezone.utc),
        snapshot_date=date(2026, 3, 8),
    )
    res = assess_snapshot_integrity(session, USER, feb)
    assert res.detail["prior_snapshot_id"] == jan.id
    assert res.result == RESULT_FAIL  # exact-20% drop vs Jan is caught (>= boundary)


# ---------------------------------------------------------------------------
# defect 4 — head cannot point at an unrelated verdict
# ---------------------------------------------------------------------------
def test_cross_snapshot_head_refused_by_db(session):
    snap1 = _add_snapshot(session, _clean_positions())
    v1 = record_integrity_verdict(session, USER, snap1)
    snap2 = _add_snapshot(session, _clean_positions(),
                          imported_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    # Aim snapshot 2's head at snapshot 1's pass verdict -> composite FK refuses.
    session.add(
        IntegrityVerdictHead(snapshot_id=snap2.id, current_verdict_id=v1.id, seq=1)
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_accessor_refuses_seq_mismatch(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    head = session.get(IntegrityVerdictHead, row.id)
    head.seq = 2  # verdict.verdict_seq is 1 -> mismatch
    session.commit()
    assert read_validated_snapshot(session, USER, row) is None


def test_accessor_refuses_wrong_user(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    assert read_validated_snapshot(session, USER, row) is not None
    assert read_validated_snapshot(session, OTHER, row) is None


# ---------------------------------------------------------------------------
# defect 5 — CAS with rollback: a lost race leaves no orphan verdict
# ---------------------------------------------------------------------------
def test_cas_rollback_leaves_no_orphan_verdict(session):
    row = _add_snapshot(session, _clean_positions())
    v1 = record_integrity_verdict(session, USER, row)
    assert v1.verdict_seq == 1

    # Simulate a concurrent advance: head is no longer at the expected old seq.
    head = session.get(IntegrityVerdictHead, row.id)
    head.seq = 99
    session.commit()

    with pytest.raises(IntegrityHeadRaced):
        record_integrity_verdict(session, USER, row)

    # The losing verdict was rolled back — still exactly ONE verdict row.
    verdicts = session.execute(
        sa.select(IntegrityVerdict).where(IntegrityVerdict.snapshot_id == row.id)
    ).scalars().all()
    assert len(verdicts) == 1


def test_cas_head_advances_across_two_records(session):
    row = _add_snapshot(session, _clean_positions())
    v1 = record_integrity_verdict(session, USER, row)
    v2 = record_integrity_verdict(session, USER, row)
    assert v2.verdict_seq == v1.verdict_seq + 1
    head = session.get(IntegrityVerdictHead, row.id)
    assert head.current_verdict_id == v2.id
    assert head.seq == 2
    verdicts = session.execute(
        sa.select(IntegrityVerdict).where(IntegrityVerdict.snapshot_id == row.id)
    ).scalars().all()
    assert len(verdicts) == 2  # append-only


# ---------------------------------------------------------------------------
# defect 6 — provenance recorded
# ---------------------------------------------------------------------------
def test_verdict_records_threshold_policy_and_checks(session):
    row = _add_snapshot(session, _clean_positions())
    v = record_integrity_verdict(session, USER, row)
    assert v.threshold_policy_version == THRESHOLD_POLICY_VERSION
    detail = json.loads(v.detail_json)
    assert detail["threshold_policy_version"] == THRESHOLD_POLICY_VERSION
    assert "checks" in detail and "checks_fired" in detail
    for name in ("account_coverage_drop", "currency_coverage_drop",
                 "total_value_drop", "per_item_value_local"):
        assert name in detail["checks"]
    assert detail["unavailable_checks"]  # honest data-prereq TODOs present


# ---------------------------------------------------------------------------
# baseline pass/fail + read gate
# ---------------------------------------------------------------------------
def test_clean_snapshot_passes_and_head_advances(session):
    row = _add_snapshot(session, _clean_positions())
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_PASS, res.reason
    verdict = record_integrity_verdict(session, USER, row)
    assert verdict.result == RESULT_PASS and verdict.verdict_seq == 1
    head = session.get(IntegrityVerdictHead, row.id)
    assert head.current_verdict_id == verdict.id and head.seq == 1


def test_duplicate_rows_fail(session):
    positions = _clean_positions() + [_pos("AAPL", 100, 200.0)]  # dup AAPL@schwab
    row = _add_snapshot(session, positions)
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "conservation" in (res.reason or "")


def test_ambiguous_blank_lots_fail(session):
    positions = [
        _pos("AAPL", 100, 200.0),
        _cash(5.0), _cash(5.0),  # byte-identical, no raw_line -> ambiguous
    ]
    row = _add_snapshot(session, positions)
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "ambiguous" in (res.reason or "").lower()


def test_item_value_mismatch_is_diagnostic_not_fail(session):
    # shares*price != value_local is EXPECTED for real rows whose contract
    # multiplier != 1 (e.g. Leumi index funds); with no per-instrument
    # multiplier feed, per-item reconciliation is a DIAGNOSTIC, not a hard gate —
    # else the whole legitimate live book false-refuses (found on prod snapshot
    # 54: 3 real Leumi index funds). Record the mismatch; do not FAIL on it.
    bad = _pos("AAPL", 100, 200.0)
    bad["current_value_local"] = 10000.0  # shares*price=20000 -> mismatch
    row = _add_snapshot(session, [bad, _pos("MSFT", 50, 400.0)])
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_PASS  # diagnostic, never a hard gate
    assert res.detail["checks"]["per_item_value_local"].startswith("diagnostic")
    assert res.detail["per_item_value_diagnostics"]  # the mismatch is recorded
    assert any("contract_multiplier" in u for u in res.detail["unavailable_checks"])


def test_read_validated_snapshot_returns_positions_on_pass_match(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    vs = read_validated_snapshot(session, USER, row)
    assert vs is not None and len(vs.positions) == 3
    assert vs.content_hash == compute_snapshot_content_hash(row)
    # Conservation passed, but reconciliation is unavailable -> diagnostic-grade.
    assert vs.proof_grade is False
    assert vs.unavailable_checks


# ---------------------------------------------------------------------------
# ROUND 2 defects
# ---------------------------------------------------------------------------
def test_r2_exact_20pct_drop_float_boundary_fails(session):
    # 128.45 * 0.8 == 102.75999999999999 in binary float; an exact 20% drop
    # (128.45 -> 102.76) must still FAIL despite the representation error.
    def _mv(v):
        return {"symbol": "X", "location": "schwab", "asset_type": "equity",
                "usd_value_k": v, "valued_as_of": TODAY, "observed_as_of": TODAY}

    _add_snapshot(session, [_mv(128.45)],
                  imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    row = _add_snapshot(session, [_mv(102.76)],
                        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert 128.45 * 0.8 == 102.75999999999999  # documents the float hazard
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert res.detail["checks"]["total_value_drop"] == RESULT_FAIL


def test_r2_nan_money_field_fails(session):
    bad = _pos("AAPL", 100, 200.0)
    bad["shares"] = float("nan")
    row = _add_snapshot(session, [bad, _pos("MSFT", 50, 400.0)])
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "non-finite" in (res.reason or "").lower()


def test_r2_inf_money_field_fails(session):
    bad = _pos("AAPL", 100, 200.0)
    bad["usd_value_k"] = float("inf")
    row = _add_snapshot(session, [bad, _pos("MSFT", 50, 400.0)])
    res = assess_snapshot_integrity(session, USER, row)
    assert res.result == RESULT_FAIL
    assert "non-finite" in (res.reason or "").lower()


def test_r2_review_status_flip_changes_hash_and_refused(session):
    base = _clean_positions()
    base[0]["review_status"] = "managed"
    row = _add_snapshot(session, base)
    record_integrity_verdict(session, USER, row)
    assert read_validated_snapshot(session, USER, row) is not None
    flipped = [dict(p) for p in base]
    flipped[0]["review_status"] = "unmanaged"  # reclassifies managed->unmanaged
    assert compute_snapshot_content_hash(base) != compute_snapshot_content_hash(flipped)
    row.positions_json = json.dumps(flipped)
    session.commit()
    assert read_validated_snapshot(session, USER, row) is None


def test_r2_huge_int_share_counts_no_collision():
    a = [{"symbol": "X", "location": "s", "shares": 9007199254740992}]
    b = [{"symbol": "X", "location": "s", "shares": 9007199254740993}]
    assert compute_snapshot_content_hash(a) != compute_snapshot_content_hash(b)


def test_r2_cross_tenant_author_refused(session):
    row = _add_snapshot(session, _clean_positions(), user_id=USER)
    with pytest.raises(CrossTenantVerdict):
        record_integrity_verdict(session, OTHER, row)  # OTHER over USER's snapshot
    # nothing was written
    verdicts = session.execute(
        sa.select(IntegrityVerdict).where(IntegrityVerdict.snapshot_id == row.id)
    ).scalars().all()
    assert verdicts == []


def test_r2_cross_tenant_read_refused(session):
    row = _add_snapshot(session, _clean_positions(), user_id=USER)
    record_integrity_verdict(session, USER, row)
    assert read_validated_snapshot(session, USER, row) is not None
    assert read_validated_snapshot(session, OTHER, row) is None  # not OTHER's snapshot


def test_r2_stale_head_not_served_after_advance(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)  # v1 pass, head seq1
    assert read_validated_snapshot(session, USER, row) is not None
    # Prime a stale identity-map cache of the head (seq1, pass).
    stale = session.get(IntegrityVerdictHead, row.id)
    assert stale.seq == 1
    # Advance the head to a FAIL verdict (dup book) via a fresh re-record.
    dup = _clean_positions() + [_pos("AAPL", 100, 200.0)]
    row.positions_json = json.dumps(dup)
    session.commit()
    v2 = record_integrity_verdict(session, USER, row)  # v2 fail, head seq2
    assert v2.result == RESULT_FAIL
    # Even with the stale seq1/pass head cached, the accessor reads FRESH -> None.
    assert read_validated_snapshot(session, USER, row) is None


def test_r2_proof_grade_false_when_reconciliation_unavailable(session):
    row = _add_snapshot(session, _clean_positions())
    record_integrity_verdict(session, USER, row)
    vs = read_validated_snapshot(session, USER, row)
    assert vs is not None
    assert vs.proof_grade is False
    assert any("item_source_binding" in c for c in vs.unavailable_checks)


def test_r2_provenance_columns_not_null(session):
    row = _add_snapshot(session, _clean_positions())
    v = IntegrityVerdict(
        user_id=USER, snapshot_id=row.id, result=RESULT_PASS,
        snapshot_content_hash="x" * 64, verdict_seq=1,
        threshold_policy_version=None, detail_json=None,  # provenance-free -> refused
    )
    session.add(v)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_read_validated_snapshot_none_without_head(session):
    row = _add_snapshot(session, _clean_positions())
    assert read_validated_snapshot(session, USER, row) is None


def test_read_validated_snapshot_none_on_fail_head(session):
    positions = _clean_positions() + [_pos("AAPL", 100, 200.0)]  # dup -> fail
    row = _add_snapshot(session, positions)
    record_integrity_verdict(session, USER, row)
    assert read_validated_snapshot(session, USER, row) is None
