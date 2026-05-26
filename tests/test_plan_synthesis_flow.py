"""Tests for plan_synthesis_flow orchestrator.

The orchestrator wires Phases 1-5 together. Tests use stub agents that
return canned outputs; no live LLM call is made. The end-to-end live
test is in tests/test_plan_synthesis_e2e.py (Task 2.13).
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionPhase, PlanVersion, User


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


def test_guidance_threads_to_synthesizer_and_fm(session, monkeypatch):
    """CRITICAL — verifies the user's ``guidance`` reaches the
    PlanSynthesizerAgent (Phase 3) AND the FundManagerAgent (Phase 5)
    via the ``user_directive`` kwarg of each agent's ``run_sync`` call.

    Pre-fix: ``run_synthesis(guidance=...)`` accepted the string and
    forwarded only to Phase 1, where it was silently discarded. Phases
    3/5 never saw it — so every POST /api/plan/draft/objections/start-new-round
    payload, every onResynthesizeWithObjections click, and every
    /api/advisor/check-in body was dropped at the orchestrator
    boundary. The FM then re-rejected the new draft on identical
    concerns, producing the 3-consecutive-rejections symptom.

    The test substitutes ``PlanSynthesizerAgent`` and
    ``FundManagerAgent`` on the orchestrator module with fakes whose
    ``run_sync`` captures the kwargs it was called with. Asserts that
    ``user_directive=<guidance>`` is in both call kwargs.

    Phases 1/2/4 are stubbed away (they don't need guidance for this
    fix — follow-up scope per the bug report).
    """
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch_mod

    GUIDANCE = (
        "AGREED: NVDA concentration capped at 12%.\n"
        "DISAGREED: tax-loss harvest is not urgent — defer to Q4 2026.\n"
        "DEFERRED: FX hedge sizing."
    )

    captured_synth_kwargs: dict = {}
    captured_fm_kwargs: dict = {}

    class _FakeSynth:
        agent_role = "plan_synthesizer"

        def __init__(self, *_args, **_kw):
            pass

        def run_sync(self, **kw):
            captured_synth_kwargs.update(kw)

            class _R:
                output = _stub_synthesis_output()
                model = "fake"

            return _R()

    class _FakeFM:
        agent_role = "fund_manager"

        def __init__(self, *_args, **_kw):
            pass

        def run_sync(self, **kw):
            captured_fm_kwargs.update(kw)

            class _Out:
                approved = True

                def model_dump_json(self):
                    return '{"approved": true}'

            class _R:
                output = _Out()
                model = "fake"

            return _R()

    # Phase 3 instantiates PlanSynthesizerAgent directly via the
    # module-scoped import; patch the orchestrator submodule's binding.
    monkeypatch.setattr(orch_mod, "PlanSynthesizerAgent", _FakeSynth)
    # Phase 5 obtains the FM via _make_fund_manager — patch that seam
    # on the package facade so the orchestrator's _pkg.<name> resolution
    # honours it.
    monkeypatch.setattr(flow, "_make_fund_manager", lambda *a, **kw: _FakeFM())

    # Stub Phases 1/2/4 so the run completes — they don't need guidance
    # for this fix per the bug report (follow-up scope).
    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "(analyst reports)")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "(debate outcomes)")
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "(risk verdict)")
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

    result = flow.run_synthesis(
        session, user_id="ariel", trigger="check_in", guidance=GUIDANCE,
    )
    assert result.draft_id is not None

    # CRITICAL — the synthesizer must have received the guidance verbatim
    # via user_directive. Without this, the new draft cannot honor the
    # user's directive.
    assert "user_directive" in captured_synth_kwargs, (
        "PlanSynthesizerAgent.run_sync was NOT called with user_directive — "
        "guidance is still being dropped at the orchestrator boundary"
    )
    assert captured_synth_kwargs["user_directive"] == GUIDANCE

    # CRITICAL — the FM must have received the guidance verbatim too.
    # Without this, the FM re-rejects on objections the user has
    # already AGREED with — explaining 3x FM rejection on the same draft.
    assert "user_directive" in captured_fm_kwargs, (
        "FundManagerAgent.run_sync was NOT called with user_directive — "
        "guidance is still being dropped at Phase 5"
    )
    assert captured_fm_kwargs["user_directive"] == GUIDANCE


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
    result = flow._run_phase_1_analysts(
        session=session,
        user_id="ariel",
        baseline=baseline,
        prior_current=None,
        decision_run_id="test-run",
        guidance="",
    )
    # T0.1 — phase functions now return (text, list[AgentReport]).
    assert isinstance(result, tuple) and len(result) == 2
    out, collected = result
    # All 9 must have been invoked exactly once.
    assert len(invoked) == 9, f"expected 9 analyst calls, got {len(invoked)}: {invoked}"
    assert isinstance(out, str)
    assert len(out) > 0
    assert isinstance(collected, list)


def test_phase_2_debates_runs_three_horizons(session, monkeypatch):
    """Phase 2 must invoke the researcher-debate flow once per horizon."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    horizons_seen: list[str] = []

    def _fake_debate(*, horizon, **kw):
        horizons_seen.append(horizon)
        return f"DEBATE OUTCOME for {horizon}"

    monkeypatch.setattr(flow, "_run_one_horizon_debate", _fake_debate)

    baseline = next(iter(session.query(PlanVersion).filter_by(role="baseline").all()))
    result = flow._run_phase_2_debates(
        session=session, user_id="ariel",
        analyst_reports_text="(stub)", baseline=baseline,
        prior_current=None, decision_run_id="test", trigger="scheduled",
    )
    # T0.1 — phase 2 now returns (text, list[AgentReport]).
    assert isinstance(result, tuple) and len(result) == 2
    out, _collected = result
    assert sorted(horizons_seen) == ["long", "medium", "short"]
    for h in ("long", "medium", "short"):
        assert f"DEBATE OUTCOME for {h}" in out


