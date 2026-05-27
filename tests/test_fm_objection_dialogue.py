"""Tests for the FM-objection ZigZag flow.

Covers:
  * Happy paths: FM_ACCEPTS_ANALYST / FM_MAINTAINS_OBJECTION /
    FM_REVISES_OBJECTION / ESCALATE_TO_USER all flow through to
    decision_runs.notes_json with the right shape.
  * Cost-cap refusal when 24h spend leaves headroom < estimate.
  * Idempotency: a second click within the 5-min window returns the
    same decision_run_id without firing a second LLM pair.
  * API route: POST .../discuss returns decision_run_id; GET
    .../dialogues re-renders the prior dialogue.
  * Agent-ref parser: pulls TechnicalAnalystAgent out of the verbatim
    NVDA-price-discrepancy objection from draft #11.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from argosy.agents.analyst_responder import AnalystResponseToFM
from argosy.agents.base import ConfidenceBand
from argosy.agents.fund_manager_dialogue_verdict import (
    FMObjectionDialogueVerdict,
)
from argosy.orchestrator.flows import fm_objection_dialogue as flow
from argosy.state.models import AgentReport, DecisionRun, PlanVersion, User


# ---------------------------------------------------------------------
# Fixtures + seeding
# ---------------------------------------------------------------------


# Draft #11 verbatim NVDA-price-discrepancy objection. Used both for
# parser tests and for the API-level seeding so the same shape that
# the user sees in production rides through the test suite.
NVDA_PRICE_DISCREPANCY_DETAIL = (
    "NVDA price discrepancy unreconciled across the plan. All gate/"
    "tranche/tax arithmetic uses portfolio/holdings $200.14, but "
    "agent_report:TechnicalAnalystAgent cites $182.50 from the most "
    "recent close. The plan never explains which figure is authoritative."
)


def _seed_user_draft_and_fm_objection(
    session_factory, *, fm_response_json: str | None = None,
) -> tuple[int, int]:
    """Insert user + a draft with a synthesis DecisionRun + FM agent_report.

    Returns ``(plan_version_id, decision_run_id)``. The default FM
    response carries one objection — the NVDA price-discrepancy one
    that names TechnicalAnalystAgent.
    """
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        # baseline
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# P"))
        sess.commit()

        # synthesis decision_run
        run = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier="T3",
            decision_kind="plan_revision",
            started_at=datetime.now(UTC),
            status="completed",
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)

        # the draft with the FK back to the run
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="synth-test",
            raw_markdown="",
            horizon_long_md="",
            horizon_medium_md="",
            horizon_short_md="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"minor_revision","posture":"x"}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
            decision_run_id=run.id,
        )
        sess.add(draft)
        sess.commit()
        sess.refresh(draft)

        # the FM agent_report whose response_text the route parses
        fm_payload = fm_response_json or json.dumps({
            "approved": False,
            "reasons": [
                f"NVDA price coherence — {NVDA_PRICE_DISCREPANCY_DETAIL}",
            ],
            "cited_sources": ["plan/draft", "agent_report:TechnicalAnalystAgent"],
        })
        fm_row = AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id=f"plan-synth-{run.id}",
            model="claude-opus-4-7",
            prompt_hash="x" * 16,
            response_text=fm_payload,
            tokens_in=1000,
            tokens_out=500,
            cost_usd=0.10,
        )
        sess.add(fm_row)

        # a prior technical analyst agent_report so the dialogue can
        # thread its excerpt into the responder prompt.
        tech_row = AgentReport(
            user_id="ariel",
            agent_role="technical",
            decision_id=f"plan-synth-{run.id}",
            model="claude-sonnet-4-6",
            prompt_hash="t" * 16,
            response_text=(
                "TechnicalReport JSON: NVDA close $182.50 on 2026-05-23, "
                "RSI 58, MACD positive cross. yfinance:NVDA:1d cited. "
                "Signal: hold."
            ),
            tokens_in=500,
            tokens_out=300,
            cost_usd=0.05,
        )
        sess.add(tech_row)
        sess.commit()

        return draft.id, run.id
    finally:
        sess.close()


# ---------------------------------------------------------------------
# Stub agent helpers — produce deterministic outputs without LLM calls
# ---------------------------------------------------------------------


class _StubAnalyst:
    """Stand-in for AnalystResponderAgent. Returns a CONCEDE response."""

    last_kwargs: dict | None = None
    stance: str = "CONCEDE"
    reasoning_md: str = "I agree — my prior figure was stale."
    suggested_fix: str = "Adopt $200.14 across the plan and re-derive the gates."
    cited_sources_list: list[str] = ["agent_report:TechnicalAnalystAgent"]

    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id

    def run_sync(self, **kwargs):
        _StubAnalyst.last_kwargs = kwargs
        out = AnalystResponseToFM(
            stance=self.stance,
            reasoning_md=self.reasoning_md,
            suggested_fix=self.suggested_fix,
            cited_sources=self.cited_sources_list,
            confidence=ConfidenceBand.MEDIUM,
        )
        return SimpleNamespace(output=out)


def _stub_fm(
    *,
    resolution: str,
    updated_objection_text: str | None = None,
    suggested_plan_amendment: str | None = None,
):
    """Factory for an FM stub returning ``resolution``."""

    class _StubFM:
        last_kwargs: dict | None = None

        def __init__(self, *, user_id: str) -> None:
            self.user_id = user_id

        def run_sync(self, **kwargs):
            _StubFM.last_kwargs = kwargs
            out = FMObjectionDialogueVerdict(
                resolution=resolution,
                updated_objection_text=updated_objection_text,
                suggested_plan_amendment=suggested_plan_amendment,
                reasoning_md=f"Final verdict: {resolution}.",
                confidence=ConfidenceBand.MEDIUM,
                cited_sources=["agent_report:TechnicalAnalystAgent"],
            )
            return SimpleNamespace(output=out)

    return _StubFM


@pytest.fixture(autouse=True)
def _reset_in_flight_registry():
    """Ensure each test starts with a clean idempotency registry."""
    with flow._in_flight_lock:
        flow._in_flight.clear()
    yield
    with flow._in_flight_lock:
        flow._in_flight.clear()


def _patch_agents(monkeypatch, *, analyst_cls, fm_cls) -> None:
    """Patch the two LLM agent imports inside ``_run_dialogue``."""
    monkeypatch.setattr(
        "argosy.agents.analyst_responder.AnalystResponderAgent", analyst_cls,
    )
    monkeypatch.setattr(
        "argosy.agents.fund_manager_dialogue_verdict.FundManagerDialogueVerdictAgent",
        fm_cls,
    )


# ---------------------------------------------------------------------
# Agent-ref parser
# ---------------------------------------------------------------------


def test_parse_agent_refs_pulls_technical_analyst_from_nvda_objection():
    """The verbatim draft-#11 NVDA objection names TechnicalAnalystAgent."""
    refs = flow.parse_agent_refs_from_objection(NVDA_PRICE_DISCREPANCY_DETAIL)
    assert refs == ["TechnicalAnalystAgent"]


