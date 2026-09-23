import json

from typer.testing import CliRunner

from argosy import secrets
from argosy.cli import discord_advisor


def test_setup_stores_secret_locally_without_echoing_it(tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(discord_advisor.getpass, "getpass", lambda _prompt: "test-only-do-not-echo-token")
    monkeypatch.setattr(secrets, "set_secret", lambda key, value: stored.update({key: value}))
    target = tmp_path / "advisor.json"
    result = CliRunner().invoke(discord_advisor.app, [
        "setup", "--guild-id", "1", "--channel-id", "2", "--discord-user-id", "3",
        "--user-id", "owner", "--path", str(target),
    ])
    assert result.exit_code == 0, result.output
    assert "test-only-do-not-echo-token" not in result.output
    assert "test-only-do-not-echo-token" not in target.read_text()
    assert stored == {"discord_advisor_bot_token": "test-only-do-not-echo-token"}
    assert json.loads(target.read_text())["enabled"] is True


def test_invalid_identity_is_rejected_before_token_prompt(tmp_path, monkeypatch):
    def forbidden(_prompt):
        raise AssertionError("Should validate configuration before asking for a secret")
    monkeypatch.setattr(discord_advisor.getpass, "getpass", forbidden)
    target = tmp_path / "advisor.json"
    result = CliRunner().invoke(discord_advisor.app, [
        "setup", "--guild-id", "not-an-id", "--channel-id", "2", "--discord-user-id", "3",
        "--user-id", "owner", "--path", str(target),
    ])
    assert result.exit_code != 0
    assert not target.exists()


def test_token_refresh_preserves_status_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_advisor.getpass, "getpass", lambda _: "replacement")
    monkeypatch.setattr(secrets, "set_secret", lambda *args: None)
    target = tmp_path / "advisor.json"
    target.write_text(json.dumps({"enabled": True, "bindings": [{
        "guild_id": "1", "channel_id": "2", "status_channel_id": "4",
        "user_id": "3", "household_user_id": "owner",
    }]}))
    result = CliRunner().invoke(discord_advisor.app, [
        "setup", "--guild-id", "1", "--channel-id", "2", "--discord-user-id", "3",
        "--user-id", "owner", "--path", str(target),
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())["bindings"][0]["status_channel_id"] == "4"
