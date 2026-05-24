"""Per-phase tests that decision_id reaches each phase's agent.run_sync calls.

One test per phase. Each test:
  - Patches only the agent class(es) for that phase
  - Calls the phase helper directly with a known decision_id
  - Asserts the captured kwargs include decision_id

This is intentionally narrow: a refactor of phase N only breaks the phase-N
test, not the others.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


_DECISION_ID = "plan-synth-42"  # the string audit token format


def _capture(target: list):
    """Build a run_sync stub that captures kwargs and returns a minimal report.

    The returned `output` exposes both `model_dump` and `model_dump_json`
    because different orchestrator phases call different accessors.
    """

    def _stub(self, *args, **kwargs):
        target.append(kwargs)
        return SimpleNamespace(
            output=SimpleNamespace(
                model_dump=lambda: {},
                model_dump_json=lambda: "{}",
                approved=True,
            ),
        )

    return _stub


def test_phase_1_passes_decision_id_to_analyst(monkeypatch):
    """_run_phase_1_analysts forwards decision_id through common_kwargs."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_1_analysts,
    )

    monkeypatch.setattr(
        flow,
        "_assemble_portfolio_summary",
        lambda *, session, user_id: "(empty)",
    )
    monkeypatch.setattr(
        flow,
        "_load_user_context_yaml",
        lambda *, session, user_id: "",
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        flow.FundamentalsAnalystAgent,
        "run_sync",
        _capture(captured),
        raising=True,
    )
    # Narrow phase 1 to a single agent so the test is focused.
    monkeypatch.setattr(
        orch,
        "_PHASE_1_AGENT_NAMES",
        ("FundamentalsAnalystAgent",),
        raising=True,
    )

    baseline = SimpleNamespace(version_label="v1", distillate_rendered="# Plan")
    _run_phase_1_analysts(
        session=None,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id=_DECISION_ID,
        guidance="",
    )

    assert len(captured) >= 1, "phase 1 did not invoke the patched agent"
    assert captured[0].get("decision_id") == _DECISION_ID, (
        f"phase 1 dropped decision_id: {captured[0]}"
    )


def test_phase_2_passes_decision_id_to_researchers(monkeypatch):
    """_run_one_horizon_debate forwards decision_id to bull, bear, facilitator."""
    from argosy.agents.researcher import BearResearcherAgent, BullResearcherAgent
    from argosy.agents.researcher_facilitator import ResearcherFacilitatorAgent
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_one_horizon_debate,
    )

    bull_kw: list[dict] = []
    bear_kw: list[dict] = []
    fac_kw: list[dict] = []
    monkeypatch.setattr(
        BullResearcherAgent, "run_sync", _capture(bull_kw), raising=True
    )
    monkeypatch.setattr(
        BearResearcherAgent, "run_sync", _capture(bear_kw), raising=True
    )
    monkeypatch.setattr(
        ResearcherFacilitatorAgent, "run_sync", _capture(fac_kw), raising=True
    )

    _run_one_horizon_debate(
        horizon="short",
        user_id="ariel",
        analyst_reports_text="",
        baseline=None,
        prior_current=None,
        decision_run_id=_DECISION_ID,
        trigger="check_in",
    )

    assert bull_kw and bull_kw[0].get("decision_id") == _DECISION_ID
    assert bear_kw and bear_kw[0].get("decision_id") == _DECISION_ID
    assert fac_kw and fac_kw[0].get("decision_id") == _DECISION_ID


def test_phase_3_passes_decision_id_to_synthesizer(monkeypatch):
    """_run_phase_3_synthesizer forwards decision_id to PlanSynthesizerAgent."""
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_3_synthesizer,
    )

    captured: list[dict] = []

    # No-op the cap enforcer so we can pass a SimpleNamespace output through.
    monkeypatch.setattr(
        flow, "_enforce_speculation_cap", lambda output, **_: output
    )

    def _synth_stub(self, *args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            output=SimpleNamespace(model_dump_json=lambda: "{}"),
        )

    monkeypatch.setattr(
        PlanSynthesizerAgent, "run_sync", _synth_stub, raising=True
    )

    baseline = SimpleNamespace(distillate_rendered="# Plan")
    _run_phase_3_synthesizer(
        session=None,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        analyst_reports_text="",
        debate_outcomes_text="",
        portfolio_summary="",
        fills_summary="",
        decision_run_id=_DECISION_ID,
    )

    assert captured and captured[0].get("decision_id") == _DECISION_ID


def test_phase_4_passes_decision_id_to_risk_agents(monkeypatch):
    """_run_phase_4_risk forwards decision_id to risk officer + facilitator."""
    from argosy.agents.risk_facilitator import RiskFacilitatorAgent
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_4_risk,
    )

    officer_kw: list[dict] = []
    facilitator_kw: list[dict] = []

    fake_officer = MagicMock()

    def _officer_run(*args, **kwargs):
        officer_kw.append(kwargs)
        return SimpleNamespace(
            output=SimpleNamespace(model_dump_json=lambda: '{"verdict":"PASS"}')
        )

    fake_officer.run_sync = _officer_run
    monkeypatch.setattr(flow, "_make_risk_officer", lambda *a, **kw: fake_officer)

    monkeypatch.setattr(
        RiskFacilitatorAgent,
        "run_sync",
        _capture(facilitator_kw),
        raising=True,
    )

    draft = SimpleNamespace(model_dump_json=lambda: "{}")
    _run_phase_4_risk(
        session=None,
        user_id="ariel",
        draft_output=draft,
        analyst_reports_text="",
        decision_run_id=_DECISION_ID,
    )

    # 3 perspectives × officer + 1 facilitator
    assert len(officer_kw) == 3, (
        f"expected 3 officer calls, got {len(officer_kw)}"
    )
    for kw in officer_kw:
        assert kw.get("decision_id") == _DECISION_ID, (
            f"phase 4 officer dropped decision_id: {kw}"
        )
    assert facilitator_kw and facilitator_kw[0].get("decision_id") == _DECISION_ID


def test_phase_5_passes_decision_id_to_fund_manager(monkeypatch):
    """_run_phase_5_fund_manager forwards decision_id to FundManagerAgent."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_5_fund_manager,
    )

    captured: list[dict] = []

    def _fm_run(*args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(output=SimpleNamespace(approved=True))

    fake_fm = MagicMock()
    fake_fm.run_sync = _fm_run
    monkeypatch.setattr(flow, "_make_fund_manager", lambda *a, **kw: fake_fm)

    draft = SimpleNamespace(model_dump_json=lambda: "{}")
    _run_phase_5_fund_manager(
        session=None,
        user_id="ariel",
        draft_output=draft,
        risk_verdict="",
        decision_run_id=_DECISION_ID,
    )

    assert captured and captured[0].get("decision_id") == _DECISION_ID
