from datetime import date

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.quality.fact_correction import (
    CorrectionPatch,
    apply_text_corrections,
    rerender_deterministic_sites,
    reverify_corrected,
    route_finding,
)
from argosy.quality.gate_types import GateCheck


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0,
        site_kind=SiteKind.STRUCTURED_FIELD, hash="h1",
    ))
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(0, 0),
        rendered_text="NVDA cap 13%", normalized_value=13.0,
        site_kind=SiteKind.TEMPLATE, hash="h2",
    ))
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#prose", byte_span=(0, 0),
        rendered_text="we keep NVDA near the cap", normalized_value=13.0,
        site_kind=SiteKind.LLM_PROSE, hash="h3",
    ))
    return led


def test_rerender_updates_template_and_structured_sites_only():
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    kinds = {p.site.site_kind for p in patches}
    assert SiteKind.LLM_PROSE not in kinds
    assert len(patches) == 2
    json_patch = next(p for p in patches if p.site.surface_id == "target_allocation_json")
    assert "18" in json_patch.new_text
    body_patch = next(p for p in patches if p.site.field_path == "long#cap")
    assert "18" in body_patch.new_text and "13" not in body_patch.new_text


def test_rerender_unknown_fact_returns_empty():
    assert rerender_deterministic_sites("nope", 1.0, _ledger()) == []


def test_apply_corrections_replaces_deterministic_and_prose_text():
    artifact = "NVDA cap 13% in the body. We keep NVDA near the cap of 13%."
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    prose_edits = [("We keep NVDA near the cap of 13%.",
                    "We keep NVDA near the cap of 18%.")]
    out = apply_text_corrections(artifact, patches, prose_edits)
    assert "NVDA cap 18%" in out
    assert "near the cap of 18%" in out
    assert "13%" not in out


def test_apply_corrections_no_ops_when_text_absent():
    artifact = "unrelated text"
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    assert apply_text_corrections(artifact, patches, []) == "unrelated text"


def test_attributable_finding_routes_surgical():
    finding = {"kind": "cross_surface", "severity": "AMBER",
               "surfaces_cited": ["NVDA cap 13% vs 18"]}
    assert route_finding(finding, _ledger()) == "surgical"


def test_unattributable_finding_routes_structural():
    finding = {"kind": "other", "severity": "YELLOW",
               "surfaces_cited": ["coverage sections not baselined"]}
    assert route_finding(finding, _ledger()) == "structural"


def test_reverify_runs_global_suite_and_catches_planted_contradiction():
    corrected = (
        "## IPS Instrument Map\nNVDA 13%\nGlobal equity 60%\nGold 18%\nBonds 20%\n"
    )  # sums to 111
    verdict = reverify_corrected(corrected, gate_kwargs={"today": date(2026, 6, 16)})
    assert verdict.violations[GateCheck.IPS_EQUALITY]


def test_reverify_clean_artifact_passes_relevant_checks():
    corrected = "All surfaces agree. Nothing contradictory."
    verdict = reverify_corrected(corrected, gate_kwargs={"today": date(2026, 6, 16)})
    assert not verdict.violations[GateCheck.IPS_EQUALITY]
