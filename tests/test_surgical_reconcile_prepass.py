"""The surgical pre-pass corrects a renderable finding deterministically and
reports it, without a full re-synthesis. Helper-level test (no live LLM)."""
from __future__ import annotations

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.orchestrator.flows.plan_synthesis import _surgical_reconcile_prepass


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(0, 0),
        rendered_text="NVDA cap 13%", normalized_value=13.0,
        site_kind=SiteKind.TEMPLATE, hash="h",
    ))
    return led


def test_prepass_corrects_renderable_finding_and_reports_resolved():
    artifact = "NVDA cap 13% in the body and NVDA cap 18% on the dashboard."
    finding = {"kind": "cross_surface", "severity": "BLOCKER",
               "surfaces_cited": ["NVDA cap 13%", "NVDA cap 18%"]}
    result = _surgical_reconcile_prepass(
        artifact_text=artifact,
        findings=[finding],
        ledger=_ledger(),
        canonical_values={"allocation.nvda_cap_pct": 18.0},
        gate_kwargs={},
    )
    assert "NVDA cap 18% in the body" in result.corrected_text
    assert "allocation.nvda_cap_pct" in result.corrected_fact_ids
    assert result.structural_findings == []


def test_prepass_reports_structural_finding_for_fallback():
    artifact = "coverage sections not baselined"
    finding = {"kind": "other", "severity": "YELLOW",
               "surfaces_cited": ["coverage sections not baselined"]}
    result = _surgical_reconcile_prepass(
        artifact_text=artifact, findings=[finding], ledger=_ledger(),
        canonical_values={}, gate_kwargs={},
    )
    assert result.structural_findings == [finding]
    assert result.corrected_fact_ids == []
