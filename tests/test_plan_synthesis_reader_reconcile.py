"""Integration wire-test for the whole-artifact READER reconcile loop.

The reader already BLOCKS a draft with coherence holes. This loop gives it the
SAME feedback the codex zigzag has: a reader BLOCK on a fixable coherence hole
folds the finding into synthesizer guidance, RE-RUNS synthesis, RE-PERSISTS the
draft, and RE-READS — so a fixable contradiction comes back RESOLVED without a
human editing prose. Bounded + fail-closed: if it still BLOCKS after the bound,
the draft stays not-auto-promotable (``fund_manager_decision='rejected'``).

Reuses the whole-artifact wire-test harness (``_wire_phase_stubs`` +
``synth_db``) and overrides phase-3 / the reader with call-counting stubs so the
loop's control flow is observable without a live LLM.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from argosy.agents.base import AgentReport
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)
from argosy.state.models import DecisionPhase, DecisionRun

from tests.test_plan_synthesis_v4_e2e import _make_agent_report, _make_synth_output
from tests.test_plan_synthesis_whole_artifact import (  # noqa: F401 — fixtures
    _reset_global_state_after_each_test,
    _wire_phase_stubs,
    synth_db,
)

_BLOCK = WholeArtifactVerdict(
    overall_assessment="BLOCK",
    findings=[CoherenceFinding(
        kind="contradiction", severity="BLOCKER",
        detail="net worth stated as two different values",
        surfaces_cited=["NW = 11.95M", "NW = 14.15M"],
    )],
)
_APPROVE = WholeArtifactVerdict(overall_assessment="APPROVE", findings=[])


def _isolate_external_phases(monkeypatch):
    """Stub the alternatives subflow — it makes a REAL agent (claude.exe) call
    that is irrelevant to the reconcile loop and slow/flaky under pytest. The
    orchestrator imports it locally, so patch the module attribute it binds."""
    from argosy.orchestrator.flows.plan_synthesis import alternatives_phase
    monkeypatch.setattr(alternatives_phase, "run_alternatives_phase",
                        lambda **kw: None)


def _counting_phase3(user_id):
    out = _make_synth_output()
    calls = {"n": 0}

    def _stub(**kw):
        calls["n"] += 1
        return out, [_make_agent_report(
            role="plan_synthesizer",
            user_id=kw.get("user_id", user_id),
            decision_id=kw.get("decision_run_id", "stub-decision"),
            output=out,
        )]

    return _stub, calls


def _sequenced_reader(verdicts):
    calls = {"n": 0}

    async def _stub(**kw):
        verdict = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        row = AgentReport(
            agent_role="whole_artifact_reader",
            user_id=kw.get("user_id", "test_ariel"),
            model="gpt-5-codex",
            response_text=verdict.model_dump_json(),
            tokens_in=0, tokens_out=0, cost_usd=0.0, prompt_hash="",
            confidence=None, output=verdict,
            decision_id=f"plan-synth-{kw.get('decision_run_id')}",
            run_correlation_id="corr-reader", system_prompt="", user_prompt="(p)",
        )
        return verdict, row

    return _stub, calls


def _reader_phase_payload(session, decision_run_id):
    session.expire_all()
    row = session.execute(
        select(DecisionPhase).where(
            DecisionPhase.decision_run_id == decision_run_id,
            DecisionPhase.kind == "synthesis.phase_55",
        )
    ).scalars().first()
    assert row is not None, "expected a synthesis.phase_55 reader phase row"
    return json.loads(row.phase_output_json)


def test_reader_block_reconciles_to_clean(synth_db, monkeypatch):
    """A reader BLOCK on a fixable hole → re-synth → re-read APPROVES, WITHOUT
    a human editing prose. Synthesis ran twice; the draft is NOT rejected; the
    reader_reconcile marker shows triggered + resolved."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    phase3, p3_calls = _counting_phase3(user_id)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", phase3)
    reader, r_calls = _sequenced_reader([_BLOCK, _APPROVE])
    monkeypatch.setattr(flow, "run_whole_artifact_review", reader)
    monkeypatch.setenv("ARGOSY_READER_RECONCILE", "1")

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    assert p3_calls["n"] == 2, f"expected 1 re-synth round; phase3 ran {p3_calls['n']}x"
    assert r_calls["n"] == 2, f"expected a re-read; reader ran {r_calls['n']}x"

    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "approved", (
        "reconcile resolved the BLOCK; FM's approval must stand "
        f"(got {dr.fund_manager_decision!r})"
    )

    marker = _reader_phase_payload(session, result.decision_run_id)["reader_reconcile"]
    assert marker["triggered"] is True
    assert marker["still_blocking"] is False


def test_reader_block_persists_when_unresolved(synth_db, monkeypatch):
    """If the re-synth does NOT clear the hole, the loop is bounded + fail-closed:
    the draft stays rejected and the marker reports still_blocking."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    phase3, p3_calls = _counting_phase3(user_id)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", phase3)
    reader, r_calls = _sequenced_reader([_BLOCK, _BLOCK, _BLOCK])
    monkeypatch.setattr(flow, "run_whole_artifact_review", reader)
    monkeypatch.setenv("ARGOSY_READER_RECONCILE", "1")

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    # Bounded to ONE reconcile round: initial synth + 1 re-synth = 2.
    assert p3_calls["n"] == 2, f"loop must be bounded; phase3 ran {p3_calls['n']}x"
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "rejected"
    marker = _reader_phase_payload(session, result.decision_run_id)["reader_reconcile"]
    assert marker["triggered"] is True
    assert marker["still_blocking"] is True


def test_reader_reconcile_disabled_by_env(synth_db, monkeypatch):
    """ARGOSY_READER_RECONCILE=0 keeps the old behavior: a BLOCK rejects with
    NO re-synth and no reconcile marker."""
    session, user_id = synth_db, "test_ariel"
    from argosy.orchestrator.flows import plan_synthesis as flow
    _wire_phase_stubs(monkeypatch, flow, user_id)
    _isolate_external_phases(monkeypatch)

    phase3, p3_calls = _counting_phase3(user_id)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", phase3)
    reader, r_calls = _sequenced_reader([_BLOCK, _APPROVE])
    monkeypatch.setattr(flow, "run_whole_artifact_review", reader)
    monkeypatch.setenv("ARGOSY_READER_RECONCILE", "0")

    result = flow.run_synthesis(session, user_id=user_id, trigger="scheduled")

    assert p3_calls["n"] == 1, "disabled: no re-synth"
    assert r_calls["n"] == 1, "disabled: no re-read"
    dr = session.get(DecisionRun, result.decision_run_id)
    assert dr.fund_manager_decision == "rejected"
    payload = _reader_phase_payload(session, result.decision_run_id)
    assert "reader_reconcile" not in payload
