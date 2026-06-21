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


# ---------------------------------------------------------------------------
# Background pre-warming
#
# The cache above is LAZY: the FIRST request after a plan-promote / snapshot-
# ingest still pays the full ~3.7s Monte-Carlo compute. ``warm`` recomputes the
# hot entries for the CURRENT version on a background thread right after the
# plan or snapshot changes, so the value is already cached by the time the user
# opens /retirement or /api/overview.
#
# CORRECTNESS: warming goes through the SAME ``get_or_compute(tag, version, ...)``
# path the routes use, with the SAME tags + version-key suffixes (params), so a
# warmed entry is a genuine cache HIT for the route — never a divergent key. The
# version is recomputed from a fresh session inside ``warm`` (the version a
# trigger saw may already be one step stale by the time the thread runs; we want
# whatever is current NOW).
# ---------------------------------------------------------------------------


def _warm_enabled() -> bool:
    """Pre-warming is ON unless ``ARGOSY_DERIVED_CACHE_WARM`` is explicitly off.

    Independent of ``ARGOSY_DERIVED_CACHE`` so warming can be disabled alone
    (e.g. to keep a test deterministic) while the lazy cache stays on. Warming
    is ALSO a no-op when the cache itself is disabled — there is nothing to warm
    into.
    """
    raw = os.environ.get("ARGOSY_DERIVED_CACHE_WARM", "1").strip().lower()
    return raw not in {"0", "false", "off", "no", ""}


