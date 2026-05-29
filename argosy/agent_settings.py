"""Per-user `agent_settings.yaml` loader (SDD Appendix A.2).

Phase 2 introduces the orchestrator + cadences. Cadence schedules,
execution mode, model overrides per agent role, and tier thresholds all
live in `${ARGOSY_HOME}/configs/<user_id>/agent_settings.yaml`. We
provide a typed `AgentSettings` pydantic model + loader.

If the file is missing, `load_agent_settings(user_id)` writes a default
copy (from the bundled `configs/example/agent_settings.yaml` template if
present, else from the in-code default) and returns it. The example
template is committed to the repo and provides a self-documenting
starting point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from argosy.config import get_settings


# ----------------------------------------------------------------------
# Pydantic shape
# ----------------------------------------------------------------------


class CadenceConfig(BaseModel):
    """One cadence loop's configuration. All fields optional; the
    scheduler treats absent fields as their defaults.

    Either `cron` or `interval_seconds`/`interval_minutes` should be set.
    `market_hours_only` defaults False; only the minute loop sets it True.
    """

    enabled: bool = True
    market_hours_only: bool = False
    cron: str | None = None
    interval_seconds: int | None = None
    interval_minutes: int | None = None
    timezone: str = "Asia/Jerusalem"


class CadencesBlock(BaseModel):
    minute: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, market_hours_only=True, interval_seconds=60
        )
    )
    hour: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, interval_minutes=60)
    )
    daily_brief: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="0 9 * * *", timezone="Asia/Jerusalem"
        )
    )
    weekly_review: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="0 18 * * SUN")
    )
    monthly_cycle: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="0 8 1 * *")
    )
    quarterly: CadenceConfig = Field(default_factory=lambda: CadenceConfig(enabled=True))
    annual: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="0 8 2 1 *")
    )
    backup: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="0 3 * * *")
    )
    audit: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="0 19 * * SUN")
    )
    watchlist: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(enabled=True, cron="30 8 * * *")
    )
    plan_watcher: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="0 7 * * *", timezone="Asia/Jerusalem"
        )
    )
    # Sprint A commit #7 — news pipeline daily cadence.
    # 17:00 IL-local: Stage 1 ingest (deterministic extractor) + Stage 2
    # analyst (Opus). 17:00 IDT = 14:00 UTC (summer) / 15:00 UTC (winter).
    # Timezone resolution lives in commit #2's croniter fix.
    news_daily: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="0 17 * * *", timezone="Asia/Jerusalem"
        )
    )
    # Sprint A commit #9 — job_runs retention + orphan reap. 03:30
    # Asia/Jerusalem daily (30 minutes after the backup loop's 03:00
    # snapshot so the backup captures the pre-retention state).
    # Window-tuning fields live on ``JobRunsRetentionConfig`` below
    # (cron/tz here; days/hours there — matches the pattern in
    # ``BackupsBlock`` where ``cadences.backup`` is the schedule and
    # ``backups`` is the retention/copy config).
    job_runs_retention: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="30 3 * * *", timezone="Asia/Jerusalem"
        )
    )
    # Sprint B commit #7 — state-observer daily cron. 17:00 Asia/Jerusalem,
    # same hour as ``news_daily`` so the observer reads a fully-settled
    # state (Tel Aviv market closed + news pipeline classified the day's
    # signals). Cool-off + force-flag tuning lives on the loop class
    # constructor, not in the cadence config — the cadence config only
    # owns the schedule.
    state_observer: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="0 17 * * *", timezone="Asia/Jerusalem"
        )
    )
    # Sprint C commit #4 — predictions outcome evaluator + retention pass.
    # 03:30 Asia/Jerusalem daily, same slot as ``job_runs_retention``
    # (they touch disjoint rows so the schedule overlap is intentional;
    # one less cron entry for the operator to track). Batch size,
    # retention windows, and price-fetcher injection live on the loop
    # class constructor — the cadence config only owns the schedule.
    predictions_evaluator: CadenceConfig = Field(
        default_factory=lambda: CadenceConfig(
            enabled=True, cron="30 3 * * *", timezone="Asia/Jerusalem"
        )
    )


class JobRunsRetentionConfig(BaseModel):
    """Sprint A commit #9 — ``job_runs`` retention/reap tuning.

    Sits alongside ``cadences.job_runs_retention`` (the cron/tz/enabled
    schedule) — same split as ``cadences.backup`` vs ``BackupsBlock``.

    ``retention_days_ok``: delete ``status='ok'`` rows whose
    ``finished_at`` is older than this many days. ``status='error'``
    rows are NEVER deleted by retention (operator needs the historical
    failure trail).

    ``stale_running_hours``: flip ``status='running' AND started_at < now
    - this_many_hours`` to ``status='cancelled'``. The 24h default is
    wide on purpose: a LongRunningJob's connect cycle row genuinely
    stays in ``running`` for the connection's lifetime; a tighter
    window would mis-flip healthy long-running rows. See the loop
    docstring at ``argosy/orchestrator/loops/job_runs_retention.py``
    for the cooperation contract with the supervisor.
    """

    retention_days_ok: int = 30
    stale_running_hours: int = 24


class BackupsBlock(BaseModel):
    """Backup retention + offsite copy config (SDD §14.4)."""

    enabled: bool = True
    backups_dir: str = ""
    offsite_path: str = ""
    retention_daily: int = 30
    retention_weekly: int = 12
    retention_monthly: int = 12


class CostBlock(BaseModel):
    """Claude monthly budget + pause threshold (SDD §14.7)."""

    monthly_budget_usd: float = 100.0
    alert_at_pct: float = 80.0
    pause_at_pct: float = 100.0


class AlertsBlock(BaseModel):
    """Alert channel configuration (SDD §11.1 row 10)."""

    email_enabled: bool = True
    email_to: str = ""
    telegram_enabled: bool = False
    telegram_chat_id: str = ""


class ExecutionBlock(BaseModel):
    default_mode: Literal["paper", "live", "queue_only"] = "paper"


class ModelsBlock(BaseModel):
    defaults: dict[str, str] = Field(
        default_factory=lambda: {
            "fundamentals": "sonnet",
            "technical": "haiku",
            "news": "sonnet",
            "sentiment": "haiku",
            "macro": "sonnet",
            "plan_critique": "sonnet",
            "concentration": "haiku",
            "tax": "sonnet",
            "fx": "haiku",
            "trader": "opus",
            "intake": "sonnet",
        }
    )
    override: dict[str, str] = Field(default_factory=dict)


class TiersBlock(BaseModel):
    t0_max_portfolio_pct: float = 0.1
    t1_max_portfolio_pct: float = 1.0
    t2_max_portfolio_pct: float = 5.0
    cooling_off_hours_t3: int = 24
    account_scoped_escalation_pct: float = 20.0
    override_mode: str = "auto"


class LimitedAccountBlock(BaseModel):
    """Argonaut limited-account configuration (SDD A.2).

    Phase 5 wires bounded autonomy: T0/T1 in this account auto-execute,
    while T2/T3 still go to the human queue. `account_id` is the IBKR
    account identifier; `execution_mode` overrides the global default for
    *this* account so the user can run paper Argonaut while main accounts
    are queue_only, etc.
    """

    size_usd: float = 1000.0
    account_id: str = ""
    execution_mode: Literal["paper", "live", "queue_only"] = "paper"
    per_decision_max_pct: float = 20.0
    daily_loss_limit_pct: float = 5.0


class SecurityBlock(BaseModel):
    """Phase 5 second-factor configuration for T3 approvals.

    `t3_second_factor`:
      - "totp"  → require a valid TOTP code (header X-TOTP-Code)
      - "delay" → require a 1h cooling-off after first approve before
                  the order is committed (cheaper UX for solo operation)
    """

    t3_second_factor: Literal["totp", "delay"] = "delay"
    delay_minutes: int = 60


class AgentSettings(BaseModel):
    """Top-level model for `agent_settings.yaml`. See SDD A.2."""

    execution: ExecutionBlock = Field(default_factory=ExecutionBlock)
    cadences: CadencesBlock = Field(default_factory=CadencesBlock)
    models: ModelsBlock = Field(default_factory=ModelsBlock)
    tiers: TiersBlock = Field(default_factory=TiersBlock)
    limited_account: LimitedAccountBlock = Field(default_factory=LimitedAccountBlock)
    security: SecurityBlock = Field(default_factory=SecurityBlock)
    # Phase 7 additions
    backups: BackupsBlock = Field(default_factory=BackupsBlock)
    cost: CostBlock = Field(default_factory=CostBlock)
    alerts: AlertsBlock = Field(default_factory=AlertsBlock)
    # Sprint A commit #9 — job_runs retention/reap window-tuning.
    job_runs_retention: JobRunsRetentionConfig = Field(
        default_factory=JobRunsRetentionConfig
    )

    def model_for_role(self, role: str) -> str | None:
        """Resolve the configured model for an agent role.

        Override semantics:
          - `override.all` wins for every role
          - `override[role]` wins for that specific role
          - else `defaults[role]`
          - else None (caller picks its own fallback)
        """
        ov = self.models.override
        if "all" in ov:
            return ov["all"]
        if role in ov:
            return ov[role]
        return self.models.defaults.get(role)


# ----------------------------------------------------------------------
# Loader
# ----------------------------------------------------------------------


_DEFAULT_YAML = """\
# Auto-generated default agent_settings.yaml (Argosy Phase 2).
# Replace values per your preferences.

