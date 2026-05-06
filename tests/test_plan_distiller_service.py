"""Service-layer tests for distill_baseline_plan.

The service is the seam between the API/loop callers and the agent.
Tests use a fake agent so no Anthropic call is made.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


class _FakeDistillerAgent:
    """Stand-in for PlanDistillerAgent — returns a fixed PlanDistillate."""

    def __init__(self, payload):
        self._payload = payload
        self.calls: list[dict] = []

    def run_sync(self, **kw):  # mimic BaseAgent.run_sync signature
        self.calls.append(kw)
        # mimic AgentReport-shaped return: an object with .output
        return type("R", (), {"output": self._payload, "model": "fake", "tokens_in": 10, "tokens_out": 20, "cost_usd": 0.001})()


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _baseline_payload():
    from argosy.agents.plan_distiller_types import (
        Goal,
        PlanDistillate,
        Target,
    )

    return PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
        targets=[
            Target(
                label="NVDA",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
    )


def test_distill_baseline_plan_populates_columns(session, monkeypatch):
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan\n\nRetirement: 2031\nNVDA target 15%\n",
    )
    session.add(pv)
    session.commit()

    fake = _FakeDistillerAgent(_baseline_payload())
    # _make_agent now takes user_id; the lambda absorbs it.
    monkeypatch.setattr(svc, "_make_agent", lambda user_id: fake)

    out = svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")

    session.refresh(pv)
    assert pv.distillate_json is not None
    parsed = json.loads(pv.distillate_json)
    assert parsed["plan_label"] == "Jacobs v2.0"
    assert pv.distillate_rendered is not None
    assert "# Plan distillate" in pv.distillate_rendered
    assert pv.distilled_at is not None
    expected_hash = hashlib.sha256(pv.raw_markdown.encode("utf-8")).hexdigest()
    assert pv.source_hash == expected_hash
    assert out.distillate.plan_label == "Jacobs v2.0"


def test_distill_baseline_plan_preserves_user_edits_on_rerun(session, monkeypatch):
    """If user edited a target, re-distill must NOT clobber it."""
    from argosy.agents.plan_distiller_types import PlanDistillate, Target
    from argosy.services import plan_distiller_service as svc

    # Initial distill.
    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
    )
    session.add(pv)
    session.commit()

    initial = _baseline_payload()
    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")

    # User edits the NVDA target down to 0.12.
    svc.set_distillate_item_user_edit(
        session,
        plan_version_id=pv.id,
        category="targets",
        item_label="NVDA",
        new_value={"value": 0.12, "user_edit_note": "tighter than plan"},
    )

    # Re-run with a fresh distiller output that says 0.15 again.
    refresh = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-06T00:00:00+00:00",
        targets=[
            Target(
                label="NVDA",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
    )
    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _FakeDistillerAgent(refresh))
    svc.distill_baseline_plan(
        session, plan_version_id=pv.id, user_id="ariel", preserve_user_edits=True
    )

    session.refresh(pv)
    parsed = json.loads(pv.distillate_json)
    nvda = next(t for t in parsed["targets"] if t["label"] == "NVDA")
    assert nvda["value"] == 0.12, f"user edit was clobbered: {nvda}"
    assert nvda["user_edited"] is True


def test_distill_baseline_plan_force_overwrites_user_edits(session, monkeypatch):
    """When force_refresh=True, user edits are dropped (with a warning)."""
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        raw_markdown="# Plan",
    )
    session.add(pv)
    session.commit()

    initial = _baseline_payload()
    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(session, plan_version_id=pv.id, user_id="ariel")
    svc.set_distillate_item_user_edit(
        session, plan_version_id=pv.id, category="targets",
        item_label="NVDA", new_value={"value": 0.12},
    )

    # Force refresh — user edit dropped.
    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _FakeDistillerAgent(initial))
    svc.distill_baseline_plan(
        session, plan_version_id=pv.id, user_id="ariel",
        preserve_user_edits=False,
    )

    session.refresh(pv)
    parsed = json.loads(pv.distillate_json)
    nvda = next(t for t in parsed["targets"] if t["label"] == "NVDA")
    assert nvda["value"] == 0.15
    assert nvda["user_edited"] is False