def test_phase_4_risk_runs_three_perspectives(monkeypatch, session):
    from argosy.orchestrator.flows import plan_synthesis as flow

    perspectives: list[str] = []

    def _fake_officer(stance):
        class _Stub:
            agent_role = f"risk_{stance}"
            def run_sync(self, **kw):
                perspectives.append(stance)
                return type("R", (), {"output": type("O", (), {"model_dump_json": lambda self: f"{stance} review"})(), "model": "fake"})()
        return _Stub()

    monkeypatch.setattr(flow, "_make_risk_officer", _fake_officer)

    out = _stub_synthesis_output()
    result = flow._run_phase_4_risk(
        session=session, user_id="ariel", draft_output=out,
        analyst_reports_text="(stub)", decision_run_id="test",
    )
    # T0.1 — phase 4 now returns (text, list[AgentReport]).
    assert isinstance(result, tuple) and len(result) == 2
    text, _collected = result
    assert sorted(perspectives) == ["aggressive", "conservative", "neutral"]
    for s in ("aggressive", "neutral", "conservative"):
        assert f"{s} review" in text


def test_phase_5_fund_manager_green_lights_or_rejects(monkeypatch, session):
    from argosy.orchestrator.flows import plan_synthesis as flow

    class _FakeFM:
        def __init__(self, ok):
            self.ok = ok
        def run_sync(self, **kw):
            class _Out:
                def __init__(s, ok): s.ok = ok
                def model_dump_json(self): return f'{{"approved": {str(self.ok).lower()}}}'
            return type("R", (), {"output": _Out(self.ok), "model": "fake"})()

    out = _stub_synthesis_output()

    monkeypatch.setattr(flow, "_make_fund_manager", lambda *args, **kw: _FakeFM(True))
    # T0.1 — phase 5 now returns (approved, list[AgentReport]).
    result_true = flow._run_phase_5_fund_manager(
        session=session, user_id="ariel", draft_output=out,
        risk_verdict="(ok)", decision_run_id="test",
    )
    assert isinstance(result_true, tuple) and len(result_true) == 2
    assert result_true[0] is True

    monkeypatch.setattr(flow, "_make_fund_manager", lambda *args, **kw: _FakeFM(False))
    result_false = flow._run_phase_5_fund_manager(
        session=session, user_id="ariel", draft_output=out,
        risk_verdict="(ok)", decision_run_id="test",
    )
    assert isinstance(result_false, tuple) and len(result_false) == 2
    assert result_false[0] is False


