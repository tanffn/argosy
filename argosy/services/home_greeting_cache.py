"""Home greeting bake + dirty-flag (§7.2).

Material events mark the greeting dirty (cheap ``kv_cache`` purge).
``GET /api/home/greeting`` regenerates when dirty / missing, then bakes
the payload so quiet revisits serve the bake without re-deriving.

Acceptance: promote a plan → next Home visit reflects it (dirty mark
on accept forces regen) without waiting for the daily input crons.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import KvCacheEntry

_log = get_logger("argosy.services.home_greeting_cache")

PROVIDER = "home_greeting"
# Safety-net TTL — dirty marks are the primary invalidation path.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _user_key(user_id: str) -> str:
    return f"user:{user_id}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hash_payload(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def mark_home_greeting_dirty(
    user_id: str,
    session: Session | None = None,
    *,
    commit: bool = False,
) -> None:
    """Cheap dirty touch: purge the baked greeting for *user_id*.

    Next Home visit regenerates via :func:`get_or_refresh_greeting`.
    Failures are logged and never raised — the primary write must win.

    When ``session`` is provided, delete through that session (same DB as
    the bake). Pass ``commit=True`` only when the caller's transaction is
    already finished (e.g. after ``action_proposals`` commit); otherwise
    flush-only so an in-flight verdict/flag write can still roll back.
    """
    try:
        if session is not None:
            from sqlalchemy import delete

            session.execute(
                delete(KvCacheEntry).where(
                    KvCacheEntry.provider == PROVIDER,
                    KvCacheEntry.key == _user_key(user_id),
                )
            )
            session.flush()
            if commit:
                session.commit()
        else:
            from argosy.adapters.data.cache import purge_cache_entry

            purge_cache_entry(PROVIDER, _user_key(user_id))
        _log.info("home_greeting.marked_dirty", user_id=user_id)
    except Exception:  # noqa: BLE001
        _log.warning("home_greeting.dirty_mark_failed", user_id=user_id)




def _read_bake(session: Session, user_id: str, *, now: datetime) -> dict[str, Any] | None:
    row = session.execute(
        select(KvCacheEntry).where(
            KvCacheEntry.provider == PROVIDER,
            KvCacheEntry.key == _user_key(user_id),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    expires = _aware_utc(row.expires_at)
    if expires is not None and expires <= now:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_bake(
    session: Session,
    user_id: str,
    payload: dict[str, Any],
    *,
    now: datetime,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    payload_hash = _hash_payload(payload_json)
    expires_at = now + timedelta(seconds=max(ttl_seconds, 0))
    key = _user_key(user_id)
    row = session.execute(
        select(KvCacheEntry).where(
            KvCacheEntry.provider == PROVIDER,
            KvCacheEntry.key == key,
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            KvCacheEntry(
                provider=PROVIDER,
                key=key,
                payload_json=payload_json,
                retrieved_at=now,
                expires_at=expires_at,
                payload_hash=payload_hash,
            )
        )
    else:
        row.payload_json = payload_json
        row.retrieved_at = now
        row.expires_at = expires_at
        row.payload_hash = payload_hash
    session.flush()


def get_or_refresh_greeting(
    session: Session,
    user_id: str,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Serve baked greeting, or regenerate when dirty/missing/forced.

    Uses the request ``session`` for both the bake table and
    :func:`build_greeting` so tests with a file-backed DB stay coherent.
    """
    from argosy.services.home_greeting import build_greeting

    now_dt = now or _utcnow()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    if not force:
        baked = _read_bake(session, user_id, now=now_dt)
        if baked is not None:
            return baked

    payload = build_greeting(session, user_id, now=now_dt)
    try:
        _write_bake(session, user_id, payload, now=now_dt)
        session.commit()
    except Exception:  # noqa: BLE001 — bake failure must not blank the greeting
        _log.warning("home_greeting.bake_failed", user_id=user_id, exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
    return payload


def clear_home_greeting_bake(user_id: str) -> None:
    """Alias kept for tests / explicit purge."""
    mark_home_greeting_dirty(user_id)


__all__ = [
    "PROVIDER",
    "clear_home_greeting_bake",
    "get_or_refresh_greeting",
    "mark_home_greeting_dirty",
]
