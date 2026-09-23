"""Supervised long-running job for the private Discord advisor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from argosy.orchestrator.loops.base import ConnectionStatus, LongRunningJob
from argosy.services.jobs.registry import JobMetadata

from .config import DiscordAdvisorConfig, load_bot_token, load_config
from .gateway import DiscordAdvisorAccessError, DiscordPyGateway, Gateway
from .worker import DiscordAdvisorWorker

_TERMINAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})


def _terminal_auth_failure(exc: BaseException) -> bool:
    if type(exc).__name__ in {
        "LoginFailure",
        "PrivilegedIntentsRequired",
        "GatewayNotFound",
    }:
        return True
    candidates = (
        getattr(exc, "code", None),
        getattr(getattr(exc, "rcvd", None), "code", None),
        getattr(getattr(exc, "sent", None), "code", None),
    )
    for candidate in candidates:
        try:
            if int(candidate) in _TERMINAL_CLOSE_CODES:
                return True
        except (TypeError, ValueError):
            pass
    return any(str(code) in str(exc) for code in _TERMINAL_CLOSE_CODES)


def discord_advisor_metadata() -> JobMetadata:
    return JobMetadata(
        name="discord_advisor",
        schedule_cron=None,
        schedule_human="long-running (supervised)",
        source_kind="notification",
        description="Private, allowlisted Discord advisory transport; separate from passive Discord research ingestion.",
        long_running=True,
    )


class DiscordAdvisorJob(LongRunningJob):
    name = "discord_advisor"

    def __init__(
        self,
        config: DiscordAdvisorConfig | None,
        token: str | None,
        *,
        gateway: Gateway | None = None,
        worker_factory: Callable[..., Any] = DiscordAdvisorWorker,
        configuration_error: str | None = None,
        configuration_repair: str | None = None,
        reload_runtime_configuration: bool = False,
        **worker_kwargs: Any,
    ) -> None:
        super().__init__()
        self.config = config
        self._token = token
        self._gateway = gateway
        self._worker_factory = worker_factory
        self._worker_kwargs = worker_kwargs
        self._worker: Any | None = None
        self._status: ConnectionStatus = "stopped"
        self._configuration_error = configuration_error
        self._configuration_repair = configuration_repair
        self._reload_runtime_configuration = reload_runtime_configuration
        self._error = configuration_error
        self._repair = configuration_repair

    def connection_status(self) -> ConnectionStatus:
        return self._status

    def status_snapshot(self) -> dict[str, Any]:
        enabled = bool(self.config and self.config.enabled)
        configured = bool(
            enabled and self.config and self.config.bindings and self._token and not self._error
        )
        repair = None
        if self._repair:
            repair = self._repair
        elif self._error:
            repair = "Fix configs/discord_advisor.json, then restart Argosy."
        elif enabled and not self._token:
            repair = "Store the bot token in the OS keyring as discord_advisor_bot_token, then restart Argosy."
        return {
            "configured": configured,
            "enabled": enabled,
            "connection": self._status,
            "error": self._error,
            "repair": repair,
            "status_board_error": getattr(self._worker, "status_board_error", None),
            "notification_error": getattr(self._worker, "notification_error", None),
        }

    async def run(self) -> None:
        self._exit_intent = "unset"
        self._worker = None
        if not self._prepare_run_configuration():
            self._exit_intent = "clean"
            return
        if self.config is None or not self.config.enabled:
            self._exit_intent = "clean"
            return
        if not self._token:
            self._error = "Discord advisor token is missing"
            self._repair = (
                "Store the bot token in the OS keyring as discord_advisor_bot_token, "
                "then reconnect the job."
            )
            self._exit_intent = "clean"
            return
        self._status = "reconnecting"
        try:
            gateway = self._gateway or DiscordPyGateway()
            self._worker = self._worker_factory(
                self.config, self._token, gateway, on_ready=self._on_ready, **self._worker_kwargs
            )
            await self._worker.run()
            if self._exit_intent != "operator_stop":
                self._exit_intent = "clean"
        except DiscordAdvisorAccessError as exc:
            self._error = str(exc)
            self._repair = exc.repair
            self._exit_intent = "clean"
        except Exception as exc:
            if not _terminal_auth_failure(exc):
                raise
            self._error = "Discord login or Message Content intent failed"
            self._repair = (
                "Verify the discord_advisor_bot_token and enable the Message Content intent "
                "in the Discord developer portal, then reconnect the job."
            )
            self._exit_intent = "clean"  # clear health, no reconnect storm
        finally:
            self._status = "stopped"

    def _prepare_run_configuration(self) -> bool:
        """Refresh production inputs and clear only retryable runtime failures."""
        if self._reload_runtime_configuration:
            try:
                config = load_config()
            except (OSError, ValueError, ValidationError) as exc:
                self.config = None
                self._token = None
                self._error = f"Invalid Discord advisor configuration: {type(exc).__name__}"
                self._repair = "Fix configs/discord_advisor.json, then reconnect the job."
                return False
            self.config = config
            if not config.enabled:
                self._token = None
                self._error = None
                self._repair = None
                return True
            try:
                self._token = load_bot_token()
            except Exception as exc:
                self._token = None
                self._error = f"Discord advisor credential lookup failed: {type(exc).__name__}"
                self._repair = "Repair OS keyring access, then reconnect the job."
                return False
            self._configuration_error = None
            self._configuration_repair = None
        elif self._configuration_error:
            self._error = self._configuration_error
            self._repair = self._configuration_repair
            return False

        # Runtime access/auth errors are retryable only through an explicit new run.
        self._error = None
        self._repair = None
        return True

    def _on_ready(self) -> None:
        self._status = "connected"

    async def cancel(self) -> None:
        self._exit_intent = "operator_stop"
        if self._worker is not None:
            await self._worker.gateway.close()


def build_discord_advisor_job(
    *,
    config: DiscordAdvisorConfig | None = None,
    token: str | None = None,
    gateway: Gateway | None = None,
    **worker_kwargs: Any,
) -> DiscordAdvisorJob | None:
    production_factory = config is None and token is None and gateway is None
    if config is None:
        try:
            config = load_config()
        except (OSError, ValueError, ValidationError) as exc:
            return DiscordAdvisorJob(
                None,
                None,
                configuration_error=f"Invalid Discord advisor configuration: {type(exc).__name__}",
                configuration_repair=("Fix configs/discord_advisor.json, then restart Argosy."),
                reload_runtime_configuration=production_factory,
            )
    if not config.enabled:
        return None
    resolved_token = token
    if resolved_token is None:
        try:
            resolved_token = load_bot_token()
        except Exception as exc:
            if not production_factory:
                raise
            return DiscordAdvisorJob(
                config,
                None,
                gateway=gateway,
                configuration_error=(
                    f"Discord advisor credential lookup failed: {type(exc).__name__}"
                ),
                configuration_repair="Repair OS keyring access, then reconnect the job.",
                reload_runtime_configuration=True,
                **worker_kwargs,
            )
    return DiscordAdvisorJob(
        config,
        resolved_token,
        gateway=gateway,
        reload_runtime_configuration=production_factory,
        **worker_kwargs,
    )


__all__ = ["DiscordAdvisorJob", "build_discord_advisor_job", "discord_advisor_metadata"]
