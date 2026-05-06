"""Tests for /api/plan/baseline — exposes the distillate to the UI."""

from __future__ import annotations

import json

import pytest

from argosy.state.models import PlanVersion, User


@pytest.fixture
def app_with_baseline(client_with_db):
    """Insert a baseline row with a populated distillate."""
    from argosy.agents.plan_distiller_types import Goal, PlanDistillate
    from argosy.agents.plan_distiller_render import render_distillate

    payload = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
    )
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        pv = PlanVersion(
            user_id="ariel",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            role="baseline",
            distillate_json=payload.model_dump_json(),
            distillate_rendered=render_distillate(payload),
        )
        sess.add(pv)
        sess.commit()
    finally:
        sess.close()
    return client_with_db


# ----- T1.10: GET /api/plan/baseline -----

def test_get_baseline_returns_distillate(app_with_baseline):
    r = app_with_baseline.get("/api/plan/baseline?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["version_label"] == "Jacobs v2.0"
    assert body["distillate"] is not None
    assert body["distillate"]["plan_label"] == "Jacobs v2.0"
    assert "retirement_target_year" in json.dumps(body["distillate"])
    assert "# Plan distillate" in body["distillate_rendered"]


def test_get_baseline_returns_404_when_absent(client_with_db):
    """Users without an imported plan get 404 (not a 500)."""
    r = client_with_db.get("/api/plan/baseline?user_id=newcomer")
    assert r.status_code == 404


# ----- T1.11: POST /api/plan/baseline/distill -----

def test_post_baseline_distill_reruns_distillation(app_with_baseline, monkeypatch):
    """The Re-distill button hits this endpoint."""
    from argosy.agents.plan_distiller_types import Goal, PlanDistillate
    from argosy.services import plan_distiller_service as svc

    new_payload = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-06T00:00:00+00:00",
        goals=[
            Goal(label="retirement_target_year", value="2031"),
            Goal(label="lifestyle", value="early retire to nature"),
        ],
    )

    class _Fake:
        def run_sync(self, **kw):
            return type("R", (), {"output": new_payload, "model": "fake", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0})()

    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _Fake())

    r = app_with_baseline.post("/api/plan/baseline/distill?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["distillate"]["distilled_at_iso"] == "2026-05-06T00:00:00+00:00"
    assert any(g["label"] == "lifestyle" for g in body["distillate"]["goals"])


def test_post_baseline_distill_404_when_no_baseline(client_with_db):
    r = client_with_db.post("/api/plan/baseline/distill?user_id=ghost")
    assert r.status_code == 404


# ----- T1.12: PATCH distillate item -----

def test_patch_distillate_item_applies_user_edit(app_with_baseline):
    body = {
        "value": "2030",
        "user_edit_note": "decided to retire one year earlier",
    }
    r = app_with_baseline.patch(
        "/api/plan/baseline/distillate/goals/retirement_target_year?user_id=ariel",
        json=body,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    goal = next(g for g in out["distillate"]["goals"] if g["label"] == "retirement_target_year")
    assert goal["value"] == "2030"
    assert goal["user_edited"] is True
    assert goal["user_edit_note"] == "decided to retire one year earlier"


def test_patch_distillate_item_404_when_label_missing(app_with_baseline):
    r = app_with_baseline.patch(
        "/api/plan/baseline/distillate/goals/no_such_label?user_id=ariel",
        json={"value": "x"},
    )
    assert r.status_code == 404
