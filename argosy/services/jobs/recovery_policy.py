"""Durable, bounded recovery policy shared by scheduler and status reporting."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

# Preserve the existing three attempts for unknown/programming failures.
# Confirmed temporary data outages get three later probes, not a busy loop.
BASE_ATTEMPTS = 3
TRANSPORT_ATTEMPTS = 6
TRANSPORT_DELAYS = (1800, 1800, 7200, 14400, 28800)


class ProviderCooldownError(RuntimeError):
    """Typed transport deferral, recorded even when a new cron slot arrives."""

    status = 503

    def __init__(self, remaining_seconds: float):
        super().__init__("Provider cooldown still active; no external request was sent")
        self.headers = {"Retry-After": str(remaining_seconds)}


def provider_deadline(row) -> datetime | None:
    if row is None or row.status != "error":
        return None
    delays = [f.get("retry_after_seconds") for f in structured_failures(row)
              if f.get("retryable") is True]
    delays = [d for d in delays if isinstance(d, (int, float)) and math.isfinite(d) and d > 0]
    if not delays:
        return None
    completed = row.finished_at or row.started_at
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    try:
        return completed + timedelta(seconds=max(delays))
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


def structured_failures(row) -> list[dict]:
    try:
        summary = json.loads(row.output_summary or "{}")
        if not isinstance(summary, dict):
            return []
        streams = summary.get("streams")
        top = [summary["failure"]] if isinstance(summary.get("failure"), dict) else []
        if isinstance(streams, dict):
            # Include unclassified failures: one temporary error must not mask
            # a simultaneous parser/permission failure or grant it extra calls.
            return top + [r.get("failure") if isinstance(r.get("failure"), dict) else {"code": "unclassified", "retryable": False}
                    for r in streams.values() if isinstance(r, dict) and r.get("status") == "error"]
        errors = summary.get("errors")
        if isinstance(errors, list) and any(isinstance(e, dict) and isinstance(e.get("failure"), dict) for e in errors):
            return top + [e["failure"] if isinstance(e, dict) and isinstance(e.get("failure"), dict)
                          else {"code": "unclassified", "retryable": False} for e in errors]
        return top
    except (ValueError, TypeError):
        return []


def recovery_window(row, attempts: int) -> dict:
    failures = structured_failures(row)
    transient = bool(failures) and all(f.get("retryable") is True for f in failures)
    permanent = any(f.get("code") in {"authentication", "http_permanent"} for f in failures)
    maximum = TRANSPORT_ATTEMPTS if transient else BASE_ATTEMPTS
    delay = TRANSPORT_DELAYS[min(max(attempts - 1, 0), 4)] if transient else 1800
    for failure in failures:
        after = failure.get("retry_after_seconds")
        if isinstance(after, (int, float)) and math.isfinite(after) and after >= 0:
            delay = max(delay, after)
    completed = row.finished_at or row.started_at
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    # Reject unrealistic provider headers safely, without timedelta overflow.
    try:
        next_at = completed + timedelta(seconds=delay)
    except OverflowError:
        next_at = datetime.max.replace(tzinfo=UTC)
    return {"attempts": attempts, "max_attempts": maximum,
            "eligible": not permanent and attempts < maximum,
            "next_retry_at": next_at.isoformat() if not permanent and attempts < maximum else None,
            "transient": transient}
