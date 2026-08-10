"""Phase-2 spine bridge — the fleet's settled verdict → observed_decision ledger.

In-memory SQLite with FOREIGN KEYS ENFORCED (never the live DB). Covers the
mapping (subject/action/falsifiers/revisit_triggers), the permanently-unscorable
missing-predictive-term path, idempotency (one observation per run/verdict), and
the best-effort contract (a recording failure never propagates).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from argosy.services.spine import fleet_recording as fr
from argosy.services.spine.decisions import (
    STATUS_DIRTY_BOOK,
    STATUS_GRADABLE,
    STATUS_MISSING_TERM,
)
from argosy.services.spine.fleet_recording import (
    FLEET_DECISION_KIND,
    record_fleet_decision_best_effort,
    record_settled_fleet_decision,
)
from argosy.state.models import (
    Base,
    ObservedDecision,
    User,
    ValidatedDecision,
)

USER = "u-test"

FULL_PROSPECTIVE = {
    "target_band": {"low": 100, "high": 120},
    "alternative_at_birth": "SPY",
    "stop": 90.0,
}


@pytest.fixture()
def session_factory():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # enforce composite FKs + append-only triggers
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id=USER, plan="free", created_at=datetime.now(timezone.utc)))
    sess.commit()
    sess.close()
    return SessionLocal


@pytest.fixture()
def session(session_factory):
    sess = session_factory()
    try:
        yield sess
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# mapping — subject / action / falsifiers / revisit_triggers flow through
# ---------------------------------------------------------------------------
def test_settled_verdict_recorded_with_subject_action_and_predictive_terms(session):
    obs = record_settled_fleet_decision(
        session,
        user_id=USER,
        subject="nvda",
        action="HOLD",
        conviction="HIGH",
        decision_run_id=101,
        snapshot_id=None,
        content_hash="hash-abc",
        stop=90.0,
        falsifiers=["operating margin < 30% for 2 quarters"],
        revisit_triggers=[{"kind": "price_below", "price": 100.0}],
    )
    session.commit()

    assert obs.id is not None
    assert obs.subject == "NVDA"           # normalised
    assert obs.action == "hold"            # normalised to the fleet vocabulary
    assert obs.decision_kind == FLEET_DECISION_KIND
    assert obs.conviction == "HIGH"
    terms = obs.predictive_terms_at_birth
    assert terms["falsifiers"] == ["operating margin < 30% for 2 quarters"]
    assert terms["revisit_triggers"] == [{"kind": "price_below", "price": 100.0}]
    assert terms["stop"] == 90.0
    # target_band / alternative not authored by the trader → explicit null.
    assert terms["target_band"] is None
    assert terms["alternative_at_birth"] is None


# ---------------------------------------------------------------------------
# missing prospective term → permanently unscorable + non-promotable
# ---------------------------------------------------------------------------
def test_verdict_without_target_band_is_missing_term_and_nonpromotable(session):
    obs = record_settled_fleet_decision(
        session,
        user_id=USER,
        subject="AAPL",
        action="sell",
        decision_run_id=7,
        # a validated book is present, but the verdict lacks target_band/alt →
        # missing-predictive-term takes precedence and it stays an observation.
        validated_snapshot_id=55,
        content_hash="h",
        stop=None,
        falsifiers=["thesis break"],
    )
    session.commit()

    assert obs.validation_status_at_birth == STATUS_MISSING_TERM
    n = session.execute(
        select(sa.func.count()).select_from(ValidatedDecision).where(
            ValidatedDecision.observed_decision_id == obs.id
        )
    ).scalar_one()
    assert n == 0  # permanently unscorable — never force-promoted


def test_dirty_book_verdict_records_as_dirty_book(session):
    # No validated_snapshot_id → the raw snapshot id is the dirty-book input ref,
    # even with all prospective terms present.
    obs = record_settled_fleet_decision(
        session,
        user_id=USER,
        subject="MSFT",
        action="buy",
        decision_run_id=9,
        snapshot_id=4242,
        content_hash="h2",
        **FULL_PROSPECTIVE,
    )
    session.commit()
    assert obs.observed_source_input_id == "4242"
    assert obs.validation_status_at_birth == STATUS_DIRTY_BOOK


# ---------------------------------------------------------------------------
# promotion — gradable verdict against a validated book promotes
# ---------------------------------------------------------------------------
def test_gradable_verdict_on_validated_book_promotes(session):
    obs = record_settled_fleet_decision(
        session,
        user_id=USER,
        subject="TSM",
        action="buy",
        conviction="MED",
        decision_run_id=11,
        validated_snapshot_id=77,   # validated book → source_input_id None → gradable
        content_hash="h3",
        **FULL_PROSPECTIVE,
    )
    session.commit()

    assert obs.validation_status_at_birth == STATUS_GRADABLE
    vd = session.execute(
        select(ValidatedDecision).where(
            ValidatedDecision.observed_decision_id == obs.id
        )
    ).scalar_one()
    assert vd.input_validated_snapshot_id == "77"
    assert vd.verdict == "BUY"
    assert vd.validated_terms["stop"] == 90.0


# ---------------------------------------------------------------------------
# idempotency — identity is the RUN, not the book/hash
# ---------------------------------------------------------------------------
def test_rerecording_same_run_does_not_duplicate(session):
    kwargs = dict(
        user_id=USER,
        subject="NVDA",
        action="hold",
        decision_run_id=200,
        content_hash="stable-hash",
        stop=90.0,
        falsifiers=["x margin breaks"],
    )
    o1 = record_settled_fleet_decision(session, **kwargs)
    session.commit()
    o2 = record_settled_fleet_decision(session, **kwargs)
    session.commit()

    assert o2.id == o1.id
    n = session.execute(
        select(sa.func.count()).select_from(ObservedDecision).where(
            ObservedDecision.user_id == USER,
            ObservedDecision.subject == "NVDA",
        )
    ).scalar_one()
    assert n == 1  # append-only ledger, but the same run is recorded ONCE
    assert o1.source_decision_run_id == 200


def test_retry_after_snapshot_hash_changed_still_one_observation(session):
    # A retry of the SAME run AFTER a new snapshot arrived carries a DIFFERENT
    # content_hash (→ different birth fingerprint). Identity is the run, so it must
    # still resolve to the ONE observation — not a duplicate keyed on the hash.
    o1 = record_settled_fleet_decision(
        session, user_id=USER, subject="NVDA", action="hold",
        decision_run_id=201, content_hash="hash-A", falsifiers=["m breaks"],
    )
    session.commit()
    o2 = record_settled_fleet_decision(
        session, user_id=USER, subject="NVDA", action="hold",
        decision_run_id=201, content_hash="hash-B-newer-snapshot",
        falsifiers=["m breaks"],
    )
    session.commit()
    assert o2.id == o1.id
    n = session.execute(
        select(sa.func.count()).select_from(ObservedDecision).where(
            ObservedDecision.subject == "NVDA"
        )
    ).scalar_one()
    assert n == 1


def test_concurrent_same_run_records_exactly_one(session, monkeypatch):
    # Simulate a race: the second recorder's pre-check MISSES (as if it ran before
    # the winner committed), so it attempts the insert and loses the DB partial
    # unique index (uq_observed_decision_run) → catch re-selects the winner.
    o1 = record_settled_fleet_decision(
        session, user_id=USER, subject="NVDA", action="hold",
        decision_run_id=202, content_hash="h", falsifiers=["m"],
    )
    session.commit()

    real_sel = fr._select_by_run
    calls = {"n": 0}

    def _miss_first(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pre-check miss (winner "not yet visible")
        return real_sel(*a, **k)  # catch-branch re-select finds the winner

    monkeypatch.setattr(fr, "_select_by_run", _miss_first)
    o2 = record_settled_fleet_decision(
        session, user_id=USER, subject="NVDA", action="hold",
        decision_run_id=202, content_hash="h", falsifiers=["m"],
    )
    session.commit()

    assert o2.id == o1.id            # got the existing row, no exception
    assert calls["n"] >= 2           # pre-check missed, catch re-selected
    n = session.execute(
        select(sa.func.count()).select_from(ObservedDecision).where(
            ObservedDecision.subject == "NVDA"
        )
    ).scalar_one()
    assert n == 1                    # unique index enforced — no double append


def test_complete_terms_but_no_validated_book_is_dirty_and_nonpromotable(session):
    # No snapshot at all (resolve failed / no book) yet complete predictive terms:
    # must NOT be gradable — a decision authored against an unknown/unvalidated
    # input is dirty-book, full stop.
    obs = record_settled_fleet_decision(
        session, user_id=USER, subject="GOOG", action="buy",
        decision_run_id=303,
        snapshot_id=None, validated_snapshot_id=None, content_hash=None,
        **FULL_PROSPECTIVE,
    )
    session.commit()
    assert obs.observed_source_input_id == fr.NO_INPUT_SENTINEL
    assert obs.validation_status_at_birth == STATUS_DIRTY_BOOK
    n = session.execute(
        select(sa.func.count()).select_from(ValidatedDecision).where(
            ValidatedDecision.observed_decision_id == obs.id
        )
    ).scalar_one()
    assert n == 0  # never gradable → never promoted


def test_distinct_runs_record_distinct_observations(session):
    base = dict(user_id=USER, subject="NVDA", action="hold", content_hash="h")
    record_settled_fleet_decision(session, decision_run_id=1, **base)
    record_settled_fleet_decision(session, decision_run_id=2, **base)
    session.commit()
    n = session.execute(
        select(sa.func.count()).select_from(ObservedDecision).where(
            ObservedDecision.subject == "NVDA"
        )
    ).scalar_one()
    assert n == 2


# ---------------------------------------------------------------------------
# best-effort — a recording failure NEVER propagates (decision flow unaffected)
# ---------------------------------------------------------------------------
def test_best_effort_swallows_observe_failure(session_factory, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("spine down")

    monkeypatch.setattr(fr, "observe_decision", _boom)

    # Must NOT raise — returns None on failure.
    result = record_fleet_decision_best_effort(
        user_id=USER,
        subject="NVDA",
        action="hold",
        decision_run_id=1,
        falsifiers=["x"],
        session_factory=session_factory,
    )
    assert result is None

    # And nothing was written.
    sess = session_factory()
    try:
        n = sess.execute(
            select(sa.func.count()).select_from(ObservedDecision)
        ).scalar_one()
    finally:
        sess.close()
    assert n == 0


def test_best_effort_records_via_wrapper(session_factory):
    result = record_fleet_decision_best_effort(
        user_id=USER,
        subject="NVDA",
        action="hold",
        conviction="MED",
        decision_run_id=5,
        stop=90.0,
        falsifiers=["operating margin collapses"],
        session_factory=session_factory,
    )
    assert result is not None
    assert result.subject == "NVDA"
    assert result.action == "hold"

    sess = session_factory()
    try:
        row = sess.execute(
            select(ObservedDecision).where(ObservedDecision.subject == "NVDA")
        ).scalar_one()
    finally:
        sess.close()
    assert row.decision_kind == FLEET_DECISION_KIND
    assert row.predictive_terms_at_birth["falsifiers"] == [
        "operating margin collapses"
    ]
