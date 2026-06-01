"""Pin the API-key lookup chain for the Finnhub + FRED adapters.

Chain (top → bottom):

  1. ``argosy.secrets.get_secret(KEYCHAIN_KEY)`` — OS keychain.
  2. ``os.environ[ENV_VAR]`` — env var override.
  3. ``argosy.secrets.get_external_api_key(provider)`` — ``~/.argosy/external_api_keys.json``.
  4. ``MissingAPIKeyError``.

Both adapters expose ``_resolve_api_key`` so we drive each layer in turn
and assert the right value lands. Wave-4 incident background: keys went
missing from the keychain on 2026-05-30, neither env nor a file path
existed, three plan-revision cycles ran with empty news/macro/fundamentals
inputs and the FM was the only agent that caught it.
"""

from __future__ import annotations

import json

import pytest

from argosy.adapters import MissingAPIKeyError
from argosy.adapters.data import finnhub_adapter as fnh
from argosy.adapters.data import fred_adapter as fred


@pytest.fixture
def patch_external_keys_path(tmp_path, monkeypatch):
    """Point ``_external_keys_path`` at a tmp file the test can write."""
    p = tmp_path / "external_api_keys.json"
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)
    return p


# ----------------------------------------------------------------------
# Finnhub
# ----------------------------------------------------------------------


def test_finnhub_uses_keychain_first(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fnh, "get_secret", lambda _k: "from-keychain")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    patch_external_keys_path.write_text(
        json.dumps({"finnhub": "from-file"}), encoding="utf-8"
    )
    assert fnh._resolve_api_key() == "from-keychain"


def test_finnhub_falls_through_to_env_var(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fnh, "get_secret", lambda _k: None)
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env")
    patch_external_keys_path.write_text(
        json.dumps({"finnhub": "from-file"}), encoding="utf-8"
    )
    assert fnh._resolve_api_key() == "from-env"


def test_finnhub_falls_through_to_file(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fnh, "get_secret", lambda _k: None)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    patch_external_keys_path.write_text(
        json.dumps({"finnhub": "from-file"}), encoding="utf-8"
    )
    assert fnh._resolve_api_key() == "from-file"


def test_finnhub_raises_when_nothing_set(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fnh, "get_secret", lambda _k: None)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    # File missing — patch_external_keys_path points at a non-existent path.
    with pytest.raises(MissingAPIKeyError):
        fnh._resolve_api_key()


# ----------------------------------------------------------------------
# FRED
# ----------------------------------------------------------------------


def test_fred_uses_keychain_first(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fred, "get_secret", lambda _k: "from-keychain")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    patch_external_keys_path.write_text(
        json.dumps({"fred": "from-file"}), encoding="utf-8"
    )
    assert fred._resolve_api_key() == "from-keychain"


def test_fred_falls_through_to_env_var(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fred, "get_secret", lambda _k: None)
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    patch_external_keys_path.write_text(
        json.dumps({"fred": "from-file"}), encoding="utf-8"
    )
    assert fred._resolve_api_key() == "from-env"


def test_fred_falls_through_to_file(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fred, "get_secret", lambda _k: None)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    patch_external_keys_path.write_text(
        json.dumps({"fred": "from-file"}), encoding="utf-8"
    )
    assert fred._resolve_api_key() == "from-file"


def test_fred_raises_when_nothing_set(monkeypatch, patch_external_keys_path):
    monkeypatch.setattr(fred, "get_secret", lambda _k: None)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError):
        fred._resolve_api_key()
