from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    _build_prompt,
)
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY


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


def test_prompt_asks_model_to_classify_subject_type_with_registry_taxonomy():
    """The reader prompt must instruct the model to set ``subject_type`` and must
    list EVERY SUBJECT_REGISTRY key, so the prompt taxonomy and the registry the
    deliberation router uses cannot silently drift apart."""
    prompt = _build_prompt(
        assembled_artifact="doc", external_context="", prior_plan_text="",
    )
    assert "subject_type" in prompt
    for key in SUBJECT_REGISTRY:
        assert key in prompt, f"taxonomy key {key!r} missing from reader prompt"
