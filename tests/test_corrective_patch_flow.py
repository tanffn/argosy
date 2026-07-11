"""Corrective patch-synthesis — orchestrator wiring tests.

Design: docs/design/corrective_patch_synthesis.md §5 (orchestrator: patch
tier chosen iff corrective + flag + PATCH verdict; degrade-to-full on
classifier/agent exception; flag OFF = shipped corrective behavior;
escalation to the full re-synth exactly ONCE when the floor reports a
surviving wrong value).

All agent phases are stubbed (no live LLM call) — same pattern as
tests/test_corrective_synthesis_flow.py. The prose rewriter is stubbed to
identity here too (the real one makes live claude.exe calls even for stub
outputs).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


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


def _stub_synthesis_output(medium_posture="medium posture"):
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
            status="minor_revision", posture=medium_posture,
        ),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly",
            status="major_revision", posture="short posture",
        ),
        inputs=SynthesisInputs(),
    )


def _stub_phases(flow, monkeypatch, ran=None, p3_output=None):
    """Stub all five phases + the prose rewriter (identity)."""
    ran = ran if ran is not None else []

    def _p1(**kw):
        ran.append(1)
        return "(analysts)"

    def _p2(**kw):
        ran.append(2)
        return "(debates)"

    def _p3(**kw):
        ran.append(3)
        return p3_output() if p3_output is not None else _stub_synthesis_output()

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
    # The real rewriter fires live claude.exe calls; identity stub keeps the
    # flow deterministic. Patch mode must NOT call it at the orchestrator
    # level (its patched-slices-only rewrite lives inside _run_phase_3_patch,
    # which is itself stubbed in these tests).
    rewriter_calls: list[int] = []

    def _rw(*, output, **kw):
        rewriter_calls.append(1)
        return output

    monkeypatch.setattr(flow, "_run_plan_language_rewriter", _rw)
    return ran, rewriter_calls


def _make_ctx(corrections=None, directives=None, forces_full_tier=False):
    from argosy.services.corrective_context import (
        CorrectiveContext,
        _render_block,
    )

    ctx = CorrectiveContext(
        corrections=corrections or [],
        directives=directives or [],
        forces_full_tier=forces_full_tier,
    )
    ctx.rendered = _render_block(ctx)
    return ctx


def _fx_correction(wrong="3.00"):
    from argosy.services.corrective_context import Correction

    return Correction(
        index=1, severity="RED", topic="fx-rate",
        plan_item_ref="assumptions.fx", summary="FX stale",
        canonical_facts=[("fx_usd_nis", 2.944)],
        wrong_values=[wrong],
    )


def _patch_reach(groups=("medium",), item_ids=(), sections=()):
    from argosy.quality.patch_reachability import (
        PatchReachability,
        ScopeDecision,
    )

    return PatchReachability(
        verdict="PATCH", reason="test patch verdict",
        decisions=[ScopeDecision(
            kind="correction", index=1, scope="PATCH", reason="test",
            implicated_groups=tuple(groups),
            implicated_item_ids=tuple(item_ids),
            implicated_sections=tuple(sections),
        )],
        implicated_groups=tuple(groups),
        implicated_item_ids=tuple(item_ids),
        implicated_sections=tuple(sections),
    )


def _stub_provenance():
    return {
        "patched_surfaces": [{
            "slice": "medium", "surface": "prose",
            "correction_indices": [1], "directive_indices": [],
            "before_sha256": "aa", "after_sha256": "bb",
        }],
        "unpatched_slice_hashes": [
            {"slice": "long", "sha256": "cc", "matches_prior": True},
            {"slice": "short", "sha256": "dd", "matches_prior": True},
        ],
        "classifier": {"verdict": "PATCH"},
    }


def _corrective(pv) -> dict:
    return json.loads(pv.synthesis_inputs_json)["corrective"]


# ----------------------------------------------------------------------
# Flag OFF — shipped corrective behavior; the classifier is never invoked.
# ----------------------------------------------------------------------


def test_flag_off_never_touches_patch_path(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "0")

    def _boom(**kw):
        raise AssertionError("classifier must not run when the flag is off")

    monkeypatch.setattr(pr, "classify_patch_reachability", _boom)
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("patch runner must not run when the flag is off")
        ),
    )
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    ran, rewriter_calls = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 in ran  # full phase 3 ran — shipped behavior
    assert rewriter_calls  # full-artifact rewrite ran — shipped behavior
    pv = session.get(PlanVersion, out.draft_id)
    corrective = _corrective(pv)
    assert "patch_classifier" not in corrective
    assert "patched_surfaces" not in corrective


# ----------------------------------------------------------------------
# Tier select — patch chosen iff corrective + flag + PATCH verdict.
# ----------------------------------------------------------------------


def test_patch_tier_chosen_and_full_rewriter_skipped(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
    )
    patch_calls: list[dict] = []

    def _patch(**kw):
        patch_calls.append(kw)
        # The patched draft LANDS the canonical value (2.944) so the
        # corrections floor passes and no escalation fires.
        return (
            _stub_synthesis_output(medium_posture="FX now 2.944"),
            [],
            _stub_provenance(),
        )

    monkeypatch.setattr(flow, "_run_phase_3_patch", _patch)
    ran, rewriter_calls = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert len(patch_calls) == 1
    assert 3 not in ran  # full synthesizer never ran
    # The orchestrator-level full-artifact rewrite is SKIPPED in patch mode
    # (the patched-slices-only rewrite lives inside _run_phase_3_patch).
    assert rewriter_calls == []
    pv = session.get(PlanVersion, out.draft_id)
    corrective = _corrective(pv)
    assert corrective["patch_classifier"]["verdict"] == "PATCH"
    assert corrective["patched_surfaces"][0]["slice"] == "medium"
    assert corrective["patch_unpatched_slice_hashes"]
    assert corrective["patch_escalated"] is False
    # The stub draft carries no "9.99", so the floor is clean.
    assert json.loads(pv.synthesis_inputs_json)["corrective_unresolved"] == []


def test_full_verdict_takes_shipped_path_and_records_classifier(
    session, monkeypatch,
):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr
    from argosy.quality.patch_reachability import PatchReachability

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability",
        lambda **kw: PatchReachability(
            verdict="FULL_RESYNTH", reason="test full verdict",
        ),
    )
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("patch runner must not run on a FULL verdict")
        ),
    )
    ran, _rw = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 in ran  # shipped full path
    corrective = _corrective(session.get(PlanVersion, out.draft_id))
    assert corrective["patch_classifier"]["verdict"] == "FULL_RESYNTH"
    assert "patched_surfaces" not in corrective


@pytest.mark.parametrize("failing", ["classifier", "runner", "base"])
def test_patch_path_exception_degrades_to_full(session, monkeypatch, failing):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    if failing == "base":
        # No structured prior plan → patch base unavailable.
        monkeypatch.setattr(
            flow, "_load_patch_base_output", lambda prior_current: None,
        )
    else:
        monkeypatch.setattr(
            flow, "_load_patch_base_output",
            lambda prior_current: _stub_synthesis_output(),
        )
    if failing == "classifier":
        def _boom(**kw):
            raise RuntimeError("classifier exploded")
        monkeypatch.setattr(pr, "classify_patch_reachability", _boom)
    else:
        monkeypatch.setattr(
            pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
        )
    if failing == "runner":
        def _boom_runner(**kw):
            raise RuntimeError("patch runner exploded")
        monkeypatch.setattr(flow, "_run_phase_3_patch", _boom_runner)
    ran, _rw = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 in ran, f"degrade-to-full must run the full phase 3 ({failing})"


def test_non_corrective_run_ignores_flag(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: None)

    def _boom(**kw):
        raise AssertionError("classifier must not run without corrective ctx")

    monkeypatch.setattr(pr, "classify_patch_reachability", _boom)
    ran, _rw = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran == [1, 2, 3, 4, 5]


# ----------------------------------------------------------------------
# Bounded escalation — exactly ONE full regeneration when the floor fails.
# ----------------------------------------------------------------------


def test_escalation_runs_full_resynth_exactly_once_and_clears(
    session, monkeypatch,
):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="3.00")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
    )
    # The PATCH lands the canonical value in medium but the wrong value
    # 3.00 SURVIVES in the (unimplicated) short posture — the classifier
    # under-scoped; the deterministic floor must catch it and escalate.
    dirty = _stub_synthesis_output(medium_posture="FX now 2.944")
    dirty = dirty.model_copy(update={
        "short": dirty.short.model_copy(
            update={"posture": "convert at 3.00 this month"}),
    })
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (dirty, [], _stub_provenance()),
    )
    # The escalation's full re-synth returns a CLEAN artifact.
    ran, _rw = _stub_phases(
        flow, monkeypatch,
        p3_output=lambda: _stub_synthesis_output(medium_posture="FX 2.944"),
    )

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran.count(3) == 1, "exactly ONE bounded escalation to full regen"
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    assert inputs["corrective_unresolved"] == []
    assert inputs["corrective"]["patch_escalated"] is True
    assert inputs["corrective"]["patched_surfaces_stale"] is True
    # The escalated (regenerated) artifact is what got persisted.
    assert "3.00" not in (pv.horizon_short_md or "")
    assert "2.944" in (pv.horizon_medium_json or "")
    # FAIL CLOSED (codex r2 blocker #1 middle ground): the reader is skipped
    # under pytest, so the regenerated artifact was never blind-re-reviewed —
    # the escalation must tighten the promotion gate rather than carry the
    # pre-escalation FM approval onto unseen content.
    from argosy.state.models import DecisionRun
    run = session.get(DecisionRun, pv.decision_run_id)
    assert run.fund_manager_decision == "rejected"


def test_directive_builder_reads_structured_payload(session):
    """codex r2 blocker #2 residual: the builder lifts target_refs +
    superseded_values from the accepted proposal's structured payload."""
    from datetime import UTC, datetime, timedelta

    from argosy.services.corrective_context import build_corrective_context
    from argosy.state.models import ActionProposal

    now = datetime.now(UTC)
    session.add(ActionProposal(
        user_id="ariel", summary="NVDA glide schedule adjudication",
        rationale_md="apply the fast-on-eligible-core schedule",
        suggested_payload=json.dumps({
            "target_refs": ["medium.targets.nvda_target_weight"],
            "superseded_values": [4136],
        }),
        severity="info", surfaced_at=now, expires_at=now + timedelta(days=30),
        status="accepted", kind="update_plan_assumption",
        dedup_key="glide:ariel", execution_state="accepted_pending_user_action",
    ))
    session.commit()

    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None and len(ctx.directives) == 1
    d = ctx.directives[0]
    assert d.target_refs == ["medium.targets.nvda_target_weight"]
    assert d.superseded_values == [4136]
    assert d.check_payload()["wrong_values"] == [4136]


