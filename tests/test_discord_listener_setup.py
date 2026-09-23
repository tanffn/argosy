import getpass
import json
import warnings

from typer.testing import CliRunner

from argosy import secrets
from argosy.cli import discord_listener
from argosy.cli.main import app
from argosy.services.discord_listener import load_creds

TOKEN = "MT" + "x" * 65


def configure(monkeypatch):
    stored = {"discord_advisor_bot_token": "untouched"}
    monkeypatch.setattr(secrets, "get_secret", stored.get)
    monkeypatch.setattr(secrets, "set_secret", lambda key, value: stored.update({key: value}))
    monkeypatch.setattr(discord_listener.getpass, "getpass", lambda _: TOKEN)
    return stored


def test_root_cli_setup_roundtrip_and_token_isolation(tmp_path, monkeypatch):
    stored = configure(monkeypatch)
    target = tmp_path / "creds.json"
    result = CliRunner().invoke(app, ["discord-listener", "setup", "--guild-id", "123",
                                    "--channel-id", "456", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert TOKEN not in result.output
    assert TOKEN not in target.read_text()
    assert load_creds(target).bot_token == TOKEN
    assert load_creds(target).server_id == 123
    assert stored == {"discord_advisor_bot_token": "untouched", "discord_listener_bot_token": TOKEN}
    assert "No connection made" in result.output


def test_interactive_ids(tmp_path, monkeypatch):
    configure(monkeypatch)
    target = tmp_path / "creds.json"
    result = CliRunner().invoke(discord_listener.app, ["setup", "--path", str(target)], input="123\n456\n")
    assert result.exit_code == 0, result.output
    assert load_creds(target).channel_id == 456


def test_refresh_reuses_ids_and_migrates_plaintext(tmp_path, monkeypatch):
    configure(monkeypatch)
    target = tmp_path / "creds.json"
    target.write_text(json.dumps({"bot_token": TOKEN, "server_id": 123, "channel_id": 456, "note": "preserve"}))
    monkeypatch.setattr(discord_listener.getpass, "getpass", lambda _: "")
    result = CliRunner().invoke(discord_listener.app, ["setup", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert "bot_token" not in json.loads(target.read_text())
    assert json.loads(target.read_text())["note"] == "preserve"
    assert load_creds(target).bot_token == TOKEN


def test_mismatched_binding_rejected_before_token_prompt(tmp_path, monkeypatch):
    stored = configure(monkeypatch)
    target = tmp_path / "creds.json"
    original = json.dumps({"server_id": 123, "channel_id": 456})
    target.write_text(original)
    monkeypatch.setattr(discord_listener.getpass, "getpass", lambda _: (_ for _ in ()).throw(AssertionError()))
    result = CliRunner().invoke(discord_listener.app, ["setup", "--guild-id", "321", "--path", str(target)])
    assert result.exit_code != 0
    assert "Saved source differs" in result.output
    assert target.read_text() == original
    assert "discord_listener_bot_token" not in stored


def test_hidden_input_unavailable_fails_closed(tmp_path, monkeypatch):
    stored = configure(monkeypatch)
    def unsafe(_):
        warnings.warn("Cannot hide input", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("must not fall through to visible input")
    monkeypatch.setattr(discord_listener.getpass, "getpass", unsafe)
    target = tmp_path / "creds.json"
    result = CliRunner().invoke(discord_listener.app, ["setup", "--guild-id", "123", "--channel-id", "456", "--path", str(target)])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output
    assert not target.exists()
    assert "discord_listener_bot_token" not in stored


def test_keychain_failure_does_not_echo_secret_or_write_config(tmp_path, monkeypatch):
    configure(monkeypatch)
    def fail(*_):
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(secrets, "set_secret", fail)
    target = tmp_path / "creds.json"
    result = CliRunner().invoke(discord_listener.app, ["setup", "--guild-id", "123", "--channel-id", "456", "--path", str(target)])
    assert result.exit_code != 0
    assert TOKEN not in result.output
    assert not target.exists()


def test_invalid_id_does_not_store_token(tmp_path, monkeypatch):
    stored = configure(monkeypatch)
    result = CliRunner().invoke(discord_listener.app, ["setup", "--guild-id", "0", "--channel-id", "456", "--path", str(tmp_path / "creds.json")])
    assert result.exit_code != 0
    assert "discord_listener_bot_token" not in stored


def test_missing_keychain_token_never_falls_back_to_stale_plaintext(tmp_path, monkeypatch):
    configure(monkeypatch)
    target = tmp_path / "creds.json"
    target.write_text(json.dumps({"token_secret": "discord_listener_bot_token", "bot_token": TOKEN,
                                  "server_id": 123, "channel_id": 456}))
    import pytest
    with pytest.raises(ValueError, match="keychain token missing"):
        load_creds(target)
