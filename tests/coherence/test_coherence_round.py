from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import run_coherence_round


def test_value_dispute_conforms_all_surfaces_and_verifies():
    bodies = {"long_md": "", "medium_md": "SGLN standalone non-UCITS leg",
              "short_md": "SGLN standalone non-UCITS leg"}
    json_surfaces = {"short_actions_json": {"actions": [
        {"label": "First UCITS dollar-cost tranche",
         "detail": "split across CSPX/FUSA/EIMI/SGLN"}]}}

    resolver_patches = {
        "sgln_ucits_membership": {
            "patches": [{"surface_id": "short_actions_json", "conform_method": "json_field",
                         "match_label": "UCITS dollar-cost", "set_field": "detail",
                         "new_value": "split across CSPX/FUSA/EIMI only; SGLN standalone"}],
            "invariant": [{"kind": "forbidden_claim", "subject_type": "sgln_ucits_membership",
                           "surface": "short_actions_json_text",
                           "pattern": "CSPX/FUSA/EIMI/SGLN"}],
        }
    }
    res = run_coherence_round(
        bodies=bodies, json_surfaces=json_surfaces,
        value_resolutions=resolver_patches, allowed_numbers=frozenset(),
    )
    assert res.ok, res.verifier.failures
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]
    assert res.verifier.ok
