"""Tests for the improved FM-objection auto-routing (root causes 1-4).

Covers:
  1. Parseable owner (explicit agent_report citation) routes directly.
  2. Uncited objection is classified by the LLM stub and routes.
  3. Genuine structural fork (needs_user_input) surfaces an ActionProposal
     with a concrete question rather than being dropped.
  4. Nothing is ever silently dropped: an unroutable objection (classifier
     returns None + False) logs at WARNING and creates a fallback proposal.
  5. New ANALYST_AGENT_NAME_TO_ROLE entries (FXAnalystAgent,
     WithdrawalSequencerAgent, EquityCompAnalystAgent, PlanCoverageAnalyst)
     are present and resolve to the correct roles.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from argosy.agents.analyst_responder import (
    ANALYST_AGENT_NAME_TO_ROLE,
    AnalystResponseToFM,
)
from argosy.agents.base import ConfidenceBand
from argosy.agents.fund_manager_dialogue_verdict import FMObjectionDialogueVerdict
from argosy.agents.objection_owner_classifier import ObjectionOwnerClassification
from argosy.orchestrator.flows import fm_objection_dialogue as flow
from argosy.state.models import ActionProposal, AgentReport, DecisionRun, PlanVersion, User


# ---------------------------------------------------------------------------
# Role-map correctness (root cause 1)
# ---------------------------------------------------------------------------


def test_fx_analyst_both_spellings_resolve_to_fx():
    """Both the alias (FxAnalystAgent) and actual class name (FXAnalystAgent) resolve."""
    assert ANALYST_AGENT_NAME_TO_ROLE["FxAnalystAgent"] == "fx"
    assert ANALYST_AGENT_NAME_TO_ROLE["FXAnalystAgent"] == "fx"


def test_withdrawal_sequencer_in_map():
    assert ANALYST_AGENT_NAME_TO_ROLE["WithdrawalSequencerAgent"] == "withdrawal_sequencer"


def test_equity_comp_analyst_in_map():
    assert ANALYST_AGENT_NAME_TO_ROLE["EquityCompAnalystAgent"] == "equity_comp_analyst"


def test_plan_coverage_analyst_in_map():
    assert ANALYST_AGENT_NAME_TO_ROLE["PlanCoverageAnalyst"] == "plan_coverage"


def test_parse_refs_withdrawal_sequencer():
    """FM citing agent_report:WithdrawalSequencerAgent now resolves (was root cause #1)."""
    text = (
        "The FI-bridge ladder is inconsistent with the retirement model. "
        "agent_report:WithdrawalSequencerAgent derives -186,670 NIS (FI NOT MET) "
        "but the user directive locks the margin at +579,730 NIS (FI MET)."
    )
    roles = flow._parse_analyst_refs_any_form(text)
    assert "withdrawal_sequencer" in roles


def test_parse_refs_fx_analyst_both_spellings():
    """Both FxAnalystAgent and FXAnalystAgent are now parseable."""
    text_alias = "agent_report:FxAnalystAgent shows USD/NIS at 3.72."
    text_real = "agent_report:FXAnalystAgent shows USD/NIS at 3.72."
    assert "fx" in flow._parse_analyst_refs_any_form(text_alias)
    assert "fx" in flow._parse_analyst_refs_any_form(text_real)


# ---------------------------------------------------------------------------
# Fixtures and stubs
# ---------------------------------------------------------------------------

FI_MARGIN_OBJECTION_TOPIC = "FI margin conflict"
FI_MARGIN_OBJECTION_DETAIL = (
    "The user directive locks the honest-liquid margin at +579,730 NIS (FI MET) "
    "while agent_report:WithdrawalSequencerAgent derives -186,670 NIS (FI NOT MET). "
    "These two figures are irreconcilable without knowing which basis governs."
)
UNCITED_OBJECTION_TOPIC = "Tax treatment of RSU vesting"
UNCITED_OBJECTION_DETAIL = (
    "The plan does not clarify whether the RSU income is treated as ordinary "
    "employment income (taxed at marginal rate) or capital gains. The total "
    "NIS tax difference between the two interpretations exceeds ₪80,000."
)
USER_FORK_TOPIC = "Retirement goal conflict"
USER_FORK_DETAIL = (
    "The user stated a retirement target of age 52 in the intake directive but "
    "the plan's glide path implies retirement at 57. These are two valid paths "
    "with different risk and spend trade-offs; only the user can choose."
)


def _seed_draft_with_fm_objections(session_factory, *, fm_reasons: list[str]) -> tuple[int, int]:
    """Insert user + draft + synthesis run + FM agent_report with given reasons."""
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# B"))
        sess.commit()

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

        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="test-auto-route",
            raw_markdown="",
            horizon_long_md="", horizon_medium_md="", horizon_short_md="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"minor_revision","posture":"x"}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
            decision_run_id=run.id,
        )
        sess.add(draft)
        sess.commit()
        sess.refresh(draft)

        fm_payload = json.dumps({
            "approved": False,
            "reasons": fm_reasons,
            "cited_sources": ["plan/draft"],
        })
        fm_row = AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id=f"plan-synth-{run.id}",
            model="claude-opus-4-7",
            prompt_hash="x" * 16,
            response_text=fm_payload,
            tokens_in=1000, tokens_out=500, cost_usd=0.10,
        )
        sess.add(fm_row)
        sess.commit()
        return draft.id, run.id
    finally:
        sess.close()


