from types import SimpleNamespace

from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import (
    run_coherence_deliberation_pass,
)
from argosy.agents.coherence_facilitator import FacilitatorOutcome
from argosy.agents.coherence_arbitrator import ArbitratorRuling
from argosy.agents.coherence_panelist import PanelistPosition
from argosy.quality.coherence.claim_markers import parse_markers


class _Panelist:
    def __init__(self, role): self.role = role
    def run_sync(self, **kw):
        return SimpleNamespace(output=PanelistPosition(
            position="age 46 leads", basis="prime_directive", cites=[]))


class _Facilitator:
    def run_sync(self, **kw):
        return SimpleNamespace(output=FacilitatorOutcome(consensus=False, ruling="", crux="x"))


class _Arbitrator:
    def run_sync(self, **kw):
        return SimpleNamespace(output=ArbitratorRuling(
            ruling_statement="age 46 leads; 54 strict track", axis="policy",
            basis="prime_directive", rationale="prime directive: earliest-safe leads",
            per_surface_instructions=[],
            coherence_invariant=[
                {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "role_field": "lead_age", "value": "46"},
                {"kind": "forbidden_claim", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "pattern": "54 is the single binding"},
            ]))


def _resolver_value_fn(dispute):
    # the SGLN value dispute conforms the action JSON detail
    if dispute.subject_type == "sgln_ucits_membership":
        return {
            "patches": [{"surface_id": "short_actions_json", "conform_method": "json_field",
                         "match_label": "UCITS dollar-cost", "set_field": "detail",
                         "new_value": "split across CSPX/FUSA/EIMI only; SGLN standalone"}],
            "invariant": [{"kind": "forbidden_claim", "subject_type": "sgln_ucits_membership",
                           "surface": "short_actions_json_text", "pattern": "EIMI/SGLN"}],
        }
    return None


def test_pass_handles_value_and_arbitration_disputes_end_to_end():
    bodies = {"long_md": "Retirement framing prose.", "medium_md": "", "short_md": ""}
    json_surfaces = {"short_actions_json": {"actions": [
        {"label": "First UCITS dollar-cost tranche", "detail": "split across CSPX/FUSA/EIMI/SGLN"}]}}
    findings = [
        {"subject_type": "retirement_age_headline", "kind": "fragile_claim",
         "surfaces_cited": ["long_md"], "field_path": "", "normalized_claim": "", "detail": "which age leads?"},
        {"subject_type": "sgln_ucits_membership", "kind": "contradiction",
         "surfaces_cited": ["short_actions_json"], "field_path": "", "normalized_claim": "", "detail": "sgln in split"},
    ]
    res = run_coherence_deliberation_pass(
        bodies=bodies, json_surfaces=json_surfaces, findings=findings,
        canonical_facts="earliest_safe_age=46; preservation_age=54",
        prime_directive="maximize finances + earliest safe retirement",
        make_panelist=_Panelist, facilitator=_Facilitator(), arbitrator=_Arbitrator(),
        resolver_value_fn=_resolver_value_fn,
    )
    assert res.ok, res.errors
    # the framing marker was inserted into long_md by the arbitration path
    assert parse_markers(res.bodies["long_md"])["retirement_age_headline"]["lead_age"] == "46"
    # the value dispute conformed the action JSON
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]
    # both rulings recorded, with the right resolver
    by_subj = {r["subject_type"]: r for r in res.rulings}
    assert by_subj["retirement_age_headline"]["resolved_by"] == "arbitrator"
    assert by_subj["sgln_ucits_membership"]["resolved_by"] == "resolver"


def test_untypeable_dispute_blocks():
    findings = [{"subject_type": "", "kind": "contradiction", "surfaces_cited": [],
                 "field_path": "", "normalized_claim": "", "detail": "?"}]
    res = run_coherence_deliberation_pass(
        bodies={"long_md": "", "medium_md": "", "short_md": ""}, json_surfaces={},
        findings=findings, canonical_facts="", prime_directive="",
    )
    assert res.ok is False
    assert any("untypeable" in e for e in res.errors)
