# tests/coherence/test_surface_registry.py
from argosy.quality.coherence.surface_registry import (
    SurfaceSite, sites_for_subject, SUBJECT_REGISTRY,
)


def test_rsu_vest_subject_has_md_and_json_surfaces():
    sites = sites_for_subject("rsu_vest_policy")
    assert sites, "rsu_vest_policy must be registered"
    methods = {s.conform_method for s in sites}
    assert "markdown" in methods
    assert "json_field" in methods


def test_every_site_names_its_surface_and_path():
    for subject, sites in SUBJECT_REGISTRY.items():
        for s in sites:
            assert isinstance(s, SurfaceSite)
            assert s.surface_id and s.conform_method in {"markdown", "json_field", "derived"}
