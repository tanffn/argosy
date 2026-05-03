"""IntakeExtractorAgent tests. Mock `_call_model` — no network call."""

from __future__ import annotations

import json

import pytest
import yaml

from argosy.agents.base import ConfidenceBand, ModelCall
from argosy.agents.intake_extractor import (
    ExtractedField,
    IntakeExtraction,
    IntakeExtractorAgent,
)


_PLAN_FIXTURE = """# Jacobs Wealth Plan v2.0

## Identity
- Tax residency: Israel
- Citizenship: US, Israel
- Family: spouse + two children under 18
- Employment: NVIDIA, 12 years

## Goals
- Retirement target: 2032
- Target annual income: 600k NIS / yr post-retirement

## Brokerages
- IBKR (primary), Schwab (legacy RSU), Leumi (NIS deposits)

## Risk
- Moderate risk tolerance; no leverage; no crypto.
"""


_CANNED_EXTRACTION = {
    "tax_residency": {
        "value": "israel",
        "source_excerpt": "Tax residency: Israel",
        "confidence": "HIGH",
    },
    "citizenship": ["us", "israel"],
    "family": {
        "value": "spouse plus two children under 18",
        "source_excerpt": "spouse + two children under 18",
        "confidence": "HIGH",
    },
    "employment": {
        "value": "NVIDIA, 12 years",
        "source_excerpt": "Employment: NVIDIA, 12 years",
        "confidence": "HIGH",
    },
    "retirement_target_year": {
        "value": "2032",
        "source_excerpt": "Retirement target: 2032",
        "confidence": "HIGH",
    },
    "target_annual_income": {
        "value": "600k NIS / yr",
        "source_excerpt": "Target annual income: 600k NIS / yr post-retirement",
        "confidence": "MEDIUM",
    },
    "near_term_spending": None,
    "primary_brokers": ["IBKR", "Schwab", "Leumi"],
    "bank_diversification_preference": None,
    "risk_tolerance": {
        "value": "moderate; no leverage; no crypto",
        "source_excerpt": "Moderate risk tolerance; no leverage; no crypto.",
        "confidence": "HIGH",
    },
    "constraints_other": ["no leverage", "no crypto"],
    "identity_yaml": (
        "tax_residency: israel\n"
        "citizenship: [us, israel]\n"
        "family: spouse plus two children under 18\n"
        "employment: NVIDIA, 12 years\n"
    ),
    "goals_yaml": (
        "retirement_target_year: 2032\n"
        "target_annual_income: 600k NIS / yr\n"
    ),
    "constraints_yaml": (
        "risk_tolerance: moderate; no leverage; no crypto\n"
        "constraints_other:\n"
        "  - no leverage\n"
        "  - no crypto\n"
    ),
    "fields_extracted": [
        "tax_residency",
        "citizenship",
        "family",
        "employment",
        "retirement_target_year",
        "target_annual_income",
        "primary_brokers",
        "risk_tolerance",
    ],
    "fields_missing": [
        "near_term_spending",
        "bank_diversification_preference",
    ],
    "confidence": "HIGH",
    "notes": "Plan v2.0 — clear on identity & goals; FX assumptions not stated.",
}


class _MockExtractor(IntakeExtractorAgent):
    """Replaces `_call_model` with a canned `ModelCall`."""

    def __init__(self, *, user_id: str, canned: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=1500,
            tokens_out=800,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_extractor_emits_intake_extraction_shape() -> None:
    agent = _MockExtractor(user_id="ariel", canned=_CANNED_EXTRACTION)
    report = await agent.run(plan_markdown=_PLAN_FIXTURE, accumulated_context="")
    out = report.output
    assert isinstance(out, IntakeExtraction)
    assert out.tax_residency is not None
    assert isinstance(out.tax_residency, ExtractedField)
    assert out.tax_residency.value == "israel"
    assert out.tax_residency.confidence == ConfidenceBand.HIGH
    assert out.citizenship == ["us", "israel"]
    assert "IBKR" in (out.primary_brokers or [])
    assert report.confidence == ConfidenceBand.HIGH


@pytest.mark.asyncio
async def test_extractor_fields_extracted_and_missing_populated() -> None:
    agent = _MockExtractor(user_id="ariel", canned=_CANNED_EXTRACTION)
    report = await agent.run(plan_markdown=_PLAN_FIXTURE, accumulated_context="")
    out: IntakeExtraction = report.output  # type: ignore[assignment]
    assert "tax_residency" in out.fields_extracted
    assert "retirement_target_year" in out.fields_extracted
    assert "near_term_spending" in out.fields_missing
    assert "bank_diversification_preference" in out.fields_missing
    # The two lists shouldn't overlap.
    assert not (set(out.fields_extracted) & set(out.fields_missing))


@pytest.mark.asyncio
async def test_extractor_yaml_strings_are_valid_yaml() -> None:
    agent = _MockExtractor(user_id="ariel", canned=_CANNED_EXTRACTION)
    report = await agent.run(plan_markdown=_PLAN_FIXTURE, accumulated_context="")
    out: IntakeExtraction = report.output  # type: ignore[assignment]
    # All three must parse and produce dicts (not None / scalar).
    identity = yaml.safe_load(out.identity_yaml)
    goals = yaml.safe_load(out.goals_yaml)
    constraints = yaml.safe_load(out.constraints_yaml)
    assert isinstance(identity, dict)
    assert identity.get("tax_residency") == "israel"
    assert isinstance(goals, dict)
    assert goals.get("retirement_target_year") == 2032
    assert isinstance(constraints, dict)
    assert constraints.get("risk_tolerance", "").startswith("moderate")


@pytest.mark.asyncio
async def test_extractor_handles_sparse_plan() -> None:
    """A plan missing most fields should produce mostly-None output and a long
    fields_missing list — the extractor must NOT fabricate."""
    sparse_canned = {
        "tax_residency": {
            "value": "israel",
            "source_excerpt": "I am an Israeli tax resident.",
            "confidence": "HIGH",
        },
        "citizenship": None,
        "family": None,
        "employment": None,
        "retirement_target_year": None,
        "target_annual_income": None,
        "near_term_spending": None,
        "primary_brokers": None,
        "bank_diversification_preference": None,
        "risk_tolerance": None,
        "constraints_other": [],
        "identity_yaml": "tax_residency: israel\n",
        "goals_yaml": "",
        "constraints_yaml": "",
        "fields_extracted": ["tax_residency"],
        "fields_missing": [
            "citizenship",
            "family",
            "employment",
            "retirement_target_year",
            "target_annual_income",
            "near_term_spending",
            "primary_brokers",
            "bank_diversification_preference",
            "risk_tolerance",
        ],
        "confidence": "LOW",
        "notes": "Sparse plan — most fields will need to be asked.",
    }
    agent = _MockExtractor(user_id="ariel", canned=sparse_canned)
    report = await agent.run(
        plan_markdown="# Plan\nI am an Israeli tax resident.\n",
        accumulated_context="",
    )
    out: IntakeExtraction = report.output  # type: ignore[assignment]
    assert out.tax_residency is not None
    assert out.citizenship is None
    assert out.family is None
    assert len(out.fields_missing) >= 5
    # Empty YAML is valid.
    assert yaml.safe_load(out.goals_yaml) is None
    assert yaml.safe_load(out.constraints_yaml) is None
