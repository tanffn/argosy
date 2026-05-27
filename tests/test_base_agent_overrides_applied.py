"""Per-user agent_settings.yaml overrides take precedence over per-role defaults."""
from __future__ import annotations
from pathlib import Path

import pytest

from argosy.agents.base import BaseAgent


class _Trader(BaseAgent):
    agent_role = "trader"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def test_yaml_thinking_budget_overrides_default(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    thinking_budget: 12000
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.thinking_budget == 12000  # 12000, not the default 8000


def test_yaml_citations_override_to_false(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    citations_enabled: false
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.citations_enabled is False  # default would be True


# ---------------------------------------------------------------------------
# Anthropic API constraint: thinking_budget < max_tokens
# ---------------------------------------------------------------------------


def test_thinking_budget_invariant_enforced(monkeypatch, tmp_path: Path):
    """A YAML override that pushes thinking_budget >= max_tokens raises ValueError.

    Anthropic's API rejects calls where the configured thinking-budget is
    greater than or equal to the call's max_tokens (thinking tokens count
    toward max_tokens, not separately). ``BaseAgent.__init__`` enforces
    this invariant at construction time so misconfiguration surfaces
    before the first live LLM call.
    """
    # Trader's table max_tokens is 64_000 — push thinking_budget AT it.
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    thinking_budget: 64000
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    with pytest.raises(ValueError, match="thinking_budget"):
        _Trader(user_id="ariel")


def test_thinking_budget_invariant_enforced_when_exceeds_max(
    monkeypatch, tmp_path: Path,
):
    """Same invariant when thinking_budget strictly exceeds max_tokens."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    thinking_budget: 80000
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    with pytest.raises(ValueError, match="thinking_budget"):
        _Trader(user_id="ariel")


def test_thinking_budget_invariant_passes_when_strictly_less(
    monkeypatch, tmp_path: Path,
):
    """Boundary check: thinking_budget = max_tokens - 1 must construct cleanly."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("""
agents:
  trader:
    thinking_budget: 63999
""")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.thinking_budget == 63999
    assert agent.max_tokens == 64000
