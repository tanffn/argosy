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


def test_phase_1_assembles_and_routes_all_payloads(monkeypatch):
    """After W1.B wiring, phase-1 calls assemble_phase1_inputs and routes
    its output through to the 9 analysts. Each analyst's run_sync
    receives the kwargs it declares (verified by capturing kwargs for
    3 representative analysts whose signatures differ)."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_1_analysts,
    )

    # Patch DB-touching helpers — synth shouldn't hit the real DB.
    monkeypatch.setattr(
        flow, "_assemble_portfolio_summary",
        lambda *, session, user_id: "stub-positions",
    )
    monkeypatch.setattr(
        flow, "_load_user_context_yaml",
        lambda *, session, user_id: "stub-ctx",
    )

    captured: dict[str, dict] = {}

    def _capture_factory(name):
        def _stub(self, *args, **kwargs):
            captured[name] = kwargs
            return SimpleNamespace(
                output=SimpleNamespace(
                    model_dump=lambda: {},
                    model_dump_json=lambda: "{}",
                    approved=True,
                ),
            )
        return _stub

    # Patch 3 representative analysts with DIFFERENT signatures.
    monkeypatch.setattr(
        flow.ConcentrationAnalystAgent, "run_sync",
        _capture_factory("Concentration"), raising=True,
    )
    monkeypatch.setattr(
        flow.FundamentalsAnalystAgent, "run_sync",
        _capture_factory("Fundamentals"), raising=True,
    )
    monkeypatch.setattr(
        flow.TaxAnalystAgent, "run_sync",
        _capture_factory("Tax"), raising=True,
    )
    # Narrow phase 1 to just these three.
    monkeypatch.setattr(
        orch, "_PHASE_1_AGENT_NAMES",
        ("ConcentrationAnalystAgent", "FundamentalsAnalystAgent", "TaxAnalystAgent"),
        raising=True,
    )

    baseline = SimpleNamespace(version_label="v1", distillate_rendered="# Plan")
    _run_phase_1_analysts(
        session=None,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id="plan-synth-42",
        guidance="",
    )

    # ConcentrationAnalystAgent needs positions_summary + plan_targets
    assert "positions_summary" in captured["Concentration"]
    assert "plan_targets" in captured["Concentration"]
    # FundamentalsAnalystAgent needs tickers + fundamentals_payload
    assert "tickers" in captured["Fundamentals"]
    assert "fundamentals_payload" in captured["Fundamentals"]
    # TaxAnalystAgent needs lots_summary + dividends_summary + rsu_schedule_summary
    assert "lots_summary" in captured["Tax"]
    assert "dividends_summary" in captured["Tax"]
    assert "rsu_schedule_summary" in captured["Tax"]
    # decision_id still flows through (W1.A baseline).
    for kw in captured.values():
        assert kw.get("decision_id") == "plan-synth-42"


def test_phase_1_bulk_persists_all_successful_agents(monkeypatch):
    """W1.C-v2: _run_phase_1_analysts collects each analyst's AgentReport
    dataclass and bulk-persists once at the end of the phase via a single
    sync writer (no aiosqlite contention).

    Patches the analyst run_sync to return real ``AgentReport`` instances
    (so the orchestrator's ``isinstance(result, AgentReport)`` filter lets
    them through to the bulk persist), wires up a real in-memory SQLite
    session, and asserts the expected number of rows are written.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from types import SimpleNamespace as _NS

    from argosy.agents.base import AgentReport, ConfidenceBand
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _run_phase_1_analysts,
    )
    from argosy.state.models import (
        AgentReport as AgentReportRow,
        Base,
        User,
    )

    # In-memory SQLite engine + schema + user.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    # Build a real AgentReport dataclass for the stub to return — its
    # ``output`` attribute carries the SimpleNamespace payload (so the
    # caller's ``out.model_dump_json()`` path still works) AND the
    # orchestrator's ``isinstance(result, AgentReport)`` check passes,
    # so the report is collected for bulk persist.
    def _make_stub(role: str):
        def _stub(self, *args, **kwargs):
            return AgentReport(
                agent_role=role,
                user_id="ariel",
                model="stub-model",
                response_text="stub-response",
                tokens_in=1, tokens_out=1, cost_usd=0.0,
                prompt_hash="hash",
                confidence=ConfidenceBand.MEDIUM,
                output=_NS(
                    model_dump=lambda: {},
                    model_dump_json=lambda: "{}",
                    approved=True,
                ),
                decision_id=kwargs.get("decision_id"),
                run_correlation_id="corr-" + role,
                system_prompt="sys",
                user_prompt="usr",
            )
        return _stub

    # Patch all 9 analyst classes. The orchestrator routes through the
    # package namespace (``_pkg.<AgentClsName>``) so we patch via ``flow``
    # rather than the submodule.
    for name in (
        "ConcentrationAnalystAgent",
        "FxAnalystAgent",
        "FundamentalsAnalystAgent",
        "MacroAnalystAgent",
        "NewsAnalystAgent",
        "PlanCritiqueAgent",
        "SentimentAnalystAgent",
        "TaxAnalystAgent",
        "TechnicalAnalystAgent",
    ):
        cls = getattr(flow, name)
        monkeypatch.setattr(
            cls, "run_sync", _make_stub(name), raising=True,
        )

    # Avoid DB-touching helpers.
    monkeypatch.setattr(
        flow, "_assemble_portfolio_summary",
        lambda *, session, user_id: "(empty)",
    )
    monkeypatch.setattr(
        flow, "_load_user_context_yaml",
        lambda *, session, user_id: "",
    )

    baseline = _NS(version_label="v1", distillate_rendered="# Plan")
    _run_phase_1_analysts(
        session=session,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id=_DECISION_ID,
        guidance="",
    )

    # All 9 agent_reports rows must be written via the single bulk
    # writer at phase end.  decision_id and other fields are populated
    # from the dataclass.
    rows = session.execute(select(AgentReportRow)).scalars().all()
    assert len(rows) == 9, (
        f"W1.C-v2 bulk persist: expected 9 rows for phase 1, got {len(rows)}"
    )
    for row in rows:
        assert row.decision_id == _DECISION_ID, (
            f"row {row.agent_role}: decision_id not stamped — "
            f"got {row.decision_id!r}"
        )
        assert row.user_id == "ariel"
        assert row.run_correlation_id is not None
    session.close()
    engine.dispose()


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
