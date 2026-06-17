from argosy.quality.coherence.dispute import Dispute
from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import (
    deliberate_dispute, DeliberationResult,
)


class _StubFacilitator:
    def __init__(self, consensus, ruling=""): self._c, self._r = consensus, ruling
    def run_sync(self, **kw):
        from types import SimpleNamespace
        from argosy.agents.coherence_facilitator import FacilitatorOutcome
        return SimpleNamespace(output=FacilitatorOutcome(consensus=self._c, ruling=self._r, crux="x"))


class _StubArbitrator:
    def run_sync(self, **kw):
        from types import SimpleNamespace
        from argosy.agents.coherence_arbitrator import ArbitratorRuling
        return SimpleNamespace(output=ArbitratorRuling(
            ruling_statement="age 46 leads; 54 strict track", axis="policy",
            basis="prime_directive", rationale="prime directive",
            per_surface_instructions=[{"surface_id": "long_md", "instruction": "lead 46"}],
            coherence_invariant=[{"kind": "required_framing_role", "surface": "long_md",
                                  "role_field": "lead_age", "value": "46"}]))


def test_no_consensus_escalates_to_arbitrator():
    d = Dispute(subject_type="retirement_age_headline", subject_field_path="",
                scope="person", conflict_type="policy_tension", question="which age leads?")
    res = deliberate_dispute(
        d, panelist_positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"}],
        facilitator=_StubFacilitator(consensus=False), arbitrator=_StubArbitrator(),
        canonical_facts="earliest_safe_age=46", prime_directive="earliest safe retirement",
    )
    assert res.resolved_by == "arbitrator"
    assert res.invariant[0]["value"] == "46"


def test_consensus_skips_arbitrator():
    d = Dispute(subject_type="x", subject_field_path="", scope="person",
                conflict_type="policy_tension", question="q")
    res = deliberate_dispute(
        d, panelist_positions=[{"role": "a", "position": "p", "basis": "canonical_fact"}],
        facilitator=_StubFacilitator(consensus=True, ruling="agreed"),
        arbitrator=_StubArbitrator(),
        canonical_facts="", prime_directive="",
    )
    assert res.resolved_by == "consensus"
    assert res.ruling == "agreed"
