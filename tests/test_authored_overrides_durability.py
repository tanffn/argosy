"""TDD — authored_overrides durability across re-synthesis.

Tests written BEFORE implementation (RED), then watched fail, then implementation
added (GREEN).

Covers:
  1. resolve_target_allocation_json honours authored_overrides end-to-end (the
     final target_allocation_json has the correct class target_pct).
  2. authored_overrides=None/absent → identical behaviour to today (no override).
  3. Carry-forward: a draft created from a parent carrying
     target_allocation_overrides_json inherits it AND its target_allocation_json
     reflects the override.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from argosy.services.allocation_plan import build_target_allocation
from argosy.services.target_allocation_doc import (
    TargetAllocationDoc,
    build_target_allocation_doc,
    load_plan_target_allocation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_today_composition() -> dict[str, float]:
    """A minimal 100%-summing full-book composition (no live DB needed)."""
    alloc = build_target_allocation()
    return {c.label: c.target_pct for c in alloc.classes}


# ---------------------------------------------------------------------------
# 1. resolve_target_allocation_json honours authored_overrides end-to-end
# ---------------------------------------------------------------------------

class TestResolveTargetAllocationJsonWithOverrides:
    """resolve_target_allocation_json passes authored_overrides into the engine
    and the produced JSON reflects the override."""

    def test_override_honored_in_produced_json(self):
        """A PlanVersion with overrides_json → resolve produces target_pct==override."""
        from argosy.services.target_allocation_doc import resolve_target_allocation_json

        override_label = "US broad-market core"
        override_pct = 8.0

        today = date(2026, 7, 3)
        today_comp = _make_minimal_today_composition()

        # Patch build_plan_target_allocation_doc so we exercise override threading
        # without needing a live DB / concentration report.
        def fake_build(db, user_id, decision_run_id, _today,
                       *, alternatives_sleeve=None, authored_overrides=None):
            return build_target_allocation_doc(
                today=today,
                today_composition=today_comp,
                authored_overrides=authored_overrides,
            )

        with patch(
            "argosy.services.target_allocation_doc.build_plan_target_allocation_doc",
            side_effect=fake_build,
        ):
            raw = resolve_target_allocation_json(
                db=MagicMock(),
                user_id="u1",
                decision_run_id=1,
                today=today,
                authored_overrides={override_label: override_pct},
            )

        assert raw is not None
        doc = TargetAllocationDoc.model_validate_json(raw)
        by_label = {c.label: c.target_pct for c in doc.classes}
        assert override_label in by_label
        assert abs(by_label[override_label] - override_pct) < 0.01, (
            f"Expected {override_label} to be {override_pct}%, got {by_label[override_label]}"
        )

    def test_no_override_produces_unchanged_output(self):
        """authored_overrides=None → same result as baseline (no regression)."""
        from argosy.services.target_allocation_doc import resolve_target_allocation_json

        today = date(2026, 7, 3)
        today_comp = _make_minimal_today_composition()

        def fake_build(db, user_id, decision_run_id, _today,
                       *, alternatives_sleeve=None, authored_overrides=None):
            return build_target_allocation_doc(
                today=today,
                today_composition=today_comp,
                authored_overrides=authored_overrides,
            )

        with patch(
            "argosy.services.target_allocation_doc.build_plan_target_allocation_doc",
            side_effect=fake_build,
        ):
            raw_none = resolve_target_allocation_json(
                db=MagicMock(),
                user_id="u1",
                decision_run_id=1,
                today=today,
                authored_overrides=None,
            )
            raw_baseline = resolve_target_allocation_json(
                db=MagicMock(),
                user_id="u1",
                decision_run_id=2,
                today=today,
                authored_overrides=None,
            )

        assert raw_none is not None
        assert raw_baseline is not None
        doc_none = TargetAllocationDoc.model_validate_json(raw_none)
        doc_base = TargetAllocationDoc.model_validate_json(raw_baseline)
        by_none = {c.label: c.target_pct for c in doc_none.classes}
        by_base = {c.label: c.target_pct for c in doc_base.classes}
        assert by_none == by_base


# ---------------------------------------------------------------------------
# 2. build_target_allocation_doc accepts and threads authored_overrides
# ---------------------------------------------------------------------------

class TestBuildTargetAllocationDocWithOverrides:
    """build_target_allocation_doc accepts authored_overrides and the doc reflects it."""

    def test_override_reflected_in_class_target_pct(self):
        override_label = "US broad-market core"
        override_pct = 8.0
        today_comp = _make_minimal_today_composition()
        doc = build_target_allocation_doc(
            today=date(2026, 7, 3),
            today_composition=today_comp,
            authored_overrides={override_label: override_pct},
        )
        by_label = {c.label: c.target_pct for c in doc.classes}
        assert abs(by_label[override_label] - override_pct) < 0.01, (
            f"Expected {override_label}=={override_pct}, got {by_label[override_label]}"
        )

    def test_no_override_is_identical_to_baseline(self):
        today_comp = _make_minimal_today_composition()
        doc_none = build_target_allocation_doc(
            today=date(2026, 7, 3),
            today_composition=today_comp,
            authored_overrides=None,
        )
        doc_base = build_target_allocation_doc(
            today=date(2026, 7, 3),
            today_composition=today_comp,
        )
        by_none = {c.label: c.target_pct for c in doc_none.classes}
        by_base = {c.label: c.target_pct for c in doc_base.classes}
        assert by_none == by_base


# ---------------------------------------------------------------------------
# 3. Carry-forward: draft inherits overrides from parent PlanVersion
# ---------------------------------------------------------------------------

class TestCarryForwardOverrides:
    """When a parent plan has target_allocation_overrides_json, a new draft
    created from it must (a) copy the overrides column and (b) produce a
    target_allocation_json that reflects the override."""

    def _parent_with_override(self, override_label: str, override_pct: float):
        """Fake parent PlanVersion-like object carrying an override."""
        parent = MagicMock()
        parent.id = 42
        parent.target_allocation_overrides_json = json.dumps(
            {override_label: override_pct}
        )
        # Give it a valid target_allocation_json too (for carry-forward path)
        today_comp = _make_minimal_today_composition()
        doc = build_target_allocation_doc(
            today=date(2026, 7, 3),
            today_composition=today_comp,
            authored_overrides={override_label: override_pct},
        )
        parent.target_allocation_json = doc.model_dump_json()
        return parent

    def test_draft_inherits_overrides_column_from_parent(self):
        """The helper that creates a draft from a parent must copy
        target_allocation_overrides_json onto the draft."""
        from argosy.services.target_allocation_doc import (
            inherit_overrides_from_parent,
        )

        override_label = "US broad-market core"
        override_pct = 8.0
        parent = self._parent_with_override(override_label, override_pct)

        overrides_json = inherit_overrides_from_parent(parent)
        assert overrides_json is not None
        parsed = json.loads(overrides_json)
        assert parsed == {override_label: override_pct}

    def test_no_parent_overrides_returns_none(self):
        """A parent with no overrides → inherit_overrides_from_parent returns None."""
        from argosy.services.target_allocation_doc import (
            inherit_overrides_from_parent,
        )

        parent = MagicMock()
        parent.target_allocation_overrides_json = None
        result = inherit_overrides_from_parent(parent)
        assert result is None

    def test_draft_target_allocation_reflects_inherited_overrides(self):
        """When we resolve target_allocation_json for a new draft using the
        inherited overrides, the class target_pct matches the override."""
        override_label = "US broad-market core"
        override_pct = 8.0
        parent = self._parent_with_override(override_label, override_pct)

        from argosy.services.target_allocation_doc import (
            inherit_overrides_from_parent,
            resolve_target_allocation_json,
        )

        inherited_json = inherit_overrides_from_parent(parent)
        assert inherited_json is not None
        inherited_overrides = json.loads(inherited_json)

        today = date(2026, 7, 3)
        today_comp = _make_minimal_today_composition()

        def fake_build(db, user_id, decision_run_id, _today,
                       *, alternatives_sleeve=None, authored_overrides=None):
            return build_target_allocation_doc(
                today=today,
                today_composition=today_comp,
                authored_overrides=authored_overrides,
            )

        with patch(
            "argosy.services.target_allocation_doc.build_plan_target_allocation_doc",
            side_effect=fake_build,
        ):
            raw = resolve_target_allocation_json(
                db=MagicMock(),
                user_id="u1",
                decision_run_id=99,
                today=today,
                authored_overrides=inherited_overrides,
            )

        assert raw is not None
        doc = TargetAllocationDoc.model_validate_json(raw)
        by_label = {c.label: c.target_pct for c in doc.classes}
        assert abs(by_label[override_label] - override_pct) < 0.01


# ---------------------------------------------------------------------------
# 4. PlanVersion model has target_allocation_overrides_json field
# ---------------------------------------------------------------------------

class TestPlanVersionModelField:
    """The PlanVersion ORM model must expose target_allocation_overrides_json."""

    def test_plan_version_has_overrides_field(self):
        from argosy.state.models import PlanVersion
        pv = PlanVersion.__table__
        col_names = {c.name for c in pv.columns}
        assert "target_allocation_overrides_json" in col_names, (
            "PlanVersion table must have target_allocation_overrides_json column"
        )

    def test_plan_version_overrides_field_is_nullable(self):
        from argosy.state.models import PlanVersion
        pv = PlanVersion.__table__
        col = next(c for c in pv.columns if c.name == "target_allocation_overrides_json")
        assert col.nullable, "target_allocation_overrides_json must be nullable"


# ---------------------------------------------------------------------------
# 5. Migration 0076 probe-style test (smoke)
# ---------------------------------------------------------------------------

class TestMigration0076:
    """Smoke: the new column appears after upgrade, disappears after downgrade."""

    @pytest.fixture
    def alembic_cfg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
        from argosy.config import reload_settings, get_settings
        reload_settings()
        db_url = get_settings().database_url
        sync_url = db_url.replace("+aiosqlite", "")
        import os
        db_path = sync_url.replace("sqlite:///", "")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        from alembic.config import Config
        cfg = Config("alembic.ini")
        return cfg, sync_url

    def test_upgrade_adds_column(self, alembic_cfg):
        from alembic import command
        import sqlalchemy as sa
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "0076_plan_allocation_overrides")
        engine = sa.create_engine(sync_url)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("plan_versions")}
        assert "target_allocation_overrides_json" in cols

    def test_downgrade_removes_column(self, alembic_cfg):
        from alembic import command
        import sqlalchemy as sa
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "0076_plan_allocation_overrides")
        command.downgrade(cfg, "0075_decision_funnel")
        engine = sa.create_engine(sync_url)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("plan_versions")}
        assert "target_allocation_overrides_json" not in cols