def _new_sync_session():
    """Build a fresh, self-owned sync SQLAlchemy session for background work.

    Warming runs on a daemon thread AFTER the request returned, so it must NOT
    borrow the request-scoped session (already closed / wrong thread). This
    mirrors ``argosy.api.routes.plan.get_db``'s sync engine construction
    (aiosqlite stripped to plain sqlite, WAL pragmas) but builds its own
    short-lived session that the caller closes. Returns ``None`` if a session
    can't be built (warming then no-ops).
    """
    try:
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        from argosy.config import get_settings

        settings = get_settings()
        sync_url = settings.database_url.replace("+aiosqlite", "")
        engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        if sync_url.startswith("sqlite") and ":memory:" not in sync_url:
            @event.listens_for(engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        return factory()
    except Exception:  # noqa: BLE001 — best-effort; no session -> no warming
        return None


def warm(user_id: str) -> None:
    """Best-effort pre-compute of the hot cache entries for ``user_id``.

    Opens its OWN sync session (background-thread-safe), resolves the current
    version, and runs each hot computation through ``get_or_compute`` so the
    stored keys exactly match what the routes read. No-ops when caching/warming
    is disabled or no current plan exists. NEVER raises into the caller.

    Warmed entries (tag -> compute), matching the routes' keys:

      * ``"overview"``               -> ``build_overview`` (GET /api/overview)
      * ``"retirement.derived-inputs"`` -> ``compute_derived_inputs``
                                        (GET /retirement/derived-inputs)
      * ``"retirement.feasible-age"`` -> ``canonical_feasible_dual_track`` with
        the route's DEFAULT params (target_p_solvent=0.90, n_paths=1500,
        seed=42) and the SAME version suffix
        ``("feasible-age", target_p_solvent, n_paths, seed)``.
      * ``"retirement.dual-track-plan"`` -> ``build_retirement_plan`` with the
        route's DEFAULT params (n_paths=1500, seed=42) and the SAME suffix
        ``("dual-track-plan", n_paths, seed)``.

    NOT warmed: ``"retirement.scenarios"`` — its key includes ``retirement_age``
    which has no single canonical default (the card requests a specific age), so
    pre-warming would compute a key the user may never request. It stays lazy.
    """
    if not _enabled() or not _warm_enabled():
        return

    session = None
    try:
        session = _new_sync_session()
        if session is None:
            return

        version = version_tuple(session, user_id)
        if version is None:
            return  # no current plan -> uncacheable -> nothing to warm

        # --- overview ------------------------------------------------------
        try:
            from argosy.services.overview_assembler import build_overview

            get_or_compute(
                "overview", version, lambda: build_overview(session, user_id=user_id)
            )
        except Exception:  # noqa: BLE001 — one entry failing must not stop others
            pass

        # --- retirement.derived-inputs ------------------------------------
        try:
            from argosy.services.retirement.derived_inputs import (
                compute_derived_inputs,
            )

            get_or_compute(
                "retirement.derived-inputs",
                version,
                lambda: compute_derived_inputs(session, user_id=user_id),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- retirement.feasible-age (route default params) ---------------
        # Route: GET /retirement/projection/feasible-age
        #   target_p_solvent=0.90, n_paths=1500, seed=42
        #   version + ("feasible-age", target_p_solvent, n_paths, seed)
        try:
            from argosy.services.retirement.retirement_plan import (
                RetirementAssumptions,
                canonical_feasible_dual_track,
            )

            fa_target, fa_paths, fa_seed = 0.90, 1500, 42
            fa_version = version + ("feasible-age", fa_target, fa_paths, fa_seed)

            def _warm_feasible_age():
                r = canonical_feasible_dual_track(
                    session=session,
                    user_id=user_id,
                    target_p_solvent=fa_target,
                    assumptions=RetirementAssumptions(n_paths=fa_paths, seed=fa_seed),
                )
                return {
                    "earliest_feasible_age": r.earliest_feasible_age,
                    "p_solvent_at_age": r.p_solvent_at_age,
                    "target_p_solvent": r.target_p_solvent,
                    "operational_target_age": r.operational_target_age,
                    "statutory_lump_age": r.statutory_lump_age,
                    "statutory_annuity_age": r.statutory_annuity_age,
                    "current_age": r.current_age,
                    "reserve_netted_nis": r.reserve_netted_nis,
                    "basis": r.basis,
                }

            get_or_compute("retirement.feasible-age", fa_version, _warm_feasible_age)
        except Exception:  # noqa: BLE001
            pass

        # --- retirement.dual-track-plan (route default params) ------------
        # Route: GET /retirement/projection/dual-track-plan
        #   n_paths=1500, seed=42; version + ("dual-track-plan", n_paths, seed)
        try:
            from argosy.services.retirement.retirement_plan import (
                RetirementAssumptions,
                build_retirement_plan,
            )

            dt_paths, dt_seed = 1500, 42
            dt_version = version + ("dual-track-plan", dt_paths, dt_seed)

            def _track(t) -> dict:
                return {
                    "name": t.name,
                    "label": t.label,
                    "mu_real": t.mu_real,
                    "drawdown_age": t.drawdown_age,
                    "drawdown_p": t.drawdown_p,
                    "preservation_age": t.preservation_age,
                    "preservation_p": t.preservation_p,
                    "frontier": [
                        {
                            "retire_age": p.retire_age,
                            "p_solvent_95": p.p_solvent_95,
                            "median_estate_nis": p.median_estate_nis,
                            "median_estate_real_nis": p.median_estate_real_nis,
                            "worst10_estate_nis": p.worst10_estate_nis,
                            "worst10_estate_real_nis": p.worst10_estate_real_nis,
                            "principal_preserved": p.principal_preserved,
                        }
                        for p in t.frontier
                    ],
                }

            def _warm_dual_track():
                plan = build_retirement_plan(
                    session=session,
                    user_id=user_id,
                    assumptions=RetirementAssumptions(n_paths=dt_paths, seed=dt_seed),
                )
                return {
                    "current_age": plan.current_age,
                    "full_portfolio_nis": plan.full_portfolio_nis,
                    "cgt_haircut_nis": plan.cgt_haircut_nis,
                    "reserve_raw_nis": plan.reserve_raw_nis,
                    "reserve_pv_nis": plan.reserve_pv_nis,
                    "deployable_nis": plan.deployable_nis,
                    "spend_central_nis": plan.spend_central_nis,
                    "spend_stress_nis": plan.spend_stress_nis,
                    "sigma_current": plan.sigma_current,
                    "tracks": [_track(t) for t in plan.tracks],
                    "stress_drawdown_age": plan.stress_drawdown_age,
                    "stress_preservation_age": plan.stress_preservation_age,
                    "spend_to_retire_now_nis": plan.spend_to_retire_now_nis,
                    "fx_stress_band": [
                        {"fx_adverse_pct": hit, "drawdown_age": age}
                        for hit, age in plan.fx_stress_band
                    ],
                    "assumptions": plan.assumptions,
                }

            get_or_compute("retirement.dual-track-plan", dt_version, _warm_dual_track)
        except Exception:  # noqa: BLE001
            pass

    except Exception:  # noqa: BLE001 — warming is best-effort; never propagate
        pass
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def warm_async(user_id: str) -> None:
    """Fire :func:`warm` on a daemon thread so it never blocks the request.

    No-ops (no thread spawned) when caching or warming is disabled. The thread
    is a daemon so it can't keep the process alive; ``warm`` itself swallows all
    errors so the thread can't crash noisily.
    """
    if not _enabled() or not _warm_enabled():
        return
    try:
        t = threading.Thread(
            target=warm, args=(user_id,), name=f"derived-cache-warm-{user_id}",
            daemon=True,
        )
        t.start()
    except Exception:  # noqa: BLE001 — spawning must never break the caller
        pass


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
    "warm",
    "warm_async",
    "clear",
    "cache_size",
]
