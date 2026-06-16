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
