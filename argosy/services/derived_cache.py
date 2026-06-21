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

import hashlib
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

# Sentinel: "no tsv_override supplied" (distinct from an explicit None, which
# means "the caller already resolved that there is no TSV").
_UNSET = object()


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


def tsv_fingerprint(tsv) -> Optional[tuple]:
    """Return ``(mtime, size)`` for a TSV path, or ``None`` on any failure.

    The cheap, structural staleness probe for the latest portfolio TSV: a new
    file (different mtime/size) yields a different fingerprint -> the key busts.
    Accepts a ``pathlib.Path``-like (anything with ``.stat()``) or ``None``.
    Never raises.
    """
    if tsv is None:
        return None
    try:
        st = tsv.stat()
        return (st.st_mtime, st.st_size)
    except Exception:  # noqa: BLE001 — None on any error
        return None


def draft_aware_version(
    session,
    user_id: str,
    *,
    include_identity: bool = False,
    include_tsv: bool = False,
    tsv_override: Any = _UNSET,
) -> Optional[tuple]:
    """Staleness key for the ``/draft/*`` plan endpoints, or ``None``.

    These endpoints read MORE than ``(current plan, snapshot)`` — they also
    read the PENDING DRAFT (a fallback share-ceiling target), and optionally the
    user's ``UserContext.identity_yaml`` (vest schedule + NVDA sale progress) and
    the latest portfolio TSV file (today's NVDA share count). The bare
    :func:`version_tuple` does NOT capture those, so caching keyed on it alone
    could serve a STALE cross-state value — forbidden by the output-trust
    doctrine. This helper extends ``version_tuple`` with every extra input so the
    resulting key fully determines the endpoint's output.

    Build order (each segment is appended, never reordered):

      1. ``version_tuple(session, user_id)`` — current plan id + decision_run_id +
         snapshot id + imported_at. If that is ``None`` (no current plan ->
         uncacheable), this returns ``None`` immediately.
      2. The pending draft's identity: ``(draft_id, draft_stamp_iso)``. The stamp
         is the first present of ``updated_at`` / ``accepted_at`` / ``imported_at``
         (isoformat). When there is NO pending draft, append ``(None, None)`` —
         the endpoints fall back to the current plan, and that absence is itself
         part of the key (so a draft appearing/disappearing busts it).
      3. When ``include_identity``: a short sha1 of ``UserContext.identity_yaml``
         (``None`` when absent) so any vest-schedule / sale-progress edit busts
         the key.
      4. When ``include_tsv``: the latest TSV file's ``(mtime, size)`` (``None``
         on any error) so a freshly-ingested TSV busts the key.

    NEVER raises — any failure degrades to ``None`` (uncacheable -> always
    compute). A partial/ambiguous key is never returned: a per-segment failure
    appends an explicit ``None`` sentinel (distinct from a real value) rather
    than dropping the segment.
    """
    try:
        base = version_tuple(session, user_id)
        if base is None:
            return None

        # --- pending-draft identity ---------------------------------------
        draft_id = None
        draft_stamp = None
        try:
            from argosy.state.queries import get_pending_draft

            draft = get_pending_draft(session, user_id)
            if draft is not None:
                draft_id = int(draft.id)
                for attr in ("updated_at", "accepted_at", "imported_at"):
                    stamp = getattr(draft, attr, None)
                    if stamp is not None:
                        draft_stamp = (
                            stamp.isoformat()
                            if hasattr(stamp, "isoformat")
                            else str(stamp)
                        )
                        break
        except Exception:  # noqa: BLE001 — uncacheable on any draft-read failure
            return None

        key = base + (draft_id, draft_stamp)

        # --- identity_yaml hash (vest schedule + NVDA sale progress) ------
        if include_identity:
            identity_hash = None
            try:
                from argosy.state.models import UserContext

                ctx = session.execute(
                    select_user_context(UserContext, user_id)
                ).scalar_one_or_none()
                raw = getattr(ctx, "identity_yaml", None) if ctx is not None else None
                if raw:
                    identity_hash = hashlib.sha1(
                        raw.encode("utf-8")
                    ).hexdigest()[:12]
            except Exception:  # noqa: BLE001 — None-safe; absence is part of key
                identity_hash = None
            key = key + (identity_hash,)

        # --- latest TSV file fingerprint (mtime + size) -------------------
        # The fingerprint is the staleness probe for the TSV the endpoint reads
        # (today's NVDA share count). A new file -> new fingerprint -> bust.
        #
        # Finding the latest TSV is an ``rglob`` over ARGOSY_HOME (~2-5s) — the
        # SAME walk the endpoint itself does. To avoid globbing TWICE per request
        # (once here, once in the compute), the route resolves the path ONCE and
        # threads it in via ``tsv_override``; we fingerprint that instead of
        # re-globbing. ``tsv_override=None`` means "caller resolved: no TSV".
        # When no override is given we fall back to resolving it ourselves.
        if include_tsv:
            try:
                if tsv_override is _UNSET:
                    from argosy.api.routes.portfolio import _find_latest_tsv

                    tsv = _find_latest_tsv()
                else:
                    tsv = tsv_override
                tsv_stamp = tsv_fingerprint(tsv)
            except Exception:  # noqa: BLE001 — None on any error
                tsv_stamp = None
            key = key + (tsv_stamp,)

        return key
    except Exception:  # noqa: BLE001 — uncacheable on any failure
        return None


