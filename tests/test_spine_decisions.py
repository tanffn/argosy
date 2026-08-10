"""PHASE 2 spine — the decision ledger (spec §2A "OBSERVED → VALIDATED → OUTCOME").

In-memory SQLite with FOREIGN KEYS ENFORCED (so the composite head FK + the
same-decision supersession FK are active) — NEVER the live DB. Covers the base
producer contracts AND one test per Sol adversarial defect (1-6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from argosy.services.spine import decisions as dec
from argosy.services.spine.decisions import (
    STATUS_DIRTY_BOOK,
    STATUS_GRADABLE,
    STATUS_MISSING_TERM,
    DecisionNotGradable,
    FingerprintMismatch,
    append_outcome,
    observe_decision,
    promote_to_validated,
)
from argosy.state.models import (
    Base,
    ObservedDecision,
    User,
    ValidatedDecision,
    ValidatedDecisionOutcome,
    ValidatedDecisionOutcomeHead,
)

USER = "u-test"
OTHER = "u-other"

FULL_TERMS = {
    "target_band": {"low": 100, "high": 120},
    "alternative_at_birth": "SPY",
    "stop": 90,
    "falsifiers": ["thesis-break"],
    "revisit_triggers": ["earnings"],
    "evaluation_due_at": "2026-12-31",
}


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # enforce composite FKs + triggers
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


def _observe(session, user=USER, *, terms=None, source_input_id=None,
             fingerprint="fp-1", subject="NVDA", conviction=None):
    return observe_decision(
        session,
        user,
        subject=subject,
        action="TRIM",
        decision_kind="trade",
        predictive_terms=FULL_TERMS if terms is None else terms,
        source_input_id=source_input_id,
        input_fingerprint=fingerprint,
        conviction=conviction,
    )


def _validated(session, *, fingerprint="fp-1"):
    obs = _observe(session, fingerprint=fingerprint)
    return promote_to_validated(
        session, obs, input_validated_snapshot_id=1, input_fingerprint=fingerprint
    )


# ---------------------------------------------------------------------------
# observe — ALWAYS writes; ingress_seq strictly monotonic per user
# ---------------------------------------------------------------------------
def test_observe_always_writes_and_freezes_six_keys(session):
    obs = _observe(session, terms={"target_band": 1, "stop": 2})
    assert obs.id is not None
    assert set(obs.predictive_terms_at_birth) == {
        "target_band", "alternative_at_birth", "stop",
        "falsifiers", "revisit_triggers", "evaluation_due_at",
    }
    assert obs.predictive_terms_at_birth["alternative_at_birth"] is None


def test_ingress_seq_monotonic_per_user_and_independent(session):
    a1 = _observe(session, USER)
    a2 = _observe(session, USER)
    a3 = _observe(session, USER)
    assert [a1.ingress_seq, a2.ingress_seq, a3.ingress_seq] == [1, 2, 3]

    b1 = _observe(session, OTHER)
    b2 = _observe(session, OTHER)
    assert [b1.ingress_seq, b2.ingress_seq] == [1, 2]
    a4 = _observe(session, USER)
    assert a4.ingress_seq == 4

    rows = session.execute(
        select(ObservedDecision.ingress_seq)
        .where(ObservedDecision.user_id == USER)
        .order_by(ObservedDecision.ingress_seq)
    ).scalars().all()
    assert rows == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# missing prospective term → permanently unscorable
# ---------------------------------------------------------------------------
def test_missing_target_band_is_missing_term_and_promotion_refused(session):
    terms = dict(FULL_TERMS)
    terms["target_band"] = None
    obs = _observe(session, terms=terms)
    assert obs.validation_status_at_birth == STATUS_MISSING_TERM

    with pytest.raises(DecisionNotGradable):
        promote_to_validated(
            session, obs, input_validated_snapshot_id=7, input_fingerprint="fp-1"
        )


def test_omitted_prospective_term_is_missing_term(session):
    obs = _observe(session, terms={"target_band": 1, "alternative_at_birth": "SPY"})
    assert obs.predictive_terms_at_birth["stop"] is None
    assert obs.validation_status_at_birth == STATUS_MISSING_TERM


# ---------------------------------------------------------------------------
# fingerprint-mismatch promotion refused
# ---------------------------------------------------------------------------
def test_fingerprint_mismatch_promotion_refused(session):
    obs = _observe(session, fingerprint="fp-birth")
    assert obs.validation_status_at_birth == STATUS_GRADABLE
    with pytest.raises(FingerprintMismatch):
        promote_to_validated(
            session, obs, input_validated_snapshot_id=3, input_fingerprint="fp-OTHER"
        )


# ---------------------------------------------------------------------------
# dirty-book observation still writes
# ---------------------------------------------------------------------------
def test_dirty_book_observation_writes_and_is_non_promotable_as_dirty(session):
    obs = _observe(session, source_input_id="raw-snap-42")
    assert obs.observed_source_input_id == "raw-snap-42"
    assert obs.validation_status_at_birth == STATUS_DIRTY_BOOK
    with pytest.raises(DecisionNotGradable):
        promote_to_validated(
            session, obs, input_validated_snapshot_id=1, input_fingerprint="fp-1"
        )


# ---------------------------------------------------------------------------
# gradable decision promotes
# ---------------------------------------------------------------------------
def test_gradable_decision_promotes(session):
    obs = _observe(session, fingerprint="fp-1", conviction="high")
    assert obs.validation_status_at_birth == STATUS_GRADABLE
    validated = promote_to_validated(
        session, obs, input_validated_snapshot_id=55, input_fingerprint="fp-1",
        verdict="TRIM", instrument_stable_id="US67066G1040",
    )
    assert validated.id is not None
    assert validated.observed_decision_id == obs.id
    assert validated.input_validated_snapshot_id == "55"
    assert validated.validated_terms["stop"] == 90


# ---------------------------------------------------------------------------
# outcome idempotency + head CAS + supersession
# ---------------------------------------------------------------------------
def test_outcome_idempotent_and_head_created(session):
    vd = _validated(session)
    o1 = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )
    o1_again = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )
    assert o1_again.outcome_id == o1.outcome_id
    assert o1.vs_benchmark_delta is None

    n = session.execute(
        select(sa.func.count()).select_from(ValidatedDecisionOutcome).where(
            ValidatedDecisionOutcome.validated_decision_id == vd.id
        )
    ).scalar_one()
    assert n == 1

    head = session.execute(
        select(ValidatedDecisionOutcomeHead).where(
            ValidatedDecisionOutcomeHead.validated_decision_id == vd.id
        )
    ).scalar_one()
    assert head.current_outcome_id == o1.outcome_id
    assert head.seq == 1


def test_outcome_supersession_advances_head_old_rows_remain(session):
    vd = _validated(session)
    o1 = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )
    o2 = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c2",
    )
    assert o2.outcome_id != o1.outcome_id
    assert o2.supersedes_outcome_id == o1.outcome_id

    head = session.execute(
        select(ValidatedDecisionOutcomeHead).where(
            ValidatedDecisionOutcomeHead.validated_decision_id == vd.id
        )
    ).scalar_one()
    assert head.current_outcome_id == o2.outcome_id
    assert head.seq == 2

    all_ids = session.execute(
        select(ValidatedDecisionOutcome.outcome_id).where(
            ValidatedDecisionOutcome.validated_decision_id == vd.id
        )
    ).scalars().all()
    assert set(all_ids) == {o1.outcome_id, o2.outcome_id}


# ===========================================================================
# Sol adversarial defects — one test each
# ===========================================================================
def test_defect1_forged_object_promotion_refused_reloads_db_state(session):
    """A forged object (real id, LIED-about gradable/fp/terms) cannot promote a
    missing-term observation — the producer RELOADS the stored birth state."""
    terms = dict(FULL_TERMS)
    terms["target_band"] = None  # missing prospective term ⇒ permanently unscorable
    real = _observe(session, terms=terms, fingerprint="fp-birth")
    assert real.validation_status_at_birth == STATUS_MISSING_TERM

    forged = SimpleNamespace(
        id=real.id,
        validation_status_at_birth=STATUS_GRADABLE,   # LIE
        birth_input_fingerprint="fp-later",           # LIE
        predictive_terms_at_birth=dict(FULL_TERMS),   # LIE
        decision_kind="trade",
        user_id=USER,
        conviction=None,
    )
    with pytest.raises(DecisionNotGradable):
        promote_to_validated(
            session, forged,
            input_validated_snapshot_id=99, input_fingerprint="fp-later",
        )
    n = session.execute(
        select(sa.func.count()).select_from(ValidatedDecision).where(
            ValidatedDecision.observed_decision_id == real.id
        )
    ).scalar_one()
    assert n == 0


def test_defect1b_promotion_requires_input_validated_snapshot_id(session):
    obs = _observe(session, fingerprint="fp-1")
    with pytest.raises(ValueError):
        promote_to_validated(
            session, obs, input_validated_snapshot_id=None, input_fingerprint="fp-1"
        )


def test_defect2_concurrent_observe_retries_no_lost_write(session):
    """A stale/colliding ingress_seq forces a UNIQUE conflict; the producer RETRIES
    with the next free seq — the write is never LOST (coverage hole)."""
    _observe(session, USER)  # seq 1 exists

    calls = {"n": 0}
    real_next = dec._next_ingress_seq

    def _stale_then_real(sess, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1  # collides with the existing seq-1 row
        return real_next(sess, uid)

    dec._next_ingress_seq = _stale_then_real
    try:
        obs = _observe(session, USER)
    finally:
        dec._next_ingress_seq = real_next

    assert obs.ingress_seq == 2  # retried onto the next free seq
    assert calls["n"] >= 2       # the first (stale) attempt collided and retried

    seqs = session.execute(
        select(ObservedDecision.ingress_seq)
        .where(ObservedDecision.user_id == USER)
        .order_by(ObservedDecision.ingress_seq)
    ).scalars().all()
    assert seqs == [1, 2]  # both writes present, none lost


def test_defect3_immutable_tables_reject_update_and_delete(session):
    """DB-level BEFORE UPDATE / BEFORE DELETE triggers make the 3 records append-only."""
    obs = _observe(session, fingerprint="fp-1")
    vd = promote_to_validated(
        session, obs, input_validated_snapshot_id=1, input_fingerprint="fp-1"
    )
    outcome = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )

    # Update a NON-FK column so ONLY the immutability trigger can raise (an FK
    # column would raise for the wrong reason and mask a missing trigger).
    cases = [
        ("observed_decision", "id", obs.id, "action"),
        ("validated_decision", "id", vd.id, "decision_kind"),
        ("validated_decision_outcome", "outcome_id", outcome.outcome_id,
         "benchmark_version"),
    ]
    for tbl, pk, pkval, col in cases:
        with pytest.raises(IntegrityError, match="append-only"):
            with session.begin_nested():
                session.execute(
                    text(f"UPDATE {tbl} SET {col}='x' WHERE {pk}=:i"), {"i": pkval}
                )
        with pytest.raises(IntegrityError, match="append-only"):
            with session.begin_nested():
                session.execute(text(f"DELETE FROM {tbl} WHERE {pk}=:i"), {"i": pkval})

    # The head table remains mutable (it is the CAS surface).
    session.execute(
        text(
            "UPDATE validated_decision_outcome_head SET seq=seq "
            "WHERE validated_decision_id=:i"
        ),
        {"i": vd.id},
    )


def test_defect4a_second_null_root_rejected(session):
    """A second disconnected supersedes-NULL root for one decision is refused by the
    partial unique index."""
    vd = _validated(session)
    append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ValidatedDecisionOutcome(
                    validated_decision_id=vd.id,
                    evaluation_window_id="w2",
                    benchmark_version="b1",
                    exposure_mapping_version="e1",
                    calculator_version="c1",
                    shadow=False,
                    vs_benchmark_delta=None,
                    supersedes_outcome_id=None,  # second disconnected root
                )
            )


def test_defect4b_concurrent_identical_append_returns_existing(session):
    """A concurrent identical append that loses the idempotency UNIQUE returns the
    EXISTING outcome, not an IntegrityError to the caller."""
    vd = _validated(session)
    o1 = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
    )

    real_sel = dec._select_outcome_by_key
    calls = {"n": 0}

    def _miss_first(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pre-check miss (row "not yet visible")
        return real_sel(*a, **k)  # catch-branch re-select finds the winner

    dec._select_outcome_by_key = _miss_first
    try:
        o2 = append_outcome(
            session, vd,
            evaluation_window_id="w1", benchmark_version="b1",
            exposure_mapping_version="e1", calculator_version="c1",
        )
    finally:
        dec._select_outcome_by_key = real_sel

    assert o2.outcome_id == o1.outcome_id  # got the existing row, no exception
    n = session.execute(
        select(sa.func.count()).select_from(ValidatedDecisionOutcome).where(
            ValidatedDecisionOutcome.validated_decision_id == vd.id
        )
    ).scalar_one()
    assert n == 1  # no double-append


def test_defect5_nonnull_vs_benchmark_delta_rejected_by_check(session):
    vd = _validated(session)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ValidatedDecisionOutcome(
                    validated_decision_id=vd.id,
                    evaluation_window_id="w1",
                    benchmark_version="b1",
                    exposure_mapping_version="e1",
                    calculator_version="c1",
                    shadow=False,
                    vs_benchmark_delta=123.45,  # forbidden — DEFERRED
                    supersedes_outcome_id=None,
                )
            )


def test_defect6_provenance_columns_present(session):
    """The §2A provenance columns exist and are populated by the producers."""
    obs = _observe(session, fingerprint="fp-1", conviction="high")
    assert obs.conviction == "high"

    vd = promote_to_validated(
        session, obs, input_validated_snapshot_id=1, input_fingerprint="fp-1",
        verdict="TRIM", instrument_stable_id="US67066G1040",
        cost_basis_completeness="full-lot", metadata_freshness="2026-08-01",
        equivalence_evidence={"index_identity_gate_result": "pass"},
    )
    assert vd.instrument_stable_id == "US67066G1040"
    assert vd.decision_kind == "trade"
    assert vd.verdict == "TRIM"
    assert vd.cost_basis_completeness == "full-lot"
    assert vd.metadata_freshness == "2026-08-01"
    assert vd.equivalence_evidence["index_identity_gate_result"] == "pass"
    assert vd.validated_terms["stop"] == 90
    assert vd.validated_terms["verdict"] == "TRIM"

    outcome = append_outcome(
        session, vd,
        evaluation_window_id="w1", benchmark_version="b1",
        exposure_mapping_version="e1", calculator_version="c1",
        linking_algorithm_version="carino-v1", outcome_kind="win",
        post_mortem_category="thesis-confirmed", regime="bull", shadow=True,
    )
    assert outcome.linking_algorithm_version == "carino-v1"
    assert outcome.outcome_kind == "win"
    assert outcome.post_mortem_category == "thesis-confirmed"
    assert outcome.regime == "bull"
    assert outcome.shadow is True
