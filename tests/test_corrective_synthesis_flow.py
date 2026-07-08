"""Corrective re-synthesis — orchestrator wiring tests.

Design: docs/design/corrective_resynthesis.md §5 (orchestrator: corrective
tier chosen iff fresh phase-1/2 outputs + no snapshot-class correction;
degrade-to-full logged; flag OFF = today's behavior; corrections-landed gate
writes corrective_unresolved + ONE inbox row).

All agent phases are stubbed (no live LLM call) — same pattern as
tests/test_plan_synthesis_flow.py; the autouse conftest guards stub the
alternatives phase and the pytest kill switches keep codex/reader off.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import (
    ActionProposal,
    DecisionPhase,
    DecisionRun,
    PlanVersion,
    User,
)


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="Jacobs v2.0",
        raw_markdown="# Plan", distillate_rendered="# Plan distillate\n",
    ))
    s.commit()
    yield s
    s.close()


def _stub_synthesis_output():
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
    )

    return PlanSynthesisOutput(
        long=HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change",
            posture="long posture",
        ),
        medium=HorizonSection(
            horizon="medium", freshness_expected="quarterly",
            status="minor_revision", posture="medium posture",
        ),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly",
            status="major_revision", posture="short posture",
        ),
        inputs=SynthesisInputs(),
    )


def _stub_phases(flow, monkeypatch, ran=None, seen_guidance=None):
    """Stub all five phases; optionally record which ran + phase-3 guidance."""
    ran = ran if ran is not None else []

    def _p1(**kw):
        ran.append(1)
        return "(analysts)"

    def _p2(**kw):
        ran.append(2)
        return "(debates)"

    def _p3(**kw):
        ran.append(3)
        if seen_guidance is not None:
            seen_guidance.append(kw.get("guidance", ""))
        return _stub_synthesis_output()

    def _p4(**kw):
        ran.append(4)
        return "(risk)"

    def _p5(**kw):
        ran.append(5)
        return True
    monkeypatch.setattr(flow, "_run_phase_1_analysts", _p1)
    monkeypatch.setattr(flow, "_run_phase_2_debates", _p2)
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", _p3)
    monkeypatch.setattr(flow, "_run_phase_4_risk", _p4)
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", _p5)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")
    return ran


def _make_ctx(corrections=None, directives=None, proposal_ids=None,
              forces_full_tier=False, source_critique_id=None):
    from argosy.services.corrective_context import CorrectiveContext

    ctx = CorrectiveContext(
        corrections=corrections or [],
        directives=directives or [],
        proposal_ids=proposal_ids or [],
        source_critique_id=source_critique_id,
        forces_full_tier=forces_full_tier,
    )
    from argosy.services.corrective_context import _render_block
    ctx.rendered = _render_block(ctx)
    return ctx


def _seed_completed_run(session, *, days_ago=0.0, phases=(1, 2)) -> int:
    """A completed plan_revision run with persisted phase outputs."""
    finished = datetime.now(timezone.utc) - timedelta(days=days_ago)
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="completed",
        started_at=finished - timedelta(hours=1), finished_at=finished,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    for seq, p in enumerate(phases, 1):
        payload = (
            json.dumps({"analyst_reports_text": "(reused analysts)",
                        "adapter_outcomes": []})
            if p == 1 else "(reused debates)"
        )
        session.add(DecisionPhase(
            decision_run_id=run.id, user_id="ariel", seq=seq,
            kind=f"synthesis.phase_{p}", started_at=finished,
            participants_json="[]", phase_output_json=payload,
        ))
    session.commit()
    return run.id


# ----------------------------------------------------------------------
# Flag OFF — today's behavior, builder never called, guidance untouched.
# ----------------------------------------------------------------------


def test_flag_off_is_todays_behavior(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_CORRECTIVE_SYNTHESIS", "0")
    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")

    def _boom(*a, **k):
        raise AssertionError("builder must not be called when the flag is off")

    monkeypatch.setattr(flow, "build_corrective_context", _boom)
    seen_guidance: list[str] = []
    _stub_phases(flow, monkeypatch, seen_guidance=seen_guidance)

    out = flow.run_synthesis(
        session, user_id="ariel", trigger="scheduled", guidance="user words",
    )
    assert out.draft_id is not None
    assert seen_guidance == ["user words"]  # byte-identical guidance
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    assert "corrective" not in inputs
    assert "corrective_unresolved" not in inputs


def test_builder_failure_is_fail_soft(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")

    def _boom(*a, **k):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(flow, "build_corrective_context", _boom)
    seen_guidance: list[str] = []
    _stub_phases(flow, monkeypatch, seen_guidance=seen_guidance)

    out = flow.run_synthesis(
        session, user_id="ariel", trigger="scheduled", guidance="user words",
    )
    assert out.draft_id is not None  # degrades to today's behavior
    assert seen_guidance == ["user words"]


# ----------------------------------------------------------------------
# Auto-attach — corrections prepended to guidance (all phases see them).
# ----------------------------------------------------------------------


def test_corrections_prepended_to_guidance(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.services.corrective_context import Correction

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    ctx = _make_ctx(corrections=[Correction(
        index=1, severity="RED", topic="fx-rate",
        plan_item_ref="assumptions.fx", summary="FX 3.00 vs 2.944",
    )])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    seen_guidance: list[str] = []
    _stub_phases(flow, monkeypatch, seen_guidance=seen_guidance)

    out = flow.run_synthesis(
        session, user_id="ariel", trigger="scheduled", guidance="user words",
    )
    assert out.draft_id is not None
    assert len(seen_guidance) == 1
    assert seen_guidance[0].startswith("CORRECTIVE RE-SYNTHESIS")
    assert "FX 3.00 vs 2.944" in seen_guidance[0]
    assert seen_guidance[0].rstrip().endswith("user words")


# ----------------------------------------------------------------------
# Corrective tier — phases 1-2 reuse iff fresh outputs + no snapshot-class
# correction; phases 3-5 ALWAYS fresh.
# ----------------------------------------------------------------------


def test_corrective_tier_reuses_fresh_phases_1_2(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    prior_run_id = _seed_completed_run(session, days_ago=2)
    ctx = _make_ctx()
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [3, 4, 5], f"phases 1-2 must be reused, 3-5 fresh; ran={ran}"
    pv = session.get(PlanVersion, out.draft_id)
    corrective = json.loads(pv.synthesis_inputs_json)["corrective"]
    assert corrective["reused_from_run_id"] == prior_run_id
    assert corrective["reused_phases"] == [1, 2]


def test_snapshot_class_correction_forces_full_tier(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    _seed_completed_run(session, days_ago=2)  # fresh — but must NOT be used
    ctx = _make_ctx(forces_full_tier=True)
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]
    pv = session.get(PlanVersion, out.draft_id)
    corrective = json.loads(pv.synthesis_inputs_json)["corrective"]
    assert corrective["reused_from_run_id"] is None
    assert corrective["reused_phases"] == []


def test_stale_prior_run_degrades_to_full(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    _seed_completed_run(session, days_ago=30)  # beyond the 14-day window
    ctx = _make_ctx()
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]


def test_partial_prior_phases_degrade_to_full(session, monkeypatch):
    """A prior run missing phase 2 must not be silently half-reused."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    _seed_completed_run(session, days_ago=1, phases=(1,))  # phase 2 missing
    ctx = _make_ctx()
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]


