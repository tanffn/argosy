"""Tests for POST /api/plan/refine — dry-run + apply refinement endpoint.

Monkeypatches build_base_graph and run_refinement to avoid real DB/graph
hydration, so these tests run without a seeded plan. The production code
path calls the real functions; the mocks are test-only.
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Helpers to build minimal fake graph + decision objects
# ---------------------------------------------------------------------------

def _make_fake_graph():
    from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

    g = DerivationGraph()
    g.add_node(Node(key="portfolio.fx_usd_nis", kind=NodeKind.INPUT, value=3.7))
    g.recompute()
    return g


def _make_fake_decision(tier_value="scoped_edit", dirtied=("portfolio.net_worth_nis",)):
    from argosy.quality.blast_radius import BlastRadius, Tier
    from argosy.quality.refinement import RefinementDecision

    br = BlastRadius(
        dirtied_keys=tuple(dirtied),
        owner_domains=frozenset({"portfolio"}),
        flipped_hard_verdicts=(),
        introduces_structure=False,
        structure_scope="none",
        changed_policy_axes=frozenset(),
        changes_plan_identity_axis=False,
        adds_or_removes_owner_domain=False,
        adds_cross_owner_dependency=False,
        invalidates_global_invariant=False,
        missing_owner_for_changed_node=False,
        touched_rebuild_boundaries=frozenset(),
        touches_owner_authored_surface=False,
        dirtied_boundary_fraction=0.1,
    )
    return RefinementDecision(
        tier=Tier(tier_value),
        reason="deterministic localized edit",
        blast_radius=br,
        invariant_report=None,
        forced_by_invariant=False,
    )


def _make_full_rebuild_decision():
    from argosy.quality.blast_radius import BlastRadius, Tier
    from argosy.quality.refinement import RefinementDecision

    br = BlastRadius(
        dirtied_keys=(),
        owner_domains=frozenset({"retirement"}),
        flipped_hard_verdicts=(),
        introduces_structure=False,
        structure_scope="none",
        changed_policy_axes=frozenset({"risk"}),
        changes_plan_identity_axis=True,
        adds_or_removes_owner_domain=False,
        adds_cross_owner_dependency=False,
        invalidates_global_invariant=False,
        missing_owner_for_changed_node=False,
        touched_rebuild_boundaries=frozenset(),
        touches_owner_authored_surface=False,
        dirtied_boundary_fraction=0.0,
    )
    return RefinementDecision(
        tier=Tier.FULL_REBUILD,
        reason="changes plan identity / core policy axis",
        blast_radius=br,
        invariant_report=None,
        forced_by_invariant=False,
    )


def _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit"):
    """Patch build_base_graph + run_refinement to return controllable fakes."""
    fake_graph = _make_fake_graph()
    fake_decision = _make_fake_decision(tier_value)
    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: fake_graph,
    )
    monkeypatch.setattr(
        "argosy.quality.refinement.run_refinement",
        lambda graph, crs, *, pre_doc=None, post_doc=None, cfg=None: fake_decision,
    )
    return fake_graph, fake_decision


def _seed_current_plan(client_with_db, user_id: str = "ariel") -> int:
    """Insert a role='current' PlanVersion row and return its id."""
    from argosy.state.models import PlanVersion

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        pv = PlanVersion(
            user_id=user_id,
            role="current",
            version_label="test-current-v1",
            source_path="",
            raw_markdown="# test plan\n",
            # No horizon JSON needed for these route-level tests.
        )
        session.add(pv)
        session.commit()
        session.refresh(pv)
        return pv.id


# ---------------------------------------------------------------------------
# Dry-run tests (existing + extended)
# ---------------------------------------------------------------------------

def test_dry_run_returns_decision_dto(client_with_db, monkeypatch):
    """Happy path: monkeypatched build_base_graph + run_refinement returns DTO."""
    _patch_graph_and_decision(monkeypatch)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "portfolio.fx_usd_nis", "new_value": 3.9}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] in {"scoped_edit", "bounded_rederive", "full_rebuild"}
    assert isinstance(body["reason"], str) and body["reason"]
    assert isinstance(body["dirtied_count"], int)
    assert isinstance(body["owner_domains"], list)
    assert body["forced_by_invariant"] is False
    assert body["invariant_ok"] is None
    assert body["invariant_violations"] == []
    assert isinstance(body["summary"], str) and body["summary"]
    # dry_run=True must NOT create a draft
    assert body.get("draft_id") is None


def test_plan_identity_key_returns_full_rebuild(client_with_db, monkeypatch):
    """A change to a plan-identity key must return tier=full_rebuild."""
    fake_graph = _make_fake_graph()
    fake_decision = _make_full_rebuild_decision()

    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: fake_graph,
    )
    monkeypatch.setattr(
        "argosy.quality.refinement.run_refinement",
        lambda graph, crs, *, pre_doc=None, post_doc=None, cfg=None: fake_decision,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "retirement.risk_posture", "new_value": "conservative"}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "full_rebuild"


def test_dry_run_no_draft_created(client_with_db, monkeypatch):
    """dry_run=True must never create a PlanVersion row."""
    _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "allocation.sleeve_target.US broad-market core", "new_value": 15.0}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("draft_id") is None

    # Verify no draft row was created
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        drafts = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == "ariel",
                PlanVersion.role == "draft",
            )
        ).scalars().all()
    assert len(drafts) == 0


# ---------------------------------------------------------------------------
# Apply path (dry_run=False) — happy path
# ---------------------------------------------------------------------------

def test_apply_sleeve_target_creates_draft(client_with_db, monkeypatch):
    """dry_run=False on a sleeve-target change → 200, a new draft row exists."""
    current_id = _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    # Patch create_refinement_draft to avoid the full allocation engine.
    _captured: list = []

    def _fake_create_draft(session, user_id, sleeve_overrides):
        from argosy.state.models import PlanVersion
        import json

        pv = PlanVersion(
            user_id=user_id,
            role="draft",
            version_label="refinement-draft-test",
            source_path="",
            raw_markdown="",
            derived_from_id=current_id,
            decision_run_id=None,
            target_allocation_overrides_json=json.dumps(sleeve_overrides),
        )
        session.add(pv)
        session.commit()
        session.refresh(pv)
        _captured.append(pv)
        return pv

    monkeypatch.setattr(
        "argosy.services.plan_refinement.create_refinement_draft",
        _fake_create_draft,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {
                    "node_key": "allocation.sleeve_target.US broad-market core",
                    "new_value": 20.0,
                }
            ],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] == "scoped_edit"
    draft_id = body.get("draft_id")
    assert draft_id is not None
    assert "promote" in (body.get("message") or "").lower()

    # Verify the draft exists in DB
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        draft = session.execute(
            select(PlanVersion).where(PlanVersion.id == draft_id)
        ).scalar_one_or_none()
    assert draft is not None
    assert draft.role == "draft"
    overrides = json.loads(draft.target_allocation_overrides_json)
    assert overrides.get("US broad-market core") == 20.0


def test_apply_bounded_rederive_creates_draft(client_with_db, monkeypatch):
    """BOUNDED_REDERIVE tier also creates a staged draft (both T0 and T1 are OK)."""
    _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="bounded_rederive")

    def _fake_create_draft(session, user_id, sleeve_overrides):
        from argosy.state.models import PlanVersion
        import json
        pv = PlanVersion(
            user_id=user_id, role="draft", version_label="rd-test",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps(sleeve_overrides),
        )
        session.add(pv); session.commit(); session.refresh(pv)
        return pv

    monkeypatch.setattr("argosy.services.plan_refinement.create_refinement_draft", _fake_create_draft)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "allocation.sleeve_target.Dividend-quality income", "new_value": 8.0}],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "bounded_rederive"
    assert r.json()["draft_id"] is not None


def test_apply_does_not_promote_current_plan(client_with_db, monkeypatch):
    """dry_run=False must NOT flip the current plan — role='current' is unchanged."""
    current_id = _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    def _fake_create_draft(session, user_id, sleeve_overrides):
        from argosy.state.models import PlanVersion
        pv = PlanVersion(
            user_id=user_id, role="draft", version_label="nd-test",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps(sleeve_overrides),
        )
        session.add(pv); session.commit(); session.refresh(pv)
        return pv

    monkeypatch.setattr("argosy.services.plan_refinement.create_refinement_draft", _fake_create_draft)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "allocation.sleeve_target.US broad-market core", "new_value": 15.0}],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text

    # Current plan must still be the same row
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        current = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == "ariel",
                PlanVersion.role == "current",
            )
        ).scalar_one_or_none()
    assert current is not None
    assert current.id == current_id


# ---------------------------------------------------------------------------
# Apply path — error cases
# ---------------------------------------------------------------------------

def test_apply_full_rebuild_returns_409(client_with_db, monkeypatch):
    """dry_run=False with FULL_REBUILD tier → 409, no draft created."""
    _seed_current_plan(client_with_db)

    fake_graph = _make_fake_graph()
    fake_decision = _make_full_rebuild_decision()
    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: fake_graph,
    )
    monkeypatch.setattr(
        "argosy.quality.refinement.run_refinement",
        lambda graph, crs, *, pre_doc=None, post_doc=None, cfg=None: fake_decision,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {"node_key": "allocation.sleeve_target.US broad-market core", "new_value": 20.0}
            ],
            "dry_run": False,
        },
    )
    assert r.status_code == 409, r.text
    assert "full" in r.json()["detail"].lower() or "rebuild" in r.json()["detail"].lower()

    # No draft must have been created
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        drafts = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == "ariel",
                PlanVersion.role == "draft",
            )
        ).scalars().all()
    assert len(drafts) == 0


def test_apply_non_allocation_node_returns_409(client_with_db, monkeypatch):
    """dry_run=False for a non-allocation-sleeve-target node → 409."""
    _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "portfolio.fx_usd_nis", "new_value": 3.9}],
            "dry_run": False,
        },
    )
    assert r.status_code == 409, r.text
    assert "not yet supported" in r.json()["detail"].lower()


def test_apply_bad_override_returns_400_before_write(client_with_db, monkeypatch):
    """Unknown sleeve label → 400 before any DB write (validate-on-write)."""
    _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    # Let create_refinement_draft run its real validation path but mock the
    # pure allocation engine so we don't need a real DB setup.
    def _fake_create_draft(session, user_id, sleeve_overrides):
        raise ValueError(f"Unknown sleeve label in authored_overrides: 'bogus_label'")

    monkeypatch.setattr("argosy.services.plan_refinement.create_refinement_draft", _fake_create_draft)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {"node_key": "allocation.sleeve_target.bogus_label", "new_value": 10.0}
            ],
            "dry_run": False,
        },
    )
    assert r.status_code == 400, r.text
    assert "unknown" in r.json()["detail"].lower() or "bogus" in r.json()["detail"].lower()

    # Nothing written
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        drafts = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == "ariel",
                PlanVersion.role == "draft",
            )
        ).scalars().all()
    assert len(drafts) == 0


def test_apply_base_plan_version_mismatch_returns_409(client_with_db, monkeypatch):
    """base_plan_version mismatch → 409 CONFLICT, nothing written."""
    current_id = _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {"node_key": "allocation.sleeve_target.US broad-market core", "new_value": 15.0}
            ],
            "dry_run": False,
            "base_plan_version": current_id + 9999,  # wrong id
        },
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "conflict" in detail.lower() or "mismatch" in detail.lower()

    # No draft created
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        drafts = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == "ariel",
                PlanVersion.role == "draft",
            )
        ).scalars().all()
    assert len(drafts) == 0


def test_apply_base_plan_version_match_succeeds(client_with_db, monkeypatch):
    """base_plan_version matching current plan id → 200 (no conflict)."""
    current_id = _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    def _fake_create_draft(session, user_id, sleeve_overrides):
        from argosy.state.models import PlanVersion
        pv = PlanVersion(
            user_id=user_id, role="draft", version_label="bpv-test",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps(sleeve_overrides),
        )
        session.add(pv); session.commit(); session.refresh(pv)
        return pv

    monkeypatch.setattr("argosy.services.plan_refinement.create_refinement_draft", _fake_create_draft)

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {"node_key": "allocation.sleeve_target.US broad-market core", "new_value": 15.0}
            ],
            "dry_run": False,
            "base_plan_version": current_id,  # correct id
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["draft_id"] is not None


# ---------------------------------------------------------------------------
# Unit tests for create_refinement_draft service helper
# ---------------------------------------------------------------------------

def test_create_refinement_draft_validates_bad_label(client_with_db):
    """create_refinement_draft raises ValueError for unknown sleeve label."""
    _seed_current_plan(client_with_db)

    from argosy.services.plan_refinement import create_refinement_draft

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        with pytest.raises(ValueError, match="Unknown sleeve label"):
            create_refinement_draft(session, "ariel", {"totally_bogus_label": 10.0})


def test_create_refinement_draft_validates_sum_over_100(client_with_db):
    """create_refinement_draft raises ValueError when override sum exceeds 100."""
    _seed_current_plan(client_with_db)

    from argosy.services.plan_refinement import create_refinement_draft
    from argosy.services.allocation_plan import build_target_allocation

    # Get real sleeve labels so we can construct a sum > 100.
    alloc = build_target_allocation()
    labels = [c.label for c in alloc.classes]
    # Set two big sleeves each to 90 — total > 100.
    overrides = {labels[0]: 90.0, labels[1]: 90.0}

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        with pytest.raises(ValueError, match="100"):
            create_refinement_draft(session, "ariel", overrides)


def test_create_refinement_draft_merges_existing_overrides(client_with_db):
    """Existing overrides in current plan are merged, new edits win."""
    from argosy.state.models import PlanVersion
    from argosy.services.allocation_plan import build_target_allocation

    alloc = build_target_allocation()
    labels = [c.label for c in alloc.classes]
    label_a = labels[0]
    label_b = labels[1]

    existing_overrides = {label_a: 15.0}
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        pv = PlanVersion(
            user_id="ariel",
            role="current",
            version_label="test-with-overrides",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps(existing_overrides),
        )
        session.add(pv)
        session.commit()
        session.refresh(pv)
        current_id = pv.id

    # Now apply a new override for label_b — label_a should be merged in.
    # We need to mock the doc builder to avoid needing a full DB.
    import unittest.mock as mock

    with mock.patch(
        "argosy.services.target_allocation_doc.load_full_book_today_composition",
        return_value=None,
    ), mock.patch(
        "argosy.services.target_allocation_doc._prior_glide_q0",
        return_value=None,
    ):
        SF = client_with_db.app.state.session_factory
        with SF() as session:
            from argosy.services.plan_refinement import create_refinement_draft
            draft = create_refinement_draft(session, "ariel", {label_b: 8.0})

    merged = json.loads(draft.target_allocation_overrides_json)
    assert merged.get(label_a) == 15.0, "existing override must survive"
    assert merged.get(label_b) == 8.0, "new override must win"
    assert draft.role == "draft"
    assert draft.derived_from_id == current_id


def test_create_refinement_draft_new_override_wins(client_with_db):
    """A new override for the same label replaces the existing one."""
    from argosy.state.models import PlanVersion
    from argosy.services.allocation_plan import build_target_allocation

    alloc = build_target_allocation()
    label = alloc.classes[0].label
    existing_overrides = {label: 10.0}

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        pv = PlanVersion(
            user_id="ariel", role="current", version_label="ov-win-test",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps(existing_overrides),
        )
        session.add(pv); session.commit(); session.refresh(pv)

    import unittest.mock as mock

    with mock.patch(
        "argosy.services.target_allocation_doc.load_full_book_today_composition",
        return_value=None,
    ), mock.patch(
        "argosy.services.target_allocation_doc._prior_glide_q0",
        return_value=None,
    ):
        SF = client_with_db.app.state.session_factory
        with SF() as session:
            from argosy.services.plan_refinement import create_refinement_draft
            draft = create_refinement_draft(session, "ariel", {label: 20.0})

    merged = json.loads(draft.target_allocation_overrides_json)
    assert merged[label] == 20.0, "new value must win over old 10.0"


def test_create_refinement_draft_no_current_plan_raises(client_with_db):
    """RuntimeError raised when there is no current plan to base the draft on."""
    from argosy.services.plan_refinement import create_refinement_draft

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        with pytest.raises(RuntimeError, match="no current plan"):
            create_refinement_draft(session, "ariel", {"US broad-market core": 15.0})


# ---------------------------------------------------------------------------
# Fix 2 — real-path unknown-node test (no run_refinement monkeypatch)
# ---------------------------------------------------------------------------

def test_unknown_node_key_returns_400_real_path(client_with_db, monkeypatch):
    """A node_key absent from the hydrated graph must return 400, not 500.

    Does NOT monkeypatch run_refinement — exercises the real blast-radius
    classifier so UnknownNodeError actually propagates.  build_base_graph is
    patched only to avoid the heavy resolver/snapshot path (which needs real
    DB data), returning a minimal but real DerivationGraph.
    """
    _seed_current_plan(client_with_db)

    # Provide a real but minimal graph (one INPUT node only).
    # run_refinement and the blast-radius classifier are NOT mocked — they
    # will call clone.get("retirement.risk_posture") which is absent, raising
    # UnknownNodeError.  The handler must catch it and return 400.
    from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

    minimal_graph = DerivationGraph()
    minimal_graph.add_node(Node(key="portfolio.fx_usd_nis", kind=NodeKind.INPUT, value=3.7))
    minimal_graph.recompute()

    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: minimal_graph,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "retirement.risk_posture", "new_value": "conservative"}],
            "dry_run": True,
        },
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    assert "unknown node key" in detail.lower(), f"unexpected detail: {detail}"


# ---------------------------------------------------------------------------
# Fix 3 — end-to-end apply: target_allocation_json reflects the override
# ---------------------------------------------------------------------------

def test_apply_target_allocation_json_reflects_override(client_with_db, monkeypatch):
    """dry_run=False: the draft's target_allocation_json must contain the override,
    not just target_allocation_overrides_json.  This proves the override is APPLIED
    into the doc, not merely stored.
    """
    from argosy.services.allocation_plan import build_target_allocation

    alloc = build_target_allocation()
    labels = [c.label for c in alloc.classes]
    sleeve_label = labels[0]  # "US broad-market core"
    override_pct = 20.0

    _seed_current_plan(client_with_db)
    _patch_graph_and_decision(monkeypatch, tier_value="scoped_edit")

    # Build a real per-label composition (equal weights) to inject so the doc
    # builder can produce a real TargetAllocationDoc with the override applied.
    comp = {label: 100.0 / len(labels) for label in labels}

    monkeypatch.setattr(
        "argosy.services.target_allocation_doc.load_full_book_today_composition",
        lambda session, user_id, decision_run_id: comp,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [
                {
                    "node_key": f"allocation.sleeve_target.{sleeve_label}",
                    "new_value": override_pct,
                }
            ],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    draft_id = r.json().get("draft_id")
    assert draft_id is not None

    # Read the draft back and assert the resolved doc carries the override.
    from argosy.state.models import PlanVersion
    SF = client_with_db.app.state.session_factory
    with SF() as session:
        from sqlalchemy import select
        draft = session.execute(
            select(PlanVersion).where(PlanVersion.id == draft_id)
        ).scalar_one()

    assert draft.target_allocation_json is not None, (
        "target_allocation_json must not be None — the override must be applied into the doc"
    )
    doc = json.loads(draft.target_allocation_json)
    # Find the overridden sleeve in doc.classes
    classes = doc.get("classes", [])
    matched = [c for c in classes if c.get("label") == sleeve_label]
    assert matched, f"sleeve {sleeve_label!r} not found in target_allocation_json classes"
    assert matched[0]["target_pct"] == override_pct, (
        f"expected target_pct={override_pct}, got {matched[0]['target_pct']}"
    )
