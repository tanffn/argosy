"""Segment-level surgical reconcile — deterministic (injected editor, no LLM/DB).

Proves: a fixable reader finding's cited offending snippet is edited IN PLACE in
the affected horizon body; the rest of the plan is untouched (no reshuffle); a
structural / uncited finding is left for the full-resynth fallback."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
    surgically_correct_draft,
)


def _verdict(findings):
    return SimpleNamespace(overall_assessment="BLOCK", findings=findings)


def test_fixable_finding_edits_only_the_cited_segment():
    bodies = {
        "long": "Capital sufficiency is fully reached today. Other strategic text stays.",
        "medium": "Medium horizon unrelated content.",
        "short": "Short horizon content.",
    }
    finding = SimpleNamespace(
        kind="fragile_claim", severity="BLOCKER",
        detail="headline sufficiency claim is fragile under the FX/margin caveat",
        surfaces_cited=["Capital sufficiency is fully reached today."],
    )

    def stub_editor(prompt: str) -> str:
        # the editor qualifies the claim (honest), does not delete it
        return "Capital sufficiency is reached only at the current FX mark — it can flip negative under a -10% shekel move."

    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None, editor=stub_editor
    )
    # the cited segment was replaced in the LONG body only
    assert "only at the current FX mark" in result.corrected_bodies["long"]
    assert "Other strategic text stays." in result.corrected_bodies["long"]  # no reshuffle
    assert result.corrected_bodies["medium"] == bodies["medium"]  # untouched
    assert result.corrected_bodies["short"] == bodies["short"]
    assert finding in result.addressed
    assert len(result.edits) == 1 and result.edits[0].horizon == "long"


def test_structural_or_uncited_finding_left_for_fallback():
    bodies = {"long": "x", "medium": "y", "short": "z"}
    # 'other' with no cited surface = reader infra-failure, not a fixable segment
    infra = SimpleNamespace(kind="other", severity="BLOCKER", detail="timeout", surfaces_cited=[])
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([infra]), resolved=None,
        editor=lambda p: "should not be called",
    )
    assert infra in result.unaddressed
    assert result.corrected_bodies == bodies
    assert result.edits == []


def test_cited_excerpt_not_in_body_is_skipped_not_crashed():
    bodies = {"long": "nothing matching here", "medium": "", "short": ""}
    finding = SimpleNamespace(
        kind="contradiction", severity="AMBER", detail="d",
        surfaces_cited=["a snippet that does not appear verbatim"],
    )
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None,
        editor=lambda p: "edited",
    )
    # no body contained the excerpt -> no edit, finding left unaddressed, no crash
    assert result.edits == []
    assert finding in result.unaddressed


def test_dict_shaped_findings_supported():
    bodies = {"long": "Liquidity runway is 8.7 months.", "medium": "", "short": ""}
    finding = {"kind": "contradiction", "severity": "AMBER",
               "detail": "runway divergence", "surfaces_cited": ["Liquidity runway is 8.7 months."]}
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict={"findings": [finding]}, resolved=None,
        editor=lambda p: "Liquidity runway is 8.7 months (operating account; the 53.5-month figure is the full investable book).",
    )
    assert "full investable book" in result.corrected_bodies["long"]
    assert finding in result.addressed
