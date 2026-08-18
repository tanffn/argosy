"""P0-b: `models:` block resolution — AgentSettings.model_for_role precedence.

Precedence under test (highest first):
  1. models.override["all"]
  2. models.override[role]
  3. code_default (the caller's DEFAULT_MODEL_BY_ROLE[role])
  4. models.defaults[role]   (legacy — must NOT beat the code default)
  5. None (caller applies its own final fallback)

Also covers: short-name resolution (opus/sonnet/haiku -> concrete ids),
unknown short names (ignored + warning, falls through), and BaseAgent
wiring end-to-end with the REAL configs/ariel/agent_settings.yaml `models:`
shape (the anti-regression case from CLAUDE.md's P0-b trap).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from argosy.agent_settings import AgentSettings, _SHORT_MODEL_ALIASES
from argosy.agents.base import BaseAgent, DEFAULT_MODEL_BY_ROLE, FALLBACK_MODEL


# ---------------------------------------------------------------------------
# Unit tests: AgentSettings.model_for_role
# ---------------------------------------------------------------------------


def test_override_all_beats_everything():
    settings = AgentSettings.model_validate(
        {"models": {"defaults": {"trader": "haiku"}, "override": {"all": "sonnet"}}}
    )
    assert settings.model_for_role("trader", code_default="claude-opus-4-8") == (
        _SHORT_MODEL_ALIASES["sonnet"]
    )


def test_override_role_beats_code_default():
    settings = AgentSettings.model_validate(
        {"models": {"override": {"concentration": "claude-opus-5"}}}
    )
    resolved = settings.model_for_role("concentration", code_default="claude-opus-4-8")
    assert resolved == "claude-opus-5"


def test_override_all_beats_override_role():
    settings = AgentSettings.model_validate(
        {"models": {"override": {"all": "opus", "trader": "haiku"}}}
    )
    resolved = settings.model_for_role("trader", code_default="claude-opus-4-8")
    assert resolved == _SHORT_MODEL_ALIASES["opus"]


def test_code_default_beats_legacy_defaults_block():
    """The anti-regression case: a stale `models.defaults` entry must NOT
    downgrade a role the code table already covers."""
    settings = AgentSettings.model_validate(
        {"models": {"defaults": {"concentration": "haiku"}, "override": {}}}
    )
    resolved = settings.model_for_role("concentration", code_default="claude-opus-4-8")
    assert resolved == "claude-opus-4-8"
    assert resolved != _SHORT_MODEL_ALIASES["haiku"]


def test_legacy_defaults_block_applies_when_no_code_default():
    """`models.defaults` is still consulted for roles the code table
    doesn't mention at all (code_default=None)."""
    settings = AgentSettings.model_validate(
        {"models": {"defaults": {"some_new_role": "sonnet"}, "override": {}}}
    )
    resolved = settings.model_for_role("some_new_role", code_default=None)
    assert resolved == _SHORT_MODEL_ALIASES["sonnet"]


def test_absent_everywhere_returns_none():
    settings = AgentSettings.model_validate({"models": {"defaults": {}, "override": {}}})
    assert settings.model_for_role("nonexistent_role", code_default=None) is None


def test_unknown_short_name_in_override_is_ignored_and_falls_through(caplog):
    settings = AgentSettings.model_validate(
        {"models": {"override": {"trader": "gpt-99"}}}
    )
    with caplog.at_level(logging.WARNING, logger="argosy.agent_settings"):
        resolved = settings.model_for_role("trader", code_default="claude-opus-4-8")
    assert resolved == "claude-opus-4-8"  # fell through to code default
    assert any("unrecognized model name" in r.message for r in caplog.records)


def test_unknown_short_name_in_defaults_is_ignored_and_falls_through(caplog):
    settings = AgentSettings.model_validate(
        {"models": {"defaults": {"orphan_role": "not-a-real-model"}}}
    )
    with caplog.at_level(logging.WARNING, logger="argosy.agent_settings"):
        resolved = settings.model_for_role("orphan_role", code_default=None)
    assert resolved is None
    assert any("unrecognized model name" in r.message for r in caplog.records)


def test_full_model_id_passed_through_unchanged():
    """An already-concrete id (starts with 'claude-') bypasses the alias
    table entirely — an operator can pin an exact id ahead of this table
    catching up."""
    settings = AgentSettings.model_validate(
        {"models": {"override": {"trader": "claude-opus-5"}}}
    )
    assert settings.model_for_role("trader", code_default="claude-opus-4-8") == (
        "claude-opus-5"
    )


