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

from pydantic import BaseModel, Field, model_validator
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
    # Config isolation for the claude_code backend (2026-07-05 telemetry fix).
    # When True (default), fleet agent sessions run the bundled claude.exe with
    # ``ClaudeAgentOptions(setting_sources=[])`` so the developer's PERSONAL
    # Claude Code user config (global CLAUDE.md, auto-memory, skills, hooks,
    # settings.json) is NOT loaded into agent context. Live telemetry showed
    # every fleet call carrying ~35-75k cache tokens of that personal config
    # for prompts whose actual content was <8k chars, plus leaked skill/hook
    # preamble in agent outputs. OAuth session auth is unaffected (credentials
    # are not a "setting source" — verified live).
    # Revert via env: `ARGOSY_ANTHROPIC__CLAUDE_CODE_ISOLATED=false` or
    # `argosy.toml [anthropic] claude_code_isolated = false`.
    claude_code_isolated: bool = True


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

    # Living-plan incremental cutover — when True, /accept promotion is routed
    # through the derivation-graph publish gate (build the living graph for the
    # draft, recheck cross-surface coherence, fail closed on any open
    # coherence/hard flag) IN ADDITION TO the authority set. This enforces that
    # a promoted plan's canonical surfaces (FI verdict/tile, retirement age,
    # net-worth bases, US-situs) are mutually consistent by construction — the
    # cross-surface-contradiction class that the whack-a-mole synthesis loop
    # kept reintroducing. When False, /accept uses the authority-only
    # evaluate_promotion path (from-scratch synthesis is the fallback, untouched).
    # Read via ARGOSY_INCREMENTAL_PLAN; incremental_plan._flag_on() also honours
    # the raw env var so scripts/the demo can toggle it without a settings reload.
    # Default flipped True after the live cutover cycle on ariel's data (run 115)
    # landed CLOSED with zero open coherence flags — the canonical surfaces (FI
    # verdict/tile, age, net-worth bases, US-situs) are byte-consistent by
    # construction, and the gate still fails closed on real authority blocks.
    # Set ARGOSY_INCREMENTAL_PLAN=0 to revert to the authority-only path.
    argosy_incremental_plan: bool = Field(default=True)

    # Blind anti-correlation gate (plan Risk/Constraint Kernel): at promotion, the
    # deterministic kernel re-derives the DRAFT's TARGET single-name look-through
    # exposure (direct + fund-embedded NVDA) and records whether it breaches the plan's
    # own cap — the fleet-missed incoherence (12% direct + embedded > 13% cap). The
    # verdict is ALWAYS computed + logged + attached as the `lookthrough_cap` authority.
    # It BLOCKS promotion when this flag is True. Default True (fail-closed): a plan whose
    # TARGET breaches its own single-name cap on a look-through basis must not be promoted
    # — and there is now an apply path to FIX it (POST /api/plan/refine → a staged draft
    # with a durable allocation override that lowers the look-through). Reversible via
    # ARGOSY_PLAN_LOOKTHROUGH_GATE_ENFORCE=false; the test suite forces it off (opt-in in
    # tests) so promotions of non-cap-relevant fixtures aren't blocked.
    plan_lookthrough_gate_enforce: bool = Field(default=True)

    # Registry-rendered reader anchor (Phase 2): the whole-artifact reviewer is
    # given a reviewer-only "canonical reconciliation anchor" rendered from the
    # derivation-graph surfaces (one owner per figure), so it judges plan prose
    # against the ONE registry value. Default flipped True after a live A/B on
    # ariel's run-117 draft: the anchor was NOT critiqued (oracle framing held),
    # it REMOVED a false positive (the 3 labeled net-worth bases stopped reading
    # as a contradiction), and it CAUGHT true prose-vs-registry drift the baseline
    # missed (RSU retention 47% vs canonical 50%/70%; FI crossing 2026-vs-2027).
    # On fresh synthesis the prose binds canonical via fact-placeholders, so the
    # anchor mostly confirms agreement. Set ARGOSY_REGISTRY_REVIEW_ARTIFACT=0 to
    # revert to the from-scratch-only reader artifact.
    argosy_registry_review_artifact: bool = Field(default=True)

    # Phase 5 prime-directive experts — when True (the default), the
    # PlanCoverageAnalyst, WithdrawalSequencerAgent and EquityCompAnalystAgent
    # run alongside the core Phase 1 analyst fleet (10 → 13). Their numeric
    # resolvers (savings.annual_net_nis, retirement.fi_*) and section bindings
    # (fi_bridge / withdrawal_schedule) are wired, so the headline savings,
    # FV trajectory and FI-bridge waterfall derive from real agent output
    # instead of rendering "[derivation pending]". Default flipped to True
    # after a supervised live-LLM run validated the outputs (T3.1). Set
    # ``ARGOSY_PHASE5_AGENTS=false`` to fall back to the 10-member core fleet.
    phase5_agents: bool = Field(default=True)

    # Decision funnel (P0) — staged kill switches for the autonomous daily
    # decision funnel. Layered so each capability can be disabled instantly and
    # independently (codex: "build it as a conservative escalation system"):
    #   - decision_funnel_enabled: master switch. When False the loop no-ops.
    #     Default ON — per the "nothing hidden" doctrine (SDD §1.6) the funnel is
    #     exposed and running in beta, not gated off; disable per-tenant if needed.
    #   - decision_funnel_shadow: when True (DEFAULT), the funnel is CALIBRATING —
    #     it records graded decisions + full trace and EXPOSES them beta-labelled
    #     (view-first via the inbox + /api/decisions/funnel/calibration), but does
    #     not act on the client's behalf. Not hidden. Flip to False to let its
    #     proposals become directly actionable once calibration is validated.
    #   - decision_funnel_stage3: when True (DEFAULT), survivors escalate to the
    #     Opus deep-decision fleet (Stage 3) so real graded decisions are produced
    #     to calibrate against. Disable to run only the cheap Stage 0–2 scan.
    #   - decision_funnel_autoact: when True, PRE-AUTHORIZED MECHANICAL rules
    #     may auto-execute (idle-cash sweep, in-band rebalance, TLH). Default
    #     OFF. Discretionary Buy/Sell/Trim is ALWAYS propose-and-ask regardless.
    # Read via ARGOSY_DECISION_FUNNEL_ENABLED / _SHADOW / _STAGE3 / _AUTOACT.
    decision_funnel_enabled: bool = Field(default=True)
    decision_funnel_shadow: bool = Field(default=True)
    decision_funnel_stage3: bool = Field(default=True)
    decision_funnel_autoact: bool = Field(default=False)
    # Discord signal listener. OFF (2026-07-07): reconnect bug (~150 supervisor
    # restarts/day) + Discord blocked the API; 0 signals since 2026-05-29.
    # Re-enable via ARGOSY_DISCORD_LISTENER_ENABLED=1 after value review + fix.
    discord_listener_enabled: bool = Field(default=False)
    # Boot-time missed-run catch-up: a cron loop whose most recent scheduled
    # fire has no recorded tick (server was down) fires once at startup,
    # sequentially, instead of waiting for its next cron slot. The daily
    # pipeline is the product — a review lost to server downtime is a
    # silent failure of proactive agency. Read via ARGOSY_SCHEDULER_CATCHUP_ON_BOOT.
    scheduler_catchup_on_boot: bool = Field(default=True)
    # Only fires missed slots at most this old (days). An out-of-season slot
    # (the annual loop's January 2nd rediscovered in July) waits for its next
    # scheduled time instead. Read via ARGOSY_SCHEDULER_CATCHUP_MAX_AGE_DAYS.
    scheduler_catchup_max_age_days: float = Field(default=7.0)
    # Research-informed deployment preflight (deterministic; Increment 1).
    #   - deployment_funnel_enabled: master switch. When False, /deploy-cash
    #     behaves exactly as before (no preflight block).
    #   - deployment_funnel_shadow: when False (DEFAULT now), the preflight
    #     RE-RANKS the buy list (drops vetoed/deferred, resizes capped) so the
    #     surfaced plan reflects the verdict. Set True to only annotate.
    deployment_funnel_enabled: bool = Field(default=True)
    deployment_funnel_shadow: bool = Field(default=False)
    # Increment 2 — LIVE fleet adjudication of NEEDS_FLEET_REVIEW candidates.
    # MASTER kill-switch. The actual per-call opt-in is the /deploy-cash
    # `fleet_review=true` query param (expensive: several agent LLM calls per held
    # candidate). When both are set, deployment judgment calls the deterministic
    # layer refuses to invent (e.g. adding NVDA-correlated exposure while the book
    # is over the plan cap) are adjudicated by the RiskOfficer (3-perspective) +
    # FundManager agents, whose bounded verdict replaces the candidate's status.
    # Verified live 2026-07-01 (sound differentiated verdicts; fail-closed on agent
    # error). Default True = feature AVAILABLE; nothing fires unless a caller
    # explicitly passes fleet_review=true, so the normal fast GET is unaffected.
    deployment_fleet_review_enabled: bool = Field(default=True)

    # Fleet-authors / determinism-verifies pivot — the LLM AUTHORS the
    # allocation and the deterministic verifier gates it (inverting the old
    # deterministic-water-fill engine, which a plain LLM prompt beat). When
    # True, /deploy-cash builds a decision packet, runs the author→verify→
    # bounce loop, and renders the accepted proposal; on rejection/timeout it
    # falls back to the deterministic cash_only_deploy engine LABELLED
    # degraded. Default True: the live author path is proven (accepted, verifier-
    # gated, plan-filling allocations first-attempt against a real claude.exe) and
    # the whole feature is reversible — additive on /deploy-cash, degrades to the
    # deterministic engine, flip off via ARGOSY_DEPLOYMENT_AUTHOR_ENABLED=false.
    deployment_author_enabled: bool = Field(default=True)
    # Backend override for the deployment-author money path ONLY. None = use
    # the global anthropic.backend (claude_code). Set to "api_key" to route
    # the money decision to the direct Anthropic SDK (no flaky claude.exe
    # subprocess, honest HTTP timeouts) once an API key is configured — the
    # production-preferred path. The reliability wrapper hardens whichever
    # backend is active (hard timeout + process-tree kill on claude_code).
    deployment_author_backend: str | None = Field(default=None)

    # Critique reconcile loop (2026-07-07) — after a weekly / on-demand plan
    # critique lands, RED findings (and notable YELLOWs) trigger ONE closer
    # pass + ONE re-verify critique (FIND -> CORRECT -> RE-VERIFY), routing
    # each finding to prose-edit / requires-re-synthesis / snapshot-refresh /
    # needs-info / dispute-ZigZag. Default ON per the nothing-hidden
    # preference; the loop itself is hard cost-bounded (1 reconcile + 1
    # re-verify per critique, never unbounded). Flip off via
    # ARGOSY_CRITIQUE_RECONCILE=false. The on-demand /api/plan/critique route
    # additionally requires reconcile=true (the button stays a fast read).
    critique_reconcile: bool = Field(default=True)

    # Corrective PATCH phase-3 path (docs/design/corrective_patch_synthesis.md):
    # when corrective mode is active AND this flag is on AND the patch-
    # reachability classifier says PATCH, phase 3 edits implicated slices of
    # the prior draft instead of regenerating. Default ON after a live
    # acceptance (clean corrective promotion). Kill switch:
    # ARGOSY_CORRECTIVE_PATCH=0. Orchestrator also reads the raw env var so
    # tests can toggle without a settings reload.
    corrective_patch: bool = Field(default=True)

    # Sliced FULL phase-3 path (docs/design/sliced_full_synthesis.md): stage-A
    # skeleton + parallel slice expansion instead of a monolith synthesizer
    # call. Precedence: corrective PATCH > sliced FULL > monolith. Default ON
    # after the same live-acceptance precondition as corrective_patch. Kill
    # switch: ARGOSY_SLICED_SYNTH=0.
    sliced_synth: bool = Field(default=True)

    # Canonical fact placeholders (item I / 2026-07-12): synthesizer emits
    # ``{{fact:key}}`` tokens instead of hand-typed headline digits; READ-time
    # rendering fills them from the live resolver so a trade updates numbers
    # without rewriting the plan. Default ON. Kill switch:
    # ARGOSY_FACT_PLACEHOLDERS=0. First LIVE synthesis under the new contract
    # is token-gated (coordinate with reviewer — do not burn casually).
    fact_placeholders: bool = Field(default=True)

    # Warn-only (default) gate: a literal headline number that HAS a matching
    # registry fact key should have been a ``{{fact:key}}`` token. Set
    # ARGOSY_FACT_LITERAL_GATE_ENFORCE=true to promote to blocking.
    fact_literal_gate_enforce: bool = Field(default=False)

    # Daily-news volatility trigger (news_daily Stage-2 gate). When Stage 1
    # ingests ZERO new items, Stage 2 (the LLM analyst) still fires if a HELD
    # single stock moved at least this many percent (absolute, close-over-
    # close) — a big move on a held name deserves analysis of any pending
    # signals even when the headline feed deduped to nothing. Deterministic
    # TRIAGE only (whether to spend LLM); the analyst does all judgment.
    # Read via ARGOSY_NEWS_VOLATILITY_MOVE_PCT.
    news_volatility_move_pct: float = Field(default=4.0)

    # Israeli surtax (mas yesef) parameters — config-sourced so the annually
    # re-set threshold is NOT a frozen magic literal. Defaults are the nominal
    # 2024/2025 values; override per tax year via ARGOSY_SURTAX_THRESHOLD_NIS /
    # ARGOSY_SURTAX_RATE_ORDINARY / ARGOSY_SURTAX_RATE_CAPITAL (or intake).
    # tax_curve.annual_surtax reads these when no explicit threshold is passed.
    surtax_threshold_nis: float = Field(default=721_560.0)
    surtax_rate_ordinary: float = Field(default=0.03)
    surtax_rate_capital: float = Field(default=0.05)  # 3% base + 2% capital (2025+)

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

    # Config-isolation revert knob (2026-07-05). Like admin_token above, the
    # explicit-kwargs constructor bypasses the SettingsConfigDict env plumbing,
    # so the documented `ARGOSY_ANTHROPIC__CLAUDE_CODE_ISOLATED` env var is
    # wired by hand here. Env wins over argosy.toml — it's the emergency
    # revert switch. Pydantic coerces "false"/"0"/"true"/"1" strings to bool.
    _iso_env = os.environ.get("ARGOSY_ANTHROPIC__CLAUDE_CODE_ISOLATED")
    if _iso_env is not None:
        anthropic_cfg = {**anthropic_cfg, "claude_code_isolated": _iso_env}

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
# Early-signal streams config
# ----------------------------------------------------------------------


