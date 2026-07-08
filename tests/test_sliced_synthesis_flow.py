"""Sliced full synthesis — orchestrator wiring tests.

Design: docs/design/sliced_full_synthesis.md §5 (orchestrator): flag OFF =
today byte-identical; patch-verdict precedence over sliced; skeleton/
assembly exception degrades to monolith (logged + provenance note); a dead
slice propagates (phase 3 fails as today); sliced provenance persisted to
``synthesis_inputs_json.sliced``; the full-artifact rewriter still runs
over the assembled output (downstream blind).

All agent phases are stubbed (no live LLM call) — same pattern as
tests/test_corrective_patch_flow.py (run_alternatives_phase and the reader
are already pytest-inert; the rewriter is stubbed to identity because the
real one fires live claude.exe calls).
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


def _stub_phases(flow, monkeypatch, ran=None):
    ran = ran if ran is not None else []

    def _p1(**kw):
        ran.append(1)
        return "(analysts)"

    def _p2(**kw):
        ran.append(2)
        return "(debates)"

    def _p3(**kw):
        ran.append(3)
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
    rewriter_calls: list[int] = []

    def _rw(*, output, **kw):
        rewriter_calls.append(1)
        return output

    monkeypatch.setattr(flow, "_run_plan_language_rewriter", _rw)
    return ran, rewriter_calls


def _sliced_provenance():
    return {
        "skeleton_sha256": "abc123",
        "skeleton_resumed": False,
        "skeleton_gate": {"attempts": 1, "first_attempt_violations": []},
        "slices": {"long": {"sha256": "d1", "retries": 0, "resumed": False}},
        "lock_restorations": 0,
    }


def _sliced_json(pv) -> dict | None:
    return json.loads(pv.synthesis_inputs_json).get("sliced")


# ----------------------------------------------------------------------
# Flag OFF — byte-identical today; the sliced runner is never invoked.
# ----------------------------------------------------------------------


def test_flag_off_never_touches_sliced_path(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.delenv("ARGOSY_SLICED_SYNTH", raising=False)
    monkeypatch.setattr(
        flow, "_run_phase_3_sliced",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("sliced runner must not run when the flag is off")
        ),
    )
    ran, rewriter_calls = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 in ran  # monolith ran — shipped behavior
    assert rewriter_calls
    pv = session.get(PlanVersion, out.draft_id)
    assert _sliced_json(pv) is None  # no provenance key — byte-identical


# ----------------------------------------------------------------------
# Flag ON — sliced runs; monolith skipped; provenance persisted;
# the full-artifact rewriter STILL runs (downstream blind).
# ----------------------------------------------------------------------


def test_sliced_tier_chosen_and_provenance_persisted(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_SLICED_SYNTH", "1")
    sliced_calls: list[dict] = []

    def _sliced(**kw):
        sliced_calls.append(kw)
        return (
            _stub_synthesis_output(medium_posture="assembled from slices"),
            [],
            _sliced_provenance(),
        )

    monkeypatch.setattr(flow, "_run_phase_3_sliced", _sliced)
    ran, rewriter_calls = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert len(sliced_calls) == 1
    assert sliced_calls[0]["decision_run_int"] is not None
    assert 3 not in ran  # monolith never ran
    # Downstream is blind: the FULL-artifact rewriter runs (unlike patch mode).
    assert rewriter_calls
    pv = session.get(PlanVersion, out.draft_id)
    sliced = _sliced_json(pv)
    assert sliced["skeleton_sha256"] == "abc123"
    assert sliced["slices"]["long"]["retries"] == 0
    assert "assembled from slices" in (pv.horizon_medium_json or "")


# ----------------------------------------------------------------------
# Precedence — corrective PATCH > sliced FULL > monolith.
# ----------------------------------------------------------------------


def test_patch_precedence_over_sliced(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.quality import patch_reachability as pr
    from argosy.quality.patch_reachability import (
        PatchReachability,
        ScopeDecision,
    )
    from argosy.services.corrective_context import (
        Correction,
        CorrectiveContext,
        _render_block,
    )

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_CORRECTIVE_PATCH", "1")
    monkeypatch.setenv("ARGOSY_SLICED_SYNTH", "1")
    ctx = CorrectiveContext(
        corrections=[Correction(
            index=1, severity="RED", topic="fx-rate",
            plan_item_ref="assumptions.fx", summary="FX stale",
            canonical_facts=[("fx_usd_nis", 2.944)], wrong_values=["9.99"],
        )],
        directives=[], forces_full_tier=False,
    )
    ctx.rendered = _render_block(ctx)
    monkeypatch.setattr(flow, "build_corrective_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        flow, "_load_patch_base_output",
        lambda prior_current: _stub_synthesis_output(),
    )
    monkeypatch.setattr(
        pr, "classify_patch_reachability",
        lambda **kw: PatchReachability(
            verdict="PATCH", reason="test",
            decisions=[ScopeDecision(
                kind="correction", index=1, scope="PATCH", reason="test",
                implicated_groups=("medium",),
            )],
            implicated_groups=("medium",),
        ),
    )
    monkeypatch.setattr(
        flow, "_run_phase_3_patch",
        lambda **kw: (
            _stub_synthesis_output(medium_posture="FX now 2.944"),
            [],
            {
                "patched_surfaces": [], "unpatched_slice_hashes": [],
                "classifier": {"verdict": "PATCH"},
            },
        ),
    )
    monkeypatch.setattr(
        flow, "_run_phase_3_sliced",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("sliced runner must not run when PATCH wins")
        ),
    )
    ran, _rw = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 not in ran  # patch won; neither sliced nor monolith ran


# ----------------------------------------------------------------------
# Fail-soft — stage-A/assembly exception degrades to the monolith.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("exc_factory", [
    lambda flow: RuntimeError("skeleton exploded"),
    lambda flow: flow.SkeletonGateError(["[manifest] boom"]),
    lambda flow: flow.SlicedAssemblyError("roster hole"),
])
def test_sliced_exception_degrades_to_monolith(
    session, monkeypatch, exc_factory,
):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_SLICED_SYNTH", "1")

    def _boom(**kw):
        raise exc_factory(flow)

    monkeypatch.setattr(flow, "_run_phase_3_sliced", _boom)
    ran, _rw = _stub_phases(flow, monkeypatch)

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None
    assert 3 in ran, "degrade-to-monolith must run the full phase 3"
    pv = session.get(PlanVersion, out.draft_id)
    sliced = _sliced_json(pv)
    assert sliced["degraded_to_monolith"] is True
    assert sliced["error"]


# ----------------------------------------------------------------------
# Dead slice — SliceExpansionError propagates; phase 3 fails as today.
# ----------------------------------------------------------------------


def test_dead_slice_fails_phase_3(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setenv("ARGOSY_DERIVED_FACTS", "0")
    monkeypatch.setenv("ARGOSY_SLICED_SYNTH", "1")

    def _dead(**kw):
        raise flow.SliceExpansionError({"short": "sdk_timeout after retries"})

    monkeypatch.setattr(flow, "_run_phase_3_sliced", _dead)
    ran, _rw = _stub_phases(flow, monkeypatch)

    with pytest.raises(flow.SliceExpansionError):
        flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert 3 not in ran, "monolith must NOT paper over a dead slice"