def test_escalation_is_bounded_when_still_dirty(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr
    from argosy.state.models import ActionProposal

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="3.00")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
    )

    def _dirty():
        out = _stub_synthesis_output()
        return out.model_copy(update={
            "short": out.short.model_copy(
                update={"posture": "convert at 3.00 this month"}),
        })

    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (_dirty(), [], _stub_provenance()),
    )
    # The escalated full re-synth is STILL dirty — no second escalation.
    ran, _rw = _stub_phases(flow, monkeypatch, p3_output=_dirty)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert ran.count(3) == 1, "escalation must fire exactly once, never loop"
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    unresolved = inputs["corrective_unresolved"]
    assert len(unresolved) == 1
    assert "3.00" in unresolved[0]["reason"]
    # Per-slice attribution feeds diagnosis (patch-synthesis §2.D).
    assert unresolved[0]["hit_surfaces"]
    assert inputs["corrective"]["patch_escalated"] is True
    # ONE aggregated inbox row (unchanged outcome path).
    rows = session.query(ActionProposal).filter_by(
        user_id="ariel", dedup_key="corrective_unresolved:ariel", status="open",
    ).all()
    assert len(rows) == 1


# ----------------------------------------------------------------------
# FIX 3 — delta-scoped verification (DELTA REVIEW framing for phases 4/5 +
# the whole-artifact reader; Ariel 2026-07-08 "review only that delta").
# ----------------------------------------------------------------------


