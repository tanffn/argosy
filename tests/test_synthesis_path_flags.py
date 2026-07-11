"""Patch/sliced synthesis flags — default ON + kill switch.

Production defaults flipped ON after live acceptance; tests force OFF via
conftest autouse. These units exercise the resolver directly (env + Settings).
"""

from __future__ import annotations

import pytest


def test_env_flag_default_on_from_settings(monkeypatch):
    from argosy.config import reload_settings
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _env_flag_on,
    )

    monkeypatch.delenv("ARGOSY_CORRECTIVE_PATCH", raising=False)
    monkeypatch.delenv("ARGOSY_SLICED_SYNTH", raising=False)
    reload_settings()
    assert _env_flag_on(
        "ARGOSY_CORRECTIVE_PATCH", settings_attr="corrective_patch",
    ) is True
    assert _env_flag_on(
        "ARGOSY_SLICED_SYNTH", settings_attr="sliced_synth",
    ) is True
    settings = reload_settings()
    assert settings.corrective_patch is True
    assert settings.sliced_synth is True


@pytest.mark.parametrize("env_name,attr", [
    ("ARGOSY_CORRECTIVE_PATCH", "corrective_patch"),
    ("ARGOSY_SLICED_SYNTH", "sliced_synth"),
])
@pytest.mark.parametrize("off_value", ["0", "false", "off", "no"])
def test_env_flag_kill_switch(monkeypatch, env_name, attr, off_value):
    from argosy.config import reload_settings
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _env_flag_on,
    )

    monkeypatch.setenv(env_name, off_value)
    reload_settings()
    assert _env_flag_on(env_name, settings_attr=attr, default=True) is False


@pytest.mark.parametrize("env_name,attr", [
    ("ARGOSY_CORRECTIVE_PATCH", "corrective_patch"),
    ("ARGOSY_SLICED_SYNTH", "sliced_synth"),
])
def test_env_flag_explicit_on(monkeypatch, env_name, attr):
    from argosy.config import reload_settings
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _env_flag_on,
    )

    monkeypatch.setenv(env_name, "1")
    reload_settings()
    assert _env_flag_on(env_name, settings_attr=attr, default=False) is True
