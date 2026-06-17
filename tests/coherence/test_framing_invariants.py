# tests/coherence/test_framing_invariants.py
from argosy.quality.coherence.claim_markers import render_marker
from argosy.quality.coherence.invariants import (
    RequiredFramingRole, ForbiddenClaim, verify_invariants,
)


def _artifact(**markers):
    return {sid: render_marker(subj, claims) for sid, (subj, claims) in markers.items()}


def test_required_framing_role_passes_when_marker_present():
    art = _artifact(long_md=("retirement_age_headline",
                             {"lead_age": "46", "capital_preservation_role": "target_sizing_basis"}))
    inv = RequiredFramingRole(
        subject_type="retirement_age_headline", surface="long_md",
        role_field="lead_age", value="46",
    )
    assert verify_invariants([inv], art).ok


def test_required_framing_role_fails_on_wrong_value():
    art = _artifact(long_md=("retirement_age_headline", {"lead_age": "54"}))
    inv = RequiredFramingRole(
        subject_type="retirement_age_headline", surface="long_md",
        role_field="lead_age", value="46",
    )
    res = verify_invariants([inv], art)
    assert not res.ok and any("lead_age" in f for f in res.failures)


def test_forbidden_claim_fails_when_pattern_present_in_prose():
    art = {"short_md": "we will retain net vested as NVDA until the cap-band fires"}
    inv = ForbiddenClaim(subject_type="rsu_vest_policy", surface="short_md",
                         pattern="retain net vested as NVDA")
    res = verify_invariants([inv], art)
    assert not res.ok