execution:
  default_mode: paper

cadences:
  minute:              { enabled: true, market_hours_only: true, interval_seconds: 60 }
  hour:                { enabled: true, interval_minutes: 60 }
  daily_brief:         { enabled: true, cron: "0 9 * * *", timezone: "Asia/Jerusalem" }
  weekly_review:       { enabled: true, cron: "0 18 * * SUN" }
  monthly_cycle:       { enabled: true, cron: "0 8 1 * *" }
  quarterly:           { enabled: true }
  annual:              { enabled: true, cron: "0 8 2 1 *" }
  backup:              { enabled: true, cron: "0 3 * * *" }
  audit:               { enabled: true, cron: "0 19 * * SUN" }
  # Sprint A commit #9 — daily job_runs retention/reap (03:30 IL-local;
  # 30 minutes after the backup cron so the backup captures the
  # pre-retention state).
  job_runs_retention:  { enabled: true, cron: "30 3 * * *", timezone: "Asia/Jerusalem" }
  # Sprint B commit #7 — daily state-observer (17:00 IL-local; same
  # hour as news_daily so the snapshot reads a fully-settled state).
  state_observer:      { enabled: true, cron: "0 17 * * *", timezone: "Asia/Jerusalem" }

models:
  defaults:
    fundamentals: sonnet
    technical: haiku
    news: sonnet
    sentiment: haiku
    macro: sonnet
    plan_critique: sonnet
    concentration: haiku
    tax: sonnet
    fx: haiku
    trader: opus
    intake: sonnet
  override: {}