def _add_reviewed_current_plan(session, *, with_fm=True, with_reader=True):
    """A CURRENT plan whose producing run has FM + reader reports on file —
    the reviewed base draft the delta proof chain anchors to."""
    from argosy.state.models import AgentReport, DecisionRun

    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier=None,
        decision_kind="plan_revision", status="completed",
        fund_manager_decision="approved",
    )
    session.add(run)
    session.commit()
    pv = PlanVersion(
        user_id="ariel", role="current", version_label="v67",
        raw_markdown="# Current plan", decision_run_id=run.id,
    )
    session.add(pv)
    if with_fm:
        session.add(AgentReport(
            user_id="ariel", agent_role="fund_manager",
            decision_id=f"plan-synth-{run.id}",
            response_text=json.dumps({"approved": True}),
        ))
    if with_reader:
        session.add(AgentReport(
            user_id="ariel", agent_role="whole_artifact_reader",
            decision_id=f"plan-synth-{run.id}",
            response_text=json.dumps({"overall_assessment": "APPROVE"}),
        ))
    session.commit()
    return pv, run


def _wire_patch_run(flow, pr, monkeypatch, *, provenance=None,
                    patch_output=None):
    """Common patch-mode wiring: corrective ctx + PATCH verdict + stub
    patch runner returning a clean draft."""
    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
    )
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (
            patch_output if patch_output is not None
            else _stub_synthesis_output(medium_posture="FX now 2.944"),
            [],
            provenance if provenance is not None else _stub_provenance(),
        ),
    )


