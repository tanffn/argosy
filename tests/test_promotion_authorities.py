"""Unit tests for the per-run authority reader feeding the promote_gate."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from argosy.quality.promotion_authorities import (
    read_codex_verdict,
    read_reader_verdict,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE decision_phases ("
            "id INTEGER PRIMARY KEY, decision_run_id INTEGER, seq INTEGER, "
            "kind TEXT, phase_output_json TEXT)"
        ))
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


def _add(session, run_id, seq, kind, payload):
    session.execute(text(
        "INSERT INTO decision_phases (decision_run_id, seq, kind, phase_output_json) "
        "VALUES (:r, :s, :k, :p)"
    ), {"r": run_id, "s": seq, "k": kind, "p": payload})
    session.commit()


def test_reads_codex_and_reader_assessments(session):
    _add(session, 7, 6, "synthesis.phase_45", '{"overall_assessment": "APPROVE_WITH_CONDITIONS"}')
    _add(session, 7, 10, "synthesis.phase_55", '{"overall_assessment": "APPROVE"}')
    assert read_codex_verdict(session, 7) == "APPROVE_WITH_CONDITIONS"
    assert read_reader_verdict(session, 7) == "APPROVE"


def test_block_assessments_round_trip(session):
    _add(session, 7, 6, "synthesis.phase_45", '{"overall_assessment": "BLOCK"}')
    _add(session, 7, 10, "synthesis.phase_55", '{"overall_assessment": "BLOCK"}')
    assert read_codex_verdict(session, 7) == "BLOCK"
    assert read_reader_verdict(session, 7) == "BLOCK"


def test_missing_phase_returns_none_for_fail_closed(session):
    # No rows at all -> None so the caller fails closed.
    assert read_codex_verdict(session, 7) is None
    assert read_reader_verdict(session, 7) is None
    assert read_codex_verdict(session, None) is None


def test_takes_latest_reconcile_round(session):
    # A reconcile re-ran codex: an earlier BLOCK then a later APPROVE.
    _add(session, 7, 6, "synthesis.phase_45", '{"overall_assessment": "BLOCK"}')
    _add(session, 7, 12, "synthesis.phase_45", '{"overall_assessment": "APPROVE"}')
    assert read_codex_verdict(session, 7) == "APPROVE"


def test_unparseable_or_missing_assessment_is_none(session):
    _add(session, 7, 6, "synthesis.phase_45", "not json")
    _add(session, 7, 10, "synthesis.phase_55", '{"no_assessment_key": 1}')
    assert read_codex_verdict(session, 7) is None
    assert read_reader_verdict(session, 7) is None
