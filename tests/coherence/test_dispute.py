# tests/coherence/test_dispute.py
from argosy.quality.coherence.dispute import Dispute, dispute_key


def test_dispute_key_is_stable_across_question_phrasing():
    a = Dispute(
        subject_type="retirement_age_headline",
        subject_field_path="retirement.earliest_safe_age",
        scope="person",
        conflict_type="policy_tension",
        normalized_options=("age_46_typical", "age_54_preservation"),
        implicated_canonical_fact_ids=("retirement.earliest_safe_age",),
        implicated_user_directive_ids=("prime_directive", "capital_preservation_style"),
        question="Which retirement age leads?",
    )
    b = Dispute(
        subject_type="retirement_age_headline",
        subject_field_path="retirement.earliest_safe_age",
        scope="person",
        conflict_type="policy_tension",
        normalized_options=("age_54_preservation", "age_46_typical"),
        implicated_canonical_fact_ids=("retirement.earliest_safe_age",),
        implicated_user_directive_ids=("capital_preservation_style", "prime_directive"),
        question="Is 46 or 54 the binding headline?",
    )
    assert dispute_key(a) == dispute_key(b)


def test_dispute_key_differs_on_subject():
    a = Dispute(subject_type="rsu_vest_policy", subject_field_path="", scope="person",
                conflict_type="value_mismatch", normalized_options=(), question="x")
    b = Dispute(subject_type="sgln_ucits_membership", subject_field_path="", scope="person",
                conflict_type="value_mismatch", normalized_options=(), question="y")
    assert dispute_key(a) != dispute_key(b)
