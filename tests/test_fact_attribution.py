import json
from pathlib import Path

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.quality.fact_attribution import FindingLocation, attribute_finding

FIXTURE = Path(__file__).parent / "fixtures" / "run106_reader_verdict.json"


def test_run106_fixture_loads_with_eleven_findings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["overall_assessment"] == "BLOCK"
    assert len(data["findings"]) == 11


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0,
        site_kind=SiteKind.STRUCTURED_FIELD, hash="h",
    ))
    return led


def test_finding_attributes_to_fact_via_ledger_text_match():
    finding = {"kind": "cross_surface", "severity": "AMBER",
               "detail": "cap mismatch", "surfaces_cited": ["NVDA cap 13.0 vs 18"]}
    locs = attribute_finding(finding, _ledger())
    assert any(loc.fact_id == "allocation.nvda_cap_pct" for loc in locs)


def test_value_substring_does_not_falsely_attribute():
    # '13.0' must not attribute via a bare substring inside a longer number like
    # '1130' or '13.05' (code-review 2026-06-16 — number-boundary-safe value match)
    finding = {"kind": "cross_surface", "severity": "AMBER",
               "surfaces_cited": ["bps moved to 1130 and 13.05 handle"]}
    locs = attribute_finding(finding, _ledger())
    # no standalone 13.0 in the excerpt -> fail-safe structural, not a false hit
    assert len(locs) == 1 and locs[0].fact_id is None and locs[0].scope == "structural"


def test_unattributable_finding_is_failsafe_structural():
    finding = {"kind": "other", "severity": "YELLOW",
               "detail": "coverage status", "surfaces_cited": ["sections not baselined"]}
    locs = attribute_finding(finding, _ledger())
    assert len(locs) == 1
    assert locs[0].fact_id is None
    assert locs[0].scope == "structural"


def test_real_run106_findings_all_route_somewhere():
    """Every run-106 finding either attributes to a fact or fail-safe routes to
    structural — none is silently dropped."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    led = _ledger()
    for finding in data["findings"]:
        locs = attribute_finding(finding, led)
        assert locs, "a finding must always yield at least one FindingLocation"
        assert all(isinstance(loc, FindingLocation) for loc in locs)