def test_corrective_resume_never_reuses_phase_3_plus(session, monkeypatch):
    """Codex blocker #2: in corrective mode a same-run resume must re-run
    phases 3-5 (the synthesizer must re-derive WITH the corrections; risk/FM
    are blind gates) even when all five phases previously completed."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="completed",
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    monkeypatch.setattr(
        flow, "_load_completed_phase_outputs",
        lambda *a, **k: {
            1: json.dumps({"analyst_reports_text": "(reused)",
                           "adapter_outcomes": []}),
            2: "(reused debates)",
            3: _stub_synthesis_output().model_dump_json(),
            4: "(reused risk)",
            5: "approved",
        },
    )
    ctx = _make_ctx()
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(
        session, user_id="ariel", trigger="check_in",
        existing_decision_run_id=run.id, resume_from_phase=5,
    )
    assert out.draft_id is not None
    # Without the corrective truncation, resume_from_phase=5 reuses 3+4 too.
    assert ran == [3, 4, 5], f"phases 3-5 must re-run in corrective mode; ran={ran}"


def test_explicit_reuse_run_validated(session, monkeypatch):
    """Codex blocker #3: an explicitly passed reuse run is refused when it
    belongs to another user (degrade to full run, never cross-tenant reuse)."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    session.add(User(id="other", plan="free"))
    session.commit()
    foreign = DecisionRun(
        user_id="other", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="completed",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(foreign)
    session.commit()
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: None)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(
        session, user_id="ariel", trigger="scheduled",
        reuse_phases_from_run_id=foreign.id,
    )
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]  # foreign run refused → full run


