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
