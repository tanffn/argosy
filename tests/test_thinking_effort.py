"""Opus 4.7 adaptive-thinking migration — effort-based config.

Replaces per-role fixed ``thinking_budget`` with adaptive thinking
(``thinking={"type": "adaptive"}`` + ``effort=low|medium|high|max``).
The legacy fixed-budget config stays as a fallback for tests / users
that explicitly opt out.

Verifies:
  * ``DEFAULT_THINKING_EFFORT_BY_ROLE`` carries the canonical levels per
    role; absent roles fall through to the "high" instance default.
  * Per-user YAML overrides (``thinking_effort`` AND ``thinking_budget``)
    resolve in the documented order:
      explicit YAML ``thinking_effort`` →
      table ``DEFAULT_THINKING_EFFORT_BY_ROLE`` →
      table ``DEFAULT_THINKING_BUDGET_BY_ROLE`` (legacy) →
      no thinking.
  * The SDK call carries ``thinking == {"type": "adaptive"}`` +
    ``effort == "<level>"`` when effort is set on the agent.
  * Setting ``thinking_effort = None`` (explicit disable) + a non-zero
    budget engages the legacy fixed-budget path verbatim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from argosy.agents.base import (
    DEFAULT_MODEL_BY_ROLE,
    DEFAULT_THINKING_BUDGET_BY_ROLE,
    DEFAULT_THINKING_EFFORT_BY_ROLE,
    BaseAgent,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _Out(BaseModel):
    text: str = "ok"


class _Trader(BaseAgent[_Out]):
    """A real role from the effort table — defaults to ``"high"``."""

    agent_role = "trader"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


class _FundManager(BaseAgent[_Out]):
    """Highest-effort role per the canonical table (``"max"``)."""

    agent_role = "fund_manager"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


class _UnknownRole(BaseAgent[_Out]):
    """Role NOT in any defaults table — exercises the "high" fallback."""

    agent_role = "test_thinking_effort_unknown"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


def _install_fake_query(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch ``claude_agent_sdk.query`` so the call records its options."""
    captured = captured if captured is not None else {}

    async def _fake_query(*, prompt, options):
        captured["options"] = options
        if hasattr(prompt, "__aiter__"):
            async for _ in prompt:
                pass
        return
        yield  # pragma: no cover — make this an async generator

    monkeypatch.setattr("claude_agent_sdk.query", _fake_query)
    return captured


# ----------------------------------------------------------------------
# Table-level smoke tests
# ----------------------------------------------------------------------


def test_default_thinking_effort_per_role():
    """Each role in DEFAULT_MODEL_BY_ROLE has an explicit effort, OR an
    agent built for that role falls through to the "high" default."""
    # Every role listed in DEFAULT_MODEL_BY_ROLE should either appear in
    # the effort table OR resolve to the "high" instance fallback. We
    # assert on the explicit entries first.
    heavy_max = {
        "fund_manager", "plan_critique",
        "fund_manager_dialogue_verdict",
    }
    for role in heavy_max:
        assert DEFAULT_THINKING_EFFORT_BY_ROLE[role] == "max", (
            f"{role!r} should default to 'max' effort"
        )

    # plan_synthesizer moved from "max" -> "high" on 2026-06-01 after
    # synth #58 hit 3-of-3 truncation failures at max effort. Codex
    # tandem audit + see argosy/agents/base.py:230 docstring. Pin
    # explicitly here so a future "let's bump it back" doesn't slip
    # silently.
    deep_high = {
        "plan_synthesizer",
        "bull_researcher", "bear_researcher", "researcher_facilitator",
        "risk_officer", "risk_facilitator", "audit", "trader",
        "analyst_responder", "plan_distiller", "intake_extractor",
        "advisor", "domain_refresh",
    }
    for role in deep_high:
        assert DEFAULT_THINKING_EFFORT_BY_ROLE[role] == "high", (
            f"{role!r} should default to 'high' effort"
        )

    moderate_medium = {
        "concentration", "fx", "fundamentals", "news", "sentiment",
        "technical", "macro", "tax", "household_budget",
        "objection_translator", "daily_briefer",
    }
    for role in moderate_medium:
        assert DEFAULT_THINKING_EFFORT_BY_ROLE[role] == "medium", (
            f"{role!r} should default to 'medium' effort"
        )

    chat_low = {"intake", "household_categorizer", "watchlist"}
    for role in chat_low:
        assert DEFAULT_THINKING_EFFORT_BY_ROLE[role] == "low", (
            f"{role!r} should default to 'low' effort"
        )


def test_unknown_role_falls_back_to_high():
    """Roles not in the table get the "high" instance default (accuracy
    over LLM cost — per the SDD binding preference)."""
    agent = _UnknownRole(user_id="ariel")
    assert agent.thinking_effort == "high"


def test_known_role_picks_table_effort():
    assert _Trader(user_id="ariel").thinking_effort == "high"
    assert _FundManager(user_id="ariel").thinking_effort == "max"


# ----------------------------------------------------------------------
# YAML override resolution
# ----------------------------------------------------------------------


def test_thinking_effort_yaml_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Explicit YAML ``thinking_effort`` overrides the table default."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  fund_manager:\n"
        "    thinking_effort: high\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _FundManager(user_id="ariel")
    # Table default is "max" — YAML overrides to "high".
    assert agent.thinking_effort == "high"


