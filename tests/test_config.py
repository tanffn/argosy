"""Verify config loading and path derivation."""

from __future__ import annotations

from pathlib import Path

from argosy.config import reload_settings, resolve_home


def test_resolve_home_finds_argosy_toml() -> None:
    home = resolve_home()
    assert (home / "argosy.toml").is_file(), "argosy.toml should exist at resolved home"


def test_settings_paths_are_under_home() -> None:
    settings = reload_settings()
    home: Path = settings.home
    # All path fields should be absolute and (by default) live under home.
    assert settings.backups_dir.is_absolute()
    assert settings.db_file.is_absolute()
    assert settings.logs_dir.is_absolute()
    assert settings.configs_dir.is_absolute()
    assert settings.domain_knowledge_dir.is_absolute()
    # Default config places these inside home.
    assert str(settings.backups_dir).startswith(str(home))
    assert str(settings.db_file).startswith(str(home))
    assert str(settings.logs_dir).startswith(str(home))


def test_database_url_is_async_sqlite() -> None:
    settings = reload_settings()
    assert settings.database_url.startswith("sqlite+aiosqlite:///")


def test_server_defaults() -> None:
    settings = reload_settings()
    assert settings.server.ui_port == 1337
    assert settings.server.api_port == 8000
