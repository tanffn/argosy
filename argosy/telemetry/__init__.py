"""Argosy telemetry (Phase 6, SDD §12.3).

Opt-in HTTP client that POSTs anonymized metrics to a configured
endpoint. Three buckets: diagnostic, usage, performance. The hard rule
is that we never collect: position values, ticker names, prices, plan
content, identity (no email, no user_id beyond a salted hash).

Telemetry is gated by `agent_settings.telemetry.enabled` (default
False); when disabled the client is a complete no-op.
"""

from __future__ import annotations

from argosy.telemetry.client import (
    TelemetryClient,
    TelemetryConfig,
    TelemetryEvent,
    get_client,
    record,
    reset_client,
)

__all__ = [
    "TelemetryClient",
    "TelemetryConfig",
    "TelemetryEvent",
    "get_client",
    "record",
    "reset_client",
]
