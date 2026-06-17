from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import run_coherence_round
from argosy.quality.coherence.claim_markers import render_marker


def test_draft45_disputes_conform_and_verify():
    age_marker = render_marker("retirement_age_headline",
                               {"lead_age": "46", "strict_track_age": "54",
                                "capital_preservation_role": "target_sizing_basis"})
    bodies = {
        "long_md": f"Retirement framing. {age_marker}",
        "medium_md": "SGLN standalone non-UCITS leg",
        "short_md": "sell net vested NVDA -> SGOV",
    }
    json_surfaces = {"short_actions_json": {"actions": [
        {"label": "First UCITS dollar-cost tranche", "detail": "split across CSPX/FUSA/EIMI/SGLN"},
        {"label": "Sell 2026-06-17 net vested NVDA", "detail": "route net-of-tax to SGOV"},
    ]}}

    value_resolutions = {
        "sgln_ucits_membership": {
            "patches": [{"surface_id": "short_actions_json", "conform_method": "json_field",
                         "match_label": "UCITS dollar-cost", "set_field": "detail",
                         "new_value": "split across CSPX/FUSA/EIMI only; SGLN standalone"}],
            "invariant": [{"kind": "forbidden_claim", "subject_type": "sgln_ucits_membership",
                           "surface": "short_actions_json_text",
                           "pattern": "CSPX/FUSA/EIMI/SGLN"}],
        },
        "retirement_age_headline": {
            "patches": [],
            "invariant": [
                {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "role_field": "lead_age", "value": "46"},
                {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "role_field": "capital_preservation_role",
                 "value": "target_sizing_basis"},
                {"kind": "forbidden_claim", "subject_type": "rsu_vest_policy",
                 "surface": "short_md", "pattern": "retain net vested as NVDA"},
            ],
        },
    }

    res = run_coherence_round(bodies=bodies, json_surfaces=json_surfaces,
                              value_resolutions=value_resolutions, allowed_numbers=frozenset())
    assert res.ok, res.verifier.failures
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]
    assert res.verifier.ok
