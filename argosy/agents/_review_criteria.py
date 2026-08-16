"""The finite, explicit acceptance-criteria list for plan-revision review.

Context (2026-08-16): seven plan drafts were rejected in a row by the fund
manager (FM) and the codex second-opinion reviewer under an open-ended "find
problems" mandate. That mandate never terminates — run 363 raised 5
objections, run 369 raised 4, run 379 raised 4, an independent Sol review of
the same draft raised a DIFFERENT 4 — because "find flaws in ~5,000 words of
prose" always succeeds. Quality improved measurably across those rounds
(SGOV US-situs parking fixed, the 9,479-vs-9,230 Section-102 share gap fixed,
the invented ₪209,389 margin fixed, unsourced spend basis fixed) yet the
objection COUNT stayed flat, because the mandate itself has no stopping
condition.

This module replaces the open-ended mandate with a FINITE list both
reviewers (``FundManagerAgent`` plan_revision prompt and the codex
second-opinion prompt) are told to check. A finding may BLOCK promotion
ONLY if it fails one of these six criteria; anything else is advisory —
reported, but not blocking. The criteria were derived from what has
ACTUALLY blocked drafts (the four bug classes above) and from the plan's
own contract (canonical resolver, Section-102 statute, US-situs estate
exposure for a non-resident alien, gross-vs-after-tax FI reporting), not
invented as a wishlist.

Do NOT add a criterion here casually — each one must be genuinely
checkable (a reviewer can point at specific evidence) and genuinely
blocking (failing it means the plan is WRONG about money or law, not
merely worded badly). Adding a 7th criterion re-opens the open-ended
search this module exists to close; if a new bug class recurs twice,
that is the bar for considering an addition.

Impact calibration: a BLOCKING failure's reported severity is a function
of DECISION IMPACT (does it change what to buy/sell/hold, a tax
treatment, an estate-exposure classification, or a share count in an
executable instruction?), never of how many findings accompany it or how
large the prose passage is. A ₪33,600 labelling dispute that changes no
action must never outrank a ₪105,000 estate-classification error that
does. See ``IMPACT_CALIBRATION_NOTE`` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    title: str
    rule: str
    example_failure: str


ACCEPTANCE_CRITERIA: tuple[AcceptanceCriterion, ...] = (
    AcceptanceCriterion(
        id="C1_HEADLINE_TRACE",
        title="Every headline figure traces to a canonical resolver key",
        rule=(
            "Every user-facing headline number (net worth, US-situs estate, "
            "NVDA weight/cap/target/share-counts, FI margin — gross AND "
            "net-of-realization) must be reproducible from the canonical "
            "resolver / raw holdings, or be explicitly rendered as "
            "'[derivation pending]'. A literal that cannot be traced, or "
            "that diverges from the canonical value beyond rounding "
            "tolerance, fails this criterion."
        ),
        example_failure=(
            "An invented ₪209,389 FI margin that does not match the "
            "resolver's retirement.fi_margin_signed_nis (or its "
            "net-of-realization variant) for this run."
        ),
    ),
    AcceptanceCriterion(
        id="C2_NO_CONTRADICTION",
        title="No internal contradiction between sections",
        rule=(
            "The same concept (NVDA cap, FI-reached status, spend basis, "
            "net worth, RSU retention %) must carry the SAME value "
            "everywhere it appears, or carry an explicit, distinct label "
            "that names why the two are different quantities. An "
            "unlabeled divergence between two sections/surfaces fails "
            "this criterion."
        ),
        example_failure=(
            "Body prose states the NVDA cap is 12% while the allocation "
            "doc and rationale state 13%, with no label distinguishing "
            "two different concepts."
        ),
    ),
    AcceptanceCriterion(
        id="C3_US_SITUS_ESTATE",
        title="No recommendation increases US-situs estate exposure for a non-resident alien without stated justification",
        rule=(
            "Classify every recommended instrument by INSTRUMENT DOMICILE, "
            "not by which broker holds it. Any action that raises US-situs "
            "estate exposure (moving proceeds into a US-domiciled "
            "instrument, e.g. an ETF or T-bill fund like SGOV, even framed "
            "as 'safe parking') fails this criterion UNLESS the draft "
            "explicitly states the estate-tax tradeoff and why it is "
            "accepted (amount, statutory threshold headroom, or an "
            "irrevocable-trust/non-US-domiciled alternative was "
            "considered and rejected with reasons)."
        ),
        example_failure=(
            "Instructing a sale be 'parked in SGOV' post-trim — SGOV is a "
            "US-domiciled ETF, so this INCREASES US-situs exposure for a "
            "non-resident-alien estate, exactly backwards from the "
            "deconcentration's own estate-reduction goal — with no "
            "estate-tradeoff sentence anywhere in the draft."
        ),
    ),
    AcceptanceCriterion(
        id="C4_SECTION_102",
        title="Section-102 eligibility is respected in any sale instruction",
        rule=(
            "Any instructed/target NVDA (or other Section-102 equity comp) "
            "share count for SALE must come from the ELIGIBLE-NOW bucket "
            "(vested AND past the statutory trust period), never from a "
            "target-retention or gross-vested count. A sale instruction "
            "whose share count exceeds what is actually eligible-now fails "
            "this criterion — it would trigger ordinary-income tax "
            "treatment on shares the plan believes get capital-gains "
            "treatment, or attempt to sell shares still inside the trust "
            "period."
        ),
        example_failure=(
            "The draft instructs selling toward a target computed from "
            "9,479 'eligible' shares when the tax-simulation lots show "
            "only 9,230 shares are actually past the trust period this "
            "round — a 249-share Section-102 eligibility gap baked into "
            "an executable sale instruction."
        ),
    ),
    AcceptanceCriterion(
        id="C5_FI_BOTH_BASES",
        title="FI / capital-sufficiency is stated on both gross and after-tax bases",
        rule=(
            "Any claim that FI is 'reached', capital is 'sufficient', or "
            "similar, must state BOTH the gross margin AND the "
            "after-tax (net-of-realization) margin in the same breath or "
            "the same section. A bare 'reached' / 'sufficient' claim that "
            "cites only the gross figure — while the embedded realization "
            "tax on concentrated/unrealized positions is knowable from the "
            "tax-simulation lots — fails this criterion."
        ),
        example_failure=(
            "'FI REACHED with a +616,678 NIS cushion' stated with no "
            "adjacent after-tax figure, while the per-lot RSU tax "
            "simulation (all 10,940 NVDA shares) shows a 2,510,030 NIS "
            "embedded realization tax that flips the margin to "
            "-1,893,351 NIS net."
        ),
    ),
    AcceptanceCriterion(
        id="C6_NO_FALSE_CERTAINTY",
        title="No action is asserted as certain while its input is admitted pending",
        rule=(
            "If any section of the SAME draft marks an input as pending, "
            "estimated, unverified, or 'derivation pending', no OTHER "
            "section may assert a conclusion or action that depends on "
            "that input as certain, final, or unconditional. An "
            "overconfident headline ('capital is genuinely sufficient') "
            "that outruns a pending input disclosed elsewhere in the same "
            "draft fails this criterion."
        ),
        example_failure=(
            "The draft asserts 'capital is genuinely sufficient' in the "
            "headline while a footnote elsewhere admits the realization-"
            "tax basis or FX assumption behind that figure is pending / "
            "not yet confirmed."
        ),
    ),
)

ACCEPTANCE_CRITERIA_BY_ID: dict[str, AcceptanceCriterion] = {
    c.id: c for c in ACCEPTANCE_CRITERIA
}


IMPACT_CALIBRATION_NOTE = (
    "IMPACT CALIBRATION — severity is a function of DECISION IMPACT, never "
    "of objection count or prose volume. Rank every criterion failure by "
    "what it would change if left uncorrected:\n"
    "  - CRITICAL: changes a tax treatment, an estate-exposure "
    "classification, or a share count inside an EXECUTABLE instruction "
    "(the family would file, sell, or transfer differently).\n"
    "  - MAJOR: changes a headline number materially (beyond rounding) but "
    "does not itself change what gets executed this round.\n"
    "  - ADVISORY: wording, formatting, an additive worksheet, a labelling "
    "choice, or any other issue that changes NO number and NO action.\n"
    "A CRITICAL failure (e.g. a ~105,000 NIS estate-classification error) "
    "must never be ranked alongside or below an ADVISORY issue (e.g. a "
    "~33,600 NIS labelling dispute that changes no action) — state which "
    "tier each finding is in and justify it by what changes, not by how "
    "it reads."
)


def render_criteria_block() -> str:
    """Render the finite criteria list as prompt text.

    Both ``FundManagerAgent`` (plan_revision) and the codex second-opinion
    reviewer inject this VERBATIM so the two independent reviewers are
    bounded by the identical list — one source, no drift.
    """
    lines = [
        "FINITE ACCEPTANCE CRITERIA — this is the COMPLETE list of things "
        "that may BLOCK this plan. A finding that fails NONE of these six "
        "criteria is ADVISORY: report it, but it must not block promotion "
        "and must not be weighed as if it were a blocker. Do not go "
        "hunting for additional flaws outside this list — the search is "
        "bounded on purpose; an open-ended 'find problems' mandate never "
        "terminates and is why prior drafts kept getting rejected on a "
        "different objection set each round.\n",
    ]
    for c in ACCEPTANCE_CRITERIA:
        lines.append(f"  [{c.id}] {c.title}")
        lines.append(f"    RULE: {c.rule}")
        lines.append(f"    Example failure class: {c.example_failure}\n")
    lines.append(IMPACT_CALIBRATION_NOTE)
    return "\n".join(lines)


class CriterionVerdict(BaseModel):
    """One reviewer's pass/fail verdict on one finite acceptance criterion.

    Required for EVERY criterion in ``ACCEPTANCE_CRITERIA`` — the reviewer
    must show its work for all six, not just the ones it wants to flag.
    """

    criterion_id: str = Field(
        description="One of the ACCEPTANCE_CRITERIA ids (C1_HEADLINE_TRACE, "
        "C2_NO_CONTRADICTION, C3_US_SITUS_ESTATE, C4_SECTION_102, "
        "C5_FI_BOTH_BASES, C6_NO_FALSE_CERTAINTY)."
    )
    verdict: Literal["PASS", "FAIL"]
    impact: Literal["CRITICAL", "MAJOR", "N/A"] = Field(
        default="N/A",
        description="Decision-impact tier per IMPACT_CALIBRATION_NOTE. "
        "'N/A' only valid when verdict=PASS.",
    )
    evidence: str = Field(
        description="Specific evidence for this verdict — a cited figure, "
        "section, or raw-data row. Must be non-empty for verdict=FAIL."
    )


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "ACCEPTANCE_CRITERIA_BY_ID",
    "AcceptanceCriterion",
    "CriterionVerdict",
    "IMPACT_CALIBRATION_NOTE",
    "render_criteria_block",
]