class GovContractsSignalConfig(BaseModel):
    enabled: bool = Field(default=True, strict=True)
    materiality_threshold: float = Field(default=0.05, gt=0, le=1)
    lookback_days: int = Field(default=90, gt=0)
    recent_scan_days: int = Field(default=2, gt=0)
    max_pages_per_query: int = Field(default=10, gt=0)
    agent_error_ttl_hours: int = Field(default=24, gt=0, le=168)


class InsiderClusterSignalConfig(BaseModel):
    enabled: bool = Field(default=False, strict=True)
    lookback_days: int = Field(default=14, gt=0)
    recent_scan_days: int = Field(default=2, gt=0)
    index_publication_lag_days: int = Field(default=2, ge=1, strict=True)
    daily_pull_days: int = Field(default=1, ge=1, le=1, strict=True)
    ledger_horizon_days: int = Field(default=45, ge=1, le=45, strict=True)
    min_distinct_buyers: int = Field(default=2, ge=2)
    min_cluster_value_usd: float = Field(default=100_000, gt=0)
    min_cluster_value_market_cap_bps: float = Field(default=0.5, ge=0)
    min_distinct_sellers: int = Field(default=2, ge=2)
    min_stake_sale_pct: float = Field(default=20, gt=0, lt=100)
    warning_ttl_days: int = Field(default=30, gt=0, le=365)
    cursor_max_catchup_days: int = Field(default=31, gt=0, le=31)

    @model_validator(mode="after")
    def validate_scan_window(self) -> InsiderClusterSignalConfig:
        if self.recent_scan_days > self.lookback_days:
            raise ValueError(
                "recent_scan_days must be no greater than lookback_days"
            )
        if self.ledger_horizon_days < self.lookback_days:
            raise ValueError(
                "ledger_horizon_days must be no less than lookback_days"
            )
        return self


class SignalStreamsConfig(BaseModel):
    enabled: bool = True
    gov_contracts: GovContractsSignalConfig = Field(
        default_factory=GovContractsSignalConfig
    )
    insider_cluster: InsiderClusterSignalConfig = Field(
        default_factory=InsiderClusterSignalConfig
    )


def load_signal_streams_config(user_id: str) -> SignalStreamsConfig:
    raw = get_user_agent_settings(user_id)
    return SignalStreamsConfig.model_validate(raw.get("signal_streams") or {})


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
