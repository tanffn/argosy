"""Argosy configuration loader.

Resolves `ARGOSY_HOME` (env var or fallback to project root) and reads
`argosy.toml`. Exposes a pydantic-settings `Settings` class with all
paths derived from the home directory.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - we require 3.12+ but keep the fallback
    import tomli as tomllib

from pydantic import BaseModel, Field
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

    # Sprint A commit #4 (BLOCKER #1) — admin auth gate for /api/jobs
    # mutating routes (`POST /api/jobs/{name}/run-now` + `/stop` + `/reconnect`).
    # Loaded from the `ARGOSY_ADMIN_TOKEN` env var. When unset, the FastAPI
    # mounter REFUSES to register the mutating routes (logs a startup
    # WARNING) — the read-only `GET /api/jobs` surface stays open for
    # monitoring. See `argosy/api/auth.py::require_admin_token`.
    admin_token: str | None = Field(default=None)

    # Phase 6 / T2.6 — when True (the default), a failing plan_output_gate
    # blocks /accept with a 422: the trust contract is ENFORCED, so a promoted
    # plan's user-facing numbers must trace to the resolver/canonical plan
    # (no fabricated headlines). Set ``ARGOSY_PLAN_GATE_ENFORCE=false`` to fall
    # back to warn-only (the violation summary surfaces on the response but the
    # accept proceeds). ``?override_gate=true`` bypasses a single accept (audited).
    plan_gate_enforce: bool = Field(default=True)

    # Phase 5 of docs/plans/argosy-comprehensive-plan-integration.md
    # — when True, PlanCoverageAnalyst and WithdrawalSequencerAgent
    # run alongside the existing Phase 1 analyst fleet. Default False
    # because both agents are deferred from MVP — live-LLM iteration
    # against real distillate output is needed to validate quality.
    # Loaded from ``ARGOSY_PHASE5_AGENTS`` env var. Recommended:
    # leave False until at least one supervised real-data run has
    # been observed and the outputs hand-checked.
    phase5_agents: bool = Field(default=False)

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

    # Sprint A commit #4 — admin token loaded directly from env so the
    # explicit-kwargs Settings(...) constructor below picks it up. The
    # SettingsConfigDict env_prefix is bypassed here because we hand-roll
    # the field assignment; routing through os.environ keeps test
    # monkeypatch.setenv working without a reload dance.
    admin_token = os.environ.get("ARGOSY_ADMIN_TOKEN") or None

    return Settings(
        home=home,
        backups_dir=backups,
        db_file=db_file,
        domain_knowledge_dir=domain_knowledge,
        configs_dir=configs,
        logs_dir=logs,
        server=ServerSettings(**server_cfg),
        anthropic=AnthropicSettings(**anthropic_cfg),
        admin_token=admin_token,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return _build_settings()


def reload_settings() -> Settings:
    """Force reload (useful in tests)."""
    get_settings.cache_clear()
    return get_settings()


# ----------------------------------------------------------------------
# Speculation cap (Wave 3 of plan-distillate work)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SpeculationCap:
    """Per-user speculation guardrails (Wave 3 of plan-distillate work).

    Loaded from agent_settings.yaml::

        speculation:
          max_pct_of_net_worth: 0.001       # 0.1% NW, very tight
          max_concurrent_positions: 3
          allowed_account_classes: ["limited"]

    The string ``"limited"`` is the DB/code account-class value that the
    routing layer (``argosy/execution/router.py``) checks; the
    *Argonaut* feature is the user-facing name for that class.

    These values constrain the synthesizer (it must never emit a
    SpeculativeCandidate that would breach the cap) AND the routing
    layer (preflight enforcement before any broker call).
    """

    max_pct_of_net_worth: float = 0.001  # 0.1% default — conservative
    max_concurrent_positions: int = 3
    allowed_account_classes: tuple[str, ...] = ("limited",)

    def validate(self) -> None:
        if self.max_pct_of_net_worth <= 0:
            raise ValueError(
                f"speculation.max_pct_of_net_worth must be > 0, got {self.max_pct_of_net_worth}"
            )
        if self.max_pct_of_net_worth > 0.05:
            raise ValueError(
                f"speculation.max_pct_of_net_worth must be <= 0.05 (5% NW); "
                f"above that it's not speculation, it's a position. Got "
                f"{self.max_pct_of_net_worth}"
            )
        if self.max_concurrent_positions < 0:
            raise ValueError(
                f"speculation.max_concurrent_positions must be >= 0, got "
                f"{self.max_concurrent_positions}"
            )


def load_speculation_cap(*, user_id: str, agent_settings: dict) -> SpeculationCap:
    """Build a SpeculationCap from a parsed agent_settings.yaml dict."""
    block = agent_settings.get("speculation") or {}
    cap = SpeculationCap(
        max_pct_of_net_worth=float(block.get("max_pct_of_net_worth", 0.001)),
        max_concurrent_positions=int(block.get("max_concurrent_positions", 3)),
        allowed_account_classes=tuple(
            block.get("allowed_account_classes", ("limited",))
        ),
    )
    cap.validate()
    return cap


# ----------------------------------------------------------------------
# Per-role agent overrides (Wave A — BaseAgent API features)
# ----------------------------------------------------------------------


class AgentRoleOverride(BaseModel):
    """Per-role override fields loaded from ``agent_settings.yaml``.

    Each field is ``None`` when unspecified, meaning "fall back to the
    per-role default baked into ``BaseAgent``". This lets the YAML be
    sparse — users only list the roles + fields they actually want to
    override.

    Fields:
      * ``thinking_effort`` — adaptive-thinking effort level (Opus 4.6+
        canonical pattern). One of ``"low" | "medium" | "high" | "max"``,
        or explicit ``null`` to disable adaptive thinking and fall back
        to ``thinking_budget`` (legacy fixed-budget mode). When unset in
        YAML, the per-role default from
        ``argosy.agents.base.DEFAULT_THINKING_EFFORT_BY_ROLE`` applies.
      * ``thinking_budget`` — legacy fixed extended-thinking token budget
        (0 disables; upper bound mirrors the Anthropic API ceiling of
        128k). Setting this WITHOUT ``thinking_effort`` is interpreted
        as opting out of adaptive thinking for the role — the fixed-
        budget path fires.
      * ``citations_enabled`` — toggle Anthropic Citations API blocks for
        this role.
    """

    model_config = {"extra": "allow"}  # tolerate future per-role fields (model, etc.)

    thinking_effort: Literal["low", "medium", "high", "max"] | None = None
    thinking_budget: int | None = Field(default=None, ge=0, le=128000)
    citations_enabled: bool | None = None


class AgentSettings(BaseModel):
    """Parsed shape of ``agent_settings.yaml`` (only the ``agents:`` block).

    Other top-level blocks (``speculation``, ``expenses``, ...) are
    handled by their own loaders; this model deliberately ignores them.
    """

    model_config = {"extra": "ignore"}

    agents: dict[str, AgentRoleOverride] = Field(default_factory=dict)

    def for_role(self, role: str) -> AgentRoleOverride:
        """Return the override for ``role``, or an empty (all-``None``) one."""
        return self.agents.get(role, AgentRoleOverride())


def load_agent_settings(path: Path) -> AgentSettings:
    """Load + validate ``agent_settings.yaml`` into an :class:`AgentSettings`.

    Missing file raises ``FileNotFoundError`` (callers that want soft
    behaviour should check ``path.exists()`` first — see
    ``resolve_agent_settings_path`` below).
    """
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AgentSettings(**raw)


def resolve_agent_settings_path(user_id: str) -> Path | None:
    """Return the path to the per-user ``agent_settings.yaml``, or ``None``.

    Lookup order:
      1. ``$ARGOSY_AGENT_SETTINGS_PATH`` env var (used by tests).
      2. ``$ARGOSY_HOME/configs/<user_id>/agent_settings.yaml``.
      3. ``None`` (no overrides applied).

    This is a thin lookup — callers must still check ``path.exists()``
    before reading; missing files are a normal, expected case (most
    users won't write any overrides at all). The ``None`` branch is
    reserved for environments where ``ARGOSY_HOME`` is unset and we have
    no sensible per-user dir to probe.
    """
    env = os.environ.get("ARGOSY_AGENT_SETTINGS_PATH")
    if env:
        return Path(env)
    home = os.environ.get("ARGOSY_HOME") or "."
    return Path(home) / "configs" / user_id / "agent_settings.yaml"


def get_user_agent_settings(user_id: str) -> dict:
    """Read configs/<user_id>/agent_settings.yaml. Returns empty dict if missing.

    ADAPTATION: the existing settings model already exposes a tailored
    helper at ``Settings.agent_settings_path(user_id)`` (line 110-112),
    so we delegate there rather than rebuilding the path from
    ``argosy_home`` + ``configs`` literals.  Falls back to an empty dict
    when the file is absent or empty so callers can rely on
    ``load_speculation_cap`` defaulting cleanly.
    """
    import yaml

    path = get_settings().agent_settings_path(user_id)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ----------------------------------------------------------------------
# Expenses config (household-expenses subsystem, Wave A)
# ----------------------------------------------------------------------


class ExpensesCategorizationConfig(BaseModel):
    confidence_threshold: float = 0.85
    llm_batch_size: int = 50
    llm_model_override: str | None = None


class ExpensesCorrelationConfig(BaseModel):
    amount_tolerance_nis: float = 50.0
    date_window_days: int = 2
    bank_row_keywords_he: list[str] = Field(default_factory=lambda: [
        "ל.מאסטרקרד", "כרטיסי אשראי", "ויזה", "דיינרס", "אמריקן אקספרס",
    ])


class ExpensesRefundMatcherConfig(BaseModel):
    amount_tolerance_pct: float = 0.05
    lookback_days: int = 90


class ExpensesAnomalyConfig(BaseModel):
    mom_category_factor: float = 1.5
    mom_category_min_baseline_nis: float = 500.0
    recurring_price_jump_pct: float = 15.0
    recurring_missed_after_days: int = 7
    new_recurring_after_n_months: int = 3
    big_one_off_nis: float = 3000.0
    coverage_gap_days: int = 35
    suppress_acknowledged_for_months: int = 3


class ExpensesParsersConfig(BaseModel):
    leumi_osh: bool = True
    isracard: bool = True
    max: bool = True
    cal: bool = False
    amex: bool = False
    diners: bool = False
    discount: bool = True   # Discount Bank Mastercard — fully implemented


class ExpensesConfig(BaseModel):
    enabled: bool = True
    parsers: ExpensesParsersConfig = Field(default_factory=ExpensesParsersConfig)
    categorization: ExpensesCategorizationConfig = Field(
        default_factory=ExpensesCategorizationConfig
    )
    correlation: ExpensesCorrelationConfig = Field(
        default_factory=ExpensesCorrelationConfig
    )
    refund_matcher: ExpensesRefundMatcherConfig = Field(
        default_factory=ExpensesRefundMatcherConfig
    )
    anomaly: ExpensesAnomalyConfig = Field(default_factory=ExpensesAnomalyConfig)


def load_expenses_config(user_id: str) -> ExpensesConfig:
    """Load expenses config from configs/<user_id>/agent_settings.yaml.
    Missing file or missing 'expenses' block → all defaults.
    """
    import yaml

    settings = get_settings()
    cfg_path = settings.agent_settings_path(user_id)
    if not cfg_path.exists():
        return ExpensesConfig()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    block = raw.get("expenses") or {}
    return ExpensesConfig.model_validate(block)
