"""T6.1 — Default the decision fleet to long-hold; disable minute/hour cadences.

Verifies:
  - ``DecisionFlow.run()`` defaults to ``consult_mode="long_hold"``.
  - ``RunRequest`` (the API body model) defaults to ``consult_mode="long_hold"``.
  - ``CadencesBlock()`` (pure Pydantic default) has minute.enabled=False and
    hour.enabled=False.
  - ``AgentSettings()`` (no YAML file) carries the same cadence defaults.
  - The ``_DEFAULT_YAML`` template written to new users has minute/hour
    enabled: false.
  - Tactical-trade mode is still reachable (opt-in not removed).
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import yaml


# ---------------------------------------------------------------------------
# 1. DecisionFlow.run() default
# ---------------------------------------------------------------------------


def test_decision_flow_run_default_is_long_hold() -> None:
    """The ``consult_mode`` parameter of ``DecisionFlow.run`` must default
    to ``"long_hold"``."""
    from argosy.decisions.flow import DecisionFlow

    sig = inspect.signature(DecisionFlow.run)
    default = sig.parameters["consult_mode"].default
    assert default == "long_hold", (
        "DecisionFlow.run consult_mode default is %r; expected 'long_hold'" % default
    )


# ---------------------------------------------------------------------------
# 2. RunRequest API model default
# ---------------------------------------------------------------------------


def test_run_request_default_is_long_hold() -> None:
    """The ``RunRequest`` Pydantic model must default ``consult_mode`` to
    ``"long_hold"`` so callers that omit the field get the long-hold fleet."""
    from argosy.api.routes.decisions import RunRequest

    req = RunRequest(ticker="AAPL")
    assert req.consult_mode == "long_hold", (
        "RunRequest.consult_mode default is %r; expected 'long_hold'" % req.consult_mode
    )


# ---------------------------------------------------------------------------
# 3. CadencesBlock Pydantic default — minute and hour disabled
# ---------------------------------------------------------------------------


def test_cadences_block_minute_disabled_by_default() -> None:
    """``CadencesBlock()`` (no YAML override) must have ``minute.enabled=False``."""
    from argosy.agent_settings import CadencesBlock

    block = CadencesBlock()
    assert block.minute.enabled is False, (
        "CadencesBlock().minute.enabled is %r; expected False" % block.minute.enabled
    )


def test_cadences_block_hour_disabled_by_default() -> None:
    """``CadencesBlock()`` (no YAML override) must have ``hour.enabled=False``."""
    from argosy.agent_settings import CadencesBlock

    block = CadencesBlock()
    assert block.hour.enabled is False, (
        "CadencesBlock().hour.enabled is %r; expected False" % block.hour.enabled
    )


# ---------------------------------------------------------------------------
# 4. AgentSettings() zero-arg default carries the disabled cadences
# ---------------------------------------------------------------------------


def test_agent_settings_default_minute_disabled() -> None:
    """``AgentSettings()`` (no file) must have ``cadences.minute.enabled=False``."""
    from argosy.agent_settings import AgentSettings

    s = AgentSettings()
    assert s.cadences.minute.enabled is False, (
        "AgentSettings().cadences.minute.enabled is %r; expected False"
        % s.cadences.minute.enabled
    )


def test_agent_settings_default_hour_disabled() -> None:
    """``AgentSettings()`` (no file) must have ``cadences.hour.enabled=False``."""
    from argosy.agent_settings import AgentSettings

    s = AgentSettings()
    assert s.cadences.hour.enabled is False, (
        "AgentSettings().cadences.hour.enabled is %r; expected False"
        % s.cadences.hour.enabled
    )


# ---------------------------------------------------------------------------
# 5. _DEFAULT_YAML template has minute/hour disabled
# ---------------------------------------------------------------------------


def test_default_yaml_minute_disabled() -> None:
    """The ``_DEFAULT_YAML`` string (written to new users) must set
    ``cadences.minute.enabled: false``."""
    from argosy.agent_settings import _DEFAULT_YAML  # type: ignore[attr-defined]

    parsed = yaml.safe_load(_DEFAULT_YAML)
    minute_enabled = parsed["cadences"]["minute"]["enabled"]
    assert minute_enabled is False, (
        "_DEFAULT_YAML cadences.minute.enabled is %r; expected False" % minute_enabled
    )


def test_default_yaml_hour_disabled() -> None:
    """The ``_DEFAULT_YAML`` string (written to new users) must set
    ``cadences.hour.enabled: false``."""
    from argosy.agent_settings import _DEFAULT_YAML  # type: ignore[attr-defined]

    parsed = yaml.safe_load(_DEFAULT_YAML)
    hour_enabled = parsed["cadences"]["hour"]["enabled"]
    assert hour_enabled is False, (
        "_DEFAULT_YAML cadences.hour.enabled is %r; expected False" % hour_enabled
    )


# ---------------------------------------------------------------------------
# 6. run_per_ticker_analysts() default
# ---------------------------------------------------------------------------


def test_run_per_ticker_analysts_default_is_long_hold() -> None:
    """``run_per_ticker_analysts`` must default ``mode`` to ``"long_hold"``."""
    from argosy.decisions.per_ticker_analysts import run_per_ticker_analysts

    sig = inspect.signature(run_per_ticker_analysts)
    default = sig.parameters["mode"].default
    assert default == "long_hold", (
        "run_per_ticker_analysts mode default is %r; expected 'long_hold'" % default
    )


# ---------------------------------------------------------------------------
# 7. Tactical-trade opt-in still works (no regression)
# ---------------------------------------------------------------------------


def test_run_request_can_opt_into_tactical_trade() -> None:
    """Callers must still be able to opt into ``tactical_trade`` mode explicitly."""
    from argosy.api.routes.decisions import RunRequest

    req = RunRequest(ticker="AAPL", consult_mode="tactical_trade")
    assert req.consult_mode == "tactical_trade"
