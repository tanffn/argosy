"""Argosy configuration loader.

Resolves `ARGOSY_HOME` (env var or fallback to project root) and reads
`argosy.toml`. Exposes a pydantic-settings `Settings` class with all
paths derived from the home directory.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - we require 3.12+ but keep the fallback
    import tomli as tomllib

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Repo root: directory containing argosy.toml, walking up from this file."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "argosy.toml").is_file():
            return candidate
    # Fallback: parent of the `argosy` package.
    return Path(__file__).resolve().parent.parent


def resolve_home() -> Path:
    """ARGOSY_HOME if set, else the project root (containing argosy.toml)."""
    env = os.environ.get("ARGOSY_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return _project_root()


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _resolve_path(value: str, home: Path) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    return (home / p).resolve()


class ServerSettings(BaseSettings):
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    ui_port: int = 1337


class AnthropicSettings(BaseSettings):
    keychain_key_name: str = "argosy.anthropic.api_key"
    # Backend selector for BaseAgent._call_model.
    #   "claude_code" — auth via the local `claude.exe` session (Claude Agent SDK).
    #                   No API key needed; cost lands on the user's Claude Code
    #                   subscription. Default — works out of the box.
    #   "api_key"     — direct Anthropic API via `anthropic` SDK; reads the key
    #                   from the OS keychain or `ANTHROPIC_API_KEY` env var.
    # Switchable per-environment via `argosy.toml [anthropic] backend = ...` or
    # via the `ARGOSY_ANTHROPIC__BACKEND` env var.
    backend: str = "claude_code"


class Settings(BaseSettings):
    """Argosy runtime settings.

    Path fields are absolute, resolved against ARGOSY_HOME.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARGOSY_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    home: Path = Field(default_factory=resolve_home)
    backups_dir: Path = Field(default_factory=lambda: resolve_home() / "backups")
    db_file: Path = Field(default_factory=lambda: resolve_home() / "db" / "argosy.db")
    domain_knowledge_dir: Path = Field(
        default_factory=lambda: resolve_home() / "domain_knowledge"
    )
    configs_dir: Path = Field(default_factory=lambda: resolve_home() / "configs")
    logs_dir: Path = Field(default_factory=lambda: resolve_home() / "logs")

    server: ServerSettings = Field(default_factory=ServerSettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)

    @property
    def app_log_file(self) -> Path:
        return self.logs_dir / "app" / "application.log"

    @property
    def database_url(self) -> str:
        # SQLAlchemy async URL for aiosqlite.
        return f"sqlite+aiosqlite:///{self.db_file.as_posix()}"

    def agent_settings_path(self, user_id: str) -> Path:
        """Per-user agent_settings.yaml path. See SDD Appendix A.2."""
        return self.configs_dir / user_id / "agent_settings.yaml"


def _build_settings() -> Settings:
    home = resolve_home()
    toml = _load_toml(home / "argosy.toml")

    paths = toml.get("paths", {}) or {}
    server_cfg = toml.get("server", {}) or {}
    anthropic_cfg = toml.get("anthropic", {}) or {}

    # `home` in toml is informational; we always trust ARGOSY_HOME / project root.
    backups = _resolve_path(paths.get("backups", "./backups"), home)
    db_file = _resolve_path(paths.get("db_file", "./db/argosy.db"), home)
    domain_knowledge = _resolve_path(
        paths.get("domain_knowledge", "./domain_knowledge"), home
    )
    configs = _resolve_path(paths.get("configs", "./configs"), home)
    logs = _resolve_path(paths.get("logs", "./logs"), home)

    return Settings(
        home=home,
        backups_dir=backups,
        db_file=db_file,
        domain_knowledge_dir=domain_knowledge,
        configs_dir=configs,
        logs_dir=logs,
        server=ServerSettings(**server_cfg),
        anthropic=AnthropicSettings(**anthropic_cfg),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return _build_settings()


def reload_settings() -> Settings:
    """Force reload (useful in tests)."""
    get_settings.cache_clear()
    return get_settings()
