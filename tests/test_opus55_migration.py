"""Default fleet identity and cost telemetry for the Opus 5.5 migration."""
import pytest

from argosy.agent_settings import _SHORT_MODEL_ALIASES
from argosy.agents.base import BaseAgent, DEFAULT_MODEL_BY_ROLE, FALLBACK_MODEL
from argosy.agents.fund_manager import FundManagerAgent
from argosy.agents.trader import TraderAgent
from argosy.agents.alternatives_sourcer import AlternativesSourcerAgent
from argosy.agents.alternatives_reviewers import AlternativesFundManagerAgent


@pytest.mark.parametrize("role", list(DEFAULT_MODEL_BY_ROLE))
def test_fleet_defaults_are_current(role):
    assert DEFAULT_MODEL_BY_ROLE[role] == "claude-opus-5-5"
    assert FALLBACK_MODEL == _SHORT_MODEL_ALIASES["opus"] == "claude-opus-5-5"


@pytest.mark.parametrize("cls", [FundManagerAgent, TraderAgent, AlternativesSourcerAgent,
                                AlternativesFundManagerAgent])
def test_constructors_do_not_pin_retired_default(cls, monkeypatch, tmp_path):
    settings = tmp_path / "agents.yaml"
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(settings))
    assert cls(user_id="test").model == "claude-opus-5-5"
    settings.write_text("models:\n  override:\n    all: claude-sonnet-4-6\n")
    assert cls(user_id="test").model == "claude-sonnet-4-6"
    settings.write_text("models:\n  override:\n    all: claude-opus-5-5\n")
    assert cls(user_id="test").model == "claude-opus-5-5"
    # An intentional explicit historical pin must remain honored.
    assert cls(user_id="test", model="claude-opus-5").model == "claude-opus-5"


@pytest.mark.parametrize("tier", ["T0", "T1"])
def test_low_tier_trader_unchanged(tier, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(tmp_path / "missing.yaml"))
    assert TraderAgent(user_id="test", tier=tier).model == "claude-sonnet-4-6"


@pytest.mark.parametrize("model, expected", [("claude-opus-5-5", 2.94),
                                           ("claude-opus-5", 3.725)])
def test_price_and_cache_rate_by_model(model, expected):
    agent = BaseAgent(user_id="test", model=model)
    # 100k uncached + 200k cache reads + 300k cache writes + 48k output + 2k thinking.
    assert agent._estimate_usd(tokens_in=100_000, tokens_out=48_000, cache_input_tokens=200_000,
                              cache_creation_tokens=300_000,
                              thinking_tokens=2_000) == pytest.approx(expected)
