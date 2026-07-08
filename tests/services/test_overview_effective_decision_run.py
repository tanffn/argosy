"""Regression — /retirement "Couldn't load your plan story" (2026-07-08).

Root cause: refinement/amendment plan versions are published with
``decision_run_id=None`` by design (scoped edit, no agent run), but two
consumers still read ``plan.decision_run_id`` DIRECTLY:

  * ``build_overview`` returned ``available=False`` ("Current plan has no
    decision run") the moment a refinement draft was accepted as current
    (live lineage v62(run 117) <- v63 <- v64 <- v66 <- v67-current), and
  * ``derived_cache.version_tuple`` returned ``None`` (uncacheable), which
    silently disabled the WHOLE derived cache + pre-warming.

The fix routes both through ``argosy.state.queries.effective_decision_run_id``
(own run id, else the nearest synthesis ancestor's — the same walk the
/accept gate already used). These tests pin the helper's semantics and both
consumers against a real in-memory DB lineage.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, PlanVersion, User
from argosy.state.queries import (
    effective_decision_run_id,
    nearest_ancestor_decision_run_id,
)


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _pv(session, *, role, label, run_id=None, parent=None) -> PlanVersion:
    pv = PlanVersion(
        user_id="ariel",
        role=role,
        version_label=label,
        raw_markdown="",
        decision_run_id=run_id,
        derived_from_id=(parent.id if parent is not None else None),
    )
    session.add(pv)
    session.commit()
    return pv


def _refinement_lineage(session, *, root_run_id=117) -> PlanVersion:
    """Mirror the live shape: root(run) <- three run-less hops <- current."""
    root = _pv(session, role="superseded", label="synth-root", run_id=root_run_id)
    a = _pv(session, role="superseded", label="refine-a", parent=root)
    b = _pv(session, role="superseded", label="refine-b", parent=a)
    return _pv(session, role="current", label="refine-current", parent=b)


# ---------------------------------------------------------------------------
# The shared helper.
# ---------------------------------------------------------------------------
def test_effective_run_id_prefers_own(session):
    pv = _pv(session, role="current", label="own-run", run_id=555)
    assert effective_decision_run_id(session, pv) == 555


def test_effective_run_id_walks_to_synthesis_ancestor(session):
    current = _refinement_lineage(session, root_run_id=117)
    assert current.decision_run_id is None
    assert effective_decision_run_id(session, current) == 117
    # Ancestor-only walk agrees (own is None).
    assert nearest_ancestor_decision_run_id(session, current) == 117


def test_effective_run_id_none_without_synthesis_anywhere(session):
    current = _refinement_lineage(session, root_run_id=None)
    assert effective_decision_run_id(session, current) is None


def test_effective_run_id_cycle_safe(session):
    pv = _pv(session, role="current", label="cycle")
    pv.derived_from_id = pv.id
    session.commit()
    assert effective_decision_run_id(session, pv) is None


# ---------------------------------------------------------------------------
# Consumer 1 — build_overview must resolve numbers via the ancestor's run
# instead of going unavailable.
# ---------------------------------------------------------------------------
def test_build_overview_available_for_refinement_current_plan(session, monkeypatch):
    import argosy.services.overview_assembler as oa
    from argosy.services import plan_numeric_resolver as resolver_module
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers

    current = _refinement_lineage(session, root_run_id=117)

    calls: list[int] = []

    def _spy_resolve(db, *, user_id, decision_run_id, **kwargs):
        calls.append(decision_run_id)
        # Empty bag: every chapter degrades (pending sentinels) but the
        # overview itself must be AVAILABLE.
        return ResolvedPlanNumbers(values={})

    # build_overview does `from argosy.services.plan_numeric_resolver import
    # resolve_plan_numbers` INSIDE the function, so patch the source module.
    monkeypatch.setattr(resolver_module, "resolve_plan_numbers", _spy_resolve)

    model = oa.build_overview(session, user_id="ariel")

    assert calls == [117], "resolver must run against the ancestor's run id"
    assert model.available is True
    assert model.reason is None
    assert model.plan_version_id == current.id
    assert model.decision_run_id == 117
    assert len(model.chapters) == 7


def test_build_overview_still_unavailable_with_no_synthesis_ancestor(session):
    import argosy.services.overview_assembler as oa

    current = _refinement_lineage(session, root_run_id=None)
    model = oa.build_overview(session, user_id="ariel")
    assert model.available is False
    assert model.reason is not None
    assert model.plan_version_id == current.id


# ---------------------------------------------------------------------------
# Consumer 2 — derived_cache.version_tuple must key on the effective run id
# (a refinement current plan must NOT disable the cache).
# ---------------------------------------------------------------------------
def test_version_tuple_uses_effective_run_id(session):
    from argosy.services import derived_cache

    current = _refinement_lineage(session, root_run_id=117)
    version = derived_cache.version_tuple(session, "ariel")
    assert version is not None, "refinement current plan must stay cacheable"
    assert version[2] == current.id
    assert version[3] == 117


def test_version_tuple_none_without_synthesis_anywhere(session):
    from argosy.services import derived_cache

    _refinement_lineage(session, root_run_id=None)
    assert derived_cache.version_tuple(session, "ariel") is None
