"""agent_settings.yaml supports thinking_budget + citations_enabled overrides."""
from __future__ import annotations

from pathlib import Path

import pytest

from argosy.config import load_agent_settings


def test_override_loaded(tmp_path: Path):
    yaml_text = """
agents:
  bear_researcher:
    thinking_budget: 6000
    citations_enabled: false
  trader:
    thinking_budget: 12000
"""
    p = tmp_path / "agent_settings.yaml"
    p.write_text(yaml_text)
    settings = load_agent_settings(p)

    assert settings.for_role("bear_researcher").thinking_budget == 6000
    assert settings.for_role("bear_researcher").citations_enabled is False
    assert settings.for_role("trader").thinking_budget == 12000
    # Unspecified field falls back to per-role default
    assert settings.for_role("trader").citations_enabled is None  # None = use default


def test_unknown_role_returns_empty_overrides(tmp_path: Path):
    p = tmp_path / "agent_settings.yaml"
    p.write_text("agents: {}")
    settings = load_agent_settings(p)
    assert settings.for_role("news_analyst").thinking_budget is None
    assert settings.for_role("news_analyst").citations_enabled is None


def test_invalid_thinking_budget_rejected_at_load(tmp_path: Path):
    p = tmp_path / "agent_settings.yaml"
    p.write_text("agents:\n  trader:\n    thinking_budget: -100\n")
    with pytest.raises(ValueError, match="thinking_budget"):
        load_agent_settings(p)
