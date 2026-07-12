"""Synthesis cost-cap per-attempt accounting (run-191 class)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.services.synthesis_cost_cap import (
    COST_CAP_FLAG_KIND,
    CostCapExceeded,
    begin_attempt,
    check_cost_cap,
    current_attempt_spend,
    trail_path_for,
)
from argosy.state.models import ActionProposal, MonitorFlag, User


def _write_costs(path: Path, amounts: list[float], *, attempt: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for amt in amounts:
            f.write(json.dumps({"cost_usd": amt, "attempt": attempt}) + "\n")


def test_resume_does_not_double_count_prior_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    token = "plan-synth-191"
    begin_attempt(token, resume_from_phase=1, home=tmp_path)
    trail = trail_path_for(token, home=tmp_path)
    # First attempt burned ~$18 across phases, then died mid phase-3.
    _write_costs(trail, [8.0, 7.0, 3.5], attempt=1)
    assert current_attempt_spend(token, home=tmp_path) == pytest.approx(18.5)

    # Resume redoes phase 3 — must NOT sum the archived 18.5 + new spend.
    begin_attempt(token, resume_from_phase=3, home=tmp_path)
    assert current_attempt_spend(token, home=tmp_path) == pytest.approx(0.0)
    archive = trail.with_name("plan-synth-191.attempt1.jsonl")
    assert archive.exists()
    _write_costs(trail, [4.0], attempt=2)
    assert current_attempt_spend(token, home=tmp_path) == pytest.approx(4.0)
    # Under a $20 cap this must PASS (run-191 wrongly saw 18.5+4 > 20).
    spent = check_cost_cap(
        decision_audit_token=token,
        cost_cap_usd=20.0,
        phase="phase_3",
        user_id="ariel",
        home=tmp_path,
        notify=False,
    )
    assert spent == pytest.approx(4.0)


def test_cap_kill_notifies_inbox_and_flag(alembic_engine_at_head, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id="ariel", plan="free"))
    sess.commit()

    token = "plan-synth-999"
    begin_attempt(token, resume_from_phase=1, home=tmp_path)
    _write_costs(trail_path_for(token, home=tmp_path), [21.0], attempt=1)

    with pytest.raises(CostCapExceeded):
        check_cost_cap(
            decision_audit_token=token,
            cost_cap_usd=20.0,
            phase="phase_2",
            user_id="ariel",
            home=tmp_path,
            notify=True,
            decision_run_id=999,
            session=sess,
        )
    sess.commit()

    flag = sess.query(MonitorFlag).filter_by(
        user_id="ariel", kind=COST_CAP_FLAG_KIND, status="active",
    ).one()
    assert "21" in flag.payload
    prop = sess.query(ActionProposal).filter_by(user_id="ariel", status="open").one()
    assert "cost cap" in prop.summary.lower()
    sess.close()