def test_yaml_effort_explicit_null_clears_adaptive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Explicit ``thinking_effort: null`` in YAML clears adaptive mode,
    enabling the legacy fixed-budget path when a budget is also set."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  trader:\n"
        "    thinking_effort: null\n"
        "    thinking_budget: 12000\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.thinking_effort is None
    assert agent.thinking_budget == 12000


def test_yaml_budget_without_effort_falls_back_to_fixed_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """When YAML sets ``thinking_budget`` but NOT ``thinking_effort``,
    the user is opting out of adaptive thinking for that role — the
    fixed-budget path fires (effort cleared, budget applied)."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  trader:\n"
        "    thinking_budget: 12000\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    agent = _Trader(user_id="ariel")
    assert agent.thinking_budget == 12000
    assert agent.thinking_effort is None  # cleared by the YAML-budget rule


# ----------------------------------------------------------------------
# SDK call shape
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adaptive_thinking_config_built_when_effort_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """FundManager builds ``thinking={"type": "adaptive"}`` + ``effort="max"``."""
    captured = _install_fake_query(monkeypatch)

    agent = _FundManager(user_id="ariel")
    await agent._call_via_claude_code_inner(system="sys", user="user")

    opts = captured["options"]
    assert opts.thinking == {"type": "adaptive"}
    assert opts.effort == "max"
    # Adaptive picks its own internal budget — we must NOT pin the SDK
    # ceiling via max_thinking_tokens (which would re-introduce the cap
    # we just removed).
    assert opts.max_thinking_tokens is None


@pytest.mark.asyncio
async def test_adaptive_thinking_uses_high_for_trader(
    monkeypatch: pytest.MonkeyPatch,
):
    """Trader's effort is "high" per the canonical table."""
    captured = _install_fake_query(monkeypatch)

    agent = _Trader(user_id="ariel")
    await agent._call_via_claude_code_inner(system="sys", user="user")

    opts = captured["options"]
    assert opts.thinking == {"type": "adaptive"}
    assert opts.effort == "high"


@pytest.mark.asyncio
async def test_fixed_thinking_used_when_effort_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Setting ``thinking_effort = None`` + a positive ``thinking_budget``
    falls back to the legacy fixed-budget config verbatim — backwards
    compat for tests + users who pinned to fixed budgets."""
    captured = _install_fake_query(monkeypatch)

    agent = _Trader(user_id="ariel")
    agent.thinking_effort = None  # explicit opt-out of adaptive
    agent.thinking_budget = 4000  # legacy fixed-budget mode

    await agent._call_via_claude_code_inner(system="sys", user="user")

    opts = captured["options"]
    assert opts.thinking == {"type": "enabled", "budget_tokens": 4000}
    assert opts.max_thinking_tokens == 4000
    # `effort` field must NOT be set in fixed-budget mode.
    assert opts.effort is None


@pytest.mark.asyncio
async def test_no_thinking_when_effort_and_budget_both_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """``thinking_effort = None`` AND ``thinking_budget = 0`` → no thinking
    config at all on the SDK options."""
    captured = _install_fake_query(monkeypatch)

    agent = _Trader(user_id="ariel")
    agent.thinking_effort = None
    agent.thinking_budget = 0

    await agent._call_via_claude_code_inner(system="sys", user="user")

    opts = captured["options"]
    assert opts.thinking is None
    assert opts.effort is None
    assert opts.max_thinking_tokens is None


# ----------------------------------------------------------------------
# Invariant
# ----------------------------------------------------------------------


def test_invariant_does_not_fire_when_effort_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """The ``thinking_budget < max_tokens`` invariant ONLY applies in
    legacy fixed-budget mode. With adaptive effort set, a "bad" budget
    value is harmless because it won't be sent."""
    yaml_path = tmp_path / "agent_settings.yaml"
    # Set BOTH effort (engages adaptive) and a budget that would have
    # tripped the legacy invariant. The invariant must not fire.
    yaml_path.write_text(
        "agents:\n"
        "  trader:\n"
        "    thinking_effort: high\n"
        "    thinking_budget: 64000\n"  # == max_tokens — would have raised
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    # Must construct cleanly — no ValueError because adaptive mode wins.
    agent = _Trader(user_id="ariel")
    assert agent.thinking_effort == "high"
    assert agent.thinking_budget == 64000


def test_invariant_fires_only_in_fixed_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """When the user explicitly clears effort (null) and sets an over-
    cap budget, the invariant DOES fire (matches legacy semantics)."""
    yaml_path = tmp_path / "agent_settings.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  trader:\n"
        "    thinking_effort: null\n"
        "    thinking_budget: 80000\n"
    )
    monkeypatch.setenv("ARGOSY_AGENT_SETTINGS_PATH", str(yaml_path))

    with pytest.raises(ValueError, match="thinking_budget"):
        _Trader(user_id="ariel")


# ----------------------------------------------------------------------
# Sanity: legacy DEFAULT_THINKING_BUDGET_BY_ROLE remains importable
# ----------------------------------------------------------------------


def test_legacy_budget_table_still_exported():
    """Backwards compat — the legacy table must remain importable
    (depended on by tests that pin to fixed-budget mode)."""
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["fund_manager"] == 16000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["trader"] == 8000
    # Sanity check that the model table is also still well-formed.
    assert DEFAULT_MODEL_BY_ROLE["fund_manager"].startswith("claude-")