# ---------------------------------------------------------------------------
# I3 — cap-load fallback path (Wave 3 review fix)
# ---------------------------------------------------------------------------


def test_synthesis_flow_falls_back_to_default_cap_when_yaml_load_fails(
    session, monkeypatch,
):
    """When ``get_user_agent_settings`` raises (e.g. a malformed
    agent_settings.yaml), the orchestrator must:

      1. Not propagate the exception (the run continues to Phase 3+).
      2. Emit ``plan.synthesis.cap_load_failed`` so the UI can surface
         a "your speculation cap fell back to defaults" warning.
      3. Apply the post-filter with the default cap (``SpeculationCap()``
         — 0.001 == 0.1% NW).  We verify by emitting an over-cap candidate
         from Phase 3 and asserting the post-filter dropped it.
    """
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection, PlanSynthesisOutput, SpeculativeCandidate, SynthesisInputs,
    )
    from argosy.orchestrator.flows import plan_synthesis as flow

    phase_3_called = {"hit": False}

    def _phase_3_with_over_cap_candidate(**kw):
        phase_3_called["hit"] = True
        # 0.5% NW — over the 0.1% default cap.
        over_cap = SpeculativeCandidate(
            ticker="HOOD", thesis_summary="momentum",
            suggested_position_usd=4_000,
            suggested_position_pct_of_net_worth=0.005,
            risk_ceiling_check=True, horizon_days=30,
            expected_drawdown_pct=0.2, exit_trigger="stop -20%",
            sourced_from=["sentiment"],
        )
        long = HorizonSection(
            horizon="long", freshness_expected="annual",
            status="no_change", posture="x",
        )
        medium = HorizonSection(
            horizon="medium", freshness_expected="quarterly",
            status="no_change", posture="x",
        )
        short = HorizonSection(
            horizon="short", freshness_expected="monthly",
            status="no_change", posture="x",
            speculative_candidates=[over_cap],
        )
        return PlanSynthesisOutput(
            long=long, medium=medium, short=short, inputs=SynthesisInputs(),
        )

    # Force the cap-load helper to raise — the production code path uses
    # ``get_user_agent_settings`` directly, so we patch the orchestrator
    # module's view of it.  Both module-local and ``argosy.config`` patches
    # are safe; the call in the orchestrator does ``from argosy.config
    # import ...`` at function call time, so the canonical patch target is
    # ``argosy.config.get_user_agent_settings``.
    def _boom(_uid):
        raise RuntimeError("simulated yaml parse error")

    monkeypatch.setattr(
        "argosy.config.get_user_agent_settings", _boom,
    )

    # Capture events to verify the operator-alert path.  ``_emit_event``
    # is a module-private helper in the orchestrator submodule that
    # delegates to ``publish_event_threadsafe``.  The orchestrator calls
    # it as a bare local name, so patch it on the orchestrator submodule
    # (NOT on the package facade — the call site doesn't go through the
    # package namespace for ``_emit_event``).
    from argosy.orchestrator.flows.plan_synthesis import orchestrator as orch_mod

    captured: list[tuple[str, dict]] = []

    def _capture(name, payload):
        captured.append((name, payload))

    monkeypatch.setattr(orch_mod, "_emit_event", _capture)

    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer",
                        _phase_3_with_over_cap_candidate)
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "x")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: True)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

    # Must not raise.
    out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")

    # Phase 3 stub must have been invoked — i.e. the run continued past
    # the cap-load failure.
    assert phase_3_called["hit"] is True

    # The cap_load_failed event should have been emitted at least once.
    assert any(name == "plan.synthesis.cap_load_failed" for name, _ in captured), (
        f"expected plan.synthesis.cap_load_failed event; got {[n for n, _ in captured]}"
    )

    # The post-filter applied the default cap (0.001) to the over-cap
    # candidate, dropping it from the persisted draft.
    pv = session.get(PlanVersion, out.draft_id)
    short = json.loads(pv.horizon_short_json)
    assert short.get("speculative_candidates") == [], (
        f"default-cap post-filter should have dropped the over-cap candidate; "
        f"got {short.get('speculative_candidates')}"
    )


