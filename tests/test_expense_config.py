"""Tests for ExpensesConfig loader."""

from pathlib import Path

import pytest
import yaml

from argosy.config import load_expenses_config, ExpensesConfig, reload_settings


def test_load_expenses_config_returns_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    cfg = load_expenses_config(user_id="ariel")
    assert isinstance(cfg, ExpensesConfig)
    assert cfg.categorization.confidence_threshold == 0.85
    assert cfg.correlation.amount_tolerance_nis == 50
    assert cfg.refund_matcher.lookback_days == 90
    assert cfg.anomaly.mom_category_factor == 1.5


def test_load_expenses_config_respects_user_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    reload_settings()  # Clear the settings cache
    cfg_path = tmp_path / "configs" / "ariel" / "agent_settings.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(yaml.safe_dump({
        "expenses": {
            "categorization": {"confidence_threshold": 0.90},
            "anomaly": {"mom_category_factor": 2.0},
        }
    }))
    cfg = load_expenses_config(user_id="ariel")
    assert cfg.categorization.confidence_threshold == 0.90
    assert cfg.anomaly.mom_category_factor == 2.0
    assert cfg.correlation.amount_tolerance_nis == 50
