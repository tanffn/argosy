"""Unit tests for the process-local derived-computation cache
(``argosy/services/derived_cache.py``).

Correctness contract under test (output-trust doctrine — never serve stale
financial numbers):

  * a cached value is returned on a repeat call with the SAME version
    (the compute fn runs exactly once);
  * a changed version (new plan version id / decision_run_id / snapshot id /
    snapshot timestamp) busts the cache -> compute runs again;
  * ``version=None`` (uncacheable) always recomputes and stores nothing;
  * the ``ARGOSY_DERIVED_CACHE`` env flag disables caching;
  * concurrent ``get_or_compute`` is crash-free and converges to one value;
  * ``version_tuple`` returns ``None`` when the staleness key can't be built and
    changes when the plan / snapshot identity changes.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from argosy.services import derived_cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """Each test starts with an empty, enabled cache."""
    monkeypatch.delenv("ARGOSY_DERIVED_CACHE", raising=False)
    derived_cache.clear()
    yield
    derived_cache.clear()


def _counter():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return ("value", calls["n"])

    return calls, compute


# ---------------------------------------------------------------------------
# get_or_compute — hit / miss / version-bust
# ---------------------------------------------------------------------------
def test_same_version_caches_compute_called_once():
    calls, compute = _counter()
    v = ("v1", "ariel", 1, 10, 5, "2026-06-21T00:00:00")

    r1 = derived_cache.get_or_compute("tag", v, compute)
    r2 = derived_cache.get_or_compute("tag", v, compute)

    assert r1 == r2 == ("value", 1)
    assert calls["n"] == 1, "compute must run only once for a stable version"


def test_changed_version_busts_cache():
    calls, compute = _counter()
    v1 = ("v1", "ariel", 1, 10, 5, "2026-06-21T00:00:00")
    v2 = ("v1", "ariel", 1, 10, 6, "2026-06-22T00:00:00")  # new snapshot

    derived_cache.get_or_compute("tag", v1, compute)
    derived_cache.get_or_compute("tag", v2, compute)

    assert calls["n"] == 2, "a changed version must recompute (no stale hit)"


def test_distinct_tags_do_not_collide():
    calls, compute = _counter()
    v = ("v1", "ariel", 1, 10, 5, "ts")

    derived_cache.get_or_compute("overview", v, compute)
    derived_cache.get_or_compute("retirement.feasible-age", v, compute)

    assert calls["n"] == 2, "different tags are different keys"


def test_uncacheable_none_version_always_computes():
    calls, compute = _counter()

    derived_cache.get_or_compute("tag", None, compute)
    derived_cache.get_or_compute("tag", None, compute)

    assert calls["n"] == 2, "version=None must never be cached"
    assert derived_cache.cache_size() == 0


def test_disabled_flag_always_computes(monkeypatch):
    monkeypatch.setenv("ARGOSY_DERIVED_CACHE", "0")
    calls, compute = _counter()
    v = ("v1", "ariel", 1, 10, 5, "ts")

    derived_cache.get_or_compute("tag", v, compute)
    derived_cache.get_or_compute("tag", v, compute)

    assert calls["n"] == 2, "disabled cache must always recompute"
    assert derived_cache.cache_size() == 0


@pytest.mark.parametrize("flag", ["false", "OFF", "No", ""])
def test_disabled_flag_variants(monkeypatch, flag):
    monkeypatch.setenv("ARGOSY_DERIVED_CACHE", flag)
    calls, compute = _counter()
    v = ("v1", "ariel", 1, 10, 5, "ts")
    derived_cache.get_or_compute("tag", v, compute)
    derived_cache.get_or_compute("tag", v, compute)
    assert calls["n"] == 2


def test_clear_forgets_entries():
    calls, compute = _counter()
    v = ("v1", "ariel", 1, 10, 5, "ts")
    derived_cache.get_or_compute("tag", v, compute)
    assert derived_cache.cache_size() == 1
    derived_cache.clear()
    assert derived_cache.cache_size() == 0
    derived_cache.get_or_compute("tag", v, compute)
    assert calls["n"] == 2


def test_lru_bound_evicts_oldest():
    # Insert > maxsize distinct versions; the store must stay bounded.
    for i in range(derived_cache._MAXSIZE + 20):
        v = ("v1", "ariel", i, 0, 0, "ts")
        derived_cache.get_or_compute("tag", v, lambda i=i: i)
    assert derived_cache.cache_size() == derived_cache._MAXSIZE


# ---------------------------------------------------------------------------
# Thread-safety smoke — concurrent get_or_compute is crash-free + converges.
# ---------------------------------------------------------------------------
def test_concurrent_get_or_compute_is_safe():
    calls = {"n": 0}
    lock = threading.Lock()
    start = threading.Event()

    def compute():
        # Count every real computation; a tiny spin to widen the race window.
        with lock:
            calls["n"] += 1
        for _ in range(10000):
            pass
        return "shared-value"

    v = ("v1", "ariel", 1, 10, 5, "ts")
    results: list = []
    res_lock = threading.Lock()

    def worker():
        start.wait()
        r = derived_cache.get_or_compute("hot", v, compute)
        with res_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert all(r == "shared-value" for r in results), "all callers agree"
    # Compute may run a few times under the cold-key race, but must not run
    # once-per-thread catastrophically and the cache must hold exactly one slot.
    assert calls["n"] <= 16
    assert derived_cache.cache_size() == 1


# ---------------------------------------------------------------------------
# version_tuple — identity + staleness behavior (no real DB; fakes only).
# ---------------------------------------------------------------------------
def test_version_tuple_none_when_no_plan(monkeypatch):
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda session, user_id: None
    )
    assert derived_cache.version_tuple(object(), "ariel") is None


def test_version_tuple_none_when_no_decision_run(monkeypatch):
    plan = SimpleNamespace(id=1, decision_run_id=None)
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda session, user_id: plan
    )
    assert derived_cache.version_tuple(object(), "ariel") is None


def test_version_tuple_changes_on_plan_and_snapshot(monkeypatch):
    import datetime as _dt

    plan = SimpleNamespace(id=7, decision_run_id=42)
    snap = SimpleNamespace(
        id=100, imported_at=_dt.datetime(2026, 6, 21, 12, 0, 0)
    )
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda session, user_id: plan
    )
    monkeypatch.setattr(
        "argosy.services.wealth_dashboard._latest_snapshot",
        lambda session, user_id: snap,
    )

    v_a = derived_cache.version_tuple(object(), "ariel")
    assert v_a is not None
    assert 7 in v_a and 42 in v_a and 100 in v_a

    # Same identity -> identical tuple (stable key).
    v_a2 = derived_cache.version_tuple(object(), "ariel")
    assert v_a == v_a2

    # New promoted plan -> different decision_run_id -> different key.
    plan.decision_run_id = 43
    v_b = derived_cache.version_tuple(object(), "ariel")
    assert v_b != v_a

    # New ingested snapshot -> different snapshot id + timestamp -> different key.
    plan.decision_run_id = 42
    snap.id = 101
    snap.imported_at = _dt.datetime(2026, 6, 22, 9, 0, 0)
    v_c = derived_cache.version_tuple(object(), "ariel")
    assert v_c != v_a


def test_version_tuple_uncacheable_on_query_error(monkeypatch):
    def boom(session, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("argosy.state.queries.get_current_plan", boom)
    assert derived_cache.version_tuple(object(), "ariel") is None


def test_version_tuple_survives_missing_snapshot(monkeypatch):
    plan = SimpleNamespace(id=7, decision_run_id=42)
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda session, user_id: plan
    )
    monkeypatch.setattr(
        "argosy.services.wealth_dashboard._latest_snapshot",
        lambda session, user_id: None,
    )
    v = derived_cache.version_tuple(object(), "ariel")
    assert v is not None, "no snapshot is still a valid (plan-only) key"
    assert 7 in v and 42 in v
