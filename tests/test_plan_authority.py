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
