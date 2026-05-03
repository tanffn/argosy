"""Telemetry client.

Fire-and-forget POSTs to a configured endpoint. Never blocks the
engine: send-on-best-effort with a short timeout, swallow all errors,
and log at debug. Tests inject a fake `transport` so we never hit the
network.

Config is loaded from `agent_settings.telemetry` if present, otherwise
the per-tenant `entitlements` `telemetry_optout` feature can disable
recording entirely.

Anonymization rules:
  * `user_id` is sha256-hashed with a per-instance salt before send.
  * Payloads filter out keys named "ticker", "price", "value_usd",
    "plan_content", "email" — see `_REDACT_KEYS`.

Buckets allowed:
  * "diagnostic" — error rates, agent failures, broker reconnects.
  * "usage"      — cadence ticks, decisions per tier, model spend by role.
  * "performance"— decision latency, API response times.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from argosy.logging import get_logger

_log = get_logger("argosy.telemetry")


Bucket = Literal["diagnostic", "usage", "performance"]
_ALLOWED_BUCKETS: set[str] = {"diagnostic", "usage", "performance"}
_REDACT_KEYS: set[str] = {
    "ticker",
    "tickers",
    "price",
    "prices",
    "value_usd",
    "value",
    "plan_content",
    "plan_text",
    "plan",
    "email",
    "name",
    "address",
    "phone",
}


# Type for an injectable transport (tests set this to a stub).
Transport = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class TelemetryConfig:
    enabled: bool = False
    endpoint: str = ""
    timeout_seconds: float = 2.0
    salt: str = field(default_factory=lambda: os.environ.get("ARGOSY_TELEMETRY_SALT", "argosy-default-salt"))


@dataclass
class TelemetryEvent:
    bucket: Bucket
    name: str
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    user_hash: str | None = None  # set by client before send


class TelemetryClient:
    """Per-instance telemetry sink.

    Construction is cheap. Tests pass `transport=...` to capture sends
    without hitting the network.
    """

    def __init__(
        self,
        config: TelemetryConfig | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config or TelemetryConfig()
        self._transport = transport
        self._sent: list[TelemetryEvent] = []
        self._lock = threading.Lock()

    @property
    def sent(self) -> list[TelemetryEvent]:
        """Test-only: events that would have been (or were) sent."""
        with self._lock:
            return list(self._sent)

    def hash_user(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        digest = hashlib.sha256(
            f"{self.config.salt}:{user_id}".encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def _filter_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Drop redacted keys and any non-jsonable values."""
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if k.lower() in _REDACT_KEYS:
                continue
            try:
                json.dumps(v)
            except TypeError:
                continue
            out[k] = v
        return out

    async def record(
        self,
        bucket: Bucket,
        name: str,
        *,
        user_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> TelemetryEvent | None:
        """Record (and best-effort send) a telemetry event.

        Returns the event for caller convenience; returns None when the
        client is disabled.
        """
        if not self.config.enabled:
            return None
        if bucket not in _ALLOWED_BUCKETS:
            _log.debug("telemetry.unknown_bucket", bucket=bucket)
            return None

        event = TelemetryEvent(
            bucket=bucket,
            name=name,
            fields=self._filter_fields(fields or {}),
            user_hash=self.hash_user(user_id),
        )
        with self._lock:
            self._sent.append(event)

        # Fire-and-forget send. We never await its completion: the goal
        # is to never block the engine on a slow telemetry endpoint.
        if self._transport is not None or self.config.endpoint:
            try:
                asyncio.create_task(self._send(event))
            except RuntimeError:  # no running loop (e.g. unit-test sync path)
                pass
        return event

    async def _send(self, event: TelemetryEvent) -> None:
        payload = {
            "bucket": event.bucket,
            "name": event.name,
            "fields": event.fields,
            "timestamp": event.timestamp,
            "user_hash": event.user_hash,
        }
        try:
            if self._transport is not None:
                await asyncio.wait_for(
                    self._transport(self.config.endpoint, payload),
                    timeout=self.config.timeout_seconds,
                )
                return
            # Real send path — we keep this minimal (no httpx-only client
            # so tests that don't install httpx still work). In practice
            # tests inject `transport`, so this branch only runs in prod.
            await self._real_send(payload)  # pragma: no cover
        except Exception as exc:  # pragma: no cover - swallow all
            _log.debug("telemetry.send_failed", err=str(exc))

    async def _real_send(self, payload: dict[str, Any]) -> None:  # pragma: no cover
        """Best-effort HTTP POST via httpx if available."""
        try:
            import httpx  # type: ignore
        except ImportError:
            return
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            await client.post(self.config.endpoint, json=payload)


# ----------------------------------------------------------------------
# Module-level singleton convenience
# ----------------------------------------------------------------------

_CLIENT: TelemetryClient | None = None
_LOCK = threading.Lock()


def get_client() -> TelemetryClient:
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = TelemetryClient()
        return _CLIENT


def reset_client(client: TelemetryClient | None = None) -> TelemetryClient:
    """Replace the singleton (used by tests + by config reload)."""
    global _CLIENT
    with _LOCK:
        _CLIENT = client or TelemetryClient()
        return _CLIENT


async def record(
    bucket: Bucket,
    name: str,
    *,
    user_id: str | None = None,
    fields: dict[str, Any] | None = None,
) -> TelemetryEvent | None:
    """Module-level convenience around `get_client().record(...)`."""
    return await get_client().record(bucket, name, user_id=user_id, fields=fields)


__all__ = [
    "Bucket",
    "TelemetryClient",
    "TelemetryConfig",
    "TelemetryEvent",
    "Transport",
    "get_client",
    "record",
    "reset_client",
]
