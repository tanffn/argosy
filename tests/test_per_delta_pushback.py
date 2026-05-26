"""Tests for the T4.3 per-delta slim re-debate flow.

Covered:
  * Happy path: stubbed bull/bear/facilitator produce a MODIFY verdict
    that lands in ``decision_runs.notes_json`` with the right shape.
  * Cost cap: when the per-user 24h spend leaves headroom <
    ``ESTIMATED_RUN_COST_USD``, the dispatcher refuses cleanly.
  * Idempotency: a second invocation within the registry window
    returns the same ``decision_run_id``.
  * 404: pushback for a non-existent ``item_id`` raises
    ``DeltaNotFoundError`` (and the API route turns that into a 404).

The bull/bear/facilitator agent classes are imported at call-time
inside ``_run_slim_redebate`` so we monkeypatch via the
``argosy.agents.researcher`` and ``argosy.agents.researcher_facilitator``
module namespaces — that's where the flow looks them up.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from argosy.agents.researcher import ResearcherTurn
from argosy.agents.researcher_facilitator import DebateOutcome
from argosy.orchestrator.flows import per_delta_pushback as flow
from argosy.state.models import DecisionRun, PlanVersion, User


def _seed_user_and_draft(
    session_factory, *, deltas: list[dict] | None = None,
) -> int:
    """Insert user 'ariel' + a draft PlanVersion with one delta on medium.

    Returns the draft id. Default delta has item_id 'medium.actions.foo'.
    """
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# P"))

        medium_payload = {
            "horizon": "medium",
            "freshness_expected": "quarterly",
            "status": "minor_revision",
            "posture": "test",
            "deltas_from_prior": deltas
            if deltas is not None
            else [
                {
                    "item_kind": "action",
                    "item_id": "medium.actions.foo",
                    "horizon": "medium",
                    "change_kind": "modified",
                    "summary": "trim NVDA over 50%",
                    "rationale": "concentration risk",
                    "cited_sources": ["agent_report:ConcentrationAnalystAgent"],
                    "prior": {"value": 55, "unit": "pct"},
                    "proposed": {
                        "label": "NVDA cap",
                        "value": 40,
                        "unit": "pct",
                    },
                    "accepted": False,
                    "user_edited": False,
                    "user_edit_note": None,
                }
            ],
        }
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="synth-test",
            raw_markdown="",
            horizon_long_md="",
            horizon_medium_md="",
            horizon_short_md="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json=json.dumps(medium_payload),
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        )
        sess.add(draft)
        sess.commit()
        return draft.id
    finally:
        sess.close()


class _StubBull:
    """Stand-in for ``BullResearcherAgent`` that captures kwargs + returns
    a deterministic ResearcherTurn."""

    last_kwargs: dict | None = None

    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id

    def run_sync(self, **kwargs):
        _StubBull.last_kwargs = kwargs
        turn = ResearcherTurn(
            side="bull",
            round_index=1,
            position_summary="keep NVDA cap at 40% — concentration risk justified",
            points=[],
            response_to_opposing="",
            cited_sources=["agent_report:ConcentrationAnalystAgent"],
        )
        # Use a SimpleNamespace so the flow's ``isinstance(..., AgentReport)``
        # guard skips persistence — keeps the test focused on outcome shape.
        return SimpleNamespace(output=turn)


class _StubBear:
    last_kwargs: dict | None = None

    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id

    def run_sync(self, **kwargs):
        _StubBear.last_kwargs = kwargs
        turn = ResearcherTurn(
            side="bear",
            round_index=1,
            position_summary="trim NVDA further — user's pushback is correct",
            points=[],
            response_to_opposing="bull underweights tax drag",
            cited_sources=["agent_report:TaxAnalystAgent"],
        )
        return SimpleNamespace(output=turn)


class _StubFacilitatorModify:
    """Facilitator stub that returns winning_side='bear' so the flow
    maps to MODIFY (revised_value populated)."""

    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id

    def run_sync(self, **kwargs):
        outcome = DebateOutcome(
            winning_side="bear",
            synthesis="Trim NVDA to 35%; user's tax-window concern is valid.",
            cited_evidence=["TaxAnalyst: §102 window expiring 2026Q4"],
            rounds_run=1,
            cited_sources=["agent_report:TaxAnalystAgent"],
        )
        return SimpleNamespace(output=outcome)


class _StubFacilitatorBullWins:
    """Facilitator stub: bull wins => KEEP verdict."""

    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id

    def run_sync(self, **kwargs):
        outcome = DebateOutcome(
            winning_side="bull",
            synthesis="Bull case prevails; NVDA cap at 40% remains correct.",
            cited_evidence=[],
            rounds_run=1,
            cited_sources=["agent_report:ConcentrationAnalystAgent"],
        )
        return SimpleNamespace(output=outcome)


@pytest.fixture(autouse=True)
def _reset_in_flight_registry():
    """Ensure each test starts with a clean idempotency registry.

    The registry is process-level state; without this the second test
    in a session inherits the first's claims.
    """
    with flow._in_flight_lock:
        flow._in_flight.clear()
    yield
    with flow._in_flight_lock:
        flow._in_flight.clear()


def test_happy_path_modify_verdict_persists_to_notes_json(client_with_db, monkeypatch):
    """Stubbed bull/bear/facilitator produce MODIFY → outcome lands in
    decision_runs.notes_json + decision_run row stamped completed."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    monkeypatch.setattr(
        "argosy.agents.researcher.BullResearcherAgent", _StubBull,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher.BearResearcherAgent", _StubBear,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher_facilitator.ResearcherFacilitatorAgent",
        _StubFacilitatorModify,
    )

    sess = session_factory()
    try:
        result = flow.start_per_delta_pushback(
            sess,
            user_id="ariel",
            draft_id=draft_id,
            item_id="medium.actions.foo",
            user_feedback="this ignores my §102 tax window",
            run_inline=True,
        )
    finally:
        sess.close()

    assert result.inflight is False
    assert result.decision_run_id > 0

    # Verify the DecisionRun row is stamped + notes_json shape.
    sess2 = session_factory()
    try:
        row = sess2.get(DecisionRun, result.decision_run_id)
        assert row is not None
        assert row.decision_kind == "delta_pushback"
        assert row.ticker == "(plan)"
        assert row.user_id == "ariel"
        assert row.status == "completed"
        assert row.finished_at is not None

        notes = json.loads(row.notes_json or "{}")
        # T4.4 contract — UI's kindLabel() needs delta_item_id.
        assert notes.get("delta_item_id") == "medium.actions.foo"
        assert notes.get("user_feedback").startswith("this ignores")
        assert notes.get("horizon") == "medium"
        assert notes.get("verdict") == "MODIFY"
        assert "rationale_md" in notes
        # The revised_value is a narrative DTO (the flow doesn't get a
        # structured value back from a Sonnet facilitator).
        assert notes.get("revised_value", {}).get("kind") == "narrative"
        original = notes.get("original_value")
        # original_value reflects the synthesizer's proposed value at
        # dispatch time.
        assert isinstance(original, dict)
        assert original.get("value") == 40
    finally:
        sess2.close()


