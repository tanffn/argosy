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
