"""Tests for argosy.services.gate_outcome_store.

Covers:
  - Persistence round-trip: outcomes written by persist_gate_outcomes can be
    read back by get_gate_outcomes with all fields intact.
  - Summary line: get_gate_receipt returns the expected one-liner from
    argosy.quality.verification.summarize.
  - Failure resilience: a bad session (simulated by patching execute to raise)
    does NOT propagate to the caller; the error is logged instead.
  - Re-run idempotency: re-persisting outcomes for the same run replaces the
    old rows cleanly.
  - get_gate_receipt returns None when no rows exist.
"""
from __future__ import annotations

import logging

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.quality.verification import GateOutcome, GateStatus
from argosy.services.gate_outcome_store import (
    get_gate_outcomes,
    get_gate_receipt,
    persist_gate_outcomes,
)
from argosy.state.models import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path):
    """In-memory SQLite session with all Argosy tables created."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'test_gate.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outcomes() -> list[GateOutcome]:
    return [
        GateOutcome.passed("whole_artifact_reader"),
        GateOutcome.did_not_run(
            "codex_math",
            "codex-tandem kit not importable under test",
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPersistRoundTrip:
    """persist_gate_outcomes → get_gate_outcomes preserves all fields."""

    def test_basic_round_trip(self, db_session):
        outcomes = _outcomes()
        persist_gate_outcomes(db_session, decision_run_id=42, outcomes=outcomes)

        got = get_gate_outcomes(db_session, decision_run_id=42)
        assert len(got) == 2

        by_gate = {o.gate: o for o in got}
        assert by_gate["whole_artifact_reader"].status is GateStatus.PASS
        assert by_gate["codex_math"].status is GateStatus.DID_NOT_RUN
        assert "kit not importable" in by_gate["codex_math"].detail

    def test_override_fields_survive(self, db_session):
        outcome = GateOutcome.did_not_run(
            "codex_math", "pytest kill-switch"
        ).with_override(
            by="pytest",
            reason="codex math gate short-circuits under pytest by design",
        )
        persist_gate_outcomes(db_session, decision_run_id=7, outcomes=[outcome])

        got = get_gate_outcomes(db_session, decision_run_id=7)
        assert len(got) == 1
        assert got[0].override_by == "pytest"
        assert "short-circuits" in (got[0].override_reason or "")

    def test_empty_outcomes_is_noop(self, db_session):
        persist_gate_outcomes(db_session, decision_run_id=99, outcomes=[])
        assert get_gate_outcomes(db_session, decision_run_id=99) == []

    def test_different_runs_are_isolated(self, db_session):
        persist_gate_outcomes(
            db_session, decision_run_id=1, outcomes=[GateOutcome.passed("g1")]
        )
        persist_gate_outcomes(
            db_session, decision_run_id=2,
            outcomes=[GateOutcome.blocked("g1", "something bad")]
        )

        run1 = get_gate_outcomes(db_session, decision_run_id=1)
        run2 = get_gate_outcomes(db_session, decision_run_id=2)

        assert run1[0].status is GateStatus.PASS
        assert run2[0].status is GateStatus.BLOCK


class TestReRunIdempotency:
    """Re-persisting outcomes for the same run overwrites the old rows."""

    def test_rerun_replaces_old_rows(self, db_session):
        original = [GateOutcome.passed("whole_artifact_reader")]
        persist_gate_outcomes(db_session, decision_run_id=10, outcomes=original)

        updated = [
            GateOutcome.did_not_run(
                "whole_artifact_reader",
                "reader returned None on retry",
            )
        ]
        persist_gate_outcomes(db_session, decision_run_id=10, outcomes=updated)

        got = get_gate_outcomes(db_session, decision_run_id=10)
        assert len(got) == 1
        assert got[0].status is GateStatus.DID_NOT_RUN


class TestSummaryLine:
    """get_gate_receipt returns the expected summary string."""

    def test_all_passed(self, db_session):
        outcomes = [
            GateOutcome.passed("whole_artifact_reader"),
            GateOutcome.passed("codex_math"),
        ]
        persist_gate_outcomes(db_session, decision_run_id=20, outcomes=outcomes)

        result = get_gate_receipt(db_session, decision_run_id=20)
        assert result is not None
        _, summary = result
        assert summary.startswith("2/2 gates passed")
        # Clean run: no problem tokens
        assert "DID_NOT_RUN" not in summary
        assert "BLOCK" not in summary

    def test_did_not_run_in_summary(self, db_session):
        outcomes = [
            GateOutcome.passed("whole_artifact_reader"),
            GateOutcome.did_not_run("codex_math", "codex hung past 900s"),
        ]
        persist_gate_outcomes(db_session, decision_run_id=21, outcomes=outcomes)

        result = get_gate_receipt(db_session, decision_run_id=21)
        assert result is not None
        _, summary = result
        assert "1/2 gates passed" in summary
        assert "codex_math" in summary
        assert "DID_NOT_RUN" in summary
        assert "codex hung" in summary

    def test_no_rows_returns_none(self, db_session):
        result = get_gate_receipt(db_session, decision_run_id=9999)
        assert result is None

    def test_receipt_contains_outcome_list(self, db_session):
        outcomes = _outcomes()
        persist_gate_outcomes(db_session, decision_run_id=22, outcomes=outcomes)

        result = get_gate_receipt(db_session, decision_run_id=22)
        assert result is not None
        got_outcomes, _ = result
        assert len(got_outcomes) == 2


class TestFailureResilience:
    """persist_gate_outcomes failure does NOT propagate to the caller."""

    def test_persist_failure_does_not_raise(self, db_session, caplog):
        """Patching session.execute to blow up must not propagate."""
        original_execute = db_session.execute

        def _explode(*args, **kwargs):
            raise RuntimeError("disk full — simulated failure")

        db_session.execute = _explode

        # Must not raise.
        with caplog.at_level(logging.ERROR, logger="argosy.services.gate_outcome_store"):
            persist_gate_outcomes(
                db_session, decision_run_id=55, outcomes=_outcomes()
            )

        # Restore so the fixture teardown works.
        db_session.execute = original_execute

        # Error must have been logged (not swallowed silently).
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert error_records, "expected at least one ERROR log on persist failure"
        assert any(
            "gate_outcomes.persist_failed" in r.message or
            "gate_outcomes.persist_gate_outcomes_raised" in r.message or
            "persist_failed" in r.message
            for r in error_records
        )

    def test_get_gate_outcomes_on_missing_table_returns_empty(self, tmp_path):
        """If the table doesn't exist (pre-migration env), return [] not crash."""
        engine = sa.create_engine(
            f"sqlite:///{tmp_path / 'empty.db'}",
            connect_args={"check_same_thread": False},
        )
        # Create only the users table — NOT gate_outcomes.
        from sqlalchemy import Column, Integer, String, MetaData, Table

        meta = MetaData()
        Table("users", meta, Column("id", String(64), primary_key=True))
        meta.create_all(engine)

        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()
        try:
            result = get_gate_outcomes(session, decision_run_id=1)
            assert result == []
        finally:
            session.close()
            engine.dispose()
