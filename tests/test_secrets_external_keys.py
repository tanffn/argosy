"""External-API-key file loader (``~/.argosy/external_api_keys.json``).

Mirrors the Discord-creds pattern (``~/.argosy/discord_creds.json``): a
single JSON object the user maintains by hand, read by adapters as a
fallback when the keychain and env var aren't set. Lets the user keep
their Finnhub / FRED / etc. keys on disk without depending on the OS
keychain backend or putting them in the launcher env.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argosy.secrets import get_external_api_key


def test_missing_file_returns_none(tmp_path, monkeypatch):
    """No file → returns None (no exception, no log spam). Caller falls
    through to the next layer in its lookup chain."""
    monkeypatch.setattr(
        "argosy.secrets._external_keys_path",
        lambda: tmp_path / "external_api_keys.json",
    )
    assert get_external_api_key("finnhub") is None


def test_returns_key_when_present(tmp_path, monkeypatch):
    p = tmp_path / "external_api_keys.json"
    p.write_text(
        json.dumps({"finnhub": "fnh-test-12345", "fred": "fred-test-67890"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)

    assert get_external_api_key("finnhub") == "fnh-test-12345"
    assert get_external_api_key("fred") == "fred-test-67890"


def test_returns_none_when_provider_absent(tmp_path, monkeypatch):
    """File exists, has SOME providers, but not the one we asked for —
    return None so the caller falls through, no error."""
    p = tmp_path / "external_api_keys.json"
    p.write_text(json.dumps({"fred": "fred-test"}), encoding="utf-8")
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)

    assert get_external_api_key("finnhub") is None
    assert get_external_api_key("fred") == "fred-test"


def test_malformed_json_raises(tmp_path, monkeypatch):
    """File exists but isn't valid JSON — raise ValueError so the user
    sees a loud error rather than silent fallthrough to MissingAPIKeyError
    (which would point them at the wrong layer)."""
    p = tmp_path / "external_api_keys.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)

    with pytest.raises(ValueError, match="not valid JSON"):
        get_external_api_key("finnhub")


def test_non_object_top_level_raises(tmp_path, monkeypatch):
    """File contains a JSON array / string / number at the top level
    instead of an object — raise ValueError. Same loudness rationale."""
    p = tmp_path / "external_api_keys.json"
    p.write_text(json.dumps(["finnhub-key"]), encoding="utf-8")
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)

    with pytest.raises(ValueError, match="JSON object at top level"):
        get_external_api_key("finnhub")


def test_empty_or_non_string_value_raises(tmp_path, monkeypatch):
    """Provider key is present but value is "", null, or non-string —
    raise so we don't pass garbage into the adapter (better to point the
    user at the file)."""
    p = tmp_path / "external_api_keys.json"

    p.write_text(json.dumps({"finnhub": ""}), encoding="utf-8")
    monkeypatch.setattr("argosy.secrets._external_keys_path", lambda: p)
    with pytest.raises(ValueError, match="non-empty string"):
        get_external_api_key("finnhub")

    p.write_text(json.dumps({"finnhub": "   "}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty string"):
        get_external_api_key("finnhub")

    p.write_text(json.dumps({"finnhub": 12345}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty string"):
        get_external_api_key("finnhub")


def test_path_resolves_under_argosy_home_dir():
    """Default path is ``~/.argosy/external_api_keys.json``. Matches the
    Discord-creds convention so users have one place to look."""
    from argosy.secrets import _external_keys_path

    p = _external_keys_path()
    assert p.name == "external_api_keys.json"
    assert p.parent.name == ".argosy"
    # Resolves to a path under the user's home directory.
    import os
    assert str(p).startswith(os.path.expanduser("~"))