def test_gate_crash_fails_closed(session, monkeypatch):
    """Codex blocker #4: a crash in the corrections-landed check must persist
    a blocking corrective_unresolved marker, never read as 'all landed'."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import corrections_check
    from argosy.services.corrective_context import Correction

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    ctx = _make_ctx(corrections=[Correction(
        index=1, severity="RED", topic="glide",
        plan_item_ref="glide.schedule", summary="stale",
    )])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)

    def _boom(**kw):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(corrections_check, "check_corrections_landed", _boom)
    _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    unresolved = inputs["corrective_unresolved"]
    assert len(unresolved) == 1
    assert "fail-closed" in unresolved[0]["reason"]
    assert inputs["corrective"]["corrections"][0]["topic"] == "glide"


def test_non_corrective_run_never_reuses(session, monkeypatch):
    """No corrective context → the fresh-run path is unchanged even when a
    fresh completed run exists."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    _seed_completed_run(session, days_ago=1)
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: None)
    ran = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]


def test_select_corrective_reuse_run_helper(session):
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _select_corrective_reuse_run,
    )

    assert _select_corrective_reuse_run(session, user_id="ariel") is None
    stale = _seed_completed_run(session, days_ago=30)
    assert _select_corrective_reuse_run(session, user_id="ariel") is None
    fresh = _seed_completed_run(session, days_ago=3)
    assert _select_corrective_reuse_run(session, user_id="ariel") == fresh
    # The run being excluded (the corrective run itself) is never selected.
    assert _select_corrective_reuse_run(
        session, user_id="ariel", exclude_decision_run_id=fresh
    ) is None  # stale one is out of window; fresh one excluded
    assert stale != fresh


# ----------------------------------------------------------------------
# Corrections-landed gate (part C deterministic floor).
# ----------------------------------------------------------------------


def test_gate_records_unresolved_and_one_inbox_row(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.services.corrective_context import Correction

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    ctx = _make_ctx(corrections=[Correction(
        index=1, severity="RED", topic="nvda-target",
        plan_item_ref="targets.nvda", summary="target stale",
        canonical_facts=[("nvda_target_sh", 4136)],
    )])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    _stub_phases(flow, monkeypatch)  # stub bodies never contain 4,136

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    unresolved = inputs["corrective_unresolved"]
    assert len(unresolved) == 1 and unresolved[0]["topic"] == "nvda-target"
    # ONE aggregated inbox row.
    rows = session.query(ActionProposal).filter_by(
        user_id="ariel", dedup_key="corrective_unresolved:ariel", status="open",
    ).all()
    assert len(rows) == 1
    assert f"draft #{out.draft_id}" in rows[0].summary


def test_gate_clean_when_corrections_landed(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.services.corrective_context import Correction

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    ctx = _make_ctx(
        corrections=[Correction(
            index=1, severity="RED", topic="posture",
            plan_item_ref="long.posture", summary="posture wrong",
            canonical_facts=[("posture_word", "long posture")],
        )],
        proposal_ids=[123],
    )
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    _stub_phases(flow, monkeypatch)  # stub long body says "long posture"

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    assert inputs["corrective_unresolved"] == []
    assert inputs["corrective"]["proposal_ids"] == [123]
    # No inbox row when everything landed.
    rows = session.query(ActionProposal).filter_by(
        user_id="ariel", dedup_key="corrective_unresolved:ariel",
    ).all()
    assert rows == []
