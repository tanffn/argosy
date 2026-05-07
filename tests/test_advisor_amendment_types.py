"""Tests for advisor amendment types (Wave 4)."""

from __future__ import annotations

import pytest


def test_amendment_intent_small_tighten_with_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta

    delta = Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="NVDA cap 15% -> 12%",
        prior={"value": 0.15}, proposed={"value": 0.12},
        rationale="user-initiated tightening",
    )
    intent = AmendmentIntent(
        tier="small",
        direction="tighten",
        proposed_delta=delta,
        rationale="single-target tightening, explicit numbers given",
    )
    payload = intent.model_dump_json()
    intent2 = AmendmentIntent.model_validate_json(payload)
    assert intent2.tier == "small"
    assert intent2.direction == "tighten"
    assert intent2.proposed_delta.item_id == "medium.targets.nvda"


def test_amendment_intent_medium_no_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    intent = AmendmentIntent(
        tier="medium",
        rationale="theme shift on medium horizon, multi-target reasoning needed",
    )
    assert intent.tier == "medium"
    assert intent.direction is None
    assert intent.proposed_delta is None


def test_amendment_intent_large_no_delta():
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    intent = AmendmentIntent(
        tier="large",
        rationale="user said 're-evaluate everything'; structural rethink",
    )
    assert intent.tier == "large"


def test_amendment_intent_rejects_unknown_tier():
    from pydantic import ValidationError

    from argosy.agents.advisor_amendment_types import AmendmentIntent

    with pytest.raises(ValidationError):
        AmendmentIntent(tier="huge", rationale="x")


def test_amendment_result_dto_round_trips():
    from argosy.agents.advisor_amendment_types import AmendmentResultDTO

    dto = AmendmentResultDTO(
        tier="small", decision_run_id=42, status="applied", draft_id=99,
    )
    payload = dto.model_dump_json()
    dto2 = AmendmentResultDTO.model_validate_json(payload)
    assert dto2.draft_id == 99
    assert dto2.eta_seconds is None


def test_amendment_result_dto_running_carries_eta():
    from argosy.agents.advisor_amendment_types import AmendmentResultDTO

    dto = AmendmentResultDTO(
        tier="medium", decision_run_id=42, status="running", eta_seconds=30,
    )
    assert dto.eta_seconds == 30
    assert dto.draft_id is None
