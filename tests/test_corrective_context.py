"""Corrective (critique-fed) re-synthesis — context-builder unit tests.

Design: docs/design/corrective_resynthesis.md §5 (builder unit tests:
reconcile-status filtering, adjudication selection, derived-fact join,
deterministic rendering).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import (
    ActionProposal,
    AgentReport,
    DecisionRun,
    PlanCritique,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
)


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="current", version_label="v67",
        raw_markdown="", horizon_long_md="# Long",
    ))
    s.commit()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_derived_facts(monkeypatch):
    """Default the derived-fact join to empty; the join test opts in."""
    from argosy.services import derived_facts

    monkeypatch.setattr(derived_facts, "build_derived_facts", lambda *a, **k: None)


def _finding(topic, ref, summary, severity="RED", evidence=None):
    return {
        "severity": severity, "topic": topic, "plan_item_ref": ref,
        "summary": summary, "evidence": evidence or [],
    }


def _add_critique(session, findings, finding_status=None, per_finding=None,
                  plan_version_id=None):
    if plan_version_id is None:
        plan_version_id = session.query(PlanVersion).filter_by(
            user_id="ariel", role="current"
        ).one().id
    payload = {"findings": findings}
    if finding_status is not None or per_finding is not None:
        payload["reconcile"] = {
            "finding_status": finding_status or [],
            "per_finding": per_finding or [],
        }
    row = PlanCritique(
        user_id="ariel", plan_version_id=plan_version_id,
        critique_json=json.dumps(payload), model="test",
    )
    session.add(row)
    session.commit()
    return row


def _add_proposal(session, *, kind, dedup_key, status="open",
                  execution_state="proposed", payload=None,
                  summary="prop", rationale="rationale"):
    now = datetime.now(timezone.utc)
    row = ActionProposal(
        user_id="ariel", summary=summary, rationale_md=rationale,
        suggested_payload=json.dumps(payload or {}), severity="warning",
        surfaced_at=now, expires_at=now + timedelta(days=30),
        status=status, kind=kind, dedup_key=dedup_key,
        execution_state=execution_state,
    )
    session.add(row)
    session.commit()
    return row


def test_returns_none_when_nothing_open(session):
    from argosy.services.corrective_context import build_corrective_context

    assert build_corrective_context(session, user_id="ariel") is None


def test_reconcile_status_filtering_excludes_fixed_and_withdrawn(session):
    """Only escalated / disputed-upheld / unresolved findings become
    corrections; fixed and disputed-withdrawn are settled — never re-fed."""
    from argosy.services.corrective_context import build_corrective_context

    findings = [
        _finding("fx-rate", "assumptions.fx", "FX 3.00 vs plan 2.944"),
        _finding("nvda-target", "targets.nvda", "IPS prose says 12% vs plan 8%"),
        _finding("glide", "glide.schedule", "glide contradicts pace"),
        _finding("settled", "prose.settled", "already fixed by prose edit"),
        _finding("withdrawn", "prose.withdrawn", "dispute withdrawn"),
    ]
    _add_critique(
        session, findings,
        finding_status=["escalated", "disputed-upheld", "unresolved", None, None],
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    topics = [c.topic for c in ctx.corrections]
    assert topics == ["fx-rate", "nvda-target", "glide"]
    statuses = [c.reconcile_status for c in ctx.corrections]
    assert statuses == ["escalated", "disputed-upheld", "unresolved"]
    # Settled findings never appear anywhere in the rendered block.
    assert "already fixed by prose edit" not in ctx.rendered
    assert "dispute withdrawn" not in ctx.rendered


def test_proposal_findings_union_without_duplicates(session):
    """The aggregated critique_resynth proposal's findings are unioned in via
    the lenient matcher — same-subject findings never duplicate."""
    from argosy.services.corrective_context import build_corrective_context

    f_same = _finding("nvda-target", "targets.nvda", "NVDA 12% vs 8%")
    f_new = _finding("fi-margin", "retirement.fi_margin", "FI margin overstated")
    _add_critique(session, [f_same], finding_status=["escalated"])
    prop = _add_proposal(
        session, kind="replan_full", dedup_key="critique_resynth:ariel",
        payload={"findings": [f_same, f_new]},
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert [c.topic for c in ctx.corrections] == ["nvda-target", "fi-margin"]
    assert prop.id in ctx.proposal_ids


def test_proposal_findings_settled_since_are_not_refed(session):
    """Codex blocker #1: a finding the latest reconcile settled (fixed /
    disputed-withdrawn) must not leak back in via a stale proposal payload."""
    from argosy.services.corrective_context import build_corrective_context

    f_fixed = _finding("fx-rate", "assumptions.fx", "FX 3.00 vs 2.944")
    f_open = _finding("glide", "glide.schedule", "stale glide")
    _add_critique(
        session, [f_open],
        finding_status=["escalated"],
        per_finding=[
            {"finding_index": 0, "topic": "fx-rate",
             "plan_item_ref": "assumptions.fx", "status": "fixed"},
            {"finding_index": 1, "topic": "glide",
             "plan_item_ref": "glide.schedule", "status": "escalated"},
        ],
    )
    # Stale proposal still carries the since-fixed FX finding.
    _add_proposal(
        session, kind="replan_full", dedup_key="critique_resynth:ariel",
        payload={"findings": [f_fixed, f_open]},
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert [c.topic for c in ctx.corrections] == ["glide"]
    assert "FX 3.00" not in ctx.rendered


def test_accepted_resynth_proposal_still_feeds_corrections(session):
    """Ariel may confirm the pending proposal before the run fires — a
    status='accepted' critique_resynth row still supplies corrections and is
    still slated to flip on promote (never treated as a directive)."""
    from argosy.services.corrective_context import build_corrective_context

    prop = _add_proposal(
        session, kind="replan_full", dedup_key="critique_resynth:ariel",
        status="accepted",
        payload={"findings": [_finding("glide", "glide.schedule", "stale glide")]},
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert len(ctx.corrections) == 1
    assert prop.id in ctx.proposal_ids
    assert ctx.directives == []


def test_adjudication_selection(session):
    """Directives = status='accepted' + execution_state='proposed' + allowlisted
    kind. Open rows, wrong kinds, and the critique_resynth row are excluded."""
    from argosy.services.corrective_context import build_corrective_context

    good = _add_proposal(
        session, kind="update_plan_assumption", dedup_key="glide_verdict:ariel",
        status="accepted", summary="NVDA glide schedule 2026/2027/2028",
        rationale="4,136 / 5,094 / 592 sh — fast-on-eligible-core",
    )
    _add_proposal(  # still open — not adjudicated yet
        session, kind="update_plan_assumption", dedup_key="other:1", status="open",
    )
    _add_proposal(  # wrong kind
        session, kind="note_only", dedup_key="note:1", status="accepted",
    )
    _add_proposal(  # already applied (dismissed execution state)
        session, kind="update_plan_assumption", dedup_key="done:1",
        status="accepted", execution_state="dismissed",
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert [d.proposal_id for d in ctx.directives] == [good.id]
    assert good.id in ctx.proposal_ids
    assert "apply verbatim" in ctx.rendered


def test_adjudication_selection_real_accept_execution_state(session):
    """The accept service sets execution_state='accepted_pending_user_action'
    (action_proposals.py) — the selector must treat it as accepted-but-
    unapplied. Regression: live run 140 attached 0 directives after Ariel's
    real accept of proposal 49 because the selector matched 'proposed' only."""
    from argosy.services.corrective_context import build_corrective_context

    accepted = _add_proposal(
        session, kind="update_plan_assumption",
        dedup_key="plan_glide_schedule_verdict:ariel:nvda",
        status="accepted", execution_state="accepted_pending_user_action",
        summary="NVDA glide schedule 2026/2027/2028",
        rationale="4,136 / 5,094 / 592 sh — fast-on-eligible-core",
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert [d.proposal_id for d in ctx.directives] == [accepted.id]
    assert "NVDA glide schedule" in ctx.rendered


def test_derived_fact_join(session, monkeypatch):
    """Each correction is joined to the derived fact covering its surface so
    it carries the canonical value, not just 'this is wrong'."""
    from argosy.services import derived_facts
    from argosy.services.corrective_context import build_corrective_context

    monkeypatch.setattr(
        derived_facts, "build_derived_facts",
        lambda *a, **k: {"nvda_target_sh": 4136, "fi_margin_liquid_nis": -50000},
    )
    _add_critique(
        session,
        [
            _finding("nvda-target", "targets.nvda", "target shares stale"),
            _finding("fx-rate", "assumptions.fx", "FX 3.00 vs 2.944"),
        ],
        finding_status=["escalated", "escalated"],
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    nvda = ctx.corrections[0]
    assert ("nvda_target_sh", 4136) in nvda.canonical_facts
    assert all(k != "fi_margin_liquid_nis" for k, _ in nvda.canonical_facts)
    fx = ctx.corrections[1]
    assert fx.canonical_facts == []  # no fact covers the FX surface
    assert "nvda_target_sh = 4,136" in ctx.rendered


def test_derived_fact_join_requires_majority_token_match(monkeypatch, session):
    """Codex finding #5 trade-off: a fact must not join on a single shared
    token out of two (e.g. nvda_breaking_sh onto any NVDA-mentioning finding)
    — every joined value is required VERBATIM by the deterministic floor."""
    from argosy.services import derived_facts
    from argosy.services.corrective_context import (
        build_corrective_context,
        match_fact_to_finding,
    )

    monkeypatch.setattr(
        derived_facts, "build_derived_facts",
        lambda *a, **k: {"nvda_target_sh": 4136, "nvda_breaking_sh": 1710},
    )
    finding = _finding("nvda-target", "targets.nvda", "target shares stale")
    assert match_fact_to_finding("nvda_target_sh", finding) is True  # 2/2
    assert match_fact_to_finding("nvda_breaking_sh", finding) is False  # 1/2
    _add_critique(session, [finding], finding_status=["escalated"])
    ctx = build_corrective_context(session, user_id="ariel")
    keys = [k for k, _ in ctx.corrections[0].canonical_facts]
    assert keys == ["nvda_target_sh"]


def test_rendering_is_deterministic(session):
    from argosy.services.corrective_context import build_corrective_context

    _add_critique(
        session,
        [_finding("a", "ref.a", "s1"), _finding("b", "ref.b", "s2")],
        finding_status=["escalated", "unresolved"],
    )
    _add_proposal(
        session, kind="update_plan_assumption", dedup_key="v:1", status="accepted",
    )
    r1 = build_corrective_context(session, user_id="ariel")
    r2 = build_corrective_context(session, user_id="ariel")
    assert r1 is not None and r2 is not None
    assert r1.rendered == r2.rendered
    assert r1.rendered.startswith("CORRECTIVE RE-SYNTHESIS")
    assert "[1]" in r1.rendered and "[2]" in r1.rendered and "[D1]" in r1.rendered
    # The base document is named (edit-don't-rebuild framing).
    assert "v67" in r1.rendered


def test_forces_full_tier_on_snapshot_routed_finding(session):
    """A refresh_snapshot-routed finding implicates phase-1 inputs — the
    corrective run must NOT reuse phases 1-2. No snapshot row exists here,
    so the freshness waiver must fail-safe toward forcing."""
    from argosy.services.corrective_context import build_corrective_context

    _add_critique(
        session,
        [_finding("stale-snap", "portfolio.snapshot", "snapshot stale")],
        finding_status=["escalated"],
        per_finding=[{"finding_index": 0, "status": "routed"}],
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert ctx.forces_full_tier is True


def _add_snapshot(session, *, user_id="ariel", imported_at):
    row = PortfolioSnapshotRow(user_id=user_id, imported_at=imported_at)
    session.add(row)
    session.commit()
    return row


def _add_routed_critique(session):
    return _add_critique(
        session,
        [_finding("stale-snap", "portfolio.snapshot", "snapshot stale")],
        finding_status=["escalated"],
        per_finding=[{"finding_index": 0, "status": "routed"}],
    )


def test_snapshot_force_kept_when_snapshot_older_than_critique(session):
    """A snapshot that PREDATES the critique is exactly the staleness the
    finding flagged — the full tier stays forced."""
    from argosy.services.corrective_context import build_corrective_context

    _add_snapshot(
        session,
        imported_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(days=1),
    )
    _add_routed_critique(session)
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert ctx.forces_full_tier is True


def test_snapshot_force_waived_when_snapshot_newer_than_critique(
    session, monkeypatch
):
    """Live bug (runs 141/143/144): the snapshot WAS refreshed after the
    critique, yet every corrective pass kept paying the full tier. A fresh
    snapshot that postdates the critique waives the forcing, loudly (the
    waiver event carries BOTH timestamps for audit)."""
    from argosy.services import corrective_context as cc

    critique = _add_routed_critique(session)
    # Backdate the critique, then land a fresh snapshot after it.
    critique.created_at = datetime.now(timezone.utc) - timedelta(hours=6)
    session.commit()
    _add_snapshot(
        session,
        imported_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=1),
    )

    events: list[tuple[str, dict]] = []

    class _LogSpy:
        def info(self, event, **kw):
            events.append((event, kw))

        def warning(self, event, **kw):
            events.append((event, kw))

    monkeypatch.setattr(cc, "_log", _LogSpy())
    ctx = cc.build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert ctx.forces_full_tier is False
    waived = [kw for ev, kw in events
              if ev == "corrective_context.snapshot_force_waived"]
    assert len(waived) == 1
    # Both timestamps are in the event for auditability.
    assert "snapshot_imported_at" in waived[0]
    assert "critique_created_at" in waived[0]


def test_snapshot_force_kept_when_fresh_snapshot_belongs_to_other_user(session):
    """Fail-safe: another user's fresh snapshot proves nothing about THIS
    user's inputs — keep forcing."""
    from argosy.services.corrective_context import build_corrective_context

    critique = _add_routed_critique(session)
    critique.created_at = datetime.now(timezone.utc) - timedelta(hours=6)
    session.commit()
    session.add(User(id="other", plan="free"))
    session.commit()
    _add_snapshot(
        session, user_id="other",
        imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert ctx.forces_full_tier is True


def test_snapshot_force_waiver_handles_naive_vs_aware_timestamps(session):
    """SQLite drops tzinfo (convention: naive == UTC). A naive snapshot
    timestamp vs an aware critique timestamp must compare, not raise —
    and still waive when the snapshot is genuinely newer."""
    from argosy.services.corrective_context import (
        _snapshot_refresh_postdates_critique,
    )

    now_aware = datetime.now(timezone.utc)
    _add_snapshot(
        session,
        imported_at=now_aware.replace(tzinfo=None) + timedelta(hours=1),
    )
    assert _snapshot_refresh_postdates_critique(
        session, user_id="ariel", critique_created_at=now_aware
    ) is True
    # Aware critique NEWER than the snapshot → no waiver.
    assert _snapshot_refresh_postdates_critique(
        session, user_id="ariel",
        critique_created_at=now_aware + timedelta(days=1),
    ) is False
    # Missing critique timestamp → fail-safe, no waiver.
    assert _snapshot_refresh_postdates_critique(
        session, user_id="ariel", critique_created_at=None
    ) is False


def test_reader_directive_lists_corrections_and_directives(session):
    from argosy.services.corrective_context import build_corrective_context

    _add_critique(
        session, [_finding("glide", "glide.schedule", "stale glide")],
        finding_status=["escalated"],
    )
    _add_proposal(
        session, kind="update_plan_assumption", dedup_key="v:2", status="accepted",
        summary="glide verdict",
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    d = ctx.reader_directive
    assert "CORRECTIVE-RUN VERIFICATION" in d
    assert "glide" in d and "[D1]" in d


# ---------------------------------------------------------------------------
# Source 5 — verdict feedback (FM rejection / reader block on a prior
# corrective draft becomes STRUCTURED corrections, not free-text paste-back)
# ---------------------------------------------------------------------------

RUN_144_FM_REASON = (
    "The medium.targets endpoint pair states 1,591/9,880 vs adjudicated "
    "9,822/1,649 for the 2028 endpoint."
)


def _current_plan_id(session):
    return session.query(PlanVersion).filter_by(
        user_id="ariel", role="current"
    ).one().id


def _add_run(session, *, fund_manager_decision="rejected", user_id="ariel"):
    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier=None,
        decision_kind="plan_revision", status="completed",
        fund_manager_decision=fund_manager_decision,
    )
    session.add(run)
    session.commit()
    return run


def _add_corrective_draft(session, run, *, base_plan_id=None, corrections=None,
                          unresolved=None, role="superseded",
                          imported_at=None):
    if base_plan_id is None:
        base_plan_id = _current_plan_id(session)
    si = {
        "corrective": {
            "base_plan_id": base_plan_id,
            "corrections": corrections or [],
            "directives": [],
        },
        "corrective_unresolved": unresolved or [],
    }
    pv = PlanVersion(
        user_id="ariel", role=role, version_label="rejected-corrective",
        raw_markdown="", decision_run_id=run.id,
        synthesis_inputs_json=json.dumps(si),
    )
    if imported_at is not None:
        pv.imported_at = imported_at
    session.add(pv)
    session.commit()
    return pv


def _add_verdict_report(session, run, *, role, payload):
    row = AgentReport(
        user_id="ariel", agent_role=role,
        decision_id=f"plan-synth-{run.id}",
        response_text=json.dumps(payload),
    )
    session.add(row)
    session.commit()
    return row


def _fm_rejection(reasons):
    return {"approved": False, "reasons": reasons, "cited_sources": []}


def _reader_block(findings):
    return {"overall_assessment": "BLOCK", "findings": findings}


def test_verdict_feedback_harvest_fm_rejected(session):
    """FM rejection reasons become source='verdict_feedback' corrections with
    the explicit wrong/canonical figure pair extracted and the cited ref
    parsed out — the run-144 shape."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    draft = _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert len(ctx.corrections) == 1
    c = ctx.corrections[0]
    assert c.source == "verdict_feedback"
    assert c.verdict_agent == "fund_manager"
    assert c.severity == "RED"
    assert c.plan_item_ref == "medium.targets"
    assert c.wrong_values == [1591, 9880]
    assert [v for _, v in c.canonical_facts] == [9822, 1649]
    assert c.source_run_id == run.id and c.source_draft_id == draft.id
    # Flows into every existing corrections channel.
    assert c.check_payload()["canonical_values"] == [9822, 1649]
    assert c.check_payload()["wrong_values"] == [1591, 9880]
    payload = c.to_payload()
    assert payload["source"] == "verdict_feedback"
    assert payload["verdict_agent"] == "fund_manager"
    ctx_payload = ctx.to_payload()
    assert ctx_payload["verdict_feedback"]["fm_rejected"] is True
    assert ctx_payload["verdict_feedback"]["draft_id"] == draft.id
    # Rendered in its own subsection + reader directive.
    assert "VERDICT FEEDBACK" in ctx.rendered
    assert f"draft #{draft.id}" in ctx.rendered
    assert "FM-rejected" in ctx.rendered
    assert "wrong (must be absent): 1,591; 9,880" in ctx.rendered
    assert "canonical (must appear verbatim): 9,822; 1,649" in ctx.rendered
    assert "1,591" in ctx.reader_directive


def test_verdict_feedback_harvest_reader_blocked(session):
    """Reader BLOCK findings: BLOCKER→RED, AMBER→YELLOW, YELLOW skipped;
    surfaces_cited become evidence."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="whole_artifact_reader",
        payload=_reader_block([
            {"kind": "contradiction", "severity": "BLOCKER",
             "detail": "short.posture cash figure states 161,000 vs "
                       "adjudicated 98,000.",
             "surfaces_cited": ["deploy the $161,000", "cash on hand 98,000"]},
            {"kind": "cross_surface", "severity": "AMBER",
             "detail": "the appendix restates a stale pace claim",
             "surfaces_cited": []},
            {"kind": "other", "severity": "YELLOW",
             "detail": "minor polish nit", "surfaces_cited": []},
        ]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert [c.severity for c in ctx.corrections] == ["RED", "YELLOW"]
    assert all(c.verdict_agent == "whole_artifact_reader"
               for c in ctx.corrections)
    blocker = ctx.corrections[0]
    assert blocker.wrong_values == [161000]
    assert [v for _, v in blocker.canonical_facts] == [98000]
    assert blocker.evidence == ["deploy the $161,000", "cash on hand 98,000"]
    assert "minor polish nit" not in ctx.rendered
    assert ctx.verdict_reader_blocked is True
    assert ctx.verdict_fm_rejected is False
    assert "reader-blocked" in ctx.rendered


def test_verdict_feedback_both_sources_and_mode_header(session):
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    _add_verdict_report(
        session, run, role="whole_artifact_reader",
        payload=_reader_block([
            {"kind": "fragile_claim", "severity": "BLOCKER",
             "detail": "the FI headline is undercut by the thin margin note",
             "surfaces_cited": []},
        ]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert ctx.verdict_fm_rejected and ctx.verdict_reader_blocked
    assert "FM-rejected / reader-blocked" in ctx.rendered
    agents = {c.verdict_agent for c in ctx.corrections}
    assert agents == {"fund_manager", "whole_artifact_reader"}


def test_verdict_feedback_figures_only_when_explicit(session):
    """A verdict finding without an explicit wrong-vs-canonical figure pair
    carries NO values — the classifier will honestly route it FULL."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([
            "The narrative conflates the 2026 pace with the 2027 waypoint "
            "and the glide rationale is internally inconsistent.",
        ]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    c = ctx.corrections[0]
    assert c.wrong_values == [] and c.canonical_facts == []
    assert "resolve the finding in substance" in ctx.rendered


def test_verdict_feedback_lineage_guard(session):
    """A rejected draft based on a DIFFERENT plan lineage must not feed."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(
        session, run, base_plan_id=_current_plan_id(session) + 999,
    )
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    assert build_corrective_context(session, user_id="ariel") is None


def test_verdict_feedback_staleness_guard(session):
    """A critique row NEWER than the rejected draft supersedes its verdicts
    — stale verdicts must not resurrect."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(
        session, run,
        imported_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    # Newer critique (created now, after the draft).
    _add_critique(
        session, [_finding("glide", "glide.schedule", "stale glide")],
        finding_status=["escalated"],
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert all(c.source != "verdict_feedback" for c in ctx.corrections)
    assert "VERDICT FEEDBACK" not in ctx.rendered


def test_verdict_feedback_not_harvested_when_run_approved(session):
    """The MOST RECENT corrective draft decides: if its run was approved,
    older rejected verdicts are superseded history."""
    from argosy.services.corrective_context import build_corrective_context

    old_run = _add_run(session)
    _add_corrective_draft(session, old_run)
    _add_verdict_report(
        session, old_run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    new_run = _add_run(session, fund_manager_decision="approved")
    _add_corrective_draft(session, new_run)
    assert build_corrective_context(session, user_id="ariel") is None


def test_verdict_feedback_dedup_vs_critique_corrections(session):
    """A verdict finding on the SAME subject as an open critique correction
    is deduped via findings_match — never double-fed."""
    from argosy.services.corrective_context import build_corrective_context

    critique = _add_critique(
        session, [_finding("glide", "glide.schedule", "stale glide")],
        finding_status=["escalated"],
    )
    critique.created_at = datetime.now(timezone.utc) - timedelta(hours=6)
    session.commit()
    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection(["glide", RUN_144_FM_REASON]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    topics = [c.topic for c in ctx.corrections]
    assert topics.count("glide") == 1  # critique correction only
    sources = [c.source for c in ctx.corrections]
    assert sources == ["critique", "verdict_feedback"]
    # Indices continue after the critique corrections.
    assert [c.index for c in ctx.corrections] == [1, 2]


def test_verdict_feedback_confirmed_resolved_do_not_reopen(session):
    """Prior-run corrections the verdicts did NOT re-flag (and the floor did
    not report unresolved) render as the do-not-reopen list."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(
        session, run,
        corrections=[
            {"index": 1, "topic": "fx-rate",
             "plan_item_ref": "assumptions.fx"},
            {"index": 2, "topic": "endpoint pair",
             "plan_item_ref": "medium.targets"},
            {"index": 3, "topic": "cash-drag",
             "plan_item_ref": "short.posture"},
        ],
        unresolved=[{"index": 3, "topic": "cash-drag", "reason": "missing"}],
    )
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),  # re-flags medium.targets
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    resolved_topics = [i["topic"] for i in ctx.verdict_confirmed_resolved]
    # fx-rate: untouched by the verdicts and floor-clean → confirmed.
    # endpoint pair: re-flagged → NOT confirmed. cash-drag: unresolved → NOT.
    assert resolved_topics == ["fx-rate"]
    assert "do NOT re-open" in ctx.rendered
    assert "fx-rate" in ctx.rendered
    payload = ctx.to_payload()
    assert payload["verdict_feedback"]["confirmed_resolved"] == [
        {"topic": "fx-rate", "plan_item_ref": "assumptions.fx"},
    ]


def test_verdict_feedback_alone_makes_run_corrective(session):
    """No open critique findings, no directives — verdict feedback alone
    still produces a corrective context (else the next pass forgets why the
    prior draft was rejected)."""
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None
    assert len(ctx.corrections) == 1
    assert ctx.corrections[0].source == "verdict_feedback"


# ---------------------------------------------------------------------------
# Figure extraction — explicit-only, deterministic
# ---------------------------------------------------------------------------


def test_extract_verdict_figures_explicit_pair():
    from argosy.services.corrective_context import extract_verdict_figures

    wrong, canonical = extract_verdict_figures(RUN_144_FM_REASON)
    assert wrong == [1591, 9880]
    assert canonical == [9822, 1649]  # 2028 filtered as a calendar year


def test_extract_verdict_figures_should_be_phrasing():
    from argosy.services.corrective_context import extract_verdict_figures

    wrong, canonical = extract_verdict_figures(
        "The FX planning rate states 3.00, should be 2.944."
    )
    assert wrong == [3]
    assert canonical == [2.944]


def test_extract_verdict_figures_requires_separator():
    from argosy.services.corrective_context import extract_verdict_figures

    assert extract_verdict_figures(
        "The glide schedule 4,136 / 5,094 / 592 is stale."
    ) == ([], [])
    assert extract_verdict_figures("") == ([], [])


def test_extract_verdict_figures_filters_years_and_overlap():
    from argosy.services.corrective_context import extract_verdict_figures

    wrong, canonical = extract_verdict_figures(
        "The 2026/2027 glide legs state 4,136 and 12 vs adjudicated 5,094 "
        "and 12 for 2027."
    )
    assert wrong == [4136]        # 12 appears on both sides → context
    assert canonical == [5094, 12]


# ---------------------------------------------------------------------------
# End-to-end: the run-144 case classifies PATCH
# ---------------------------------------------------------------------------


def _run_144_prior_output():
    """Minimal prior artifact where the wrong endpoint pair lives ONLY in
    the medium slice — the exact run-144 shape."""
    from datetime import date

    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
        SynthTarget,
    )

    return PlanSynthesisOutput(
        long=HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change",
            posture="Stay the course.", rationale="Diversified core holds.",
        ),
        medium=HorizonSection(
            horizon="medium", freshness_expected="quarterly",
            status="minor_revision",
            posture="Continue the NVDA glide.",
            rationale=(
                "The glide endpoint pair is 1,591/9,880 shares at the "
                "2028 endpoint."
            ),
            targets=[SynthTarget(
                label="NVDA endpoint shares", value=1591.0, unit="shares",
                stated_at=date(2026, 7, 1), revisit_after=date(2026, 10, 1),
                rationale="endpoint anchor",
            )],
        ),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly", status="no_change",
            posture="No near-term change.", rationale="Cash is deployed.",
        ),
        inputs=SynthesisInputs(),
        sections=[],
    )


