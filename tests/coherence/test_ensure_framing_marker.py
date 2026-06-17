from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import (
    ensure_framing_marker, run_coherence_round,
)
from argosy.quality.coherence.claim_markers import parse_markers


_INV = [
    {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
     "surface": "long_md", "role_field": "lead_age", "value": "46"},
    {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
     "surface": "long_md", "role_field": "capital_preservation_role",
     "value": "target_sizing_basis"},
]


def test_marker_inserted_and_verifies():
    bodies = {"long_md": "Retirement framing prose.", "medium_md": "", "short_md": ""}
    bodies = ensure_framing_marker(bodies, "retirement_age_headline", _INV)
    claims = parse_markers(bodies["long_md"])["retirement_age_headline"]
    assert claims["lead_age"] == "46"
    assert claims["capital_preservation_role"] == "target_sizing_basis"
    # the same invariants now verify green via the round driver
    res = run_coherence_round(
        bodies=bodies, json_surfaces={},
        value_resolutions={"retirement_age_headline": {"patches": [], "invariant": _INV}},
    )
    assert res.ok, res.verifier.failures


def test_marker_insertion_is_idempotent():
    bodies = {"long_md": "prose", "medium_md": "", "short_md": ""}
    once = ensure_framing_marker(bodies, "retirement_age_headline", _INV)
    twice = ensure_framing_marker(once, "retirement_age_headline", _INV)
    assert once["long_md"] == twice["long_md"]


def test_stale_marker_is_replaced_not_duplicated():
    bodies = {"long_md": "prose <!--coh:retirement_age_headline lead_age=54-->",
              "medium_md": "", "short_md": ""}
    fixed = ensure_framing_marker(bodies, "retirement_age_headline", _INV)
    assert fixed["long_md"].count("coh:retirement_age_headline") == 1
    assert parse_markers(fixed["long_md"])["retirement_age_headline"]["lead_age"] == "46"
