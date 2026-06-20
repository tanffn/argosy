"""Integration wire-test for the COHERENCE-DELIBERATION reconcile path.

With ARGOSY_COHERENCE_DELIBERATION=1, a reader BLOCK on an arbitrable coherence
hole is routed through ``run_coherence_deliberation_pass`` (cluster -> route ->
resolver|panel+arbitrator -> conform -> verify), each ruling is persisted to the
coherence ledger, and the draft is RE-READ with the accumulated rulings injected.
Fail-closed: a non-ok pass does NOT promote and does NOT fall back to the markdown
closer (the full re-synth is skipped entirely while this path is on).

Deterministic: the deliberation pass is stubbed (no live panel/arbitrator
claude.exe calls — the pass internals are unit-tested in tests/coherence), the
reader is sequenced BLOCK->APPROVE, and the numeric resolver is stubbed off.
"""
from __future__ import annotations

from sqlalchemy import select

from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import PassResult
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)
from argosy.state.models import CoherenceDecision, DecisionRun

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

_BLOCK = WholeArtifactVerdict(
    overall_assessment="BLOCK",
    findings=[CoherenceFinding(
        kind="contradiction", severity="BLOCKER",
        detail="retirement age stated two ways",
        surfaces_cited=["age 46", "age 54"],
        subject_type="retirement_age_headline",
    )],
)
_APPROVE = WholeArtifactVerdict(overall_assessment="APPROVE", findings=[])

_RULING = {
    "dispute_key": "dk-ret-age", "subject_type": "retirement_age_headline",
    "question": "which age leads?", "ruling": "age 46 leads; 54 strict track",
    "rationale": "prime directive: earliest-safe leads", "basis": "canonical_fact",
    "resolved_by": "arbitrator", "invariants": [], "conformed_surfaces": ["long_md"],
}


def _stub_pass(*, ok: bool):
    calls = {"n": 0, "findings": None, "rulings": None}

    def _fn(**kw):
        calls["n"] += 1
        calls["findings"] = kw.get("findings")
        calls["rulings"] = kw.get("settled_rulings")
        if not ok:
            return PassResult(ok=False, bodies=kw["bodies"],
                              json_surfaces=kw["json_surfaces"], rulings=[],
                              errors=["untypeable dispute -> BLOCK"])
        return PassResult(
            ok=True,
            bodies={"long_md": "conformed long", "medium_md": "conformed medium",
                    "short_md": "conformed short"},
            json_surfaces={}, rulings=[_RULING],
        )

    return _fn, calls


def _wire(monkeypatch, flow, reader_verdicts, pass_ok):
    """Common wiring: phase stubs, isolated externals, stubbed phase-3, sequenced
    reader, stubbed deliberation pass, resolver off, both flags on."""
    _wire_phase_stubs(monkeypatch, flow, "test_ariel")
    _isolate_external_phases(monkeypatch)
    phase3, p3_calls = _counting_phase3("test_ariel")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", phase3)
    reader, r_calls = _sequenced_reader(reader_verdicts)
    monkeypatch.setattr(flow, "run_whole_artifact_review", reader)
    pass_fn, pass_calls = _stub_pass(ok=pass_ok)
    monkeypatch.setattr(flow, "run_coherence_deliberation_pass", pass_fn)
    monkeypatch.setenv("ARGOSY_READER_RECONCILE", "1")
    monkeypatch.setenv("ARGOSY_COHERENCE_DELIBERATION", "1")
    return p3_calls, r_calls, pass_calls


def test_deliberation_resolves_block_persists_ruling_and_rereads(synth_db, monkeypatch):
    """A reader BLOCK -> deliberation pass (ok) -> ledger ruling persisted ->
    re-read with rulings injected APPROVES. The full re-synth is SKIPPED."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    p3_calls, r_calls, pass_calls = _wire(monkeypatch, flow, [_BLOCK, _APPROVE], pass_ok=True)

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    # Deliberation ran instead of a full re-synth.
    assert pass_calls["n"] == 1, "deliberation pass must run once"
    assert p3_calls["n"] == 1, f"full re-synth must be SKIPPED; phase3 ran {p3_calls['n']}x"
    assert r_calls["n"] == 2, f"expected initial read + re-read; reader ran {r_calls['n']}x"

    # The reader classified the finding; the orchestrator passed it through.
    assert pass_calls["findings"] and pass_calls["findings"][0]["subject_type"] == "retirement_age_headline"

    # A ledger ruling was persisted.
    rows = session.execute(
        select(CoherenceDecision).where(CoherenceDecision.user_id == user_id)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].subject_type == "retirement_age_headline"
    assert rows[0].resolved_by == "arbitrator"

    # The re-read APPROVED, so the FM approval stands.
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "approved", f"got {dr.fund_manager_decision!r}"

    # The conformed bodies were persisted to the draft.
    session.expire_all()
    from argosy.state.models import PlanVersion
    draft = session.execute(
        select(PlanVersion).where(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
        .order_by(PlanVersion.id.desc())
    ).scalars().first()
    assert draft.horizon_long_md == "conformed long"


def test_deliberation_failclosed_does_not_promote_or_fall_back(synth_db, monkeypatch):
    """A non-ok deliberation pass must NOT promote and must NOT fall back to the
    full re-synth — the draft stays rejected (fail-closed)."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    p3_calls, r_calls, pass_calls = _wire(monkeypatch, flow, [_BLOCK, _APPROVE], pass_ok=False)

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    assert pass_calls["n"] == 1, "deliberation pass must run once"
    assert p3_calls["n"] == 1, "fail-closed: NO fall-back full re-synth"
    assert r_calls["n"] == 1, "fail-closed: no re-read after a blocked pass"

    # No ruling persisted on a failed pass.
    rows = session.execute(
        select(CoherenceDecision).where(CoherenceDecision.user_id == user_id)
    ).scalars().all()
    assert rows == []

    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "rejected", f"got {dr.fund_manager_decision!r}"


def test_deliberation_disabled_by_env_uses_old_path(synth_db, monkeypatch):
    """ARGOSY_COHERENCE_DELIBERATION unset → the deliberation pass never runs;
    the reader BLOCK takes the existing reconcile/re-synth path."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    p3_calls, r_calls, pass_calls = _wire(monkeypatch, flow, [_BLOCK, _APPROVE], pass_ok=True)
    monkeypatch.setenv("ARGOSY_COHERENCE_DELIBERATION", "0")
    # Isolate the OLD reconcile/re-synth fallback: with the default-ON owner-routed
    # path also disabled, a reader BLOCK must reach the legacy full re-synth.
    monkeypatch.setenv("ARGOSY_OWNER_ROUTED_RECONCILE", "0")

    flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    assert pass_calls["n"] == 0, "disabled: deliberation pass must NOT run"
    # The old reader-reconcile path re-synthesized (phase3 ran a 2nd time).
    assert p3_calls["n"] == 2, f"disabled: old re-synth path runs; phase3 ran {p3_calls['n']}x"
