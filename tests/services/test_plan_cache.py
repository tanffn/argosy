"""Tests for caching the two slow ``/plan/draft/*`` endpoints
(``cashflow-projection`` + ``nvda-trajectory``) via the derived cache's
:func:`draft_aware_version` key.

Correctness contract (output-trust doctrine — NEVER serve a stale number):
the key must capture EVERY input each endpoint reads, so a change to any of
them busts the cache. These tests prove ``draft_aware_version`` changes when:

  * the pending draft's id or stamp changes,
  * (include_identity) the user's identity_yaml changes,
  * (include_tsv) the latest TSV's mtime/size changes,

and that the two endpoints memoize per key + bust on each of those changes.

The DB / filesystem inputs are stubbed so the tests are fast + deterministic;
the cache machinery itself is exercised for real.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from argosy.services import derived_cache


# A stable, non-None base version_tuple so draft_aware_version has something
# to extend. (The real version_tuple is stubbed per-test below.)
_BASE = ("v1", "ariel", 7, 42, 100, "2026-06-21T12:00:00")


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    monkeypatch.delenv("ARGOSY_DERIVED_CACHE", raising=False)
    monkeypatch.delenv("ARGOSY_DERIVED_CACHE_WARM", raising=False)
    derived_cache.clear()
    yield
    derived_cache.clear()


class _Draft:
    """A stand-in pending-draft row with id + stamp attributes."""

    def __init__(self, draft_id, imported_at=None, updated_at=None, accepted_at=None):
        self.id = draft_id
        self.imported_at = imported_at
        self.updated_at = updated_at
        self.accepted_at = accepted_at


class _Stamp:
    """Minimal isoformat()-able stamp."""

    def __init__(self, s):
        self._s = s

    def isoformat(self):
        return self._s


def _stub_base_version(monkeypatch, version=_BASE):
    monkeypatch.setattr(
        derived_cache, "version_tuple", lambda session, user_id: version
    )


def _stub_draft(monkeypatch, draft):
    monkeypatch.setattr(
        "argosy.state.queries.get_pending_draft",
        lambda session, user_id: draft,
    )


# ---------------------------------------------------------------------------
# draft_aware_version — base behaviour
# ---------------------------------------------------------------------------
def test_returns_none_when_base_version_is_none(monkeypatch):
    _stub_base_version(monkeypatch, version=None)
    _stub_draft(monkeypatch, None)
    assert derived_cache.draft_aware_version(object(), "ariel") is None


def test_no_draft_appends_none_none(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)
    key = derived_cache.draft_aware_version(object(), "ariel")
    assert key == _BASE + (None, None)


def test_changes_when_draft_id_changes(monkeypatch):
    _stub_base_version(monkeypatch)

    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("2026-06-20T00:00:00")))
    k1 = derived_cache.draft_aware_version(object(), "ariel")

    _stub_draft(monkeypatch, _Draft(12, imported_at=_Stamp("2026-06-20T00:00:00")))
    k2 = derived_cache.draft_aware_version(object(), "ariel")

    assert k1 != k2
    assert k1[-2:] == (11, "2026-06-20T00:00:00")
    assert k2[-2:] == (12, "2026-06-20T00:00:00")


def test_changes_when_draft_stamp_changes(monkeypatch):
    _stub_base_version(monkeypatch)

    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("2026-06-20T00:00:00")))
    k1 = derived_cache.draft_aware_version(object(), "ariel")

    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("2026-06-21T09:00:00")))
    k2 = derived_cache.draft_aware_version(object(), "ariel")

    assert k1 != k2


def test_draft_stamp_prefers_updated_then_accepted_then_imported(monkeypatch):
    _stub_base_version(monkeypatch)
    # updated_at wins over accepted_at + imported_at.
    _stub_draft(
        monkeypatch,
        _Draft(
            11,
            imported_at=_Stamp("I"),
            accepted_at=_Stamp("A"),
            updated_at=_Stamp("U"),
        ),
    )
    assert derived_cache.draft_aware_version(object(), "ariel")[-1] == "U"


def test_returns_none_when_draft_read_raises(monkeypatch):
    _stub_base_version(monkeypatch)

    def _boom(session, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("argosy.state.queries.get_pending_draft", _boom)
    assert derived_cache.draft_aware_version(object(), "ariel") is None


# ---------------------------------------------------------------------------
# include_identity — identity_yaml hash busts the key
# ---------------------------------------------------------------------------
def _stub_identity_yaml(monkeypatch, yaml_text):
    """Make session.execute(...).scalar_one_or_none() return a ctx with yaml."""
    ctx = SimpleNamespace(identity_yaml=yaml_text)

    class _Sess:
        def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: ctx)

    return _Sess()


def test_include_identity_appends_hash_and_busts_on_change(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)

    sess_a = _stub_identity_yaml(monkeypatch, "vest_schedule: A")
    k_a = derived_cache.draft_aware_version(
        sess_a, "ariel", include_identity=True
    )

    sess_b = _stub_identity_yaml(monkeypatch, "vest_schedule: B")
    k_b = derived_cache.draft_aware_version(
        sess_b, "ariel", include_identity=True
    )

    assert k_a != k_b
    # The identity hash is the final element; it differs for differing yaml.
    assert k_a[-1] != k_b[-1]
    assert isinstance(k_a[-1], str) and len(k_a[-1]) == 12


def test_include_identity_none_safe_when_no_ctx(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)

    class _Sess:
        def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    key = derived_cache.draft_aware_version(
        _Sess(), "ariel", include_identity=True
    )
    assert key[-1] is None  # absence of identity -> None sentinel, not a crash


# ---------------------------------------------------------------------------
# include_tsv — TSV (mtime, size) busts the key
# ---------------------------------------------------------------------------
def _stub_tsv(monkeypatch, mtime, size, path="X.tsv"):
    stat = SimpleNamespace(st_mtime=mtime, st_size=size)
    tsv = SimpleNamespace(stat=lambda: stat)
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: tsv
    )


def test_include_tsv_busts_on_mtime_change(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)

    _stub_tsv(monkeypatch, mtime=1000.0, size=500)
    k1 = derived_cache.draft_aware_version(object(), "ariel", include_tsv=True)

    _stub_tsv(monkeypatch, mtime=2000.0, size=500)  # new TSV ingested
    k2 = derived_cache.draft_aware_version(object(), "ariel", include_tsv=True)

    assert k1 != k2
    assert k1[-1] == (1000.0, 500)
    assert k2[-1] == (2000.0, 500)


def test_include_tsv_none_when_no_tsv(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None
    )
    key = derived_cache.draft_aware_version(object(), "ariel", include_tsv=True)
    assert key[-1] is None


def test_tsv_override_avoids_glob_and_busts_on_change(monkeypatch):
    """When the route passes a resolved path via tsv_override, draft_aware_version
    fingerprints THAT (no glob) and still busts when the file changes."""
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)

    # Sabotage the glob: if it's called, the test fails loudly.
    def _must_not_glob():
        raise AssertionError("tsv_override must prevent a glob")

    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", _must_not_glob
    )

    p1 = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=1000.0, st_size=500))
    p2 = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=2000.0, st_size=512))
    k1 = derived_cache.draft_aware_version(
        object(), "ariel", include_tsv=True, tsv_override=p1
    )
    k2 = derived_cache.draft_aware_version(
        object(), "ariel", include_tsv=True, tsv_override=p2
    )
    assert k1[-1] == (1000.0, 500)
    assert k2[-1] == (2000.0, 512)
    assert k1 != k2


def test_tsv_override_none_means_no_tsv(monkeypatch):
    """tsv_override=None means 'caller resolved: no TSV' -> None fingerprint,
    and still no glob."""
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv",
        lambda: (_ for _ in ()).throw(AssertionError("must not glob")),
    )
    key = derived_cache.draft_aware_version(
        object(), "ariel", include_tsv=True, tsv_override=None
    )
    assert key[-1] is None


def test_tsv_fingerprint_helper():
    assert derived_cache.tsv_fingerprint(None) is None
    p = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=5.0, st_size=9))
    assert derived_cache.tsv_fingerprint(p) == (5.0, 9)

    def _bad_stat():
        raise OSError("gone")

    assert derived_cache.tsv_fingerprint(SimpleNamespace(stat=_bad_stat)) is None


def test_include_tsv_none_on_error(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", _boom
    )
    key = derived_cache.draft_aware_version(object(), "ariel", include_tsv=True)
    assert key[-1] is None  # error -> None sentinel, never raises


def test_full_nvda_key_captures_all_inputs(monkeypatch):
    """include_identity + include_tsv together: key = base + draft + idhash + tsv."""
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("2026-06-20T00:00:00")))
    sess = _stub_identity_yaml(monkeypatch, "id: 1")
    _stub_tsv(monkeypatch, mtime=1000.0, size=500)

    key = derived_cache.draft_aware_version(
        sess, "ariel", include_identity=True, include_tsv=True
    )
    # base(6) + (draft_id, draft_stamp) + (idhash,) + (tsv,) = 10 elements
    assert len(key) == len(_BASE) + 4
    assert key[len(_BASE)] == 11                 # draft id
    assert key[len(_BASE) + 1] == "2026-06-20T00:00:00"  # draft stamp
    assert isinstance(key[-2], str)              # identity hash
    assert key[-1] == (1000.0, 500)              # tsv (mtime, size)


# ---------------------------------------------------------------------------
# Endpoint-level memoization: the two endpoints memoize per key + bust on change
# ---------------------------------------------------------------------------
def test_cashflow_projection_memoizes_per_key(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("S1")))

    version = derived_cache.draft_aware_version(object(), "ariel") + (
        "cashflow-projection", 30, None, 0.25, None, None, 0.08, 0.18, 0.0,
    )
    runs = {"n": 0}

    def _compute():
        runs["n"] += 1
        return {"series": []}

    a = derived_cache.get_or_compute("plan.cashflow-projection", version, _compute)
    b = derived_cache.get_or_compute("plan.cashflow-projection", version, _compute)
    assert runs["n"] == 1, "second call must be a cache HIT"
    assert a is b


def test_cashflow_projection_busts_on_draft_change(monkeypatch):
    _stub_base_version(monkeypatch)

    runs = {"n": 0}

    def _compute():
        runs["n"] += 1
        return {"series": []}

    suffix = ("cashflow-projection", 30, None, 0.25, None, None, 0.08, 0.18, 0.0)

    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("S1")))
    v1 = derived_cache.draft_aware_version(object(), "ariel") + suffix
    derived_cache.get_or_compute("plan.cashflow-projection", v1, _compute)

    _stub_draft(monkeypatch, _Draft(12, imported_at=_Stamp("S2")))  # draft changed
    v2 = derived_cache.draft_aware_version(object(), "ariel") + suffix
    derived_cache.get_or_compute("plan.cashflow-projection", v2, _compute)

    assert runs["n"] == 2, "a changed draft must recompute (no stale hit)"


def test_cashflow_projection_busts_on_whatif_param_change(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, _Draft(11, imported_at=_Stamp("S1")))
    base = derived_cache.draft_aware_version(object(), "ariel")

    runs = {"n": 0}

    def _compute():
        runs["n"] += 1
        return {"series": []}

    v_default = base + ("cashflow-projection", 30, None, 0.25, None, None, 0.08, 0.18, 0.0)
    v_slider = base + ("cashflow-projection", 30, None, 0.25, None, None, 0.04, 0.18, 0.0)
    derived_cache.get_or_compute("plan.cashflow-projection", v_default, _compute)
    derived_cache.get_or_compute("plan.cashflow-projection", v_slider, _compute)
    assert runs["n"] == 2, "a different what-if param is a distinct key -> recompute"


def test_nvda_trajectory_memoizes_and_busts_on_identity_change(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)
    _stub_tsv(monkeypatch, mtime=1000.0, size=500)

    runs = {"n": 0}

    def _compute():
        runs["n"] += 1
        return {"today_shares": 11471}

    sess_a = _stub_identity_yaml(monkeypatch, "vest: A")
    v1 = derived_cache.draft_aware_version(
        sess_a, "ariel", include_identity=True, include_tsv=True
    ) + ("nvda-trajectory",)
    derived_cache.get_or_compute("plan.nvda-trajectory", v1, _compute)
    derived_cache.get_or_compute("plan.nvda-trajectory", v1, _compute)
    assert runs["n"] == 1, "same key -> HIT"

    sess_b = _stub_identity_yaml(monkeypatch, "vest: B")  # sale-progress edit
    v2 = derived_cache.draft_aware_version(
        sess_b, "ariel", include_identity=True, include_tsv=True
    ) + ("nvda-trajectory",)
    derived_cache.get_or_compute("plan.nvda-trajectory", v2, _compute)
    assert runs["n"] == 2, "identity_yaml edit must recompute"


def test_nvda_trajectory_busts_on_tsv_change(monkeypatch):
    _stub_base_version(monkeypatch)
    _stub_draft(monkeypatch, None)
    sess = _stub_identity_yaml(monkeypatch, "vest: A")

    runs = {"n": 0}

    def _compute():
        runs["n"] += 1
        return {"today_shares": 11471}

    _stub_tsv(monkeypatch, mtime=1000.0, size=500)
    v1 = derived_cache.draft_aware_version(
        sess, "ariel", include_identity=True, include_tsv=True
    ) + ("nvda-trajectory",)
    derived_cache.get_or_compute("plan.nvda-trajectory", v1, _compute)

    _stub_tsv(monkeypatch, mtime=2000.0, size=512)  # new TSV
    v2 = derived_cache.draft_aware_version(
        sess, "ariel", include_identity=True, include_tsv=True
    ) + ("nvda-trajectory",)
    derived_cache.get_or_compute("plan.nvda-trajectory", v2, _compute)
    assert runs["n"] == 2, "a new TSV must recompute (today_shares may change)"
