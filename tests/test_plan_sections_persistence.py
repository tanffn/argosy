"""Migration 0065 — persist + reconstruct the structured synthesis sections.

The synthesizer builds ``PlanSynthesisOutput.sections`` at runtime, but they
were never persisted, so the plan-output gate reconstructed a sectionless
object at promote-time and section_coverage / evidence_per_section failed for
EVERY plan. These tests pin:

1. ``_run_plan_output_gate`` reconstructs sections from ``sections_json`` so
   section_coverage + evidence_per_section evaluate the REAL sections.
2. A legacy row (no ``sections_json``) demotes those evidence checks to WARN
   at /accept (never blocking), while history/jargon/numeric still block.
"""
from __future__ import annotations

import json

from argosy.agents.plan_synthesizer_types import Section, SectionEvidence
from argosy.quality.canonical_sections import (
    CANONICAL_SECTION_IDS,
    MVP_COVERAGE_THRESHOLD,
)
from argosy.quality.gate_types import GateCheck
from argosy.state.models import PlanVersion, User


def _horizon_json(horizon: str, freshness: str) -> str:
    """Minimal valid HorizonSection JSON so the gate reconstructs `synth`."""
    return json.dumps(
        {
            "horizon": horizon,
            "freshness_expected": freshness,
            "status": "no_change",
            "posture": "Steady growth across diversified holdings.",
        }
    )


def _canonical_sections(n: int) -> list[Section]:
    """``n`` valid sections with distinct canonical ids. Each declares
    ``missing_data`` (no facts) — the minimal shape that satisfies the
    per-section evidence contract (facts OR missing_data)."""
    ids = sorted(CANONICAL_SECTION_IDS.keys())[:n]
    return [
        Section(
            section_id=sid,
            horizon="long",
            title=f"Section {sid}",
            body_md=f"Body for {sid}.",
            evidence=SectionEvidence(
                facts=[],
                source_span=[],
                assumptions=[],
                missing_data=[f"pending intake for {sid}"],
            ),
        )
        for sid in ids
    ]


def _insert_draft(session_factory, *, sections_json: str | None) -> int:
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="sections-persist-test",
            raw_markdown="",
            horizon_long_md="# Long\n\n**Posture.** Steady.\n",
            horizon_medium_md="# Medium\n\n**Posture.** Steady.\n",
            horizon_short_md="# Short\n\n**Posture.** Steady.\n",
            horizon_long_json=_horizon_json("long", "annual"),
            horizon_medium_json=_horizon_json("medium", "quarterly"),
            horizon_short_json=_horizon_json("short", "monthly"),
            sections_json=sections_json,
        )
        sess.add(draft)
        sess.commit()
        return draft.id
    finally:
        sess.close()


def test_gate_reconstructs_sections_from_sections_json(client_with_db):
    """A draft carrying sections_json → the gate sees the real sections →
    section_coverage + evidence_per_section pass (no violations)."""
    from argosy.api.routes.plan import _run_plan_output_gate

    sections = _canonical_sections(MVP_COVERAGE_THRESHOLD)
    sj = json.dumps([s.model_dump(mode="json") for s in sections])
    draft_id = _insert_draft(client_with_db.app.state.session_factory, sections_json=sj)

    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        verdict = _run_plan_output_gate(pv, db=None)
    finally:
        sess.close()

    assert verdict is not None
    assert not verdict.for_check(GateCheck.SECTION_COVERAGE), verdict.summary()
    assert not verdict.for_check(GateCheck.EVIDENCE_PER_SECTION), verdict.summary()


def test_legacy_row_without_sections_json_fails_those_checks(client_with_db):
    """No sections_json → the gate verdict still reports the section checks
    as failing (the route is what demotes them to WARN)."""
    from argosy.api.routes.plan import _run_plan_output_gate

    draft_id = _insert_draft(client_with_db.app.state.session_factory, sections_json=None)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        verdict = _run_plan_output_gate(pv, db=None)
    finally:
        sess.close()

    assert verdict.for_check(GateCheck.SECTION_COVERAGE)
    assert verdict.for_check(GateCheck.EVIDENCE_PER_SECTION)


def test_accept_legacy_demotes_section_checks_to_warn(client_with_db, monkeypatch):
    """Enforce mode + a legacy draft with history/jargon leaks: blocking set
    excludes the section-dependent checks (warned-only), so the 422 lists
    only history/jargon/numeric — never section_coverage/evidence."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "true")
    from argosy.config import reload_settings

    reload_settings()
    # History + jargon leak in the body; no sections_json (legacy).
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="legacy-demote-test",
            raw_markdown="",
            horizon_long_md="## Deltas vs. prior current\n\nThe ConcentrationAnalyst flagged drift.\n",
            horizon_medium_md="# Medium\n\n**Posture.** Steady.\n",
            horizon_short_md="# Short\n\n**Posture.** Steady.\n",
            horizon_long_json=_horizon_json("long", "annual"),
            horizon_medium_json=_horizon_json("medium", "quarterly"),
            horizon_short_json=_horizon_json("short", "monthly"),
            sections_json=None,
        )
        sess.add(draft)
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()

    try:
        r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
        assert r.status_code == 422, r.text
        by_check = r.json()["detail"]["violations_by_check"]
        assert GateCheck.SECTION_COVERAGE.value not in by_check
        assert GateCheck.EVIDENCE_PER_SECTION.value not in by_check
        assert GateCheck.HISTORY_LEAK.value in by_check
        assert GateCheck.JARGON_LEAK.value in by_check
    finally:
        reload_settings()