def test_run_144_verdict_feedback_classifies_patch(session):
    """THE gap this feature closes: run 144's one-finding FM rejection
    (explicit wrong pair 1,591/9,880, explicit canonical 9,822/1,649, ref
    medium.targets) must classify PATCH with a single implicated slice —
    not pay another full 70k-char phase-3 rewrite."""
    from argosy.quality.patch_reachability import classify_patch_reachability
    from argosy.services.corrective_context import build_corrective_context

    run = _add_run(session)
    _add_corrective_draft(session, run)
    _add_verdict_report(
        session, run, role="fund_manager",
        payload=_fm_rejection([RUN_144_FM_REASON]),
    )
    ctx = build_corrective_context(session, user_id="ariel")
    assert ctx is not None

    reach = classify_patch_reachability(
        corrections=[c.to_payload() for c in ctx.corrections],
        directives=[d.to_payload() for d in ctx.directives],
        prior=_run_144_prior_output(),
        forces_full_tier=ctx.forces_full_tier,
    )
    assert reach.verdict == "PATCH"
    assert reach.implicated_groups == ("medium",)
    decision = reach.decisions[0]
    assert decision.scope == "PATCH"
    assert decision.implicated_groups == ("medium",)


def test_migration_admits_executed_status(session):
    """Migration 0078: the CHECK enum admits status='executed' at head."""
    row = _add_proposal(
        session, kind="replan_full", dedup_key="x:1", status="accepted",
    )
    row.status = "executed"
    session.commit()  # raises IntegrityError if the CHECK still rejects it
    session.refresh(row)
    assert row.status == "executed"