def _capture_verifiers(flow, monkeypatch):
    """Capture the guidance seen by phases 4/5 and the reader's external
    context (the three surfaces the DELTA REVIEW framing targets)."""
    p4_guidance: list[str] = []
    p5_guidance: list[str] = []
    reader_ctx: list[str] = []

    def _p4(**kw):
        p4_guidance.append(kw.get("guidance") or "")
        return "(risk)"

    def _p5(**kw):
        p5_guidance.append(kw.get("guidance") or "")
        return True

    async def _reader(**kw):
        reader_ctx.append(kw.get("external_context") or "")
        return (None, None)

    monkeypatch.setattr(flow, "_run_phase_4_risk", _p4)
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", _p5)
    monkeypatch.setattr(flow, "run_whole_artifact_review", _reader)
    return p4_guidance, p5_guidance, reader_ctx


def test_delta_framing_present_with_full_proof_chain(session, monkeypatch):
    """PATCH run + FM/reader reports on the base draft + all-matching
    unpatched hashes → phases 4/5 and the reader get the DELTA REVIEW block;
    the payload records delta_scoped_review + the basis."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    _pv, base_run = _add_reviewed_current_plan(session)
    _wire_patch_run(flow, pr, monkeypatch)
    ran, _rw = _stub_phases(flow, monkeypatch)
    p4, p5, reader = _capture_verifiers(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert len(p4) == 1 and "DELTA REVIEW" in p4[0]
    assert len(p5) == 1 and "DELTA REVIEW" in p5[0]
    assert reader and "DELTA REVIEW" in reader[0]
    # The framing names the reviewed base + the changed surface, and states
    # authority stays whole-artifact.
    assert f"decision run #{base_run.id}" in p5[0]
    assert "slice medium" in p5[0]
    assert "binding on the WHOLE" in p5[0]
    # The base corrective guidance still precedes the framing (never replaced).
    assert "CORRECTIVE RE-SYNTHESIS" in p5[0]

    corrective = _corrective(session.get(PlanVersion, out.draft_id))
    assert corrective["delta_scoped_review"] is True
    basis = corrective["delta_scoped_basis"]
    assert basis["base_decision_run_id"] == base_run.id
    assert basis["fm_report_id"] and basis["reader_report_id"]
    assert basis["changed_surfaces"][0]["slice"] == "medium"
    assert basis["unpatched_slices_verified"] == 2


@pytest.mark.parametrize("missing", ["fund_manager", "whole_artifact_reader"])
def test_delta_framing_absent_without_prior_reports(
    session, monkeypatch, missing,
):
    """No prior FM (or reader) report for the base draft = proof chain
    incomplete → no framing anywhere; payload records False, no basis."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    _add_reviewed_current_plan(
        session,
        with_fm=(missing != "fund_manager"),
        with_reader=(missing != "whole_artifact_reader"),
    )
    _wire_patch_run(flow, pr, monkeypatch)
    _stub_phases(flow, monkeypatch)
    p4, p5, reader = _capture_verifiers(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert "DELTA REVIEW" not in p4[0]
    assert "DELTA REVIEW" not in p5[0]
    assert all("DELTA REVIEW" not in c for c in reader)
    corrective = _corrective(session.get(PlanVersion, out.draft_id))
    assert corrective["delta_scoped_review"] is False
    assert "delta_scoped_basis" not in corrective


def test_delta_framing_absent_on_hash_mismatch(session, monkeypatch):
    """A provenance row with matches_prior=False breaks the byte-identity
    proof → no framing, even with prior reports on file."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    _add_reviewed_current_plan(session)
    prov = _stub_provenance()
    prov["unpatched_slice_hashes"][1]["matches_prior"] = False
    _wire_patch_run(flow, pr, monkeypatch, provenance=prov)
    _stub_phases(flow, monkeypatch)
    p4, p5, reader = _capture_verifiers(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert "DELTA REVIEW" not in p4[0]
    assert "DELTA REVIEW" not in p5[0]
    assert all("DELTA REVIEW" not in c for c in reader)
    corrective = _corrective(session.get(PlanVersion, out.draft_id))
    assert corrective["delta_scoped_review"] is False


def test_delta_framing_absent_on_non_patch_run(session, monkeypatch):
    """A corrective run that took the FULL path (flag off) never frames —
    even with a fully reviewed base on file."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    _add_reviewed_current_plan(session)
    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "0")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="9.99")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    _stub_phases(flow, monkeypatch)
    p4, p5, reader = _capture_verifiers(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert "DELTA REVIEW" not in p4[0]
    assert "DELTA REVIEW" not in p5[0]
    assert all("DELTA REVIEW" not in c for c in reader)
    corrective = _corrective(session.get(PlanVersion, out.draft_id))
    assert "delta_scoped_review" not in corrective


def test_delta_framing_voided_by_floor_escalation(session, monkeypatch):
    """The bounded escalation REGENERATES the artifact — the byte-identity
    proof is void: the escalation's blind re-read must NOT carry the frame,
    and the persisted payload records delta_scoped_review=False (the basis
    stays for provenance)."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr

    _add_reviewed_current_plan(session)
    # The patch leaves the wrong value 3.00 alive in the (unimplicated)
    # short slice → the floor fails → ONE escalation to full regen.
    ctx_wrong = "3.00"
    dirty = _stub_synthesis_output(medium_posture="FX now 2.944")
    dirty = dirty.model_copy(update={
        "short": dirty.short.model_copy(
            update={"posture": f"convert at {ctx_wrong} this month"}),
    })
    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    ctx = _make_ctx(corrections=[_fx_correction(wrong=ctx_wrong)])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability", lambda **kw: _patch_reach(),
    )
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (dirty, [], _stub_provenance()),
    )
    _stub_phases(
        flow, monkeypatch,
        p3_output=lambda: _stub_synthesis_output(medium_posture="FX 2.944"),
    )
    p4, p5, reader = _capture_verifiers(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    # Pre-escalation verdicts were legitimately delta-framed (the artifact
    # WAS patch-authored with a valid proof at that point).
    assert "DELTA REVIEW" in p4[0]
    assert "DELTA REVIEW" in p5[0]
    # Reader ran twice: first read framed; the post-escalation blind re-read
    # of the REGENERATED artifact must not be.
    assert len(reader) == 2
    assert "DELTA REVIEW" in reader[0]
    assert "DELTA REVIEW" not in reader[1]
    inputs = json.loads(
        session.get(PlanVersion, out.draft_id).synthesis_inputs_json
    )
    assert inputs["corrective"]["patch_escalated"] is True
    assert inputs["corrective"]["delta_scoped_review"] is False
    assert "delta_scoped_basis" in inputs["corrective"]


def test_no_escalation_when_full_path_was_used(session, monkeypatch):
    """A non-patch corrective run that fails the floor must NOT trigger the
    patch escalation (unchanged shipped behavior: unresolved + inbox row)."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "0")
    ctx = _make_ctx(corrections=[_fx_correction(wrong="3.00")])
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)

    def _dirty():
        out = _stub_synthesis_output()
        return out.model_copy(update={
            "short": out.short.model_copy(
                update={"posture": "convert at 3.00 this month"}),
        })

    ran, _rw = _stub_phases(flow, monkeypatch, p3_output=_dirty)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert ran.count(3) == 1  # the normal phase 3; no escalation re-run
    pv = session.get(PlanVersion, out.draft_id)
    inputs = json.loads(pv.synthesis_inputs_json)
    assert len(inputs["corrective_unresolved"]) == 1
    assert "patch_escalated" not in inputs["corrective"]
