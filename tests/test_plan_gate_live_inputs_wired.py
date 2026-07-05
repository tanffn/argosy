"""#11 — the live caller (`_run_plan_output_gate`) must PASS the
whole-artifact + freshness inputs into `gate_plan_output`, so the
dormant cross-surface-coherence / FI-shock / input-freshness checks
actually RUN in production.

Before this wiring the caller passed only horizon_text/synth/resolved, so
`artifact`, `today`, `snapshot_date`, and `analyst_report_dates` were never
supplied and those three checks were silently skipped. This pins that the
four kwargs reach the gate (with `today` non-None and the keyword present
for the rest), captured via a monkeypatched gate so the assertion is exact
and independent of the live check outcomes.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from argosy.agents.plan_synthesizer_types import Section, SectionEvidence
from argosy.quality.canonical_sections import (
    CANONICAL_SECTION_IDS,
    MVP_COVERAGE_THRESHOLD,
)
from argosy.quality.gate_types import GateVerdict
from argosy.state.models import (
    AgentReport,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
)


def _horizon_json(horizon: str, freshness: str) -> str:
    return json.dumps(
        {
            "horizon": horizon,
            "freshness_expected": freshness,
            "status": "no_change",
            "posture": "Steady growth across diversified holdings.",
        }
    )


def _canonical_sections(n: int) -> list[Section]:
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


def _insert_draft(session_factory) -> int:
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sections = _canonical_sections(MVP_COVERAGE_THRESHOLD)
        sj = json.dumps([s.model_dump(mode="json") for s in sections])
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="live-inputs-test",
            raw_markdown="",
            decision_run_id=4242,
            horizon_long_md="# Long\n\n**Posture.** Steady.\n",
            horizon_medium_md="# Medium\n\n**Posture.** Steady.\n",
            horizon_short_md="# Short\n\n**Posture.** Steady.\n",
            horizon_long_json=_horizon_json("long", "annual"),
            horizon_medium_json=_horizon_json("medium", "quarterly"),
            horizon_short_json=_horizon_json("short", "monthly"),
            sections_json=sj,
        )
        sess.add(draft)
        # A snapshot so snapshot_date resolves to a real date, and an
        # agent report so analyst_report_dates assembles a {role: date}.
        sess.add(
            PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=date(2026, 6, 14),
                imported_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )
        sess.add(
            AgentReport(
                user_id="ariel",
                agent_role="macro",
                decision_id="4242",
                created_at=datetime(2026, 6, 14, 9, 0, 0, tzinfo=timezone.utc),
            )
        )
        sess.commit()
        return draft.id
    finally:
        sess.close()


def test_live_caller_passes_artifact_and_freshness_inputs(
    client_with_db, monkeypatch
):
    """`_run_plan_output_gate(pv, db)` must forward artifact + today +
    snapshot_date + analyst_report_dates into `gate_plan_output`."""
    from argosy.api.routes import plan as plan_module
    import argosy.quality as quality_module

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return GateVerdict()

    # The caller does `from argosy.quality import gate_plan_output` INSIDE the
    # function, so patch it on the source module (argosy.quality) — patching
    # the route module would be a no-op against the function-local import.
    monkeypatch.setattr(quality_module, "gate_plan_output", _spy)

    draft_id = _insert_draft(client_with_db.app.state.session_factory)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        plan_module._run_plan_output_gate(pv, db=sess)
    finally:
        sess.close()

    # The four new kwargs must have reached the gate.
    assert "artifact" in captured
    assert "today" in captured
    assert "snapshot_date" in captured
    assert "analyst_report_dates" in captured

    # `today` always derivable; snapshot + reports were seeded → non-None.
    assert captured["today"] == date.today()
    assert captured["snapshot_date"] == date(2026, 6, 14)
    assert captured["analyst_report_dates"] == {"macro": date(2026, 6, 14)}


# ---------------------------------------------------------------------------
# Refinement-path drafts (run_refinement: decision_run_id=None, derived_from_id
# set, horizon markdown copied VERBATIM from the refined plan). The
# headline_numeric_source check must validate them against the nearest
# synthesis ANCESTOR's resolver manifest — not fail closed on the (by-design)
# missing own run id — while a draft with NO synthesis ancestor still fails
# closed in enforce mode.
# ---------------------------------------------------------------------------


class _StubResolved:
    """Minimal ResolvedPlanNumbers stand-in: `.get(key)` → None everywhere."""

    def get(self, key):  # noqa: D102 — trivial stub
        return None


def _insert_refinement_lineage(
    session_factory, *, root_run_id: int | None
) -> int:
    """root (decision_run_id=root_run_id) <- mid (None) <- draft (None).

    Mirrors the live v62 <- v63 <- v64 lineage: the draft's nearest ancestor
    WITH a run id is two hops up. Returns the draft id."""
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        common = dict(
            user_id="ariel",
            raw_markdown="",
            horizon_long_md="# Long\n\n**Posture.** Steady.\n",
            horizon_medium_md="# Medium\n\n**Posture.** Steady.\n",
            horizon_short_md="# Short\n\n**Posture.** Steady.\n",
            horizon_long_json=_horizon_json("long", "annual"),
            horizon_medium_json=_horizon_json("medium", "quarterly"),
            horizon_short_json=_horizon_json("short", "monthly"),
        )
        root = PlanVersion(
            role="superseded", version_label="refine-root",
            decision_run_id=root_run_id, **common,
        )
        sess.add(root)
        sess.commit()
        mid = PlanVersion(
            role="current", version_label="refine-mid",
            decision_run_id=None, derived_from_id=root.id, **common,
        )
        sess.add(mid)
        sess.commit()
        draft = PlanVersion(
            role="draft", version_label="refine-draft",
            decision_run_id=None, derived_from_id=mid.id, **common,
        )
        sess.add(draft)
        sess.commit()
        return draft.id
    finally:
        sess.close()


def test_refinement_draft_validates_against_ancestor_run_manifest(
    client_with_db, monkeypatch
):
    """A refinement draft (decision_run_id=None, derived_from set) must rebuild
    the resolver manifest from the nearest synthesis ANCESTOR's run id (two
    hops up here) and pass it into the gate — no synthetic fail-closed
    HEADLINE_NUMERIC_SOURCE violation."""
    from argosy.api.routes import plan as plan_module
    from argosy.quality.gate_types import GateCheck
    from argosy.services import plan_numeric_resolver as resolver_module
    import argosy.quality as quality_module

    captured: dict = {}
    resolver_calls: list[int] = []
    stub = _StubResolved()

    def _spy_gate(**kwargs):
        captured.update(kwargs)
        return GateVerdict()

    def _spy_resolve(db, *, user_id, decision_run_id, **kwargs):
        resolver_calls.append(decision_run_id)
        return stub

    monkeypatch.setattr(quality_module, "gate_plan_output", _spy_gate)
    monkeypatch.setattr(resolver_module, "resolve_plan_numbers", _spy_resolve)

    draft_id = _insert_refinement_lineage(
        client_with_db.app.state.session_factory, root_run_id=4242
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        verdict = plan_module._run_plan_output_gate(pv, db=sess)
    finally:
        sess.close()

    # The resolver ran against the ANCESTOR's run id, its manifest reached the
    # gate, and no fail-closed violation was manufactured.
    assert resolver_calls == [4242]
    assert captured["resolved"] is stub
    assert verdict is not None
    assert verdict.for_check(GateCheck.HEADLINE_NUMERIC_SOURCE) == []


def test_refinement_draft_without_synthesis_ancestor_fails_closed(
    client_with_db, monkeypatch
):
    """Fail-closed behavior is UNCHANGED when there is genuinely no manifest to
    validate against: a draft whose whole lineage carries no decision_run_id
    still records the synthetic HEADLINE_NUMERIC_SOURCE violation in enforce
    mode."""
    from argosy.api.routes import plan as plan_module
    from argosy.quality.gate_types import GateCheck
    import argosy.quality as quality_module

    monkeypatch.setattr(
        quality_module, "gate_plan_output", lambda **kwargs: GateVerdict()
    )

    draft_id = _insert_refinement_lineage(
        client_with_db.app.state.session_factory, root_run_id=None
    )
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        verdict = plan_module._run_plan_output_gate(pv, db=sess)
    finally:
        sess.close()

    assert verdict is not None
    viols = verdict.for_check(GateCheck.HEADLINE_NUMERIC_SOURCE)
    assert len(viols) == 1
    assert "no synthesis ancestor" in viols[0].detail


def test_nearest_ancestor_walk_is_cycle_safe(client_with_db):
    """A corrupt self-referential lineage must return None, not loop."""
    from argosy.api.routes import plan as plan_module

    session_factory = client_with_db.app.state.session_factory
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        a = PlanVersion(
            user_id="ariel", role="draft", version_label="cycle-a",
            raw_markdown="", decision_run_id=None,
        )
        sess.add(a)
        sess.commit()
        a.derived_from_id = a.id  # self-cycle
        sess.commit()
        assert plan_module._nearest_ancestor_decision_run_id(sess, a) is None
    finally:
        sess.close()