def test_parse_agent_refs_dedupes_and_filters_non_analyst():
    """References to non-analyst agents are filtered; dupes deduped."""
    text = (
        "agent_report:TechnicalAnalystAgent says X; "
        "agent_report:TechnicalAnalystAgent again says X. "
        "agent_report:TraderAgent says Y (not an analyst). "
        "agent_report:ConcentrationAnalystAgent says Z."
    )
    refs = flow.parse_agent_refs_from_objection(text)
    # Trader is not in the analyst map; dupes collapsed.
    assert refs == ["TechnicalAnalystAgent", "ConcentrationAnalystAgent"]


def test_parse_agent_refs_empty_when_no_refs():
    assert flow.parse_agent_refs_from_objection("") == []
    assert flow.parse_agent_refs_from_objection(
        "no agent reference in this text"
    ) == []


# ---------------------------------------------------------------------
# Flow — all four resolutions
# ---------------------------------------------------------------------


def _run_inline(
    client_with_db,
    monkeypatch,
    *,
    analyst_stance: str = "CONCEDE",
    fm_resolution: str = "FM_ACCEPTS_ANALYST",
    updated_objection_text: str | None = None,
    suggested_plan_amendment: str | None = None,
):
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    class _AnalystCls(_StubAnalyst):
        pass

    _AnalystCls.stance = analyst_stance
    _patch_agents(
        monkeypatch,
        analyst_cls=_AnalystCls,
        fm_cls=_stub_fm(
            resolution=fm_resolution,
            updated_objection_text=updated_objection_text,
            suggested_plan_amendment=suggested_plan_amendment,
        ),
    )

    sess = session_factory()
    try:
        result = flow.start_fm_objection_dialogue(
            sess,
            user_id="ariel",
            plan_version_id=draft_id,
            objection_index=0,
            analyst_role="technical",
            objection_topic="NVDA price coherence",
            objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
            objection_severity="AMBER",
            prior_decision_audit_token=f"plan-synth-{decision_run_id}",
            run_inline=True,
        )
    finally:
        sess.close()

    return result, session_factory, draft_id, decision_run_id