def select_user_context(UserContext, user_id):
    """Build the ``select(UserContext).where(...)`` used by draft_aware_version.

    Factored out so the import of ``sqlalchemy.select`` is local and the helper
    above stays readable. Returns a SQLAlchemy ``Select``.
    """
    from sqlalchemy import select

    return select(UserContext).where(UserContext.user_id == user_id)


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

    Additionally warmed (matching each route's exact key/param defaults):

      * ``"retirement.scenarios"`` -> ``run_retirement_scenarios`` at the route
        default (retirement_age=49.0, n_paths=2000, seed=42) PLUS the dual-track's
        canonical drawdown + preservation ages, key
        ``("scenarios", retirement_age, n_paths, seed)``.
      * ``"portfolio.wealth-dashboard"`` -> ``compute_wealth_dashboard`` (default
        exclude_nvda=False), key ``("wealth-dashboard", exclude_nvda, today_iso)``.
      * ``"portfolio.allocation-breakdown"`` -> ``get_allocation_breakdown``
        (default exclude_nvda=False), key ``("allocation-breakdown", exclude_nvda)``.
      * ``"portfolio.real-estate"`` -> ``_compute_real_estate``, key ``("real-estate",)``.
      * ``"plan.allocation-glidepath"`` -> ``compute_allocation_glidepath``, key
        ``("allocation-glidepath", today_iso)``.
      * ``"plan.cashflow-projection"`` -> ``_compute_cashflow_projection`` at the
        route DEFAULTS, key ``draft_aware_version(...) + ("cashflow-projection",
        30, None, 0.25, None, None, 0.08, 0.18, 0.0)``. Deterministic (no random
        seed) so the warmed value is reproducible.
      * ``"plan.nvda-trajectory"`` -> ``_compute_nvda_trajectory``, key
        ``draft_aware_version(..., include_identity=True, include_tsv=True) +
        ("nvda-trajectory",)`` — captures plan+snapshot+draft+identity_yaml+TSV.

    NOT warmed (and NOT cached): the unseeded-MC plan endpoints
    (cashflow-monte-carlo / plan-series) — they default seed=None (random), so
    a cached value would pin one draw. Those stay lazy + uncached.
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

        # --- retirement.scenarios (route default params + canonical ages) -
        # Route: GET /retirement/projection/scenarios
        #   retirement_age=49.0, n_paths=2000, seed=42 (seed now DEFAULTS to 42)
        #   version + ("scenarios", retirement_age, n_paths, seed)
        # The card requests a specific age; we warm the route default plus the
        # dual-track's canonical drawdown + preservation ages so a fresh load at
        # any of those hits the warm. Each is best-effort + individually guarded.
        try:
            from argosy.services.retirement.scenario_mc import (
                run_retirement_scenarios,
            )

            sc_paths, sc_seed = 2000, 42
            # Default age + the canonical dual-track ages (deduped). The
            # dual-track ages are pulled best-effort from a cheap recompute via
            # the SAME assumptions; if that fails we still warm the default.
            ages: list[float] = [49.0]
            try:
                from argosy.services.retirement.retirement_plan import (
                    RetirementAssumptions,
                    build_retirement_plan,
                )

                _p = build_retirement_plan(
                    session=session,
                    user_id=user_id,
                    assumptions=RetirementAssumptions(n_paths=1500, seed=42),
                )
                for t in _p.tracks:
                    for a in (
                        getattr(t, "drawdown_age", None),
                        getattr(t, "preservation_age", None),
                    ):
                        if a is not None:
                            ages.append(float(a))
            except Exception:  # noqa: BLE001 — default age still warmed below
                pass

            seen_ages: set[float] = set()
            for age in ages:
                if age in seen_ages:
                    continue
                seen_ages.add(age)
                sc_version = version + ("scenarios", age, sc_paths, sc_seed)

                def _warm_scenarios(_age=age):
                    return run_retirement_scenarios(
                        user_id=user_id,
                        session=session,
                        retirement_age=_age,
                        n_paths=sc_paths,
                        seed=sc_seed,
                    )

                try:
                    get_or_compute(
                        "retirement.scenarios", sc_version, _warm_scenarios
                    )
                except Exception:  # noqa: BLE001 — one age failing is fine
                    pass
        except Exception:  # noqa: BLE001
            pass

        # --- portfolio.wealth-dashboard (route default params) ------------
        # Route: GET /portfolio/wealth-dashboard
        #   exclude_nvda=False; version + ("wealth-dashboard", exclude_nvda,
        #   today_iso). today_iso folds the date.today() anchor of the cash-
        #   runway / RSU blocks so a day rollover busts the key.
        try:
            import datetime as _dt

            from argosy.api.routes.wealth_dashboard import WealthDashboardDTO
            from argosy.services.wealth_dashboard import (
                compute_wealth_dashboard,
                wealth_dashboard_to_dict,
            )

            wd_exclude = False
            wd_today = _dt.date.today().isoformat()
            wd_version = version + ("wealth-dashboard", wd_exclude, wd_today)

            def _warm_wealth_dashboard():
                dash = compute_wealth_dashboard(
                    session, user_id=user_id, exclude_nvda=wd_exclude
                )
                return WealthDashboardDTO(**wealth_dashboard_to_dict(dash))

            get_or_compute(
                "portfolio.wealth-dashboard", wd_version, _warm_wealth_dashboard
            )
        except Exception:  # noqa: BLE001
            pass

        # --- portfolio.allocation-breakdown (route default params) --------
        # Route: GET /portfolio/allocation-breakdown
        #   exclude_nvda=False; version + ("allocation-breakdown", exclude_nvda)
        try:
            from argosy.api.routes.portfolio import get_allocation_breakdown

            ab_exclude = False
            ab_version = version + ("allocation-breakdown", ab_exclude)
            get_or_compute(
                "portfolio.allocation-breakdown",
                ab_version,
                lambda: get_allocation_breakdown(
                    user_id=user_id, exclude_nvda=ab_exclude, db=session
                ),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- portfolio.real-estate ----------------------------------------
        # Route: GET /portfolio/real-estate; version + ("real-estate",)
        try:
            from argosy.api.routes.portfolio import _compute_real_estate

            re_version = version + ("real-estate",)
            get_or_compute(
                "portfolio.real-estate",
                re_version,
                lambda: _compute_real_estate(session, user_id),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- plan.allocation-glidepath (route default params) -------------
        # Route: GET /plan/current/allocation-glidepath
        #   version + ("allocation-glidepath", today_iso)
        try:
            import datetime as _dt2

            from argosy.api.routes.plan import _glidepath_to_response
            from argosy.services.allocation_glidepath import (
                compute_allocation_glidepath,
            )

            gp_today = _dt2.datetime.now(_dt2.timezone.utc).date()
            gp_version = version + ("allocation-glidepath", gp_today.isoformat())

            def _warm_glidepath():
                out = compute_allocation_glidepath(session, user_id, gp_today)
                return None if out is None else _glidepath_to_response(out)

            get_or_compute(
                "plan.allocation-glidepath", gp_version, _warm_glidepath
            )
        except Exception:  # noqa: BLE001
            pass

        # --- plan.cashflow-projection (route default params) --------------
        # Route: GET /plan/draft/cashflow-projection
        #   draft_aware_version(db,user_id) + ("cashflow-projection", years,
        #   retirement_age, tax_rate, portfolio_value_usd_override,
        #   monthly_expenses_nis_override, mu_nominal_annual, sigma_annual,
        #   lifestyle_drift_annual). Defaults: years=30, retirement_age=None,
        #   tax_rate=0.25, overrides=None, mu=0.08, sigma=0.18, drift=0.0.
        try:
            from argosy.api.routes.plan import _compute_cashflow_projection

            cf_base = draft_aware_version(session, user_id)
            if cf_base is not None:
                cf_version = cf_base + (
                    "cashflow-projection",
                    30,      # years
                    None,    # retirement_age (canonical default resolved inside)
                    0.25,    # tax_rate
                    None,    # portfolio_value_usd_override
                    None,    # monthly_expenses_nis_override
                    0.08,    # mu_nominal_annual
                    0.18,    # sigma_annual
                    0.0,     # lifestyle_drift_annual
                )

                def _warm_cashflow():
                    return _compute_cashflow_projection(
                        db=session,
                        user_id=user_id,
                        years=30,
                        retirement_age=None,
                        tax_rate=0.25,
                        portfolio_value_usd_override=None,
                        monthly_expenses_nis_override=None,
                        mu_nominal_annual=0.08,
                        sigma_annual=0.18,
                        lifestyle_drift_annual=0.0,
                    )

                get_or_compute(
                    "plan.cashflow-projection", cf_version, _warm_cashflow
                )
        except Exception:  # noqa: BLE001
            pass

        # --- plan.nvda-trajectory (no params) -----------------------------
        # Route: GET /plan/draft/nvda-trajectory
        #   draft_aware_version(db,user_id, include_identity=True,
        #   include_tsv=True) + ("nvda-trajectory",)
        try:
            from argosy.api.routes.plan import _compute_nvda_trajectory

            # Resolve the TSV once (matches the route) so the warmed key + value
            # use the SAME single-glob path the route reads.
            try:
                from argosy.api.routes.portfolio import _find_latest_tsv

                _nt_tsv = _find_latest_tsv()
            except Exception:  # noqa: BLE001
                _nt_tsv = None

            nt_base = draft_aware_version(
                session,
                user_id,
                include_identity=True,
                include_tsv=True,
                tsv_override=_nt_tsv,
            )
            if nt_base is not None:
                nt_version = nt_base + ("nvda-trajectory",)
                get_or_compute(
                    "plan.nvda-trajectory",
                    nt_version,
                    lambda: _compute_nvda_trajectory(
                        user_id=user_id, db=session, tsv=_nt_tsv
                    ),
                )
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
    "draft_aware_version",
    "tsv_fingerprint",
    "get_or_compute",
    "warm",
    "warm_async",
    "clear",
    "cache_size",
]
