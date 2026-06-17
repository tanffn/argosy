from argosy.agents.coherence_facilitator import CoherenceFacilitatorAgent, FacilitatorOutcome


def test_build_prompt_lists_positions():
    agent = CoherenceFacilitatorAgent(user_id="ariel")
    system, user = agent.build_prompt(
        dispute_question="which age leads?",
        positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"},
                   {"role": "goals", "position": "54", "basis": "user_directive"}],
    )
    assert "which age leads?" in user
    assert "withdrawal" in user and "goals" in user
    assert agent.agent_role == "coherence_facilitator"


def test_outcome_model():
    o = FacilitatorOutcome(consensus=False, ruling="", crux="prime vs stated style")
    assert o.consensus is False