def test_resolution_fm_accepts_analyst(client_with_db, monkeypatch):
    result, sf, draft_id, _ = _run_inline(
        client_with_db, monkeypatch,
        analyst_stance="CONCEDE",
        fm_resolution="FM_ACCEPTS_ANALYST",
        suggested_plan_amendment="Apply $200.14 across the plan.",
    )
    assert result.inflight is False
    assert result.decision_run_id > 0

    sess = sf()
    try:
        row = sess.get(DecisionRun, result.decision_run_id)
        assert row is not None
        assert row.decision_kind == "fm_objection_dialogue"
        assert row.ticker == "(plan)"
        assert row.status == "completed"
        notes = json.loads(row.notes_json or "{}")
        assert notes["objection_index"] == 0
        assert notes["analyst_role"] == "technical"
        assert notes["resolution"] == "FM_ACCEPTS_ANALYST"
        assert notes["analyst_stance"] == "CONCEDE"
        assert notes["suggested_plan_amendment"] == "Apply $200.14 across the plan."
        assert notes["updated_objection_text"] is None
        # plan_version_id mirrored back into notes for the dialogues GET filter.
        assert notes["plan_version_id"] == draft_id
    finally:
        sess.close()


def test_resolution_fm_maintains_objection(client_with_db, monkeypatch):
    result, sf, _, _ = _run_inline(
        client_with_db, monkeypatch,
        analyst_stance="REBUT",
        fm_resolution="FM_MAINTAINS_OBJECTION",
    )
    sess = sf()
    try:
        row = sess.get(DecisionRun, result.decision_run_id)
        notes = json.loads(row.notes_json or "{}")
        assert notes["resolution"] == "FM_MAINTAINS_OBJECTION"
        assert notes["analyst_stance"] == "REBUT"
        assert notes["suggested_plan_amendment"] is None
        assert notes["updated_objection_text"] is None
    finally:
        sess.close()


def test_resolution_fm_revises_objection(client_with_db, monkeypatch):
    result, sf, _, _ = _run_inline(
        client_with_db, monkeypatch,
        analyst_stance="CLARIFY",
        fm_resolution="FM_REVISES_OBJECTION",
        updated_objection_text="Pick ONE NVDA price and document the choice.",
    )
    sess = sf()
    try:
        row = sess.get(DecisionRun, result.decision_run_id)
        notes = json.loads(row.notes_json or "{}")
        assert notes["resolution"] == "FM_REVISES_OBJECTION"
        assert notes["analyst_stance"] == "CLARIFY"
        assert "ONE NVDA price" in notes["updated_objection_text"]
    finally:
        sess.close()


def test_resolution_escalate_to_user(client_with_db, monkeypatch):
    result, sf, _, _ = _run_inline(
        client_with_db, monkeypatch,
        analyst_stance="CLARIFY",
        fm_resolution="ESCALATE_TO_USER",
    )
    sess = sf()
    try:
        row = sess.get(DecisionRun, result.decision_run_id)
        notes = json.loads(row.notes_json or "{}")
        assert notes["resolution"] == "ESCALATE_TO_USER"
    finally:
        sess.close()


# ---------------------------------------------------------------------
# Cost-cap
# ---------------------------------------------------------------------


