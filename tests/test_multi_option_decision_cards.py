"""Multi-option accept + corrective directive carries CHOICE (§7.3)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.action_proposals import accept_action_proposal
from argosy.services.corrective_context import build_corrective_context
from argosy.state.models import ActionProposal, Base, User

_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'choice.db'}",
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


def _seed_multi(s, *, recommendation="A_keep_5pct"):
    payload = {
        "recommendation": recommendation,
        "options": {
            "A_keep_5pct": "status quo; falsifiers earn the raise",
            "B_raise_10pct_now": "REJECTED by fleet",
            "C_ladder": {
                "auto_raise_to_pct": 8.0,
                "then": "8->10% owner fork",
            },
        },
        "facts": {"sigma_anchor": 0.18},
    }
    row = ActionProposal(
        user_id="ariel",
        summary="Raise high-growth sleeve?",
        rationale_md="Fleet recommended A.",
        suggested_payload=json.dumps(payload),
        severity="info",
        kind="update_plan_assumption",
        status="open",
        surfaced_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
        execution_state="proposed",
        dedup_key="growth_sleeve_size_test:ariel",
    )
    s.add(row)
    s.commit()
    return row


def test_accept_choice_persists_decision_and_note(db):
    row = _seed_multi(db, recommendation="A_keep_5pct")
    out = accept_action_proposal(
        db, row.id, user_id="ariel", choice_key="C_ladder"
    )
    assert out.status == "accepted"
    assert out.decided_by_user_note == "C_ladder"
    payload = json.loads(out.suggested_payload)
    assert payload["decision"] == "C_ladder"
    assert payload["recommendation"] == "A_keep_5pct"  # unchanged


def test_accept_choice_rejects_unknown_key(db):
    row = _seed_multi(db)
    with pytest.raises(ValueError, match="not in options"):
        accept_action_proposal(db, row.id, user_id="ariel", choice_key="Z_nope")


def test_corrective_directive_carries_chosen_not_recommendation(db):
    row = _seed_multi(db, recommendation="A_keep_5pct")
    accept_action_proposal(db, row.id, user_id="ariel", choice_key="C_ladder")

    ctx = build_corrective_context(db, user_id="ariel")
    assert ctx is not None
    assert ctx.directives
    d = ctx.directives[0]
    assert d.summary == "Owner chose C_ladder"
    assert "CHOSEN OPTION `C_ladder`" in d.detail
    assert "not the recommendation" in d.detail
    assert "`A_keep_5pct`" in d.detail
    assert "auto_raise_to_pct" in d.detail or "8.0" in d.detail
    # Chosen body must not be option A's prose
    chosen_section = d.detail.split("CHOSEN OPTION", 1)[1]
    assert "status quo" not in chosen_section
    assert "C_ladder" in (ctx.rendered or "")