# ---------------------------------------------------------------------------
# I2 — _horizon_md operator-precedence fix
# ---------------------------------------------------------------------------

def test_horizon_md_renders_targets_with_and_without_rationale():
    """Both targets must appear; empty rationale must not drop the bullet.

    Regression for the operator-precedence bug where the whole f-string
    expression was conditional on t.rationale, causing targets with
    rationale="" to be appended as empty strings instead of bullet lines.
    """
    from datetime import date

    from argosy.agents.plan_synthesizer_types import HorizonSection, SynthTarget, Theme
    from argosy.orchestrator.flows.plan_synthesis import _horizon_md

    t_with = SynthTarget(
        label="Equity allocation",
        value=60.0,
        unit="pct_of_portfolio",
        stated_at=date(2025, 1, 1),
        revisit_after=date(2026, 1, 1),
        rationale="Matches long-term risk tolerance",
    )
    t_without = SynthTarget(
        label="Cash buffer",
        value=5.0,
        unit="pct_of_portfolio",
        stated_at=date(2025, 1, 1),
        revisit_after=date(2026, 1, 1),
        # rationale intentionally omitted — defaults to ""
    )
    th_with = Theme(
        label="Tighten NVDA cap",
        direction="lean_away_from",
        rationale="Concentration risk post-rally",
    )
    th_without = Theme(
        label="Hold bonds",
        direction="monitor",
        # rationale intentionally omitted — defaults to ""
    )

    section = HorizonSection(
        horizon="long",
        freshness_expected="annual",
        status="minor_revision",
        posture="Steady accumulation with defensive tilt",
        targets=[t_with, t_without],
        themes=[th_with, th_without],
    )

    md = _horizon_md(section)

    # Both target bullets must be present.
    assert "**Equity allocation**" in md, "target with rationale should render"
    assert "**Cash buffer**" in md, "target without rationale should render (I2 regression)"

    # The target WITH rationale should include the suffix.
    assert "Matches long-term risk tolerance" in md

    # The target WITHOUT rationale must NOT produce a trailing " — " dash.
    # Find the Cash buffer line and check it has no dangling dash.
    cash_line = next(l for l in md.splitlines() if "Cash buffer" in l)
    assert not cash_line.rstrip().endswith("—"), (
        f"empty-rationale target should not have trailing dash; got: {cash_line!r}"
    )

    # Both theme bullets must be present.
    assert "**Tighten NVDA cap**" in md, "theme with rationale should render"
    assert "**Hold bonds**" in md, "theme without rationale should render"

    # Theme WITH rationale includes suffix; theme WITHOUT must not trail a dash.
    assert "Concentration risk post-rally" in md
    hold_line = next(l for l in md.splitlines() if "Hold bonds" in l)
    assert not hold_line.rstrip().endswith("—"), (
        f"empty-rationale theme should not have trailing dash; got: {hold_line!r}"
    )


# ---------------------------------------------------------------------------
# T0.1 — per-phase agent_report_ids → decision_phases.participants_json
# ---------------------------------------------------------------------------


def _make_stub_agent_report(role: str, decision_id: str, corr_suffix: str):
    """Build a real ``AgentReport`` dataclass for the T0.1 threading test.

    The orchestrator's per-phase tuple-detect path only treats list items
    that are real ``AgentReport`` instances as persistable, so we cannot
    use ``SimpleNamespace`` here.
    """
    from argosy.agents.base import AgentReport, ConfidenceBand

    return AgentReport(
        agent_role=role,
        user_id="ariel",
        model="stub-model",
        response_text=f"stub response for {role}",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.001,
        prompt_hash="stubhash",
        confidence=ConfidenceBand.MEDIUM,
        output=SimpleNamespace(
            model_dump=lambda: {},
            model_dump_json=lambda: "{}",
            approved=True,
        ),
        decision_id=decision_id,
        run_correlation_id=f"corr-{role}-{corr_suffix}",
        system_prompt="sys",
        user_prompt="usr",
    )


