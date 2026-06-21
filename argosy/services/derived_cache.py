"""Process-local memo cache for Argosy's expensive derived computations.

WHY: ``/retirement`` panels + ``GET /api/overview`` recompute the same heavy
work on every request — chiefly :func:`resolve_plan_numbers(..., include_canonical_ages=True)`
(which runs a Monte-Carlo via ``canonical_feasible_dual_track``) plus the
MC-heavy retirement projection endpoints. Those outputs are pure functions of
the current plan + portfolio snapshot, so they only change when a plan is
promoted or a new snapshot is ingested.

CORRECTNESS (output-trust doctrine — NEVER serve stale financial numbers):
invalidation is STRUCTURAL, not time-based. The cache key embeds a
:func:`version_tuple` that captures everything that makes derived data stale:

  * the current plan version id + its ``decision_run_id`` (a new promoted plan
    bumps the id; a re-synthesis bumps the decision_run_id), and
  * the latest portfolio snapshot id + its ``imported_at`` timestamp
    (a new ingested snapshot bumps both — and ``portfolio.*`` resolver facts
    depend on the snapshot, so it MUST be in the key).

When any of those change the key changes -> automatic cache MISS -> recompute.
There is no explicit "bust" path to forget. If a version cannot be determined
(no current plan, no decision run) the tuple is ``None`` and callers treat the
result as UNCACHEABLE (always compute) rather than risk a stale/cross-plan hit.

The cache is transparent: it returns exactly what ``compute()`` would return,
just memoized. It never mutates or reshapes a value.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

# Bounded so a long-lived process can't grow without limit. Each distinct
# (tag, version) is one slot; with a handful of tags and a version that only
# changes on plan-promote / snapshot-ingest, 64 is generous headroom.
_MAXSIZE = 64

_LOCK = threading.RLock()
_STORE: "OrderedDict[tuple, Any]" = OrderedDict()


def _enabled() -> bool:
    """Cache is ON unless ``ARGOSY_DERIVED_CACHE`` is explicitly falsey.

    Accepts ``0`` / ``false`` / ``off`` / ``no`` (case-insensitive) as OFF.
    """
    raw = os.environ.get("ARGOSY_DERIVED_CACHE", "1").strip().lower()
    return raw not in {"0", "false", "off", "no", ""}


def version_tuple(session, user_id: str) -> Optional[tuple]:
    """Cheap staleness key for ``user_id``'s derived data, or ``None``.

    Returns a hashable tuple capturing the current plan version id + its
    decision_run_id + the latest portfolio snapshot id + its imported_at
    timestamp. Returns ``None`` (treat as UNCACHEABLE — always compute) when
    there is no current plan or the plan has no decision run, since without a
    decision_run_id the derived numbers cannot be resolved and we must not
    risk a stale or cross-plan cache hit.

    Any DB error degrades to ``None`` (uncacheable) — never raises, and never
    returns a partial key that could collide across distinct states.
    """
    try:
        from argosy.state.queries import get_current_plan

        plan = get_current_plan(session, user_id)
        if plan is None or plan.decision_run_id is None:
            return None
        plan_id = int(plan.id)
        decision_run_id = int(plan.decision_run_id)

        snap_id = None
        snap_stamp = None
        try:
            from argosy.services.wealth_dashboard import _latest_snapshot

            snap = _latest_snapshot(session, user_id)
            if snap is not None:
                snap_id = int(snap.id)
                # imported_at uniquely advances on every ingest; include it so a
                # re-import that reuses an id (shouldn't happen, but be safe)
                # still busts the key. isoformat keeps it hashable + stable.
                stamp = getattr(snap, "imported_at", None)
                snap_stamp = stamp.isoformat() if stamp is not None else None
        except Exception:  # noqa: BLE001 — snapshot is optional; key still valid
            snap_id = None
            snap_stamp = None

        return (
            "v1",
            str(user_id),
            plan_id,
            decision_run_id,
            snap_id,
            snap_stamp,
        )
    except Exception:  # noqa: BLE001 — uncacheable on any failure
        return None


def get_or_compute(
    tag: str,
    version: Optional[tuple],
    compute: Callable[[], T],
) -> T:
    """Return a cached value for ``(tag, version)`` or compute + store it.

    ``version`` is the :func:`version_tuple` result (user_id is embedded in it).
    When ``version`` is ``None`` (uncacheable) or the cache is disabled, this
    computes every time and stores nothing — the value is returned verbatim.

    The cache is correctness-first: a changed ``version`` is a different key, so
    a stale entry can never be served; it simply ages out via the LRU bound.

    Thread-safety: the store + LRU bookkeeping are guarded by a lock. The
    ``compute()`` call itself runs OUTSIDE the lock so a slow MC computation
    doesn't serialize unrelated requests; the trade-off is that a thundering
    herd on a cold key may compute a few times concurrently (each result is
    identical for a fixed version, so this is correctness-safe — last writer
    wins, and all writers agree).
    """
    if version is None or not _enabled():
        return compute()

    key = (tag, version)

    with _LOCK:
        if key in _STORE:
            _STORE.move_to_end(key)  # mark most-recently-used
            return _STORE[key]

    # Compute outside the lock — see docstring.
    value = compute()

    with _LOCK:
        # Another thread may have populated it while we computed; prefer the
        # already-stored value to keep a single canonical object, but either is
        # correct for a fixed version.
        if key in _STORE:
            _STORE.move_to_end(key)
            return _STORE[key]
        _STORE[key] = value
        _STORE.move_to_end(key)
        while len(_STORE) > _MAXSIZE:
            _STORE.popitem(last=False)  # evict least-recently-used
    return value


def clear() -> None:
    """Drop all cached entries (test hook / manual bust)."""
    with _LOCK:
        _STORE.clear()


def cache_size() -> int:
    """Current number of cached entries (introspection / tests)."""
    with _LOCK:
        return len(_STORE)


__all__ = [
    "version_tuple",
    "get_or_compute",
    "clear",
    "cache_size",
]