def test_happy_path_bull_wins_yields_keep_verdict(client_with_db, monkeypatch):
    """When the facilitator awards the bull, the outcome is KEEP with
    no revised_value (the synthesizer's proposed value stands)."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    monkeypatch.setattr(
        "argosy.agents.researcher.BullResearcherAgent", _StubBull,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher.BearResearcherAgent", _StubBear,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher_facilitator.ResearcherFacilitatorAgent",
        _StubFacilitatorBullWins,
    )

    sess = session_factory()
    try:
        result = flow.start_per_delta_pushback(
            sess,
            user_id="ariel",
            draft_id=draft_id,
            item_id="medium.actions.foo",
            user_feedback="this seems aggressive",
            run_inline=True,
        )
    finally:
        sess.close()

    sess2 = session_factory()
    try:
        row = sess2.get(DecisionRun, result.decision_run_id)
        notes = json.loads(row.notes_json or "{}")
        assert notes.get("verdict") == "KEEP"
        assert notes.get("revised_value") is None
    finally:
        sess2.close()


def test_cost_cap_refusal_when_headroom_too_tight(
    client_with_db, monkeypatch
):
    """When 24h spend leaves headroom < ESTIMATED_RUN_COST_USD, the
    dispatcher raises CostCapExceededError without writing a
    decision_runs row."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    # Cap = $10; pretend $9.80 spent so headroom is $0.20 < $0.50.
    monkeypatch.setenv("ARGOSY_SYNTHESIS_COST_CAP_USD", "10.0")
    monkeypatch.setattr(
        "argosy.orchestrator.flows.per_delta_pushback._total_recent_cost_usd",
        lambda session, user_id: 9.80,
    )

    sess = session_factory()
    try:
        with pytest.raises(flow.CostCapExceededError) as exc_info:
            flow.start_per_delta_pushback(
                sess,
                user_id="ariel",
                draft_id=draft_id,
                item_id="medium.actions.foo",
                user_feedback="please re-look",
                run_inline=True,
            )
        msg = str(exc_info.value)
        assert "$9.80" in msg
        assert "$10.00" in msg
    finally:
        sess.close()

    # No decision_runs row should have been opened.
    sess2 = session_factory()
    try:
        rows = sess2.query(DecisionRun).filter_by(user_id="ariel").all()
        assert len(rows) == 0
    finally:
        sess2.close()


