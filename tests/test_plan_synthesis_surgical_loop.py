"""Integration wire-test for the SURGICAL reconcile path in the reader loop.

With ARGOSY_SURGICAL_CORRECTION=1, a reader BLOCK on a RENDERABLE finding (its
cited snippet is a verbatim substring of a horizon body) is fixed by a cheap
in-place prose edit and re-read — and if that clears the BLOCK the expensive full
re-synth is SKIPPED. Deterministic: the prose editor is stubbed (no live LLM),
the reader is sequenced BLOCK→APPROVE.
"""
from __future__ import annotations

from sqlalchemy import select

from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)
from argosy.state.models import DecisionPhase, DecisionRun

from tests.test_plan_synthesis_whole_artifact import (  # noqa: F401 — fixtures
    _reset_global_state_after_each_test,
    _wire_phase_stubs,
    synth_db,
)
from tests.test_plan_synthesis_reader_reconcile import (
    _counting_phase3,
    _isolate_external_phases,
    _sequenced_reader,
)

# The long posture rendered into the long body by _make_synth_output.
_CITED = "Steady accumulation with defensive tilt."
_BLOCK_RENDERABLE = WholeArtifactVerdict(
    overall_assessment="BLOCK",
    findings=[CoherenceFinding(
        kind="contradiction", severity="BLOCKER",
        detail="posture overstates certainty",
        surfaces_cited=[_CITED],
    )],
)
_APPROVE = WholeArtifactVerdict(overall_assessment="APPROVE", findings=[])
_FIXED = "Steady accumulation with a defensive tilt, reviewed each quarter."


def test_surgical_path_fixes_segment_and_skips_resynth(synth_db, monkeypatch):
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    phase3, p3_calls = _counting_phase3(user_id)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", phase3)
    reader, r_calls = _sequenced_reader([_BLOCK_RENDERABLE, _APPROVE])
    monkeypatch.setattr(flow, "run_whole_artifact_review", reader)
    # Stub the prose editor so no live LLM call (the routine imports it lazily
    # from this module, so patching the module attribute intercepts it).
    monkeypatch.setattr(
        "argosy.agents.prose_editor.correct_prose_site",
        lambda **kw: _FIXED,
    )
    monkeypatch.setenv("ARGOSY_READER_RECONCILE", "1")
    monkeypatch.setenv("ARGOSY_SURGICAL_CORRECTION", "1")

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    # The surgical edit cleared the BLOCK on the re-read, so the full re-synth
    # (a SECOND phase-3) was SKIPPED — phase-3 ran only for the initial synth.
    assert p3_calls["n"] == 1, f"surgical must skip full re-synth; phase3 ran {p3_calls['n']}x"
    assert r_calls["n"] == 2, f"expected initial read + surgical re-read; reader ran {r_calls['n']}x"

    # FM approval stands (the block was resolved surgically).
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "approved", f"got {dr.fund_manager_decision!r}"

    # The corrected snippet is persisted in the draft's long body.
    session.expire_all()
    from argosy.state.models import PlanVersion
    draft = session.execute(
        select(PlanVersion).where(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
        .order_by(PlanVersion.id.desc())
    ).scalars().first()
    assert "reviewed each quarter" in (draft.horizon_long_md or "")
    assert _CITED not in (draft.horizon_long_md or "")

    # A phase_54 surgical row was recorded.
    row = session.execute(
        select(DecisionPhase).where(
            DecisionPhase.decision_run_id == result.decision_run_id,
            DecisionPhase.kind == "synthesis.phase_54",
        )
    ).scalars().first()
    assert row is not None and "surgical" in (row.phase_output_json or "")
