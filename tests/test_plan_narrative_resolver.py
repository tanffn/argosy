"""H10 + B2: the plan-narrator must NOT hardcode headline numbers — it consumes
a resolver-derived <resolved_numbers> block (mirroring the synthesizer), and the
stale literals it used to bake in (22M / 4.5% / 3.5% / retire 49 / 2031) are gone.
"""
import pytest

# The exact stale figures the numeric resolver was built to kill — they must not
# appear anywhere in the narrator's prompt.
STALE_LITERALS = ["4.5%", "3.5%", "22M", "2031", "target 49"]


def test_system_prompt_has_no_hardcoded_headline_numbers():
    from argosy.agents.plan_narrative import _SYSTEM_PROMPT

    for s in STALE_LITERALS:
        assert s not in _SYSTEM_PROMPT, (
            f"stale hardcoded number {s!r} still present in the narrator system prompt"
        )


def test_system_prompt_instructs_resolved_numbers_only():
    from argosy.agents.plan_narrative import _SYSTEM_PROMPT

    assert "<resolved_numbers>" in _SYSTEM_PROMPT
    assert "derivation pending" in _SYSTEM_PROMPT.lower()


def test_build_prompt_includes_resolved_block_and_omits_stale():
    from argosy.agents.plan_narrative import PlanNarrativeAgent

    agent = PlanNarrativeAgent(user_id="ariel")
    block = "net_worth_nis: 11,200,000 (resolved)\nfi_target_nis: [derivation pending]"
    system, user = agent.build_prompt(
        plan_input="portfolio composition + horizons here",
        resolved_numbers_block=block,
    )
    combined = system + "\n" + user
    for s in STALE_LITERALS:
        assert s not in combined
    assert "<resolved_numbers>" in user
    assert "net_worth_nis: 11,200,000" in user
