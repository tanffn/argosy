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
        # the editor qualifies the claim honestly, in WORDS, with no NEW numbers
        return "Capital sufficiency is reached only on a thin margin that a routine currency move could erase."

    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None, editor=stub_editor
    )
    # the cited segment was replaced in the LONG body only
    assert "thin margin" in result.corrected_bodies["long"]
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


def test_edit_introducing_new_number_is_rejected():
    # the editor must not inject a figure absent from the original (it creates
    # fresh numeric contradictions); such an edit is rejected, finding falls back
    bodies = {"long": "Capital sufficiency is reached.", "medium": "", "short": ""}
    finding = SimpleNamespace(
        kind="fragile_claim", severity="BLOCKER", detail="fragile",
        surfaces_cited=["Capital sufficiency is reached."],
    )
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None,
        editor=lambda p: "Capital sufficiency is reached with net worth of $3.93M.",  # NEW number
    )
    assert result.edits == []
    assert finding in result.unaddressed
    assert result.corrected_bodies["long"] == bodies["long"]


def test_edit_leaking_prior_plan_is_rejected():
    bodies = {"long": "", "medium": "", "short": "Execute the NVDA tranche."}
    finding = SimpleNamespace(
        kind="regression", severity="AMBER", detail="dropped lot IDs",
        surfaces_cited=["Execute the NVDA tranche."],
    )
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None,
        editor=lambda p: "Execute the NVDA tranche per the prior plan's lot list.",  # history leak
    )
    assert result.edits == []
    assert finding in result.unaddressed


def test_qualitative_edit_with_no_new_number_is_accepted():
    bodies = {"long": "Runway is 8.7 months.", "medium": "", "short": ""}
    finding = SimpleNamespace(
        kind="contradiction", severity="AMBER", detail="basis ambiguous",
        surfaces_cited=["Runway is 8.7 months."],
    )
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict=_verdict([finding]), resolved=None,
        editor=lambda p: "Runway is 8.7 months on a cash-only basis (longer on the full investable basis).",
    )
    assert len(result.edits) == 1
    assert "cash-only basis" in result.corrected_bodies["long"]


def test_dict_shaped_findings_supported():
    bodies = {"long": "Liquidity runway is 8.7 months.", "medium": "", "short": ""}
    finding = {"kind": "contradiction", "severity": "AMBER",
               "detail": "runway divergence", "surfaces_cited": ["Liquidity runway is 8.7 months."]}
    result = surgically_correct_draft(
        bodies=bodies, reader_verdict={"findings": [finding]}, resolved=None,
        editor=lambda p: "Liquidity runway is 8.7 months on the cash-only operating account (longer on the full investable book).",
    )
    assert "full investable book" in result.corrected_bodies["long"]
    assert finding in result.addressed
