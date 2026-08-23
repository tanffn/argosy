"""``anchor_plan_version_id`` — let a corrected DRAFT seed the next full run.

Breaks a closed loop found 2026-08-23:

  promote a draft   -> needs codex + reader verdicts
  get those verdicts-> only a full run_synthesis writes them
  a full run        -> anchors on role='current', which was plan 92 of 13 July
                       and so rebuilds from July, discarding drafts 93..119

Every route led back. The shortcut — promote the draft "just as an anchor" —
was rejected because ``role='current'`` is not a private synthesis variable:
promotion durably stamps acceptance, supersedes the prior plan, emits
current-change events, refreshes caches and can close corrective proposals,
none of which a later supersede undoes.

These tests pin the VALIDATION, which is the part that must not rot. A bad
anchor has to raise: silently falling back to ``get_current_plan`` would
rebuild from a stale plan and look exactly like success — the failure shape
this codebase keeps producing.
"""
from __future__ import annotations

import inspect

import pytest

from argosy.orchestrator.flows.plan_synthesis.orchestrator import run_synthesis


def test_parameter_exists_and_defaults_to_none():
    """Default None must preserve the historical role='current' behaviour."""
    sig = inspect.signature(run_synthesis)
    assert "anchor_plan_version_id" in sig.parameters
    assert sig.parameters["anchor_plan_version_id"].default is None


def test_parameter_is_keyword_only():
    """Positional would be a footgun next to the other int params."""
    sig = inspect.signature(run_synthesis)
    assert sig.parameters["anchor_plan_version_id"].kind is inspect.Parameter.KEYWORD_ONLY


class _FakePlan:
    def __init__(self, pid, user_id, role):
        self.id, self.user_id, self.role = pid, user_id, role


class _FakeSession:
    """Minimal stand-in: only ``get`` is reached before validation raises."""

    def __init__(self, plan=None):
        self._plan = plan

    def get(self, model, pk):  # noqa: ARG002
        return self._plan


def _run(session, anchor):
    return run_synthesis(
        session, user_id="ariel", trigger="check_in",
        anchor_plan_version_id=anchor,
    )


def test_missing_anchor_raises_rather_than_falling_back():
    with pytest.raises(ValueError, match="not found"):
        _run(_FakeSession(None), 999999)


def test_cross_tenant_anchor_is_refused():
    plan = _FakePlan(119, "someone_else", "draft")
    with pytest.raises(ValueError, match="cross-tenant"):
        _run(_FakeSession(plan), 119)


@pytest.mark.parametrize("role", ["superseded", "baseline", "archived", ""])
def test_only_current_or_draft_may_anchor(role):
    """A superseded plan must never seed a run — that is how a regression
    silently re-enters the chain."""
    plan = _FakePlan(119, "ariel", role)
    with pytest.raises(ValueError, match="may anchor"):
        _run(_FakeSession(plan), 119)