def test_cost_cap_refusal_when_headroom_too_tight(client_with_db, monkeypatch):
    """Cap = $10, $9.80 spent → headroom $0.20 < $0.50 → refuse cleanly."""
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    monkeypatch.setenv("ARGOSY_SYNTHESIS_COST_CAP_USD", "10.0")
    monkeypatch.setattr(
        "argosy.orchestrator.flows.fm_objection_dialogue._total_recent_cost_usd",
        lambda session, user_id: 9.80,
    )

    sess = session_factory()
    try:
        with pytest.raises(flow.CostCapExceededError) as exc_info:
            flow.start_fm_objection_dialogue(
                sess,
                user_id="ariel",
                plan_version_id=draft_id,
                objection_index=0,
                analyst_role="technical",
                objection_topic="NVDA",
                objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
                objection_severity="AMBER",
                prior_decision_audit_token=f"plan-synth-{decision_run_id}",
                run_inline=True,
            )
        msg = str(exc_info.value)
        assert "$9.80" in msg
        assert "$10.00" in msg
    finally:
        sess.close()

    # No new fm_objection_dialogue row should have been opened.
    sess2 = session_factory()
    try:
        rows = (
            sess2.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="fm_objection_dialogue")
            .all()
        )
        assert len(rows) == 0
    finally:
        sess2.close()


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_idempotency_double_click_returns_same_run_id(
    client_with_db, monkeypatch,
):
    """Two start calls within 5 min return the same decision_run_id."""
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    _patch_agents(
        monkeypatch,
        analyst_cls=_StubAnalyst,
        fm_cls=_stub_fm(resolution="FM_ACCEPTS_ANALYST"),
    )

    called: list[int] = []

    def _no_execute(**kwargs):
        called.append(kwargs["decision_run_id"])

    monkeypatch.setattr(flow, "_execute_and_finalize", _no_execute)

    sess = session_factory()
    try:
        first = flow.start_fm_objection_dialogue(
            sess,
            user_id="ariel",
            plan_version_id=draft_id,
            objection_index=0,
            analyst_role="technical",
            objection_topic="NVDA",
            objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
            objection_severity="AMBER",
            prior_decision_audit_token=f"plan-synth-{decision_run_id}",
            run_inline=True,
        )
    finally:
        sess.close()

    # Reclaim the in-flight slot to simulate a still-running first call.
    import time as _time

    with flow._in_flight_lock:
        flow._in_flight[
            ("ariel", draft_id, 0, "technical")
        ] = (first.decision_run_id, _time.monotonic())

    sess2 = session_factory()
    try:
        second = flow.start_fm_objection_dialogue(
            sess2,
            user_id="ariel",
            plan_version_id=draft_id,
            objection_index=0,
            analyst_role="technical",
            objection_topic="NVDA",
            objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
            objection_severity="AMBER",
            prior_decision_audit_token=f"plan-synth-{decision_run_id}",
            run_inline=True,
        )
    finally:
        sess2.close()

    assert second.inflight is True
    assert second.decision_run_id == first.decision_run_id
    assert called == [first.decision_run_id]

    sess3 = session_factory()
    try:
        rows = (
            sess3.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="fm_objection_dialogue")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == first.decision_run_id
    finally:
        sess3.close()


# ---------------------------------------------------------------------
# Invalid analyst role
# ---------------------------------------------------------------------


def test_invalid_analyst_role_raises(client_with_db):
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    sess = session_factory()
    try:
        with pytest.raises(flow.InvalidAnalystRoleError):
            flow.start_fm_objection_dialogue(
                sess,
                user_id="ariel",
                plan_version_id=draft_id,
                objection_index=0,
                analyst_role="not_a_real_role",
                objection_topic="NVDA",
                objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
                objection_severity="AMBER",
                prior_decision_audit_token=f"plan-synth-{decision_run_id}",
                run_inline=True,
            )
    finally:
        sess.close()


# ---------------------------------------------------------------------
# API route
# ---------------------------------------------------------------------


