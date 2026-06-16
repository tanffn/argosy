from __future__ import annotations

from sqlalchemy import select

from argosy.state.models import DecisionPhase

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