def _make_stub_analyst(stance: str = "REBUT") -> type:
    class _StubAnalyst:
        def __init__(self, *, user_id: str) -> None:
            self.user_id = user_id
        def run_sync(self, **_kwargs: Any):
            out = AnalystResponseToFM(
                stance=stance,
                reasoning_md="Stub analyst response.",
                cited_sources=[],
                confidence=ConfidenceBand.MEDIUM,
            )
            return SimpleNamespace(output=out)
    return _StubAnalyst


def _make_stub_fm(resolution: str = "FM_ACCEPTS_ANALYST") -> type:
    class _StubFM:
        def __init__(self, *, user_id: str) -> None:
            self.user_id = user_id
        def run_sync(self, **_kwargs: Any):
            out = FMObjectionDialogueVerdict(
                resolution=resolution,
                reasoning_md="Stub FM verdict.",
                confidence=ConfidenceBand.MEDIUM,
                cited_sources=[],
            )
            return SimpleNamespace(output=out)
    return _StubFM


def _patch_dialogue_agents(monkeypatch, *, stance: str = "REBUT", resolution: str = "FM_ACCEPTS_ANALYST") -> None:
    monkeypatch.setattr(
        "argosy.agents.analyst_responder.AnalystResponderAgent",
        _make_stub_analyst(stance),
    )
    monkeypatch.setattr(
        "argosy.agents.fund_manager_dialogue_verdict.FundManagerDialogueVerdictAgent",
        _make_stub_fm(resolution),
    )


def _make_classifier_stub(
    *,
    owner_role: str | None = None,
    needs_user_input: bool = False,
    user_question: str = "",
    rationale: str = "stub",
) -> type:
    """Factory for ObjectionOwnerClassifierAgent stubs."""
    class _StubClassifier:
        def __init__(self, *, user_id: str) -> None:
            self.user_id = user_id
        def run_sync(self, **_kwargs: Any):
            out = ObjectionOwnerClassification(
                owner_role=owner_role,
                needs_user_input=needs_user_input,
                user_question=user_question,
                rationale=rationale,
            )
            return SimpleNamespace(output=out)
    return _StubClassifier


@pytest.fixture(autouse=True)
def _reset_in_flight_registry():
    with flow._in_flight_lock:
        flow._in_flight.clear()
    yield
    with flow._in_flight_lock:
        flow._in_flight.clear()


# ---------------------------------------------------------------------------
# Test 1: parseable owner (explicit citation) routes directly
# ---------------------------------------------------------------------------


def test_parseable_owner_routes_without_classifier(client_with_db, monkeypatch):
    """An objection that cites agent_report:WithdrawalSequencerAgent routes directly.

    The LLM classifier must NOT be called (it's not even stubbed; if called,
    the ObjectionOwnerClassifierAgent constructor would raise on the real
    claude.exe path — its absence proves the classifier was skipped).
    """
    session_factory = client_with_db.app.state.session_factory
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[
            f"FI margin conflict — {FI_MARGIN_OBJECTION_DETAIL}",
        ],
    )
    # Find the decision_run_id from the DB.
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    _patch_dialogue_agents(monkeypatch, stance="REBUT", resolution="FM_ACCEPTS_ANALYST")
    # Stub the classifier to FAIL loudly if called — proves it was skipped.
    def _classifier_should_not_be_called(user_id, topic, detail, severity):
        raise AssertionError("classifier should not be called when explicit citation exists")
    monkeypatch.setattr(flow, "_classify_objection_owner_llm", _classifier_should_not_be_called)

    sess = session_factory()
    try:
        dispatched = flow.schedule_auto_dialogues_for_draft(
            sess,
            user_id="ariel",
            plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
    finally:
        sess.close()

    assert dispatched == 1

    # Verify the dialogue was created with the correct role.
    sess2 = session_factory()
    try:
        dialogues = (
            sess2.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="fm_objection_dialogue")
            .all()
        )
        assert len(dialogues) == 1
        notes = json.loads(dialogues[0].notes_json or "{}")
        assert notes["analyst_role"] == "withdrawal_sequencer"
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Test 2: uncited objection gets agent-resolved owner
# ---------------------------------------------------------------------------


