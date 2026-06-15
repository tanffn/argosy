"""Unit tests for the forcing-loop detect helper + manifest-aware codex prompt."""

from __future__ import annotations

from argosy.orchestrator.flows.plan_synthesis.codex_second_opinion import (
    CodexFinding,
    CodexSecondOpinion,
    _build_prompt,
)
from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    _codex_numeric_reconcile_guidance,
)


def _op(assessment, findings):
    return CodexSecondOpinion(overall_assessment=assessment, findings=findings)


def test_numeric_blocker_triggers_reconcile():
    op = _op("BLOCK", [
        CodexFinding(severity="BLOCKER", topic="Fabricated FI target",
                     detail="States ₪21M but derived target is ₪10.4M"),
    ])
    g = _codex_numeric_reconcile_guidance(op)
    assert g is not None
    assert "21M" in g or "FI target" in g


def test_methodology_blocker_triggers_reconcile():
    op = _op("BLOCK", [
        CodexFinding(severity="BLOCKER", topic="Methodology",
                     detail="Yield uses 4.5% expected return, not a safe withdrawal rate"),
    ])
    assert _codex_numeric_reconcile_guidance(op) is not None


def test_amber_only_does_not_trigger():
    # AMBER numeric finding is advisory — the FM handles it, no re-synth.
    op = _op("APPROVE_WITH_CONDITIONS", [
        CodexFinding(severity="AMBER", topic="yield", detail="SWR slightly optimistic"),
    ])
    assert _codex_numeric_reconcile_guidance(op) is None


def test_unrelated_blocker_does_not_trigger():
    # A BLOCK on a non-numeric topic (tax sequencing) is left to the FM.
    op = _op("BLOCK", [
        CodexFinding(severity="BLOCKER", topic="Tax sequencing",
                     detail="Section 102 holding period not respected"),
    ])
    assert _codex_numeric_reconcile_guidance(op) is None


def test_approve_does_not_trigger():
    op = _op("APPROVE", [])
    assert _codex_numeric_reconcile_guidance(op) is None


def test_none_opinion_does_not_trigger():
    assert _codex_numeric_reconcile_guidance(None) is None


def test_build_prompt_includes_derived_numbers_block():
    p = _build_prompt(
        synth_draft_json="{}", analyst_reports_text="", debate_outcomes_text="",
        risk_verdict_text="", user_directive="",
        derived_numbers_block="FI target: ₪10,386,133",
    )
    assert "₪10,386,133" in p
    # The manifest is now framed as the pipeline's CLAIM to reproduce, not the
    # "single source of truth" — see codex_second_opinion blind-re-derivation.
    assert "PIPELINE-CLAIMED HEADLINE NUMBERS" in p


def test_build_prompt_sentinel_when_no_block():
    p = _build_prompt(
        synth_draft_json="{}", analyst_reports_text="", debate_outcomes_text="",
        risk_verdict_text="", user_directive="",
    )
    assert "pipeline-claimed numbers unavailable" in p
