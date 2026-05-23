"""_build_system_blocks splits the system prompt into cacheable + role-specific."""
from __future__ import annotations

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return ("", "")


def test_returns_two_blocks_when_boilerplate_present():
    agent = _DummyAgent(user_id="ariel")
    # Simulate the system string BaseAgent.run() assembles: boilerplate + role.
    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news analyst. Output schema: ..."
    blocks = agent._build_system_blocks(full_system)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == BaseAgent.BOILERPLATE_SYSTEM
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["type"] == "text"
    assert "Role: news analyst" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_returns_single_block_when_boilerplate_missing():
    """If a caller passed a system prompt that does NOT start with the boilerplate,
    we return a single uncached block (defensive — should not happen in practice)."""
    agent = _DummyAgent(user_id="ariel")
    blocks = agent._build_system_blocks("Just role-specific text, no boilerplate prefix.")
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Just role-specific text, no boilerplate prefix."
    assert "cache_control" not in blocks[0]