def test_idempotency_double_click_returns_same_run_id(
    client_with_db, monkeypatch
):
    """Two start calls within the idempotency window return the same
    decision_run_id without firing a second flow."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    monkeypatch.setattr(
        "argosy.agents.researcher.BullResearcherAgent", _StubBull,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher.BearResearcherAgent", _StubBear,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher_facilitator.ResearcherFacilitatorAgent",
        _StubFacilitatorModify,
    )

    # First call: register in-flight WITHOUT actually executing so the
    # registry entry is fresh when the second call lands. We patch
    # `_execute_and_finalize` to do nothing — it would normally release
    # the in-flight entry at end-of-thread, but for this test we want
    # to assert short-circuit behaviour mid-flight.
    called: list[int] = []

    def _no_execute(**kwargs):
        called.append(kwargs["decision_run_id"])

    monkeypatch.setattr(
        flow, "_execute_and_finalize", _no_execute,
    )

    sess = session_factory()
    try:
        first = flow.start_per_delta_pushback(
            sess,
            user_id="ariel",
            draft_id=draft_id,
            item_id="medium.actions.foo",
            user_feedback="first click",
            run_inline=True,
        )
    finally:
        sess.close()

    # In our patched _execute_and_finalize the registry doesn't get
    # released automatically (the patched function doesn't run the
    # cleanup wrapper). Re-claim manually to simulate a still-running
    # flow so the second call hits the short-circuit.
    with flow._in_flight_lock:
        # The inline run released the slot via the `finally` clause
        # around `_execute_and_finalize` in start_per_delta_pushback.
        # Reclaim it for the test.
        flow._in_flight[("ariel", draft_id, "medium.actions.foo")] = (
            first.decision_run_id,
            __import__("time").monotonic(),
        )

    sess2 = session_factory()
    try:
        second = flow.start_per_delta_pushback(
            sess2,
            user_id="ariel",
            draft_id=draft_id,
            item_id="medium.actions.foo",
            user_feedback="second click 10s later",
            run_inline=True,
        )
    finally:
        sess2.close()

    assert second.inflight is True
    assert second.decision_run_id == first.decision_run_id
    # Only the first call's run_id should have made it to _execute_and_finalize.
    assert called == [first.decision_run_id]

    # Only ONE DecisionRun row exists for the user.
    sess3 = session_factory()
    try:
        rows = sess3.query(DecisionRun).filter_by(user_id="ariel").all()
        assert len(rows) == 1
        assert rows[0].id == first.decision_run_id
    finally:
        sess3.close()


def test_delta_not_found_raises(client_with_db):
    """Pushback for a non-existent item_id raises DeltaNotFoundError."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    sess = session_factory()
    try:
        with pytest.raises(flow.DeltaNotFoundError):
            flow.start_per_delta_pushback(
                sess,
                user_id="ariel",
                draft_id=draft_id,
                item_id="medium.actions.nope_not_real",
                user_feedback="this should 404",
                run_inline=True,
            )
    finally:
        sess.close()