# ---------------------------------------------------------------------------
# plan_critique resolves to an accessible model by default (no YAML at all)
# ---------------------------------------------------------------------------


def test_plan_critique_default_is_accessible_model():
    assert DEFAULT_MODEL_BY_ROLE["plan_critique"] == "claude-opus-5"
    assert DEFAULT_MODEL_BY_ROLE["plan_critique"] != "claude-fable-5"


# ---------------------------------------------------------------------------
# BaseAgent end-to-end wiring, incl. the REAL configs/ariel/agent_settings.yaml
# `models:` shape (the anti-regression case).
# ---------------------------------------------------------------------------


class _RoleAgent(BaseAgent):
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return ("", "")


def _make_agent_cls(role: str) -> type[_RoleAgent]:
    return type(f"_{role}_Agent", (_RoleAgent,), {"agent_role": role})


def test_plan_critique_resolves_without_yaml(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(tmp_path / "missing.yaml"))
    agent = _make_agent_cls("plan_critique")(user_id="ariel")
    assert agent.model == "claude-opus-5"


def test_absent_yaml_file_is_a_no_op(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(tmp_path / "does_not_exist.yaml"))
    agent = _make_agent_cls("trader")(user_id="ariel")
    assert agent.model == DEFAULT_MODEL_BY_ROLE["trader"]


def test_unparseable_yaml_is_a_no_op(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text("not: [valid: yaml: at all")
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))
    agent = _make_agent_cls("trader")(user_id="ariel")
    assert agent.model == DEFAULT_MODEL_BY_ROLE["trader"]


def test_explicit_model_kwarg_still_wins_over_yaml(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "models:\n  override:\n    trader: sonnet\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))
    agent = _make_agent_cls("trader")(user_id="ariel", model="claude-opus-4-8")
    assert agent.model == "claude-opus-4-8"


def test_override_role_wins_via_base_agent(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "models:\n  override:\n    trader: claude-opus-5\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))
    agent = _make_agent_cls("trader")(user_id="ariel")
    assert agent.model == "claude-opus-5"


def test_override_all_wins_via_base_agent(monkeypatch, tmp_path: Path):
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "models:\n  override:\n    all: claude-sonnet-4-6\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))
    agent = _make_agent_cls("trader")(user_id="ariel")
    assert agent.model == "claude-sonnet-4-6"


_REAL_ARIEL_MODELS_BLOCK = """
models:
  defaults:
    fundamentals: sonnet
    technical: haiku
    news: sonnet
    sentiment: haiku
    macro: sonnet
    plan_critique: sonnet
    concentration: haiku
    tax: sonnet
    fx: haiku
    trader: opus
    intake: sonnet
  override: {}
"""


@pytest.mark.parametrize(
    "role",
    ["concentration", "technical", "sentiment", "fx"],
)
def test_stale_defaults_block_does_not_downgrade_to_haiku(
    monkeypatch, tmp_path: Path, role: str,
):
    """The anti-regression test for the CLAUDE.md P0-b trap: the REAL
    `configs/ariel/agent_settings.yaml` `models:` shape (stale, mixed
    Haiku/Sonnet) must NOT silently downgrade roles the code table
    already covers on Opus."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(_REAL_ARIEL_MODELS_BLOCK)
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _make_agent_cls(role)(user_id="ariel")
    assert agent.model == DEFAULT_MODEL_BY_ROLE[role]
    assert "haiku" not in agent.model.lower()


def test_real_ariel_yaml_shape_plan_critique_stays_on_code_default(
    monkeypatch, tmp_path: Path,
):
    """`models.defaults.plan_critique: sonnet` in the real file must not
    override the code default (claude-opus-5)."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(_REAL_ARIEL_MODELS_BLOCK)
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _make_agent_cls("plan_critique")(user_id="ariel")
    assert agent.model == "claude-opus-5"


def test_real_ariel_yaml_shape_trader_matches_code_default_anyway(
    monkeypatch, tmp_path: Path,
):
    """trader: opus in the legacy defaults block happens to agree with
    the code default here — resolved value must still come from the
    code default per precedence, not (coincidentally) from the YAML."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(_REAL_ARIEL_MODELS_BLOCK)
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _make_agent_cls("trader")(user_id="ariel")
    assert agent.model == DEFAULT_MODEL_BY_ROLE["trader"] == "claude-opus-4-8"