def test_phase_completion_threads_agent_report_ids(tmp_path, monkeypatch):
    """T0.1 — every ``decision_phases`` row written during synthesis must
    have a non-empty ``participants_json`` that references the
    ``agent_reports`` ids that actually participated in the phase.

    Pre-T0.1 behavior: ``_record_phase_completion`` hard-coded
    ``agent_report_ids=[]`` so the column was always ``[]`` for every
    phase row — making the ``/decisions/[id]`` sequence diagram
    meaningless even though 18 agent_reports rows existed.

    This test stubs each phase to return ``(<text>, [AgentReport, ...])``
    and drives ``run_synthesis`` to completion; afterwards it asserts
    that all 5 ``synthesis.phase_N`` rows carry a non-empty
    ``participants_json`` and that each id resolves to an
    ``agent_reports`` row back-linked via ``phase_id``.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings
    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    # Build BOTH the sync engine (for the orchestrator's `session` arg)
    # and the async engine (for `_record_phase_completion` →
    # `db_mod.get_session`) pointing at the same SQLite file so writes
    # from the async path are visible to the sync queries below. The
    # `alembic_engine_at_head`-based fixture used elsewhere only creates
    # the sync side; here we recreate both sides ourselves so the test
    # can exercise the full async-recorder path.
    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"

    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False},
    )

    # Run alembic upgrade to head against the same DB file so the sync
    # engine sees the same schema the production code expects.
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    # Re-bind the async engine to the same file (after alembic finishes
    # so the schema is in place).
    from argosy.state import db as db_mod
    db_mod.init_engine(async_url)

    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        session.add(User(id="ariel", plan="free"))
        session.add(PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
        ))
        session.commit()

        from argosy.orchestrator.flows import plan_synthesis as flow

        def _stub_phase_1(**kw):
            decision_id = kw.get("decision_run_id", "")
            reports = [
                _make_stub_agent_report(role, decision_id, "p1")
                for role in ("fundamentals_analyst", "news_analyst")
            ]
            return "(analyst reports)", reports

        def _stub_phase_2(**kw):
            decision_id = kw.get("decision_run_id", "")
            reports = [
                _make_stub_agent_report(role, decision_id, "p2")
                for role in ("bull_researcher", "bear_researcher", "researcher_facilitator")
            ]
            return "(debate outcomes)", reports

        def _stub_phase_3(**kw):
            decision_id = kw.get("decision_run_id", "")
            reports = [_make_stub_agent_report("plan_synthesizer", decision_id, "p3")]
            return _stub_synthesis_output(), reports

        def _stub_phase_4(**kw):
            decision_id = kw.get("decision_run_id", "")
            reports = [
                _make_stub_agent_report(role, decision_id, "p4")
                for role in ("risk_aggressive", "risk_neutral", "risk_conservative", "risk_facilitator")
            ]
            return "(risk verdict)", reports

        def _stub_phase_5(**kw):
            decision_id = kw.get("decision_run_id", "")
            reports = [_make_stub_agent_report("fund_manager", decision_id, "p5")]
            return True, reports

        monkeypatch.setattr(flow, "_run_phase_1_analysts", _stub_phase_1)
        monkeypatch.setattr(flow, "_run_phase_2_debates", _stub_phase_2)
        monkeypatch.setattr(flow, "_run_phase_3_synthesizer", _stub_phase_3)
        monkeypatch.setattr(flow, "_run_phase_4_risk", _stub_phase_4)
        monkeypatch.setattr(flow, "_run_phase_5_fund_manager", _stub_phase_5)
        monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
        monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

        result = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        assert result.draft_id is not None

        # All 5 synthesis.phase_N rows must exist and have non-empty
        # participants_json referencing real agent_reports ids.
        phase_rows = session.execute(
            select(DecisionPhase).where(
                DecisionPhase.decision_run_id == result.decision_run_id
            ).order_by(DecisionPhase.seq.asc())
        ).scalars().all()

        synthesis_phase_rows = [
            p for p in phase_rows if p.kind and p.kind.startswith("synthesis.phase_")
        ]
        assert len(synthesis_phase_rows) == 5, (
            f"expected 5 synthesis.phase_N rows, got {len(synthesis_phase_rows)}: "
            f"{[p.kind for p in synthesis_phase_rows]}"
        )

        for p in synthesis_phase_rows:
            participants = json.loads(p.participants_json or "[]")
            assert isinstance(participants, list) and len(participants) > 0, (
                f"phase {p.kind} seq={p.seq} has empty participants_json — T0.1 "
                f"thread-through regressed; participants_json={p.participants_json!r}"
            )
            # Every participant id must resolve to a real agent_reports row
            # whose phase_id back-link points at this phase. Verifies the
            # full round-trip (persist → record → back-fill).
            from argosy.state.models import AgentReport as AgentReportRow

            for part in participants:
                ar_id = part.get("agent_report_id")
                assert ar_id is not None, (
                    f"participant entry missing agent_report_id: {part}"
                )
                ar = session.get(AgentReportRow, ar_id)
                assert ar is not None, (
                    f"agent_report_id={ar_id} from phase {p.kind} does not "
                    f"resolve to a real row"
                )
                assert ar.phase_id == p.id, (
                    f"agent_reports.phase_id back-link missing: "
                    f"expected {p.id}, got {ar.phase_id}"
                )
    finally:
        import asyncio
        session.close()
        sync_engine.dispose()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db_mod.dispose_engine())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# T0.3 — phase 1 phase_output_json carries adapter_outcomes
# ---------------------------------------------------------------------------


def test_phase_1_phase_output_carries_adapter_outcomes(tmp_path, monkeypatch):
    """T0.3 — after a stubbed synthesis run, the persisted phase 1 row's
    ``phase_output_json`` must be a JSON-encoded dict containing both
    ``analyst_reports_text`` and ``adapter_outcomes``.

    To prove the outcomes flow through end-to-end we stub the phase 1
    analyst function to record a couple of adapter outcomes via
    ``track_adapter_call`` before returning. The orchestrator then calls
    ``collect_outcomes()`` at end of phase 1 and writes the list onto
    ``phase_output_json['adapter_outcomes']``. We read it back from the
    DB and assert the names, statuses, and ordering all flowed through.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings
    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"

    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False},
    )

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    from argosy.state import db as db_mod
    db_mod.init_engine(async_url)

    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        session.add(User(id="ariel", plan="free"))
        session.add(PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
        ))
        session.commit()

        from argosy.orchestrator.flows import plan_synthesis as flow
        from argosy.services.adapter_outcomes import track_adapter_call

        def _stub_phase_1_records_outcomes(**kw):
            # Simulate two adapter calls landing on the contextvar buffer
            # during phase 1 — one healthy, one HTTP 404. The orchestrator
            # is what calls ``collect_outcomes()`` after phase 1 returns,
            # so this stub doesn't need to do anything else.
            with track_adapter_call("finnhub_news", target="NVDA") as o:
                o.set_payload_size_bytes(2048)
            with track_adapter_call("sec_13f", target="13F-HR") as o:
                o.record_http_error(status_code=404, body="Not Found")
            return "(analyst reports text)", []

        monkeypatch.setattr(flow, "_run_phase_1_analysts", _stub_phase_1_records_outcomes)
        monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: ("(debate)", []))
        monkeypatch.setattr(flow, "_run_phase_3_synthesizer",
                            lambda **kw: (_stub_synthesis_output(), []))
        monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: ("(risk)", []))
        monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: (True, []))
        monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
        monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

        result = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        assert result.draft_id is not None

        # Pull the persisted phase 1 row and parse its JSON payload.
        phase_1_row = session.execute(
            select(DecisionPhase)
            .where(DecisionPhase.decision_run_id == result.decision_run_id)
            .where(DecisionPhase.kind == "synthesis.phase_1")
        ).scalar_one_or_none()
        assert phase_1_row is not None, (
            "expected a synthesis.phase_1 row to be persisted"
        )
        assert phase_1_row.phase_output_json is not None, (
            "phase 1 phase_output_json must not be NULL — T0.3 writes a dict"
        )
        payload = json.loads(phase_1_row.phase_output_json)
        assert isinstance(payload, dict), (
            f"phase 1 phase_output_json must be a JSON object dict, got "
            f"{type(payload).__name__}: {payload!r}"
        )
        assert "analyst_reports_text" in payload, (
            f"phase 1 phase_output_json missing analyst_reports_text key: "
            f"{sorted(payload.keys())}"
        )
        assert payload["analyst_reports_text"] == "(analyst reports text)"

        assert "adapter_outcomes" in payload, (
            f"phase 1 phase_output_json missing adapter_outcomes key — T0.3 "
            f"regressed; keys present: {sorted(payload.keys())}"
        )
        outcomes = payload["adapter_outcomes"]
        assert isinstance(outcomes, list), (
            f"adapter_outcomes must be a list, got {type(outcomes).__name__}"
        )
        # The stub recorded exactly two outcomes; the orchestrator's
        # reset_outcomes() at synthesis start guarantees no spill-over
        # from earlier tests in this process.
        assert len(outcomes) == 2, (
            f"expected 2 adapter outcomes (finnhub_news ok + sec_13f 404), "
            f"got {len(outcomes)}: {outcomes!r}"
        )
        names = [o["adapter_name"] for o in outcomes]
        statuses = [o["status"] for o in outcomes]
        assert names == ["finnhub_news", "sec_13f"], (
            f"outcome ordering wrong: {names!r}"
        )
        assert statuses == ["ok", "http_error"], (
            f"outcome statuses wrong: {statuses!r}"
        )
        # And the 404 carries its status code through into the dict shape.
        assert outcomes[1]["http_status_code"] == 404
        assert outcomes[1]["target"] == "13F-HR"
    finally:
        import asyncio
        session.close()
        sync_engine.dispose()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db_mod.dispose_engine())
        finally:
            loop.close()


