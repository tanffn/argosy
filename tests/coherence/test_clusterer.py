# tests/coherence/test_clusterer.py
from argosy.quality.coherence.dispute import cluster_findings, dispute_key


def _f(subject, kind="contradiction", severity="BLOCKER", surfaces=("long_md",)):
    return {"subject_type": subject, "kind": kind, "severity": severity,
            "field_path": "", "normalized_claim": "", "surfaces_cited": list(surfaces),
            "detail": "d"}


def test_findings_with_same_subject_cluster_to_one_dispute():
    disputes = cluster_findings([_f("rsu_vest_policy", surfaces=("long_md",)),
                                 _f("rsu_vest_policy", surfaces=("short_actions_json",))])
    assert len(disputes) == 1
    assert disputes[0].subject_type == "rsu_vest_policy"
    assert set(disputes[0].surfaces_cited) == {"long_md", "short_actions_json"}


def test_policy_tension_kind_maps_conflict_type():
    disputes = cluster_findings([_f("retirement_age_headline", kind="fragile_claim")])
    assert disputes[0].conflict_type == "policy_tension"


def test_untyped_finding_yields_block_dispute():
    disputes = cluster_findings([_f("", kind="contradiction")])
    assert disputes[0].subject_type == ""
