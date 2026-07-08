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


def test_migration_admits_executed_status(session):
    """Migration 0078: the CHECK enum admits status='executed' at head."""
    row = _add_proposal(
        session, kind="replan_full", dedup_key="x:1", status="accepted",
    )
    row.status = "executed"
    session.commit()  # raises IntegrityError if the CHECK still rejects it
    session.refresh(row)
    assert row.status == "executed"
