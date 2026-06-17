from argosy.agents.coherence_panelist import CoherencePanelistAgent, PanelistPosition


def test_build_prompt_includes_role_dispute_and_peer_positions():
    agent = CoherencePanelistAgent(user_id="ariel")
    system, user = agent.build_prompt(
        represented_role="withdrawal_sequencer",
        dispute_question="Which retirement age is the binding headline?",
        canonical_facts="earliest_safe_age=46; preservation_age=54",
        peer_positions=["equity perspective: capital-preservation style => 54"],
    )
    assert "withdrawal_sequencer" in user
    assert "binding headline" in user
    assert "54" in user
    assert agent.agent_role == "coherence_panelist"


def test_output_model_shape():
    p = PanelistPosition(position="age 46 leads", basis="prime_directive",
                         cites=["retirement.earliest_safe_age"])
    assert p.basis == "prime_directive"
