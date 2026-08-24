"""``_large_worker`` must forward ``resume_from_phase`` to ``run_synthesis``.

Run 456 (2026-08-24) died with a gate-passed skeleton and five good
phase-3 slice checkpoints on disk. Reusing them required
``resume_from_phase``, which the worker did not accept — so the only way
to resume was to call ``run_synthesis`` directly, which drops the
``plan.amendment.*`` events and the mid-run cancellation re-check the
worker exists to provide. The passthrough removes that dilemma.

Pinned here: the parameter reaches ``run_synthesis``, it is OMITTED (not
passed as None) when unset so the historical call shape is unchanged, and
``anchor_plan_version_id`` still rides along with it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from argosy.orchestrator.flows.plan_amendment import workers


class _Run:
    def __init__(self):
        self.id = 456
        self.status = "running"
        self.notes_json = None
        self.finished_at = None


class _Session:
    def refresh(self, _obj):
        pass

    def commit(self):
        pass


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def _fake_run_synthesis(session, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(draft_id=999)

    monkeypatch.setattr(workers, "run_synthesis", _fake_run_synthesis)
    monkeypatch.setattr(workers, "publish_event_threadsafe",
                        lambda *a, **k: None)
    return seen


def _call(**extra):
    workers._large_worker(
        session=_Session(), user_id="ariel", decision_run=_Run(),
        guidance="clear the blockers", **extra,
    )


def test_resume_from_phase_is_forwarded(captured):
    _call(anchor_plan_version_id=121, resume_from_phase=3)
    assert captured["resume_from_phase"] == 3
    assert captured["anchor_plan_version_id"] == 121
    # The audit chain the worker exists to protect.
    assert captured["existing_decision_run_id"] == 456


def test_omitted_when_unset_so_the_old_call_shape_is_unchanged(captured):
    _call(anchor_plan_version_id=121)
    assert "resume_from_phase" not in captured
    assert captured["anchor_plan_version_id"] == 121


def test_phase_zero_would_be_forwarded_not_swallowed(captured):
    """Guard the ``if x is not None`` vs truthiness distinction."""
    _call(resume_from_phase=0)
    assert captured["resume_from_phase"] == 0


def test_cancelled_run_still_short_circuits(monkeypatch, captured):
    """The cancellation re-check must not be bypassed by the new param."""
    run = _Run()
    run.status = "cancelled"
    workers._large_worker(
        session=_Session(), user_id="ariel", decision_run=run,
        guidance="g", resume_from_phase=3,
    )
    assert captured == {}, "cancelled run must never reach run_synthesis"
