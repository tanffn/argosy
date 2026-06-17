from argosy.services.plan_export import render_coherence_deliberation_appendix
from argosy.services.assembled_artifact import _strip_internal_metadata_sections


def test_appendix_renders_one_row_per_ruling():
    rows = [{"subject_type": "retirement_age_headline", "question": "which age leads?",
             "resolved_by": "arbitrator", "ruling": "age 46 leads; 54 strict track",
             "conformed_surfaces": ["long_md", "medium_md"]}]
    md = render_coherence_deliberation_appendix(rows)
    assert "## Appendix — Coherence deliberations" in md
    assert "retirement_age_headline" in md
    assert "arbitrator" in md


def test_appendix_is_stripped_from_reader_artifact():
    art = "## Current Plan\nbody\n\n## Appendix — Coherence deliberations\nrow\n"
    stripped = _strip_internal_metadata_sections(art)
    assert "Coherence deliberations" not in stripped
    assert "## Current Plan" in stripped
