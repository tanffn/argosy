from argosy.agents.coherence_arbitrator import CoherenceArbitratorAgent, ArbitratorRuling


def test_build_prompt_states_authority_order_and_two_axes():
    agent = CoherenceArbitratorAgent(user_id="ariel")
    system, user = agent.build_prompt(
        dispute_question="which age leads?",
        positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"}],
        canonical_facts="earliest_safe_age=46; preservation_age=54",
        prime_directive="maximize finances + earliest safe retirement",
    )
    assert "authority" in system.lower()
    assert "factual" in system.lower() and "policy" in system.lower()
    assert "earliest safe retirement" in user
    assert agent.agent_role == "coherence_arbitrator"


def test_ruling_model_carries_invariant_and_per_surface_instructions():
    r = ArbitratorRuling(
        ruling_statement="age 46 leads; 54 strict track",
        axis="policy", basis="prime_directive", rationale="conservatism costs years",
        per_surface_instructions=[{"surface_id": "long_md", "instruction": "lead with 46"}],
        coherence_invariant=[{"kind": "required_framing_role", "surface": "long_md",
                              "role_field": "lead_age", "value": "46"}],
    )
    assert r.axis == "policy"
    assert r.coherence_invariant[0]["value"] == "46"