def test_uncited_objection_classifier_resolves_owner(client_with_db, monkeypatch):
    """An objection with no explicit citation is routed via the LLM classifier."""
    session_factory = client_with_db.app.state.session_factory
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[
            f"Tax treatment ambiguity — {UNCITED_OBJECTION_DETAIL}",
        ],
    )
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    _patch_dialogue_agents(monkeypatch, stance="REBUT", resolution="FM_ACCEPTS_ANALYST")

    # Stub the classifier to return "tax" as owner.
    monkeypatch.setattr(
        flow,
        "_classify_objection_owner_llm",
        lambda user_id, topic, detail, severity: ("tax", False, ""),
    )

    sess = session_factory()
    try:
        dispatched = flow.schedule_auto_dialogues_for_draft(
            sess,
            user_id="ariel",
            plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
    finally:
        sess.close()

    assert dispatched == 1

    sess2 = session_factory()
    try:
        dialogues = (
            sess2.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="fm_objection_dialogue")
            .all()
        )
        assert len(dialogues) == 1
        notes = json.loads(dialogues[0].notes_json or "{}")
        assert notes["analyst_role"] == "tax"
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Test 3: genuine structural fork produces ActionProposal with specific question
# ---------------------------------------------------------------------------


def test_structural_fork_surfaces_action_proposal(client_with_db, monkeypatch):
    """When the classifier says needs_user_input=True, an ActionProposal is created.

    The proposal must carry the concrete question the classifier provided, not
    a generic 'synthesis failed' fallback.
    """
    session_factory = client_with_db.app.state.session_factory
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[
            f"Retirement goal conflict — {USER_FORK_DETAIL}",
        ],
    )
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    concrete_question = (
        "Your intake directive targets retirement at age 52; the plan glide path "
        "implies age 57. These differ in risk and spend by ~₪1.2M over 5 years. "
        "Which target governs — and if 52, what should the plan sacrifice?"
    )
    monkeypatch.setattr(
        flow,
        "_classify_objection_owner_llm",
        lambda user_id, topic, detail, severity: (None, True, concrete_question),
    )

    sess = session_factory()
    try:
        dispatched = flow.schedule_auto_dialogues_for_draft(
            sess,
            user_id="ariel",
            plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
    finally:
        sess.close()

    # No dialogue was dispatched (user question, not an analyst route).
    assert dispatched == 0

    # An ActionProposal must exist with the concrete question.
    sess2 = session_factory()
    try:
        proposals = (
            sess2.query(ActionProposal)
            .filter_by(user_id="ariel", status="open", kind="note_only")
            .all()
        )
        assert len(proposals) == 1, f"Expected 1 proposal, got {len(proposals)}"
        p = proposals[0]
        assert concrete_question in p.rationale_md, (
            f"Proposal rationale must contain the concrete question. Got: {p.rationale_md}"
        )
        assert "FM objection needs your input" in p.summary
        assert p.dedup_key is not None
        assert "fm_objection_unroutable" in p.dedup_key
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Test 4: completely unroutable → WARNING logged + fallback proposal
# ---------------------------------------------------------------------------


def test_unroutable_logs_warning_and_creates_fallback_proposal(
    client_with_db, monkeypatch, caplog
):
    """When both regex AND classifier find nothing, a WARNING is logged and a
    fallback proposal is created so the objection never silently vanishes.
    """
    session_factory = client_with_db.app.state.session_factory
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[
            "Completely unroutable mysterious objection with no agent citation.",
        ],
    )
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    # Classifier returns nothing — truly unresolvable.
    monkeypatch.setattr(
        flow,
        "_classify_objection_owner_llm",
        lambda user_id, topic, detail, severity: (None, False, ""),
    )

    with caplog.at_level(logging.WARNING):
        sess = session_factory()
        try:
            dispatched = flow.schedule_auto_dialogues_for_draft(
                sess,
                user_id="ariel",
                plan_version_id=plan_version_id,
                decision_run_id=decision_run_id,
            )
        finally:
            sess.close()

    assert dispatched == 0

    # A WARNING must have been emitted (not just INFO).
    warning_records = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "unroutable" in (r.getMessage() + getattr(r, "event", "") + str(getattr(r, "msg", ""))).lower()
    ]
    assert warning_records, (
        "Expected at least one WARNING log about an unroutable objection. "
        f"Captured records: {[r.getMessage() for r in caplog.records]}"
    )

    # A fallback ActionProposal must exist.
    sess2 = session_factory()
    try:
        proposals = (
            sess2.query(ActionProposal)
            .filter_by(user_id="ariel", status="open", kind="note_only")
            .all()
        )
        assert len(proposals) == 1, (
            f"Expected 1 fallback proposal, got {len(proposals)}"
        )
        p = proposals[0]
        assert "FM objection needs your input" in p.summary
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Test 5: run-359 scenario — FI margin conflict with WithdrawalSequencerAgent
# ---------------------------------------------------------------------------


