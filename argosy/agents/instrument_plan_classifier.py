"""Batch fleet classifier for held instrument -> current plan sleeve.

This is judgment, so the LLM authors the mapping.  Deterministic code only
checks that every returned label exists in the current plan and persists the
result with ``source=fleet``; owner-authored rows retain precedence.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class InstrumentPlanClassDecision(BaseModel):
    symbol: str = Field(min_length=1)
    plan_class_label: str = Field(min_length=1)
    confidence: Literal["HIGH", "MED", "LOW"]
    what_it_is: str = Field(min_length=1)
    why_held: str = Field(min_length=1)


class InstrumentPlanClassBatch(BaseModel):
    decisions: list[InstrumentPlanClassDecision] = Field(default_factory=list)


class InstrumentPlanClassifierAgent(BaseAgent[InstrumentPlanClassBatch]):
    """Classify all currently-unmapped holdings in one bounded fleet call."""

    agent_role = "instrument_plan_classifier"
    output_model = InstrumentPlanClassBatch
    require_citations = False

    def build_prompt(
        self,
        *,
        plan_classes: list[dict[str, Any]],
        instruments: list[dict[str, Any]],
    ) -> tuple[str, str]:
        system = (
            "You classify HELD instruments into the owner's CURRENT portfolio-plan "
            "sleeves. This is portfolio-role classification, not a buy/sell verdict "
            "and not merely a sector label. Choose exactly one supplied plan class "
            "for every instrument. Use the instrument structure, sector, region, "
            "identity, current stance and the plan sleeve mandates. If the fit is "
            "imperfect, still choose the honest best fit and mark confidence LOW; "
            "never invent a new class and never omit a symbol. Keep what_it_is and "
            "why_held factual and concise."
        )
        user = (
            f"CURRENT PLAN CLASSES:\n{plan_classes}\n\n"
            f"UNMAPPED HELD INSTRUMENTS:\n{instruments}\n\n"
            "Return JSON: {\"decisions\": [{\"symbol\": str, "
            "\"plan_class_label\": one exact supplied label, "
            "\"confidence\": \"HIGH|MED|LOW\", \"what_it_is\": str, "
            "\"why_held\": str}]}. Return one row per supplied symbol."
        )
        return system, user


__all__ = [
    "InstrumentPlanClassBatch",
    "InstrumentPlanClassDecision",
    "InstrumentPlanClassifierAgent",
]
