"""Tests for plan_synthesis_flow orchestrator.

The orchestrator wires Phases 1-5 together. Tests use stub agents that
return canned outputs; no live LLM call is made. The end-to-end live
test is in tests/test_plan_synthesis_e2e.py (Task 2.13).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    # Insert a baseline so synthesis has an input.
    s.add(PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
        distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
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

    long = HorizonSection(
        horizon="long", freshness_expected="annual", status="no_change",
        posture="long posture",
    )
    medium = HorizonSection(
        horizon="medium", freshness_expected="quarterly", status="minor_revision",
        posture="medium posture",
    )
    short = HorizonSection(
        horizon="short", freshness_expected="monthly", status="major_revision",
        posture="short posture",
    )
    return PlanSynthesisOutput(
        long=long, medium=medium, short=short,
        inputs=SynthesisInputs(),
    )


def test_synthesis_flow_writes_role_draft(session, monkeypatch):
    from argosy.orchestrator.flows import plan_synthesis as flow

    # Stub each phase. We only verify the *integration* — that the flow
    # writes a draft row with the expected horizons; the per-agent prompt
    # tests live in their own test files.
    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "(analyst reports)")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "(debate outcomes)")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", lambda **kw: _stub_synthesis_output())
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "(risk verdict)")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: True)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "NVDA 14%")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "(no fills)")

    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    assert out.draft_id is not None

    pv = session.get(PlanVersion, out.draft_id)
    assert pv.role == "draft"
    assert pv.user_id == "ariel"
    assert pv.horizon_long_json is not None
    assert pv.horizon_medium_json is not None
    assert pv.horizon_short_json is not None
    parsed = json.loads(pv.horizon_medium_json)
    assert parsed["status"] == "minor_revision"


def test_synthesis_flow_replaces_existing_draft(session, monkeypatch):
    """Idempotency: if a draft already exists, replace it (do not stack)."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", lambda **kw: _stub_synthesis_output())
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: True)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

    out1 = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
    out2 = flow.run_synthesis(session, user_id="ariel", trigger="check_in")

    drafts = session.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 1, f"expected 1 draft after idempotent rerun, got {len(drafts)}"
    # The fresh draft is the second one; the first should be superseded.
    superseded = session.query(PlanVersion).filter_by(
        user_id="ariel", role="superseded"
    ).all()
    assert any(pv.id == out1.draft_id for pv in superseded), \
        "first draft should be moved to role=superseded after replacement"


def test_synthesis_flow_fails_loudly_when_no_baseline(alembic_engine_at_head, monkeypatch):
    """Without a baseline, synthesis cannot run — the orchestrator must
    raise rather than silently produce a draft from nothing.
    """
    from sqlalchemy.orm import sessionmaker
    from argosy.orchestrator.flows import plan_synthesis as flow

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id="newcomer", plan="free"))
    sess.commit()

    with pytest.raises(flow.NoBaselineError):
        flow.run_synthesis(sess, user_id="newcomer", trigger="scheduled")
    sess.close()


def test_phase_1_runs_all_nine_analysts(session, monkeypatch):
    """Phase 1 should invoke each of the 9 analyst agents once.

    We track invocations via a side-effect list. Real calls are stubbed.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    invoked = []

    class _Stub:
        agent_role = "stub"
        def run_sync(self, **kw):
            invoked.append(self.__class__.__name__)
            return type("R", (), {"output": type("O", (), {"model_dump_json": lambda self: "{}"})(), "model": "fake"})()

    # Build stubs for all 9 analyst classes; monkeypatch the import points.
    for name in (
        "FundamentalsAnalystAgent", "TechnicalAnalystAgent",
        "NewsAnalystAgent", "SentimentAnalystAgent",
        "MacroAnalystAgent", "PlanCritiqueAgent",
        "ConcentrationAnalystAgent", "TaxAnalystAgent", "FxAnalystAgent",
    ):
        cls = type(name, (_Stub,), {})
        monkeypatch.setattr(f"argosy.orchestrator.flows.plan_synthesis.{name}", cls, raising=False)

    baseline = next(iter(session.query(PlanVersion).filter_by(role="baseline").all()))
    out = flow._run_phase_1_analysts(
        session=session,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id="test-run",
        guidance="",
    )
    # All 9 must have been invoked exactly once.
    assert len(invoked) == 9, f"expected 9 analyst calls, got {len(invoked)}: {invoked}"
    assert isinstance(out, str)
    assert len(out) > 0


def test_phase_2_debates_runs_three_horizons(session, monkeypatch):
    """Phase 2 must invoke the researcher-debate flow once per horizon."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    horizons_seen: list[str] = []

    def _fake_debate(*, horizon, **kw):
        horizons_seen.append(horizon)
        return f"DEBATE OUTCOME for {horizon}"

    monkeypatch.setattr(flow, "_run_one_horizon_debate", _fake_debate)

    baseline = next(iter(session.query(PlanVersion).filter_by(role="baseline").all()))
    out = flow._run_phase_2_debates(
        session=session, user_id="ariel",
        analyst_reports_text="(stub)", baseline=baseline,
        prior_current=None, decision_run_id="test", trigger="scheduled",
    )
    assert sorted(horizons_seen) == ["long", "medium", "short"]
    for h in ("long", "medium", "short"):
        assert f"DEBATE OUTCOME for {h}" in out