def test_phase_1_phase_output_adapter_outcomes_empty_when_no_calls(tmp_path, monkeypatch):
    """T0.3 — when no adapter records an outcome during phase 1, the
    persisted ``adapter_outcomes`` list must still be present (just empty).

    Without this, downstream consumers (UI / audit) need to defensively
    check for both "key absent" and "empty list", which is annoying and
    bug-prone. T0.3 chooses the empty-list contract — the buffer is
    always reset at synthesis start, so absence-of-calls produces ``[]``.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings
    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"

    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False},
    )

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    from argosy.state import db as db_mod
    db_mod.init_engine(async_url)

    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        session.add(User(id="ariel", plan="free"))
        session.add(PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
        ))
        session.commit()

        from argosy.orchestrator.flows import plan_synthesis as flow

        monkeypatch.setattr(flow, "_run_phase_1_analysts",
                            lambda **kw: ("(analyst reports)", []))
        monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: ("(debate)", []))
        monkeypatch.setattr(flow, "_run_phase_3_synthesizer",
                            lambda **kw: (_stub_synthesis_output(), []))
        monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: ("(risk)", []))
        monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: (True, []))
        monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "x")
        monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "x")

        result = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")

        phase_1_row = session.execute(
            select(DecisionPhase)
            .where(DecisionPhase.decision_run_id == result.decision_run_id)
            .where(DecisionPhase.kind == "synthesis.phase_1")
        ).scalar_one_or_none()
        assert phase_1_row is not None
        payload = json.loads(phase_1_row.phase_output_json)
        assert payload.get("adapter_outcomes") == [], (
            f"expected empty adapter_outcomes list, got "
            f"{payload.get('adapter_outcomes')!r}"
        )
    finally:
        import asyncio
        session.close()
        sync_engine.dispose()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db_mod.dispose_engine())
        finally:
            loop.close()
