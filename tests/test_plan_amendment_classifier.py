"""Tests for plan_amendment classifier (Wave 4)."""

from __future__ import annotations

import pytest


def _make_intent(**kw):
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    base = dict(tier="medium", rationale="x")
    base.update(kw)
    return AmendmentIntent(**base)


def _make_delta():
    from argosy.agents.plan_synthesizer_types import Delta
    return Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="x",
        prior={"value": 0.15}, proposed={"value": 0.12},
    )


def test_classify_small_tighten_with_delta_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.SMALL
    assert out.proposed_delta is not None


def test_classify_small_loosen_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="loosen", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason == "small_with_loosen_direction"


def test_classify_small_ambiguous_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="ambiguous", proposed_delta=_make_delta())
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM


def test_classify_small_without_delta_escalates_to_medium():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="small", direction="tighten", proposed_delta=None)
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason == "small_without_delta"


def test_classify_medium_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="medium")
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.MEDIUM
    assert out.escalation_reason is None


def test_classify_large_passes_through():
    from argosy.orchestrator.flows.plan_amendment.classifier import classify
    from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

    intent = _make_intent(tier="large")
    out = classify(intent)
    assert out.effective_tier == EffectiveTier.LARGE
