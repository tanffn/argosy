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


def test_fact_placeholders_are_rendered_into_the_persisted_body(monkeypatch):
    """Regression for the `_os not defined` bug (commit ca8251f): the placeholder
    render shares a try-block with the headline scrub; a NameError in the
    ARGOSY_FACT_PLACEHOLDERS branch was swallowed and the render SKIPPED, so 77
    raw `{{fact:}}` tokens leaked into the live pv53 body. Assert the branch
    runs: tokens are filled, no raw `{{fact:}}` survives, and the render manifest
    is built WITH include_canonical_ages so canonical-age facts resolve too."""
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

    output = SimpleNamespace(long=object(), medium=object(), short=object(), sections=[])
    bodies = orch._assemble_draft_bodies(
        session=object(), output=output, user_id="u",
        decision_run_id="plan-synth-1", alternatives_sleeve=None,
    )

    long_md = bodies["horizon_long_md"]
    assert "{{fact:" not in long_md, f"raw placeholder leaked into body: {long_md!r}"
    assert "3.000" in long_md  # fx rendered
    assert "age 46" in long_md  # canonical-age fact rendered
    assert seen_kwargs.get("include_canonical_ages") is True, (
        "the render manifest must be built with include_canonical_ages=True so "
        "canonical-age placeholders (earliest_safe_age) resolve instead of leaking"
    )
