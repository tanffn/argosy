"""Critique reconcile loop — routing, escalation, cost bound, knob.

All LLM calls stubbed (the live e2e proof runs separately). Covers:

* per-class routing (prose_edit / requires_resynthesis / refresh_snapshot /
  needs_user_input / dispute-ZigZag) including the graph-authored downgrade
  (the 12%-ghost horizon-row class -> requires re-synthesis, never a patch);
* escalation on non-convergence (ONE needs-info inbox item, never a re-loop);
* the hard cost bound (exactly 1 closer call + 1 re-verify call);
* the ARGOSY_CRITIQUE_RECONCILE knob (off = old weekly behavior).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.critique_closer import CritiqueCloserAgent
from argosy.agents.plan_critique import PlanCritiqueAgent, PlanCritiqueReport
from argosy.services.critique_reconcile import (
    ReconcileOutcome,
    findings_match,
    reconcile_critique,
)
from argosy.state import db as db_mod
from argosy.state.models import ActionProposal, PlanCritique, PlanVersion, User


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _closer_factory(routes: list[dict], calls: list[str]):
    class _Closer(CritiqueCloserAgent):
        async def _call_model(self, *, system: str, user: str, **_: Any) -> ModelCall:
            calls.append("closer")
            return ModelCall(
                text=json.dumps({"routes": routes, "notes": "test"}),
                tokens_in=1,
                tokens_out=1,
                model=self.model,
            )

    return lambda: _Closer(user_id="ariel")


def _critique_factory(canned: dict, calls: list[str], prompts: list[str]):
    class _Critique(PlanCritiqueAgent):
        async def _call_model(self, *, system: str, user: str, **_: Any) -> ModelCall:
            calls.append("critique")
            prompts.append(user)
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=1,
                tokens_out=1,
                model=self.model,
            )

    return lambda: _Critique(user_id="ariel")


def _finding(severity: str, topic: str, ref: str, summary: str) -> dict:
    return {
        "plan_item_ref": ref,
        "severity": severity,
        "topic": topic,
        "summary": summary,
        "evidence": [f"evidence for {topic}"],
        "cited_sources": ["plan/markdown"],
        "recommended_action": None,
    }


def _report(findings: list[dict]) -> PlanCritiqueReport:
    return PlanCritiqueReport(
        plan_label="Test Plan",
        snapshot_label="test",
        findings=findings,
        overall_summary="test summary",
        cited_sources=["plan/markdown"],
    )


def _reverify_json(findings: list[dict]) -> dict:
    return {
        "plan_label": "Test Plan",
        "snapshot_label": "(reconcile re-verify)",
        "findings": findings,
        "overall_summary": "re-verified",
        "confidence": "MEDIUM",
        "cited_sources": ["plan/markdown"],
    }


async def _seed_plan(raw_markdown: str) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="Test Plan",
                source_path="(test)",
                raw_markdown=raw_markdown,
            )
        )
        await session.commit()


async def _open_proposals() -> list[ActionProposal]:
    async with db_mod.get_session() as session:
        return list(
            (await session.execute(select(ActionProposal))).scalars().all()
        )


# ---------------------------------------------------------------------------
# Routing per finding class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_per_finding_class(engine: None) -> None:
    await _seed_plan("# Plan\n\nNVDA sleeve target is 12% of the book.\n")

    findings = [
        _finding("RED", "Plan Coherence", "NVDA sleeve target 12% vs 8%", "prose says 12%"),
        _finding("RED", "Data Staleness", "holdings as of 2026-06-12", "prices 25d stale"),
        _finding("RED", "Missing Info", "pension statement absent", "need the client's pension statement"),
        _finding("RED", "FX", "FX 2.944 frozen", "verdict flips at live FX"),
        _finding("RED", "Ghost Row", "horizon row NVDA 12%", "stale horizon row"),
    ]
    routes = [
        {"finding_index": 0, "action": "prose_edit", "rationale": "r", "find": "12%", "replace": "8%"},
        {"finding_index": 1, "action": "refresh_snapshot", "rationale": "r"},
        {"finding_index": 2, "action": "needs_user_input", "rationale": "r", "question_for_user": "Upload the pension statement."},
        {"finding_index": 3, "action": "dispute", "rationale": "r", "rebuttal": "live FX 3.006 clears the target"},
        {"finding_index": 4, "action": "requires_resynthesis", "rationale": "r"},
    ]
    # Re-verify: the escalated Ghost Row RED persists (accounted for); the
    # disputed FX finding is gone (withdrawn).
    reverify = _reverify_json(
        [_finding("RED", "Ghost Row", "horizon row NVDA 12%", "still stale")]
    )

    calls: list[str] = []
    prompts: list[str] = []
    refreshed: list[str] = []

    async def _refresher(user_id: str) -> None:
        refreshed.append(user_id)

    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export)",
        report=_report(findings),
        source_critique_id=None,
        closer_factory=_closer_factory(routes, calls),
        critique_factory=_critique_factory(reverify, calls, prompts),
        snapshot_refresher=_refresher,
    )

    assert outcome.triggered
    assert outcome.fixed == 1
    assert outcome.routed_to_service == 1
    assert outcome.disputed_withdrawn == 1
    assert outcome.disputed_upheld == 0
    # needs_user_input + requires_resynthesis both escalate.
    assert outcome.escalated == 2
    assert outcome.converged  # remaining RED maps to the escalation

    # Prose edit landed on the stored plan markdown.
    async with db_mod.get_session() as session:
        plan = await session.get(PlanVersion, 1)
        assert "8% of the book" in plan.raw_markdown
        assert "12%" not in plan.raw_markdown

    # Inbox sinks: one replan_full + one needs-info note_only, no unconverged.
    proposals = await _open_proposals()
    kinds = sorted(p.kind for p in proposals)
    assert kinds == ["note_only", "replan_full"]
    assert not any(
        (p.dedup_key or "").startswith("critique_reconcile_unconverged")
        for p in proposals
    )

    # The data finding was routed to the snapshot-refresh service.
    assert refreshed == ["ariel"]

    # ZigZag: the rebuttal reached the blind re-verification as a directive.
    assert any("live FX 3.006" in p for p in prompts)

    # The re-verify critique row landed with the reconcile payload embedded.
    async with db_mod.get_session() as session:
        rows = (await session.execute(select(PlanCritique))).scalars().all()
        assert len(rows) == 1
        payload = json.loads(rows[0].critique_json)
        rec = payload["reconcile"]
        assert rec["summary_line"] == "reconciled: 1 fixed, 2 escalated, 1 disputed-withdrawn"
        assert rec["finding_status"] == ["escalated"]
        statuses = {e["topic"]: e["status"] for e in rec["per_finding"]}
        assert statuses == {
            "Plan Coherence": "fixed",
            "Data Staleness": "routed",
            "Missing Info": "escalated",
            "FX": "disputed-withdrawn",
            "Ghost Row": "escalated",
        }

    # Cost bound: exactly one closer call + one re-verify call.
    assert calls == ["closer", "critique"]


@pytest.mark.asyncio
async def test_graph_authored_prose_edit_downgrades_to_resynthesis(engine: None) -> None:
    """The 12%-ghost class: graph-authored plans (raw_markdown='') have no
    editable prose surface — a prose_edit route is downgraded to an explicit
    requires-re-synthesis escalation, never a hand-patch."""
    await _seed_plan("")  # graph-authored

    findings = [
        _finding("RED", "Plan Coherence", "NVDA sleeve 8% table vs 12% prose", "ghost 12%"),
    ]
    routes = [
        {"finding_index": 0, "action": "prose_edit", "rationale": "r", "find": "12%", "replace": "8%"},
    ]
    reverify = _reverify_json(
        [_finding("RED", "Plan Coherence", "NVDA sleeve 8% table vs 12% prose", "ghost 12%")]
    )
    calls: list[str] = []

    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export with 12% ghost)",
        report=_report(findings),
        closer_factory=_closer_factory(routes, calls),
        critique_factory=_critique_factory(reverify, calls, []),
        snapshot_refresher=lambda _uid: None,
    )

    assert outcome.fixed == 0
    assert outcome.escalated == 1
    assert outcome.converged  # the persisting RED matches the escalation
    proposals = await _open_proposals()
    assert [p.kind for p in proposals] == ["replan_full"]
    assert outcome.per_finding[0]["status"] == "escalated"
    assert "unreachable" in outcome.per_finding[0]["detail"]


# ---------------------------------------------------------------------------
# Non-convergence -> ONE inbox item, no re-loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonconvergence_escalates_once_and_stops(engine: None) -> None:
    await _seed_plan("# Plan\n")

    findings = [_finding("RED", "FX", "FX frozen", "stale FX")]
    routes = [
        {"finding_index": 0, "action": "dispute", "rationale": "r", "rebuttal": "it is fine"},
    ]
    # Re-verify upholds the dispute AND surfaces a brand-new unmatched RED.
    reverify = _reverify_json(
        [
            _finding("RED", "FX", "FX frozen", "still stale"),
            _finding("RED", "Brand New Problem", "something else entirely", "new"),
        ]
    )
    calls: list[str] = []

    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export)",
        report=_report(findings),
        closer_factory=_closer_factory(routes, calls),
        critique_factory=_critique_factory(reverify, calls, []),
        snapshot_refresher=lambda _uid: None,
    )

    assert outcome.disputed_upheld == 1
    assert not outcome.converged
    assert outcome.finding_status == ["disputed-upheld", "unresolved"]

    proposals = await _open_proposals()
    unconverged = [
        p
        for p in proposals
        if (p.dedup_key or "").startswith("critique_reconcile_unconverged")
    ]
    assert len(unconverged) == 1
    assert "Brand New Problem" in unconverged[0].rationale_md

    # Cost bound holds even when not converged: NO second reconcile pass.
    assert calls == ["closer", "critique"]


# ---------------------------------------------------------------------------
# Trigger gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_trigger_below_threshold(engine: None) -> None:
    await _seed_plan("# Plan\n")
    findings = [
        _finding("YELLOW", "A", "a", "a"),
        _finding("YELLOW", "B", "b", "b"),
        _finding("GREEN", "C", "c", "c"),
    ]
    calls: list[str] = []
    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export)",
        report=_report(findings),
        yellow_threshold=3,
        closer_factory=_closer_factory([], calls),
        critique_factory=_critique_factory(_reverify_json([]), calls, []),
    )
    assert not outcome.triggered
    assert calls == []  # zero LLM calls, zero rows
    assert await _open_proposals() == []


@pytest.mark.asyncio
async def test_yellow_threshold_triggers(engine: None) -> None:
    await _seed_plan("# Plan\n")
    findings = [
        _finding("YELLOW", "A", "ref a", "a"),
        _finding("YELLOW", "B", "ref b", "b"),
        _finding("YELLOW", "C", "ref c", "c"),
    ]
    routes = [
        {"finding_index": i, "action": "requires_resynthesis", "rationale": "r"}
        for i in range(3)
    ]
    calls: list[str] = []
    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export)",
        report=_report(findings),
        yellow_threshold=3,
        closer_factory=_closer_factory(routes, calls),
        critique_factory=_critique_factory(_reverify_json([]), calls, []),
    )
    assert outcome.triggered
    assert outcome.escalated == 3
    assert outcome.converged


# ---------------------------------------------------------------------------
# Knob wiring (weekly loop)
# ---------------------------------------------------------------------------


def _weekly_loop(reconcile_calls: list[dict]):
    from argosy.orchestrator.loops.base import LoopSchedule
    from argosy.orchestrator.loops.weekly_review import (
        WeeklyReviewInputs,
        WeeklyReviewLoop,
    )

    canned = {
        "plan_label": "Test Plan",
        "snapshot_label": "t",
        "overall_summary": "s",
        "confidence": "MEDIUM",
        "cited_sources": ["x"],
        "findings": [
            {
                "plan_item_ref": "r",
                "severity": "RED",
                "topic": "T",
                "summary": "s",
                "evidence": ["e"],
                "cited_sources": ["x"],
            }
        ],
    }

    class _Critique(PlanCritiqueAgent):
        async def _call_model(self, *, system: str, user: str, **_: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned), tokens_in=1, tokens_out=1, model=self.model
            )

    async def _spy_reconcile(**kwargs: Any) -> ReconcileOutcome:
        reconcile_calls.append(kwargs)
        return ReconcileOutcome(triggered=True)

    return WeeklyReviewLoop(
        schedule=LoopSchedule(cron="0 18 * * SUN"),
        user_id="ariel",
        plan_critique_factory=lambda: _Critique(user_id="ariel"),
        gather_inputs=lambda _uid: WeeklyReviewInputs(
            user_id="ariel",
            plan_label="Test Plan",
            plan_markdown="# Plan\n",
            plan_version_id=1,
            snapshot_label="t",
            snapshot_summary="",
        ),
        reconcile_fn=_spy_reconcile,
    )


@pytest.mark.asyncio
async def test_weekly_loop_runs_reconcile_by_default(engine: None) -> None:
    await _seed_plan("# Plan\n")
    calls: list[dict] = []
    await _weekly_loop(calls).tick()
    assert len(calls) == 1
    assert calls[0]["plan_version_id"] == 1
    assert calls[0]["source_critique_id"] is not None


@pytest.mark.asyncio
async def test_weekly_loop_knob_off_restores_old_behavior(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argosy.config import reload_settings

    await _seed_plan("# Plan\n")
    calls: list[dict] = []
    monkeypatch.setenv("ARGOSY_CRITIQUE_RECONCILE", "false")
    reload_settings()
    try:
        await _weekly_loop(calls).tick()
        assert calls == []  # knob off = pre-reconcile behavior
        # The critique itself still landed (old behavior intact).
        async with db_mod.get_session() as session:
            rows = (await session.execute(select(PlanCritique))).scalars().all()
            assert len(rows) == 1
    finally:
        monkeypatch.delenv("ARGOSY_CRITIQUE_RECONCILE", raising=False)
        reload_settings()


# ---------------------------------------------------------------------------
# Matcher unit
# ---------------------------------------------------------------------------


def test_findings_match_topic_and_ref_overlap() -> None:
    a = {"topic": "Plan Coherence", "plan_item_ref": "NVDA sleeve 8% vs 12%"}
    assert findings_match(a, {"topic": "plan coherence", "plan_item_ref": "x"})
    assert findings_match(
        a, {"topic": "Other", "plan_item_ref": "NVDA sleeve target 12%"}
    )
    assert not findings_match(
        a, {"topic": "Other", "plan_item_ref": "completely unrelated thing"}
    )


@pytest.mark.asyncio
async def test_resynth_escalations_aggregate_to_one_proposal(engine: None) -> None:
    """N refinement-unreachable findings = ONE re-synthesis decision = ONE
    inbox row; prior per-finding rows (suffixed dedup keys) are superseded.
    (Live regression: the greeting showed 9 replan_full rows for one yes.)"""
    await _seed_plan("# Plan -- content")
    # Pre-existing per-finding rows from the old sink shape.
    from datetime import datetime, timedelta, timezone as _tz

    _now = datetime.now(_tz.utc)
    async with db_mod.get_session() as session:
        for i in range(2):
            session.add(ActionProposal(
                user_id="ariel", kind="replan_full", status="open",
                dedup_key=f"critique_resynth:ariel:oldsuffix{i}",
                summary=f"old row {i}", rationale_md="",
                suggested_payload="{}", severity="warning",
                surfaced_at=_now, expires_at=_now + timedelta(days=30),
            ))
        await session.commit()

    findings = [
        _finding("RED", f"Topic{i}", f"ref{i}", f"summary {i}") for i in range(3)
    ]
    routes = [
        {"finding_index": i, "action": "requires_resynthesis", "rationale": "r"}
        for i in range(3)
    ]
    reverify = _reverify_json(findings)  # escalated findings may re-appear
    calls: list[str] = []

    outcome = await reconcile_critique(
        user_id="ariel",
        plan_version_id=1,
        plan_label="Test Plan",
        plan_markdown="(export)",
        report=_report(findings),
        source_critique_id=None,
        closer_factory=_closer_factory(routes, calls),
        critique_factory=_critique_factory(reverify, calls, []),
    )

    assert outcome.escalated == 3
    rows = [
        r for r in await _open_proposals()
        if r.dedup_key and r.dedup_key.startswith("critique_resynth:ariel")
    ]
    open_rows = [r for r in rows if r.status == "open"]
    superseded = [r for r in rows if r.status == "superseded"]
    assert len(open_rows) == 1, [(r.dedup_key, r.status) for r in rows]
    assert open_rows[0].dedup_key == "critique_resynth:ariel"
    assert "3 critique finding(s)" in open_rows[0].summary
    assert len(superseded) == 2
