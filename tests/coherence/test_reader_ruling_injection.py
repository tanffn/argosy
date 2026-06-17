from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    build_settled_rulings_block,
)


def test_ruling_block_lists_settled_and_states_appeal_contract():
    block = build_settled_rulings_block([
        {"subject_type": "retirement_age_headline",
         "ruling": "age 46 leads; 54 strict track; capital-preservation = target-sizing basis"},
    ])
    assert "retirement_age_headline" in block
    assert "age 46 leads" in block
    assert "ruling_divergence" in block
    assert "ruling_defect" in block


def test_empty_rulings_yields_empty_block():
    assert build_settled_rulings_block([]) == ""
