"""The TargetAllocationDoc must be built BEFORE horizon markdown render so
allocation sites can render from the canonical doc (codex v2 #6). We assert the
call ORDER via spies on the real call targets, driving _assemble_draft_bodies
with a minimal stub output (no DB writes; the headline-scrub try-block degrades
to a no-op when its resolver call fails on the stub session)."""
from __future__ import annotations

from types import SimpleNamespace

import argosy.orchestrator.flows.plan_synthesis as flow
import argosy.services.target_allocation_doc as tad
import argosy.orchestrator.flows.plan_synthesis.orchestrator as orch


def test_target_allocation_doc_built_before_horizon_render(monkeypatch):
    calls: list[str] = []

    def spy_render(section):
        calls.append("_horizon_md_user")
        return ""

    def spy_resolve(*a, **k):
        calls.append("resolve_target_allocation_json")
        return "{}"

    monkeypatch.setattr(flow, "_horizon_md_user", spy_render)
    monkeypatch.setattr(flow, "render_plan_appendices", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_horizon_md_audit", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_strip_history_leak", lambda x: x)
    monkeypatch.setattr(flow, "_strip_jargon", lambda x: x)
    # resolve_target_allocation_json is imported locally inside the function from
    # this module, so patching the source-module attribute intercepts it.
    monkeypatch.setattr(tad, "resolve_target_allocation_json", spy_resolve)

    output = SimpleNamespace(long=object(), medium=object(), short=object(), sections=[])

    orch._assemble_draft_bodies(
        session=object(), output=output, user_id="u",
        decision_run_id="plan-synth-1", alternatives_sleeve=None,
    )

    assert "resolve_target_allocation_json" in calls and "_horizon_md_user" in calls
    assert calls.index("resolve_target_allocation_json") < calls.index("_horizon_md_user"), (
        f"the TargetAllocationDoc must be resolved before horizon render; got {calls}"
    )


def test_fact_placeholders_persist_as_tokens_when_protocol_on(monkeypatch):
    """Item I: with placeholders ON, assemble PERSISTS tokens (READ-time fills
    them). Legacy bake-digits path is flag-OFF only."""
    import argosy.services.plan_numeric_resolver as rmod
    import argosy.quality.numeric_source_gate as nsg
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue

    monkeypatch.setenv("ARGOSY_FACT_PLACEHOLDERS", "1")

    man = ResolvedPlanNumbers(values={
        "fx.usd_nis": ResolvedValue(
            key="fx.usd_nis", value=3.0, unit="nis_per_usd", status="resolved",
            source_locator="test",
        ),
        "retirement.earliest_safe_age": ResolvedValue(
            key="retirement.earliest_safe_age", value=46.0, unit="age",
            status="resolved", source_locator="test",
        ),
    })
    seen_kwargs: dict = {}

    def fake_resolve(session, *, user_id, decision_run_id, include_canonical_ages=False):
        seen_kwargs["include_canonical_ages"] = include_canonical_ages
        return man

    monkeypatch.setattr(rmod, "resolve_plan_numbers", fake_resolve)
    monkeypatch.setattr(nsg, "scrub_headline_numeric_source", lambda td, m: (td, []))
    monkeypatch.setattr(
        flow, "_horizon_md_user",
        lambda s: "Rate is {{fact:fx.usd_nis}}; earliest-safe {{fact:retirement.earliest_safe_age}}.",
    )
    monkeypatch.setattr(flow, "render_plan_appendices", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_horizon_md_audit", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_strip_history_leak", lambda x: x)
    monkeypatch.setattr(flow, "_strip_jargon", lambda x: x)

    _section = SimpleNamespace(
        model_dump=lambda mode="json": {"title": "T", "body_md": "NVDA at {{fact:fx.usd_nis}}."}
    )
    output = SimpleNamespace(long=object(), medium=object(), short=object(), sections=[_section])
    bodies = orch._assemble_draft_bodies(
        session=object(), output=output, user_id="u",
        decision_run_id="plan-synth-1", alternatives_sleeve=None,
    )

    long_md = bodies["horizon_long_md"]
    assert "{{fact:fx.usd_nis}}" in long_md
    assert "{{fact:retirement.earliest_safe_age}}" in long_md
    assert "{{fact:fx.usd_nis}}" in bodies["sections_json"]
    assert seen_kwargs.get("include_canonical_ages") is True


def test_fact_placeholders_bake_digits_when_protocol_off(monkeypatch):
    """Legacy path: flag OFF still bakes digits into the persisted body."""
    import argosy.services.plan_numeric_resolver as rmod
    import argosy.quality.numeric_source_gate as nsg
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue

    monkeypatch.setenv("ARGOSY_FACT_PLACEHOLDERS", "0")

    man = ResolvedPlanNumbers(values={
        "fx.usd_nis": ResolvedValue(
            key="fx.usd_nis", value=3.0, unit="nis_per_usd", status="resolved",
            source_locator="test",
        ),
        "retirement.earliest_safe_age": ResolvedValue(
            key="retirement.earliest_safe_age", value=46.0, unit="age",
            status="resolved", source_locator="test",
        ),
    })

    monkeypatch.setattr(
        rmod, "resolve_plan_numbers",
        lambda *a, **k: man,
    )
    monkeypatch.setattr(nsg, "scrub_headline_numeric_source", lambda td, m: (td, []))
    monkeypatch.setattr(
        flow, "_horizon_md_user",
        lambda s: "Rate is {{fact:fx.usd_nis}}; earliest-safe {{fact:retirement.earliest_safe_age}}.",
    )
    monkeypatch.setattr(flow, "render_plan_appendices", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_horizon_md_audit", lambda *a, **k: "")
    monkeypatch.setattr(flow, "_strip_history_leak", lambda x: x)
    monkeypatch.setattr(flow, "_strip_jargon", lambda x: x)

    _section = SimpleNamespace(
        model_dump=lambda mode="json": {"title": "T", "body_md": "NVDA at {{fact:fx.usd_nis}}."}
    )
    output = SimpleNamespace(long=object(), medium=object(), short=object(), sections=[_section])
    bodies = orch._assemble_draft_bodies(
        session=object(), output=output, user_id="u",
        decision_run_id="plan-synth-1", alternatives_sleeve=None,
    )

    long_md = bodies["horizon_long_md"]
    assert "{{fact:" not in long_md
    assert "3.000" in long_md
    assert "age 46" in long_md
    assert "{{fact:" not in bodies["sections_json"]
    assert "3.000" in bodies["sections_json"]
