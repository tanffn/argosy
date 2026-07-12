"""Verdict provenance projection — UX §7.1 (falsifier state + clocks)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.verdict_registry import (
    UNLOCK_DEDUP_PREFIX,
    provenance_for_subject,
    provenance_for_subjects,
    write_verdict,
)
from argosy.state.models import (
    ActionProposal,
    Base,
    DecisionRun,
    HoldingReview,
    User,
    Verdict,
)

_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'prov.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _run(s, *, finished_at: datetime, ticker: str = "ORCL") -> DecisionRun:
    row = DecisionRun(
        user_id="ariel",
        ticker=ticker,
        decision_kind="consult",
        tier="T2",
        status="completed",
        started_at=finished_at - timedelta(minutes=30),
        finished_at=finished_at,
    )
    s.add(row)
    s.commit()
    return row


def test_none_recorded_when_no_verdict_row(db):
    """Missing registry row still emits the WARNING state — never blank."""
    p = provenance_for_subject(db, user_id="ariel", subject="ZZZZ")
    assert p.falsifier_state == "none_recorded"
    assert p.falsifiers == ()
    assert p.next_validation is None
    d = p.to_dict()
    assert d["falsifier_state"] == "none_recorded"
    assert "falsifiers" in d


def test_armed_with_falsifiers_and_clock(db):
    run = _run(db, finished_at=_NOW - timedelta(days=2))
    write_verdict(
        db,
        user_id="ariel",
        subject="ORCL",
        verdict="WAIT",
        conviction="HIGH",
        falsifiers=["FCF turns sustainably positive"],
        next_validation=date(2026, 10, 1),
        source_decision_run_id=run.id,
        reasoning_md="hold for now",
    )
    db.commit()

    p = provenance_for_subject(db, user_id="ariel", subject="orcl")
    assert p.falsifier_state == "armed"
    assert p.falsifiers == ("FCF turns sustainably positive",)
    assert p.next_validation == "2026-10-01"
    assert p.last_fleet_check_at is not None
    assert p.last_fleet_check_at.startswith("2026-07-10")


def test_empty_falsifiers_list_is_none_recorded(db):
    write_verdict(
        db,
        user_id="ariel",
        subject="META",
        verdict="HOLD",
        conviction="MED",
        falsifiers=[],
        next_validation=None,
    )
    db.commit()
    p = provenance_for_subject(db, user_id="ariel", subject="META")
    assert p.falsifier_state == "none_recorded"


def test_fired_when_unlock_inbox_row_open(db):
    v = write_verdict(
        db,
        user_id="ariel",
        subject="OKLO",
        verdict="HOLD",
        conviction="MED",
        falsifiers=["First criticality slips past July 2026"],
        next_validation=date(2026, 7, 31),
    )
    db.add(
        ActionProposal(
            user_id="ariel",
            summary="revisit unlocked: OKLO",
            rationale_md="trigger fired",
            suggested_payload="{}",
            severity="info",
            kind="note_only",
            status="open",
            dedup_key=f"{UNLOCK_DEDUP_PREFIX}:OKLO:{v.id}",
            surfaced_at=_NOW,
            expires_at=_NOW + timedelta(days=30),
            execution_state="proposed",
        )
    )
    db.commit()

    p = provenance_for_subject(db, user_id="ariel", subject="OKLO")
    assert p.falsifier_state == "fired"


def test_holding_review_fallback_for_last_check(db):
    reviewed = _NOW - timedelta(days=5)
    db.add(
        HoldingReview(
            user_id="ariel",
            symbol="AMD",
            reviewed_at=reviewed,
            verdict="HOLD",
            outcome="hold",
            reason="ok",
        )
    )
    db.commit()
    p = provenance_for_subject(db, user_id="ariel", subject="AMD")
    assert p.falsifier_state == "none_recorded"
    assert p.last_fleet_check_at is not None
    assert "2026-07-07" in p.last_fleet_check_at


def test_batch_returns_all_requested_subjects(db):
    write_verdict(
        db,
        user_id="ariel",
        subject="SOFI",
        verdict="HOLD",
        conviction="LOW",
        falsifiers=["Dilution accelerates"],
    )
    db.commit()
    m = provenance_for_subjects(
        db, user_id="ariel", subjects=["SOFI", "BMY", "missing"]
    )
    assert set(m) == {"SOFI", "BMY", "MISSING"}
    assert m["SOFI"].falsifier_state == "armed"
    assert m["BMY"].falsifier_state == "none_recorded"
    assert m["MISSING"].falsifier_state == "none_recorded"


def test_verdict_model_importable(db):
    assert db.execute(sa.select(Verdict)).scalars().all() == []
