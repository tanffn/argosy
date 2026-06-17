# tests/coherence/test_value_resolution.py
from argosy.quality.coherence.dispute import Dispute
from argosy.quality.coherence.resolver_route import build_value_resolution


def test_builds_equals_canonical_patches_for_markdown_sites():
    d = Dispute(subject_type="nvda_cap", subject_field_path="concentration.nvda_cap_pct",
                scope="person", conflict_type="value_mismatch", question="q",
                surfaces_cited=("long_md", "short_md"))
    sites = [("long_md", "markdown"), ("short_md", "markdown")]
    res = build_value_resolution(d, canonical_text="13.0", sites=sites,
                                 stale_text="12.0")
    assert all(p["conform_method"] == "markdown" for p in res["patches"])
    assert {p["surface_id"] for p in res["patches"]} == {"long_md", "short_md"}
    assert any(i["kind"] == "equals_canonical" for i in res["invariant"])