def test_post_discuss_returns_decision_run_id(client_with_db, monkeypatch):
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    _patch_agents(
        monkeypatch,
        analyst_cls=_StubAnalyst,
        fm_cls=_stub_fm(resolution="FM_ACCEPTS_ANALYST",
                        suggested_plan_amendment="Use $200.14."),
    )
    # Force inline so the test doesn't race a background thread.
    real_start = flow.start_fm_objection_dialogue

    def _inline_start(session, **kwargs):
        kwargs.setdefault("run_inline", True)
        return real_start(session, **kwargs)

    monkeypatch.setattr(
        "argosy.orchestrator.flows.fm_objection_dialogue.start_fm_objection_dialogue",
        _inline_start,
    )

    r = client_with_db.post(
        "/api/plan/draft/objections/0/discuss",
        json={"user_id": "ariel", "analyst_role": "technical"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "dialogue_started"
    assert body["decision_run_id"] is not None
    assert body["inflight"] is False
    assert body["detail"] == "TechnicalAnalystAgent"


def test_post_discuss_cost_cap_returns_clean_status(
    client_with_db, monkeypatch,
):
    session_factory = client_with_db.app.state.session_factory
    _seed_user_draft_and_fm_objection(session_factory)

    monkeypatch.setenv("ARGOSY_SYNTHESIS_COST_CAP_USD", "1.0")
    monkeypatch.setattr(
        "argosy.orchestrator.flows.fm_objection_dialogue._total_recent_cost_usd",
        lambda session, user_id: 0.99,
    )

    r = client_with_db.post(
        "/api/plan/draft/objections/0/discuss",
        json={"user_id": "ariel", "analyst_role": "technical"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cost_cap_refused"
    assert body["decision_run_id"] is None
    assert "cap" in (body["detail"] or "").lower()


def test_post_discuss_unknown_analyst_role_400(client_with_db):
    session_factory = client_with_db.app.state.session_factory
    _seed_user_draft_and_fm_objection(session_factory)

    r = client_with_db.post(
        "/api/plan/draft/objections/0/discuss",
        json={"user_id": "ariel", "analyst_role": "totally_made_up"},
    )
    assert r.status_code == 400, r.text


def test_post_discuss_objection_index_out_of_range_404(client_with_db):
    session_factory = client_with_db.app.state.session_factory
    _seed_user_draft_and_fm_objection(session_factory)

    r = client_with_db.post(
        "/api/plan/draft/objections/99/discuss",
        json={"user_id": "ariel", "analyst_role": "technical"},
    )
    assert r.status_code == 404, r.text


def test_get_dialogues_lists_prior_run(client_with_db, monkeypatch):
    """Run a dialogue inline; GET /dialogues returns it."""
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    _patch_agents(
        monkeypatch,
        analyst_cls=_StubAnalyst,
        fm_cls=_stub_fm(resolution="FM_ACCEPTS_ANALYST",
                        suggested_plan_amendment="Use $200.14 across the plan."),
    )

    sess = session_factory()
    try:
        result = flow.start_fm_objection_dialogue(
            sess,
            user_id="ariel",
            plan_version_id=draft_id,
            objection_index=0,
            analyst_role="technical",
            objection_topic="NVDA price coherence",
            objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
            objection_severity="AMBER",
            prior_decision_audit_token=f"plan-synth-{decision_run_id}",
            run_inline=True,
        )
    finally:
        sess.close()
    assert result.inflight is False

    r = client_with_db.get(
        "/api/plan/draft/objections/0/dialogues?user_id=ariel",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["objection_index"] == 0
    assert body["plan_version_id"] == draft_id
    assert len(body["dialogues"]) == 1
    d = body["dialogues"][0]
    assert d["decision_run_id"] == result.decision_run_id
    assert d["analyst_role"] == "technical"
    assert d["resolution"] == "FM_ACCEPTS_ANALYST"
    assert d["analyst_stance"] == "CONCEDE"
    assert d["suggested_plan_amendment"] == "Use $200.14 across the plan."


def test_get_dialogues_filters_other_objections(client_with_db, monkeypatch):
    """A dialogue on idx=0 must NOT show up under GET idx=1."""
    session_factory = client_with_db.app.state.session_factory
    draft_id, decision_run_id = _seed_user_draft_and_fm_objection(session_factory)

    _patch_agents(
        monkeypatch,
        analyst_cls=_StubAnalyst,
        fm_cls=_stub_fm(resolution="FM_MAINTAINS_OBJECTION"),
    )

    sess = session_factory()
    try:
        flow.start_fm_objection_dialogue(
            sess,
            user_id="ariel",
            plan_version_id=draft_id,
            objection_index=0,
            analyst_role="technical",
            objection_topic="NVDA",
            objection_detail=NVDA_PRICE_DISCREPANCY_DETAIL,
            objection_severity="AMBER",
            prior_decision_audit_token=f"plan-synth-{decision_run_id}",
            run_inline=True,
        )
    finally:
        sess.close()

    # GET idx=1 should be empty.
    r = client_with_db.get(
        "/api/plan/draft/objections/1/dialogues?user_id=ariel",
    )
    assert r.status_code == 200
    assert r.json()["dialogues"] == []
