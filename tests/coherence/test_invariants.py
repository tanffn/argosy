# tests/coherence/test_invariants.py
from argosy.quality.coherence.invariants import (
    EqualsCanonical, AllRegisteredSurfacesPresent, verify_invariants, VerifyResult,
)


def test_equals_canonical_passes_when_all_surfaces_match():
    artifact = {"long_md": "NVDA cap is 13.0%", "short_md": "the 13.0% NVDA ceiling"}
    inv = EqualsCanonical(
        subject_type="nvda_cap", canonical_text="13.0",
        surfaces=("long_md", "short_md"),
    )
    res = verify_invariants([inv], artifact)
    assert res.ok is True
    assert res.failures == []


def test_equals_canonical_fails_when_a_surface_diverges():
    artifact = {"long_md": "NVDA cap is 13.0%", "short_md": "the 12.0% NVDA ceiling"}
    inv = EqualsCanonical(
        subject_type="nvda_cap", canonical_text="13.0",
        surfaces=("long_md", "short_md"),
    )
    res = verify_invariants([inv], artifact)
    assert res.ok is False
    assert any("short_md" in f for f in res.failures)


def test_all_surfaces_present_fails_when_one_missing():
    artifact = {"long_md": "x"}
    inv = AllRegisteredSurfacesPresent(subject_type="vest", surfaces=("long_md", "short_md"))
    res = verify_invariants([inv], artifact)
    assert res.ok is False