def test_run_359_fi_margin_objection_routes_to_withdrawal_sequencer(
    client_with_db, monkeypatch
):
    """Reproduce the run-359 scenario: the FM cited WithdrawalSequencerAgent in a
    FI-margin objection. With the old code, ANALYST_AGENT_NAME_TO_ROLE was missing
    that entry and the objection was silently dropped. Now it should route.
    """
    session_factory = client_with_db.app.state.session_factory
    run_359_objection = (
        "Financial Independence margin conflict — "
        "The user directive locks the honest-liquid margin at +579,730 NIS (FI MET) "
        "while agent_report:WithdrawalSequencerAgent derives -186,670 NIS (FI NOT "
        "reached). These two bases are irreconcilable without knowing which governs: "
        "the user's stated directive or the retirement model's arithmetic."
    )
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[run_359_objection],
    )
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    _patch_dialogue_agents(monkeypatch, stance="CLARIFY", resolution="ESCALATE_TO_USER")
    # Classifier should NOT be called because the regex will find the explicit citation.
    monkeypatch.setattr(
        flow,
        "_classify_objection_owner_llm",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("classifier called even though citation was explicit")
        ),
    )

    sess = session_factory()
    try:
        dispatched = flow.schedule_auto_dialogues_for_draft(
            sess,
            user_id="ariel",
            plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
    finally:
        sess.close()

    assert dispatched == 1

    sess2 = session_factory()
    try:
        dialogues = (
            sess2.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="fm_objection_dialogue")
            .all()
        )
        assert len(dialogues) == 1
        notes = json.loads(dialogues[0].notes_json or "{}")
        # analyst_role is set in the initial notes_json before async finalization.
        # resolution is set by the background thread's _finalize_async(); we only
        # assert the routing was correct (dispatched to the right analyst).
        assert notes["analyst_role"] == "withdrawal_sequencer"
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Test 6: proposal is idempotent (dedup_key prevents double-write)
# ---------------------------------------------------------------------------


def test_unroutable_proposal_is_idempotent(client_with_db, monkeypatch):
    """Two calls for the same objection create only one open proposal."""
    session_factory = client_with_db.app.state.session_factory
    _seed_draft_with_fm_objections(
        session_factory,
        fm_reasons=[
            "Another unroutable one without citations.",
        ],
    )
    sess = session_factory()
    try:
        run = sess.query(DecisionRun).filter_by(decision_kind="plan_revision").first()
        draft = sess.query(PlanVersion).filter_by(role="draft").first()
        decision_run_id = run.id
        plan_version_id = draft.id
    finally:
        sess.close()

    monkeypatch.setattr(
        flow,
        "_classify_objection_owner_llm",
        lambda user_id, topic, detail, severity: (None, True, "Concrete user question here."),
    )

    for _ in range(2):
        sess = session_factory()
        try:
            flow.schedule_auto_dialogues_for_draft(
                sess,
                user_id="ariel",
                plan_version_id=plan_version_id,
                decision_run_id=decision_run_id,
            )
        finally:
            sess.close()

    sess2 = session_factory()
    try:
        proposals = (
            sess2.query(ActionProposal)
            .filter_by(user_id="ariel", status="open", kind="note_only")
            .all()
        )
        assert len(proposals) == 1, (
            f"Dedup should prevent two open proposals for the same objection. "
            f"Got {len(proposals)}."
        )
    finally:
        sess2.close()
