"""Speculation cap config — schema and defaults."""

from __future__ import annotations

import pytest


def test_speculation_cap_defaults():
    """If `speculation` block is absent, defaults apply."""
    from argosy.config import load_speculation_cap

    cfg = load_speculation_cap(user_id="ariel", agent_settings={})
    assert cfg.max_pct_of_net_worth == 0.001  # 0.1% — conservative default
    assert cfg.max_concurrent_positions == 3


def test_speculation_cap_user_override():
    from argosy.config import load_speculation_cap

    cfg = load_speculation_cap(
        user_id="ariel",
        agent_settings={"speculation": {"max_pct_of_net_worth": 0.002, "max_concurrent_positions": 5}},
    )
    assert cfg.max_pct_of_net_worth == 0.002
    assert cfg.max_concurrent_positions == 5


def test_speculation_cap_rejects_negative():
    from argosy.config import load_speculation_cap

    with pytest.raises(ValueError):
        load_speculation_cap(
            user_id="ariel",
            agent_settings={"speculation": {"max_pct_of_net_worth": -0.01}},
        )


def test_speculation_cap_clamps_excessive():
    """Cap above 5% NW is rejected — that's not speculation, it's a position."""
    from argosy.config import load_speculation_cap

    with pytest.raises(ValueError):
        load_speculation_cap(
            user_id="ariel",
            agent_settings={"speculation": {"max_pct_of_net_worth": 0.10}},
        )