def test_draft_not_found_raises(client_with_db):
    """Pushback against a draft id that doesn't exist for the user → 404."""
    session_factory = client_with_db.app.state.session_factory
    _seed_user_and_draft(session_factory)

    sess = session_factory()
    try:
        with pytest.raises(flow.DeltaNotFoundError):
            flow.start_per_delta_pushback(
                sess,
                user_id="ariel",
                draft_id=999_999,
                item_id="medium.actions.foo",
                user_feedback="x",
                run_inline=True,
            )
    finally:
        sess.close()


def test_api_route_returns_decision_run_id_on_success(
    client_with_db, monkeypatch
):
    """POST .../pushback returns the new decision_run_id when the slim
    flow successfully kicks off."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    monkeypatch.setattr(
        "argosy.agents.researcher.BullResearcherAgent", _StubBull,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher.BearResearcherAgent", _StubBear,
    )
    monkeypatch.setattr(
        "argosy.agents.researcher_facilitator.ResearcherFacilitatorAgent",
        _StubFacilitatorModify,
    )

    # Force the route to run the slim flow inline so the test doesn't
    # race the background thread. We patch the function the route
    # imports to default ``run_inline=True``.
    real_start = flow.start_per_delta_pushback

    def _inline_start(session, **kwargs):
        kwargs.setdefault("run_inline", True)
        return real_start(session, **kwargs)

    monkeypatch.setattr(
        "argosy.orchestrator.flows.per_delta_pushback.start_per_delta_pushback",
        _inline_start,
    )

    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/items/medium.actions.foo/pushback?user_id=ariel",
        json={"feedback": "please reconsider tax timing"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "slim_redebate_started"
    assert body["decision_run_id"] is not None
    assert body["inflight"] is False
    assert body["item_id"] == "medium.actions.foo"

    # Legacy side-effect preserved: user_edit_note carries PUSHBACK.
    r2 = client_with_db.get("/api/plan/draft?user_id=ariel")
    delta = r2.json()["horizon_medium"]["deltas_from_prior"][0]
    assert delta["user_edit_note"].startswith("PUSHBACK:")


def test_api_route_cost_cap_returns_clean_status(
    client_with_db, monkeypatch
):
    """Cost-cap refusal surfaces as ``status=cost_cap_refused`` on the
    200 response (not a 500). The user_edit_note side-effect is still
    persisted so the feedback isn't lost."""
    session_factory = client_with_db.app.state.session_factory
    draft_id = _seed_user_and_draft(session_factory)

    monkeypatch.setenv("ARGOSY_SYNTHESIS_COST_CAP_USD", "1.0")
    monkeypatch.setattr(
        "argosy.orchestrator.flows.per_delta_pushback._total_recent_cost_usd",
        lambda session, user_id: 0.99,
    )

    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/items/medium.actions.foo/pushback?user_id=ariel",
        json={"feedback": "near-cap pushback"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cost_cap_refused"
    assert body["decision_run_id"] is None
    assert "cap" in (body["detail"] or "").lower()

    # Legacy side-effect: pushback note still persisted even though
    # the slim flow refused.
    r2 = client_with_db.get("/api/plan/draft?user_id=ariel")
    delta = r2.json()["horizon_medium"]["deltas_from_prior"][0]
    assert delta["user_edit_note"].startswith("PUSHBACK: near-cap")