tiers:
  t0_max_portfolio_pct: 0.1
  t1_max_portfolio_pct: 1.0
  t2_max_portfolio_pct: 5.0
  cooling_off_hours_t3: 24
  account_scoped_escalation_pct: 20
  override_mode: auto

limited_account:
  size_usd: 1000
  account_id: ""
  execution_mode: paper
  per_decision_max_pct: 20
  daily_loss_limit_pct: 5

security:
  t3_second_factor: delay
  delay_minutes: 60

backups:
  enabled: true
  backups_dir: ""
  offsite_path: ""
  retention_daily: 30
  retention_weekly: 12
  retention_monthly: 12

cost:
  monthly_budget_usd: 100
  alert_at_pct: 80
  pause_at_pct: 100

alerts:
  email_enabled: true
  email_to: ""
  telegram_enabled: false
  telegram_chat_id: ""

# Sprint A commit #9 — job_runs retention window tuning.
# ``retention_days_ok``: delete status='ok' rows older than this many
# days. status='error' rows are kept forever regardless.
# ``stale_running_hours``: flip status='running' rows older than this
# to status='cancelled' (orphan reap). LongRunningJob rows are excluded
# from the reap so a healthy long-lived connect cycle isn't mis-flipped.
job_runs_retention:
  retention_days_ok: 30
  stale_running_hours: 24
"""


def load_agent_settings(user_id: str) -> AgentSettings:
    """Return AgentSettings for a user.

    If the per-user file is missing, the default YAML is written there so
    the user has a discoverable starting point. The function never raises
    on parse errors — corrupt files yield defaults plus a stderr warning;
    callers are expected to be resilient (the scheduler must not crash on
    a malformed config).
    """
    settings = get_settings()
    path = settings.agent_settings_path(user_id)

    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_YAML, encoding="utf-8")
        return AgentSettings()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:  # pragma: no cover - defensive
        return AgentSettings()
    if not isinstance(data, dict):
        return AgentSettings()

    try:
        return AgentSettings.model_validate(data)
    except Exception:  # pragma: no cover - defensive
        return AgentSettings()


def write_default_agent_settings(path: Path) -> None:
    """Write the default YAML to `path`, ensuring the parent dir exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_YAML, encoding="utf-8")


def save_agent_settings(user_id: str, settings: AgentSettings) -> Path:
    """Persist a modified `AgentSettings` back to the user's YAML file.

    Returns the path written. Used by the Argonaut mode-toggle endpoint
    and the `argosy argonaut mode` CLI command.
    """
    settings_obj = get_settings()
    path = settings_obj.agent_settings_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json")
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


__all__ = [
    "AgentSettings",
    "AlertsBlock",
    "BackupsBlock",
    "CadenceConfig",
    "CadencesBlock",
    "CostBlock",
    "ExecutionBlock",
    "JobRunsRetentionConfig",
    "LimitedAccountBlock",
    "ModelsBlock",
    "SecurityBlock",
    "TiersBlock",
    "load_agent_settings",
    "save_agent_settings",
    "write_default_agent_settings",
]
