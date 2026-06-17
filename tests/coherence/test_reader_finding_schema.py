from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import CoherenceFinding


def test_finding_accepts_structured_fields_and_appeal_kinds():
    f = CoherenceFinding(
        kind="ruling_defect", severity="BLOCKER",
        detail="ruling uses a stale FX rate", surfaces_cited=["long_md"],
        subject_type="retirement_age_headline", field_path="retirement.earliest_safe_age",
        normalized_claim="age_54_leads",
    )
    assert f.kind == "ruling_defect"
    assert f.subject_type == "retirement_age_headline"


def test_structured_fields_default_empty_for_back_compat():
    f = CoherenceFinding(kind="contradiction", severity="AMBER", detail="x", surfaces_cited=[])
    assert f.subject_type == ""
    assert f.normalized_claim == ""
