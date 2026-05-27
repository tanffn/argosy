"""PlanCritiqueAgent tests. Mock the Anthropic client."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.errors import AgentRunError
from argosy.agents.plan_critique import Finding, PlanCritiqueAgent, PlanCritiqueReport


class _MockPlanCritiqueAgent(PlanCritiqueAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output
        self.last_sources: list[tuple[str, str]] | None = None
        self.last_system: str | None = None
        self.last_user: str | None = None

    async def _call_model(
        self, *, system: str, user: str, **_extra: object,
    ) -> ModelCall:
        # Wave A: BaseAgent.run forwards `sources` (and `image_attachments`)
        # when build_prompt returns the 3-tuple form. Accept-and-stash so
        # tests below can assert source extraction without coupling the
        # mock to the exact kwargs the base class forwards.
        self.last_system = system
        self.last_user = user
        self.last_sources = _extra.get("sources")  # type: ignore[assignment]
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=300,
            tokens_out=400,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_plan_critique_produces_findings() -> None:
    canned = {
        "plan_label": "Jacobs_Wealth_Plan v2.0",
        "snapshot_label": "TSV 26-May",
        "overall_summary": (
            "NVDA concentration ~50% remains the dominant risk; FX assumption "
            "is stale; pension data for spouse is missing."
        ),
        "confidence": "MEDIUM",
        "cited_sources": [
            "domain_knowledge/tax/israel/retirement/section_102.md",
            "domain_knowledge/tax/us/estate_tax_nonresidents.md",
        ],
        "findings": [
            {
                "plan_item_ref": "Concentration target — NVDA 15%",
                "severity": "RED",
                "topic": "Concentration Risk",
                "summary": "NVDA still ~50% of liquid; far from the 15% target.",
                "evidence": [
                    "Snapshot shows 11,471 NVDA shares × $200 = $2.296M.",
                    "Liquid total ~$3.36M → ~68% NVDA concentration.",
                ],
                "cited_sources": [
                    "domain_knowledge/tax/israel/retirement/section_102.md",
                ],
                "recommended_action": "Hold the 2,000-share Q2 sale per current action plan.",
            },
            {
                "plan_item_ref": "FX assumption 3.09 NIS/USD",
                "severity": "YELLOW",
                "topic": "FX",
                "summary": "Plan FX is stale vs current 2.94.",
                "evidence": [
                    "Plan says 3.09 NIS/USD; TSV header shows 2.94.",
                ],
                "cited_sources": [],
                "recommended_action": "Refresh plan FX assumption.",
            },
        ],
    }
    agent = _MockPlanCritiqueAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        plan_label="Jacobs_Wealth_Plan v2.0",
        plan_markdown="# Plan\n\nNVDA target 15%.\n",
        snapshot_label="TSV 26-May",
        snapshot_summary="11471 NVDA at $200; total liquid ~$3.36M",
        user_context_yaml="tax_residency: israel\n",
        domain_kb_files={
            "domain_knowledge/tax/israel/retirement/section_102.md": "S.102 rules...",
            "domain_knowledge/tax/us/estate_tax_nonresidents.md": "US estate rules...",
        },
    )
    out = report.output
    assert isinstance(out, PlanCritiqueReport)
    assert len(out.findings) == 2
    severities = {f.severity for f in out.findings}
    assert severities == {"RED", "YELLOW"}
    assert all(isinstance(f, Finding) for f in out.findings)
    assert out.cited_sources, "Top-level cited_sources must be non-empty"
    assert report.tokens_in == 300

    # Wave A: build_prompt should extract sources (plan + snapshot +
    # domain_kb_files) into Citations API document blocks rather than
    # inlining them in the user prompt.
    assert agent.last_sources is not None
    source_ids = [sid for sid, _ in agent.last_sources]
    assert "plan/markdown" in source_ids
    assert "portfolio/snapshot" in source_ids
    assert "domain_knowledge/tax/israel/retirement/section_102.md" in source_ids
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in source_ids
    # The actual plan body must NOT be inlined in the user prompt anymore
    # — it must come through the document source so the Citations API can
    # attribute spans back to it.
    assert "NVDA target 15%" not in (agent.last_user or "")
    # ... but the user prompt must still REFERENCE the source by id.
    assert "plan/markdown" in (agent.last_user or "")


def test_build_prompt_includes_user_directive_when_provided() -> None:
    """When the orchestrator threads a non-empty ``user_directive``, the
    plan_critique system prompt MUST include the DIRECTIVE POINTER block
    and the user prompt MUST carry the verbatim directive text at the
    top. Closes the D1 self-review finding for Phase 1 plan_critique —
    without this thread the critique re-raises findings the user has
    already AGREED with, which the synthesizer + FM then re-litigate
    downstream.
    """
    directive = (
        "AGREED: NVDA concentration must be capped at 12%.\n"
        "DISAGREED: tax-loss harvest is NOT urgent — user counter-position\n"
        "  is to defer harvesting until Q4 2026.\n"
        "DEFERRED: FX hedge sizing — re-evaluate honestly."
    )
    agent = PlanCritiqueAgent(user_id="ariel")
    sys, usr, _sources = agent.build_prompt(
        plan_label="x", plan_markdown="m", snapshot_label="y",
        snapshot_summary="s", user_context_yaml="",
        domain_kb_files={}, recent_events="",
        user_directive=directive,
    )
    # Post-fix: system holds the POINTER + instructions for the three
    # stances; verbatim directive content lives in the user prompt to
    # dodge the bundled claude.exe SDK's empty-output path observed
    # with large variable content in system prompts.
    assert "USER DIRECTIVE PRESENT" in sys
    # Verbatim directive content reaches the model — via the USER prompt.
    assert "AGREED: NVDA concentration must be capped at 12%." in usr
    assert "DISAGREED: tax-loss harvest is NOT urgent" in usr
    assert "DEFERRED: FX hedge sizing" in usr
    # Per-stance instruction language must be in the system prompt so
    # the critique knows how to act.
    assert "do NOT re-raise" in sys
    assert "counter-position" in sys
    assert "DEFERRED" in sys


def test_build_prompt_omits_directive_section_when_empty() -> None:
    """Empty ``user_directive`` (default) MUST produce a byte-identical
    system+user prompt to the no-kwarg call. Guards the happy path on
    the scheduled monthly synthesis cycle that doesn't carry user
    feedback.
    """
    agent = PlanCritiqueAgent(user_id="ariel")
    base_kwargs = dict(
        plan_label="x", plan_markdown="m", snapshot_label="y",
        snapshot_summary="s", user_context_yaml="",
        domain_kb_files={}, recent_events="",
    )
    sys_a, usr_a, _ = agent.build_prompt(**base_kwargs)
    sys_b, usr_b, _ = agent.build_prompt(**base_kwargs, user_directive="")
    assert sys_a == sys_b, (
        "empty user_directive must produce a byte-identical system prompt "
        "to the no-kwarg call"
    )
    assert usr_a == usr_b
    # The section header must NOT appear when directive is empty.
    assert "USER DIRECTIVE" not in sys_a
    assert "USER DIRECTIVE" not in usr_a


@pytest.mark.asyncio
async def test_plan_critique_rejects_uncited_output() -> None:
    canned = {
        "plan_label": "X",
        "snapshot_label": "Y",
        "overall_summary": "All good.",
        "confidence": "HIGH",
        "cited_sources": [],
        "findings": [
            {
                "plan_item_ref": "Z",
                "severity": "GREEN",
                "topic": "T",
                "summary": "ok",
                "evidence": [],
                "cited_sources": [],
                "recommended_action": None,
            }
        ],
    }
    agent = _MockPlanCritiqueAgent(user_id="ariel", canned_output=canned)
    with pytest.raises(AgentRunError):
        await agent.run(
            plan_label="X",
            plan_markdown="m",
            snapshot_label="Y",
            snapshot_summary="s",
            user_context_yaml="",
            domain_kb_files={},
        )
