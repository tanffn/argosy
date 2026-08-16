"""Tests for the pre-acceptance numeric-literal guard
(argosy/quality/plan_synth_numeric_guard.py).

Covers: detection over a synthesized PlanSynthesisOutput (both the
"typed-but-matching" and "drifted" cases), under-reach on unanchored
numbers, corrective-feedback rendering, and the bounded reject/retry loop
against a stub agent.
"""
from __future__ import annotations

from datetime import date

import pytest

from argosy.agents.plan_synthesizer_types import (
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthesisInputs,
)
from argosy.quality.plan_synth_numeric_guard import (
    build_corrective_feedback,
    scan_output,
    synthesize_with_numeric_guard,
)
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue

SELL = "concentration.nvda_sell_sh"
CAP = "concentration.nvda_cap_pct"


def _resolved(**kv) -> ResolvedPlanNumbers:
    values = {
        k: ResolvedValue(key=k, value=v, unit=unit, status="resolved", source_locator="test")
        for k, (v, unit) in kv.items()
    }
    return ResolvedPlanNumbers(values=values)


def _horizon(body_md: str, rationale: str = "narrative rationale, no digits") -> HorizonSection:
    return HorizonSection(
        horizon="medium",
        freshness_expected="monthly",
        status="minor_revision",
        posture="steady deconcentration posture",
        rationale=rationale,
        cited_sources=[],
    )


def _output(body_md: str) -> PlanSynthesisOutput:
    section = Section(
        section_id="concentration",
        horizon="medium",
        title="Concentration",
        body_md=body_md,
        evidence=SectionEvidence(missing_data=["n/a"]),
    )
    return PlanSynthesisOutput(
        long=_horizon(""),
        medium=_horizon(""),
        short=_horizon(""),
        inputs=SynthesisInputs(),
        sections=[section],
    )


def test_typed_matching_literal_is_a_violation():
    resolved = _resolved(**{SELL: (9479, "shares")})
    output = _output(
        "The forward glide sells 9,479 shares from Section-102 inventory. NVDA weight is high."
    )
    findings = scan_output(output, resolved)
    assert len(findings) == 1
    assert findings[0].kind == "typed_literal"
    assert findings[0].key == SELL
    assert "9,479" in findings[0].message


def test_drifted_literal_is_flagged_not_silently_fixed():
    resolved = _resolved(**{SELL: (9479, "shares")})
    output = _output(
        "The forward glide sells 9,417 shares from Section-102 inventory. NVDA weight is high."
    )
    findings = scan_output(output, resolved)
    assert len(findings) == 1
    assert findings[0].kind == "drift"
    assert findings[0].key == SELL
    assert findings[0].literal == "9,417 shares"
    assert f"{{{{fact:{SELL}}}}}" in findings[0].message


def test_already_tokenized_text_is_clean():
    resolved = _resolved(**{SELL: (9479, "shares")})
    output = _output(f"The forward glide sells {{{{fact:{SELL}}}}} shares. NVDA weight is high.")
    assert scan_output(output, resolved) == []


def test_unanchored_number_is_never_flagged_under_reach():
    """A number with no registered concept anchor nearby (sleeve allocation,
    tax rate, age, year) is left alone — the module must not become a
    greedy numeric scanner."""
    resolved = _resolved(**{SELL: (9479, "shares")})
    output = _output(
        "The US broad-market sleeve is anchored at 28.5% of the portfolio. "
        "Marginal tax runs 47% this bracket at age 46."
    )
    assert scan_output(output, resolved) == []


def test_corrective_feedback_names_literal_and_token():
    resolved = _resolved(**{CAP: (0.13, "pct")})
    output = _output("NVDA sits against a binding instrument-level ceiling of 12.0%.")
    findings = scan_output(output, resolved)
    fb = build_corrective_feedback(findings)
    assert "12.0%" in fb
    assert f"{{{{fact:{CAP}}}}}" in fb


class _StubAgent:
    """Minimal stand-in for PlanSynthesizerAgent — records prompts, returns
    a queued sequence of AgentReport-like objects."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        out = self._outputs.pop(0)

        class _Report:
            pass

        r = _Report()
        r.output = out
        return r


@pytest.mark.asyncio
async def test_guard_retries_then_accepts_clean_draft():
    resolved = _resolved(**{SELL: (9479, "shares")})
    dirty = _output("The forward glide sells 9,417 shares. NVDA weight is high.")
    clean = _output(f"The forward glide sells {{{{fact:{SELL}}}}} shares. NVDA weight is high.")
    agent = _StubAgent([dirty, clean])

    result = await synthesize_with_numeric_guard(agent, resolved=resolved, max_retries=2)

    assert result.accepted is True
    assert result.attempts == 2
    assert result.findings == []
    # second call carried the corrective feedback naming the offending literal
    assert "9,417" in agent.calls[1]["numeric_literal_feedback"]
    assert agent.calls[0]["numeric_literal_feedback"] == ""


@pytest.mark.asyncio
async def test_guard_surfaces_violation_when_retry_budget_exhausted():
    resolved = _resolved(**{SELL: (9479, "shares")})
    always_dirty = [
        _output("The forward glide sells 9,417 shares. NVDA weight is high.")
        for _ in range(3)
    ]
    agent = _StubAgent(always_dirty)

    result = await synthesize_with_numeric_guard(agent, resolved=resolved, max_retries=2)

    assert result.accepted is False
    assert result.attempts == 3
    assert len(result.findings) == 1
    assert len(agent.calls) == 3
