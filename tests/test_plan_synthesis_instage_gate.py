from __future__ import annotations

from sqlalchemy import select

from argosy.state.models import DecisionPhase, DecisionRun

from tests.test_plan_synthesis_whole_artifact import (  # noqa: F401 — fixtures
    _reset_global_state_after_each_test, _wire_phase_stubs, synth_db,
)
from tests.test_plan_synthesis_reader_reconcile import _isolate_external_phases


def test_instage_gate_phase_recorded(synth_db, monkeypatch):
    """A synthesis run records a synthesis.phase_53 row holding the in-stage
    deterministic gate summary — proving the suite ran BEFORE the reader."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    session.expire_all()
    row = session.execute(
        select(DecisionPhase).where(
            DecisionPhase.decision_run_id == result.decision_run_id,
            DecisionPhase.kind == "synthesis.phase_53",
        )
    ).scalars().first()
    assert row is not None, "expected a synthesis.phase_53 in-stage gate row"
    assert row.phase_output_json  # carries the gate summary


def test_run_stays_running_through_gate_and_reader(synth_db, monkeypatch):
    """The terminal status is committed only after synchronous authorities."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow

    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)
    observed: list[tuple[str, str]] = []

    gate_impl = flow.run_deterministic_gate_instage

    def observing_gate(**kwargs):
        session.expire_all()
        run = session.get(DecisionRun, kwargs["decision_run_id"])
        observed.append(("gate", run.status))
        return gate_impl(**kwargs)

    reader_impl = flow.run_whole_artifact_review

    async def observing_reader(**kwargs):
        session.expire_all()
        run = session.get(DecisionRun, kwargs["decision_run_id"])
        observed.append(("reader", run.status))
        return await reader_impl(**kwargs)

    monkeypatch.setattr(flow, "run_deterministic_gate_instage", observing_gate)
    monkeypatch.setattr(flow, "run_whole_artifact_review", observing_reader)

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    assert observed == [("gate", "running"), ("reader", "running")]
    session.expire_all()
    run = session.get(DecisionRun, result.decision_run_id)
    assert run.status == "completed"
    assert run.finished_at is not None
