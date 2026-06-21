"""Tests for background pre-warming of the derived cache
(``argosy/services/derived_cache.py`` :func:`warm` / :func:`warm_async`).

Contract under test:

  * ``warm(user_id)`` populates the cache for the current version so a
    subsequent ``get_or_compute`` for the SAME (tag, version) is a HIT — the
    heavy compute runs once during warm and ZERO times on the cached read;
  * warming with no current plan (version_tuple -> None) is a safe no-op;
  * ``ARGOSY_DERIVED_CACHE`` disabled -> warm is a no-op;
  * ``ARGOSY_DERIVED_CACHE_WARM`` disabled -> warm is a no-op (lazy cache stays
    on);
  * ``warm`` never raises even when a heavy compute blows up;
  * ``warm_async`` spawns a daemon thread that completes the same population.

The heavy computations (build_overview, compute_derived_inputs, the MC
projections) are monkeypatched to cheap counters so the tests are fast and
deterministic, and the background session factory is stubbed.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from argosy.services import derived_cache


# A stable, non-None version so warming has something to key on.
_VERSION = ("v1", "ariel", 7, 42, 100, "2026-06-21T12:00:00")


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    monkeypatch.delenv("ARGOSY_DERIVED_CACHE", raising=False)
    monkeypatch.delenv("ARGOSY_DERIVED_CACHE_WARM", raising=False)
    derived_cache.clear()
    yield
    derived_cache.clear()


def _patch_session(monkeypatch):
    """Stub the background session factory with a harmless sentinel session."""
    sess = SimpleNamespace(closed=False)

    def _close():
        sess.closed = True

    sess.close = _close
    monkeypatch.setattr(derived_cache, "_new_sync_session", lambda: sess)
    return sess


def _patch_version(monkeypatch, version=_VERSION):
    monkeypatch.setattr(
        derived_cache, "version_tuple", lambda session, user_id: version
    )


def _patch_heavy_computes(monkeypatch):
    """Replace every heavy compute the warm() path imports with a counter.

    Returns the ``calls`` dict keyed by the symbol name. We patch the symbols on
    their DEFINING module so warm()'s ``from x import y`` picks up the stub.
    """
    calls = {
        "build_overview": 0,
        "compute_derived_inputs": 0,
        "canonical_feasible_dual_track": 0,
        "build_retirement_plan": 0,
    }

    def _overview(session, *, user_id):
        calls["build_overview"] += 1
        return SimpleNamespace(kind="overview")

    def _derived(session, *, user_id, **kw):
        calls["compute_derived_inputs"] += 1
        return {"kind": "derived-inputs"}

    def _feasible(*, session, user_id, target_p_solvent, assumptions):
        calls["canonical_feasible_dual_track"] += 1
        return SimpleNamespace(
            earliest_feasible_age=47, p_solvent_at_age=0.91, target_p_solvent=0.90,
            operational_target_age=49, statutory_lump_age=60, statutory_annuity_age=67,
            current_age=44, reserve_netted_nis=1.0, basis={},
        )

    def _plan(*, session, user_id, assumptions):
        calls["build_retirement_plan"] += 1
        return SimpleNamespace(
            current_age=44, full_portfolio_nis=1.0, cgt_haircut_nis=0.0,
            reserve_raw_nis=0.0, reserve_pv_nis=0.0, deployable_nis=1.0,
            spend_central_nis=1.0, spend_stress_nis=1.0, sigma_current=0.2,
            tracks=[], stress_drawdown_age=50, stress_preservation_age=55,
            spend_to_retire_now_nis=1.0, fx_stress_band=[], assumptions={},
        )

    monkeypatch.setattr(
        "argosy.services.overview_assembler.build_overview", _overview
    )
    monkeypatch.setattr(
        "argosy.services.retirement.derived_inputs.compute_derived_inputs", _derived
    )
    monkeypatch.setattr(
        "argosy.services.retirement.retirement_plan.canonical_feasible_dual_track",
        _feasible,
    )
    monkeypatch.setattr(
        "argosy.services.retirement.retirement_plan.build_retirement_plan", _plan
    )
    return calls


# ---------------------------------------------------------------------------
# warm populates the cache; cached reads don't recompute.
# ---------------------------------------------------------------------------
def test_warm_populates_cache_and_read_is_a_hit(monkeypatch):
    sess = _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    calls = _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    # Each heavy compute ran exactly once during warm.
    assert calls["build_overview"] == 1
    assert calls["compute_derived_inputs"] == 1
    assert calls["canonical_feasible_dual_track"] == 1
    assert calls["build_retirement_plan"] == 1

    # The session was opened and closed.
    assert sess.closed is True

    # All four hot entries are now cached.
    assert derived_cache.cache_size() == 4

    # A subsequent get_or_compute for overview's (tag, version) is a HIT:
    # the compute fn is NOT called again.
    sentinel = {"recomputed": False}

    def _should_not_run():
        sentinel["recomputed"] = True
        return "fresh"

    r = derived_cache.get_or_compute("overview", _VERSION, _should_not_run)
    assert sentinel["recomputed"] is False, "warmed entry must serve as a cache HIT"
    assert getattr(r, "kind", None) == "overview"


def test_warm_feasible_age_key_matches_route_suffix(monkeypatch):
    """The warmed feasible-age key must equal the route's
    ``version + ("feasible-age", 0.90, 1500, 42)`` so the route reads a HIT."""
    _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    route_version = _VERSION + ("feasible-age", 0.90, 1500, 42)
    sentinel = {"recomputed": False}

    def _should_not_run():
        sentinel["recomputed"] = True
        return {}

    derived_cache.get_or_compute(
        "retirement.feasible-age", route_version, _should_not_run
    )
    assert sentinel["recomputed"] is False, "route's exact key must hit the warm"


def test_warm_dual_track_key_matches_route_suffix(monkeypatch):
    _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    route_version = _VERSION + ("dual-track-plan", 1500, 42)
    sentinel = {"recomputed": False}

    def _should_not_run():
        sentinel["recomputed"] = True
        return {}

    derived_cache.get_or_compute(
        "retirement.dual-track-plan", route_version, _should_not_run
    )
    assert sentinel["recomputed"] is False


# ---------------------------------------------------------------------------
# no-op paths
# ---------------------------------------------------------------------------
def test_warm_no_current_plan_is_safe_noop(monkeypatch):
    _patch_session(monkeypatch)
    _patch_version(monkeypatch, version=None)  # no current plan
    calls = _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    assert all(v == 0 for v in calls.values()), "no plan -> nothing computed"
    assert derived_cache.cache_size() == 0


def test_warm_noop_when_cache_disabled(monkeypatch):
    monkeypatch.setenv("ARGOSY_DERIVED_CACHE", "0")
    _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    calls = _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    assert all(v == 0 for v in calls.values()), "cache off -> warm is a no-op"
    assert derived_cache.cache_size() == 0


def test_warm_noop_when_warming_disabled(monkeypatch):
    monkeypatch.setenv("ARGOSY_DERIVED_CACHE_WARM", "0")
    _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    calls = _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")

    assert all(v == 0 for v in calls.values()), "warm flag off -> no-op"
    assert derived_cache.cache_size() == 0


def test_warm_no_session_is_noop(monkeypatch):
    monkeypatch.setattr(derived_cache, "_new_sync_session", lambda: None)
    _patch_version(monkeypatch)
    calls = _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")
    assert all(v == 0 for v in calls.values())
    assert derived_cache.cache_size() == 0


# ---------------------------------------------------------------------------
# best-effort: never raises
# ---------------------------------------------------------------------------
def test_warm_swallows_compute_errors(monkeypatch):
    sess = _patch_session(monkeypatch)
    _patch_version(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("MC blew up")

    monkeypatch.setattr(
        "argosy.services.overview_assembler.build_overview", _boom
    )
    monkeypatch.setattr(
        "argosy.services.retirement.derived_inputs.compute_derived_inputs", _boom
    )
    monkeypatch.setattr(
        "argosy.services.retirement.retirement_plan.canonical_feasible_dual_track",
        _boom,
    )
    monkeypatch.setattr(
        "argosy.services.retirement.retirement_plan.build_retirement_plan", _boom
    )

    # Must not raise; session still gets closed.
    derived_cache.warm("ariel")
    assert sess.closed is True
    assert derived_cache.cache_size() == 0


def test_warm_swallows_version_error(monkeypatch):
    _patch_session(monkeypatch)

    def _boom(session, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(derived_cache, "version_tuple", _boom)
    _patch_heavy_computes(monkeypatch)

    derived_cache.warm("ariel")  # must not raise
    assert derived_cache.cache_size() == 0


# ---------------------------------------------------------------------------
# warm_async — daemon thread completes the population
# ---------------------------------------------------------------------------
def test_warm_async_spawns_thread_and_populates(monkeypatch):
    _patch_session(monkeypatch)
    _patch_version(monkeypatch)
    calls = _patch_heavy_computes(monkeypatch)

    done = threading.Event()
    real_warm = derived_cache.warm

    def _warm_and_signal(user_id):
        try:
            real_warm(user_id)
        finally:
            done.set()

    monkeypatch.setattr(derived_cache, "warm", _warm_and_signal)

    derived_cache.warm_async("ariel")
    assert done.wait(timeout=5.0), "warm_async must run warm on a thread"

    assert calls["build_overview"] == 1
    assert derived_cache.cache_size() == 4


def test_warm_async_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("ARGOSY_DERIVED_CACHE_WARM", "0")
    spawned = {"n": 0}

    def _warm(user_id):
        spawned["n"] += 1

    monkeypatch.setattr(derived_cache, "warm", _warm)
    derived_cache.warm_async("ariel")
    # Give any (erroneously) spawned thread a moment.
    import time

    time.sleep(0.1)
    assert spawned["n"] == 0, "disabled warming spawns no thread"
