"""Trader one-voice reconciliation rule (NVDA verdict-34 contradiction fix,
2026-08-10).

The per-holding fleet re-derived "thesis intact -> HOLD" and authored a settled
Verdict that CONTRADICTED a standing plan SELL. Ariel's ruling — ONE VOICE PER
POSITION, MIRROR-OR-PROPOSE-REVISION: when the packet carries a STANDING PLAN
STANCE of SELL/TRIM, the trader must NOT emit a bare HOLD over it — it either
MIRRORs the reduction or explicitly states a PROPOSED STANCE REVISION justified
by new facts.

These tests assert the PROMPT carries the rule (the LLM behavior itself is
validated by live e2e, not here).
"""
import pytest

from argosy.agents.trader import TraderAgent


@pytest.fixture
def agent():
    return TraderAgent(user_id="ariel", tier="T2")


_STANCE_CTX = (
    "CURRENT POSITION — NVDA (latest portfolio snapshot):\n"
    "- HELD: 10,940 shares.\n"
    "STANDING PLAN STANCE (one-voice, authoritative): SELL — "
    "source=plan, conviction=LOW.\n"
    "- This position is on an active plan trim/deconcentration pace "
    "(standing SELL). Your verdict MUST reconcile with this standing decision."
)


def _build(agent, mode, user_constraints):
    return agent.build_prompt(
        analyst_reports=[{"agent_role": "fundamentals", "summary": "intact"}],
        debate_outcome={"winner": "bull"},
        positions_snapshot=_STANCE_CTX,
        user_constraints=user_constraints,
        ticker="NVDA",
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_reconcile_rule_present_in_system_prompt(agent, mode):
    system, _ = _build(agent, mode, _STANCE_CTX)
    assert "RECONCILE WITH THE STANDING PLAN STANCE" in system
    assert "one voice per position" in system
    # The rule explicitly forbids a bare HOLD over a standing SELL/TRIM.
    assert "MUST NOT output a bare HOLD that contradicts it" in system
    assert "PROPOSED STANCE REVISION:" in system
    assert "MIRROR" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_prompt_does_not_instruct_bare_hold_over_sell(agent, mode):
    system, _ = _build(agent, mode, _STANCE_CTX)
    # Guard against a regression that would license thesis-intact -> HOLD over a
    # standing SELL. The rule states the opposite explicitly.
    assert (
        "thesis that is merely intact is NOT grounds to HOLD over a standing "
        "SELL/TRIM"
    ) in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_standing_stance_flows_into_user_prompt(agent, mode):
    _, user = _build(agent, mode, _STANCE_CTX)
    assert "STANDING PLAN STANCE (one-voice, authoritative): SELL" in user


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_hold_stays_valid_when_no_standing_sell(agent, mode):
    """No standing SELL/TRIM in the packet — the rule still ships but explicitly
    preserves the normal HOLD validity."""
    system, _ = _build(agent, mode, "no standing stance here")
    assert (
        "When there is NO standing SELL/TRIM stance, a HOLD remains perfectly "
        "valid"
    ) in system
