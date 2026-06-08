"""The authority disclaimer is a shared constant; every plan-touching
agent must pull from this single source so the message stays consistent.
"""

def test_authority_disclaimer_contains_required_phrases():
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER

    text = AUTHORITY_DISCLAIMER.lower()
    for phrase in (
        "one input",
        "disagree",
        "loyal",
        "not authority",
    ):
        assert phrase in text, f"disclaimer missing required phrase: {phrase!r}"


def test_authority_disclaimer_is_singleton():
    """Importing twice returns the same object — no per-call mutation."""
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER as A
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER as B

    assert A is B


def test_prime_directive_contains_required_phrases():
    """H11: the prime directive is the single canonical source injected into
    the fund manager, plan synthesizer, and risk officers. Pin the load-
    bearing phrases so a wording refactor can't silently regress it.
    """
    from argosy.agents._plan_authority import PRIME_DIRECTIVE

    text = PRIME_DIRECTIVE.lower()
    for phrase in (
        "prime directive",
        "maximize",
        "earliest safe retirement",
        "anti-goal",
        "trade-off",
    ):
        assert phrase in text, f"prime directive missing phrase: {phrase!r}"


def test_fund_manager_uses_canonical_prime_directive():
    """After the H11 refactor the fund manager must IMPORT the directive
    (one source), not keep a duplicated copy. The rendered plan-revision
    system prompt must therefore contain the canonical block verbatim.
    """
    from argosy.agents._plan_authority import PRIME_DIRECTIVE
    from argosy.agents.fund_manager import FundManagerAgent

    agent = FundManagerAgent(user_id="ariel")
    sys, _ = agent.build_prompt(
        decision_kind="plan_revision",
        draft_plan='{"long": {}}',
        risk_verdict="APPROVE",
    )
    assert PRIME_DIRECTIVE in sys


def test_conservative_counterweight_contains_required_phrases():
    from argosy.agents._plan_authority import CONSERVATIVE_FI_COUNTERWEIGHT

    text = CONSERVATIVE_FI_COUNTERWEIGHT.lower()
    assert "cost-in-years" in text
    assert "retirement" in text
