"""Codex ZigZag — independent second-opinion reviewer between Phase 4 and Phase 5.

After the risk team produces its consolidated verdict (Phase 4) and BEFORE
the FundManagerAgent reads anything (Phase 5), dispatch ``codex (gpt-5)``
via the ``tools/codex-tandem`` kit as an INDEPENDENT reviewer. Codex sees
the same inputs FM is about to see, MINUS the prior-round FM objections —
contamination guard so the codex verdict isn't a mirror of FM's framing.

Design contract
---------------
**Inputs (verbatim, no editorial summary):**
  - the synthesizer's draft (``PlanSynthesisOutput.model_dump_json()``)
  - the phase 1 analyst reports (concatenated text)
  - the phase 2 debate outcomes (concatenated text)
  - the phase 4 consolidated risk verdict (text)
  - the user_directive (``guidance`` from ``run_synthesis``) — same
    AGREED/DISAGREED/DEFERRED resolution stances the FM is told to
    respect.

**Codex does NOT see:**
  - prior round's FM objections (would bias toward FM's framing)
  - prior round's codex verdicts (forces fresh reasoning)
  - the current synthesis's downstream FM output (this is the FIRST
    codex call this run)

**Persistence:**
The verdict is persisted as one ``agent_reports`` row with
``agent_role="codex_second_opinion"`` so it shows up in the FM-rooted
sequence diagram and the audit trail. A ``decision_phases`` row of
``kind='synthesis.phase_4_5'`` back-links the agent_report.

**Availability and independence:**
A provider outage can produce a Claude advisory review. An advisory report
preserves its author and findings, while the independent gate records DID_NOT_RUN.
Malformed output from a completed primary review still blocks the gate.

**Kill switches:**
  - ``ARGOSY_CODEX_REVIEW_ENABLED != "1"`` → skipped silently.
  - Running under pytest (``PYTEST_CURRENT_TEST`` env var present)
    → skipped silently, so existing tests pass unchanged.

**Idempotency:**
``run_codex_second_opinion`` checks ``agent_reports`` for an existing
``codex_second_opinion`` row keyed on ``(decision_id, agent_role)``. If
present, the row's stored verdict is parsed and returned without
re-dispatching codex. This makes the resume path safe.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import AgentReport
from argosy.agents._review_criteria import (
    ACCEPTANCE_CRITERIA,
    ACCEPTANCE_CRITERIA_BY_ID,
    CriterionVerdict,
    render_criteria_block,
)
from argosy.orchestrator.flows.plan_synthesis.review_dispatch import (
    ReviewUnavailable, dispatch_plan_review,
)
from argosy.logging import get_logger
from argosy.quality.review_provenance import codex_contributed
from argosy.quality.verification import GateOutcome, GateStatus  # noqa: F401

log = get_logger(__name__)


# ----------------------------------------------------------------------
# The tandem worker is reaped with its descendants before an alternate starts.
_HARD_CEILING_S = 540  # 480s run_codex timeout_s + 60s grace


# ----------------------------------------------------------------------
# Output schema — pydantic so callers (FM prompt builder, replay UI) get
# typed access to severity, topic, citations.
# ----------------------------------------------------------------------


class CodexFinding(BaseModel):
    """One finding emitted by the codex second-opinion reviewer."""

    severity: Literal["BLOCKER", "AMBER", "YELLOW"]
    topic: str = Field(description="Short label (1-5 words).")
    detail: str = Field(description="Full explanation, model's own words.")
    suggested_fix: str = ""
    cited_synthesizer_paragraphs: list[str] = Field(
        default_factory=list,
        description="Excerpts (verbatim) from the synthesizer draft that "
        "anchor this finding. Strengthens auditability.",
    )


class CodexAgreement(BaseModel):
    """How the codex verdict relates to the Argosy fleet's own consensus."""

    agrees_with_risk_verdict: bool | Literal["partial"] | None = None
    novel_concerns_argosy_missed: list[str] = Field(default_factory=list)


class HeadlineNumberAudit(BaseModel):
    """One independently re-derived headline number vs the pipeline's claim.

    The reviewer MUST populate one row per load-bearing headline figure it can
    recompute from the raw holdings (net worth, US-situs estate, NVDA weight,
    FI target). This forces the reviewer to SHOW its independent math rather
    than rubber-stamp the manifest — ``status="DIVERGES"`` or ``"UNVERIFIABLE"``
    forces an overall BLOCK.
    """

    metric: str = Field(description="e.g. us_situs_estate_nis, nvda_weight_pct")
    independent_value: float | None = Field(
        default=None,
        description="The reviewer's OWN figure, re-derived from raw holdings.",
    )
    claimed_value: float | None = Field(
        default=None, description="The pipeline's claimed figure for this metric."
    )
    formula: str = Field(default="", description="How the independent value was derived.")
    raw_rows_used: list[str] = Field(
        default_factory=list, description="Raw-holding rows the derivation used."
    )
    status: Literal["MATCH", "DIVERGES", "UNVERIFIABLE"] = "UNVERIFIABLE"


class CodexSecondOpinion(BaseModel):
    """The full structured codex second-opinion verdict."""

    overall_assessment: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "BLOCK"]
    findings: list[CodexFinding] = Field(
        default_factory=list,
        description="ALL observations, blocking or not. A finding with "
        "severity=BLOCKER is only a real blocker if its topic is prefixed "
        "with a recognized finite-criterion id (e.g. "
        "'[C3_US_SITUS_ESTATE] ...') — see criteria_verdicts, which is the "
        "authoritative source for overall_assessment. A BLOCKER finding "
        "with no criterion id is automatically downgraded to AMBER "
        "(advisory) and does not block promotion.",
    )
    criteria_verdicts: list[CriterionVerdict] = Field(
        default_factory=list,
        description=f"One entry for EVERY one of the {len(ACCEPTANCE_CRITERIA)} "
        "finite acceptance criteria — pass/fail with evidence. The ONLY "
        "thing that may drive overall_assessment=BLOCK (together with a "
        "headline_number_audit divergence, which IS a C1_HEADLINE_TRACE "
        "failure and must be reflected as one).",
    )
    headline_number_audit: list[HeadlineNumberAudit] = Field(
        default_factory=list,
        description="Independent re-derivation of each recomputable headline "
        "number. Any DIVERGES/UNVERIFIABLE row forces overall_assessment=BLOCK "
        "and is a C1_HEADLINE_TRACE failure.",
    )
    agreement_with_argosy: CodexAgreement = Field(default_factory=CodexAgreement)
    user_directive_respected: bool | None = None


# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------


_PROMPT_TEMPLATE = """\
You are an INDEPENDENT second-opinion reviewer for a multi-million-dollar \
financial plan. The Argosy multi-agent system has produced a draft plan \
after analyst reports + bull/bear debates + risk officer reviews. Your \
job: form an INDEPENDENT verdict on whether to approve, approve-with-\
conditions, or block.

Stay independent — don't mirror the analysts or the risk officers. You \
may agree with their framing or disagree. Cite the EVIDENCE for your \
verdict (which paragraph of the synthesizer draft, which analyst quote, \
which risk concern).

YOUR MANDATE IS BOUNDED, NOT OPEN-ENDED. Seven drafts in a row were \
rejected under an unbounded "find problems in the prose" mandate — each \
round raised a DIFFERENT objection set (5, then 4, then 4, then an \
independent reviewer's own different 4 on the SAME draft) because an \
open-ended search never terminates. ``overall_assessment`` may be BLOCK \
ONLY on the finite list below (plus the headline-number-audit rule, \
which IS criterion C1). Stay adversarial WITHIN that list — this bounds \
the search, it does not relax it — but do not roam outside it for \
blocking purposes. Anything outside the list still gets reported (as a \
``findings`` entry with severity AMBER or YELLOW) but MUST NOT be the \
reason for a BLOCK.

{criteria_block}

Required structured output (return as JSON — and ONLY JSON, no prose \
before or after the JSON block):

{{
  "overall_assessment": "APPROVE" | "APPROVE_WITH_CONDITIONS" | "BLOCK",
  "findings": [
    {{
      "severity": "BLOCKER" | "AMBER" | "YELLOW",
      "topic": "<short — prefix with '[C#_ID] ' if this IS a finite-criterion failure, e.g. '[C4_SECTION_102] ...'>",
      "detail": "<long>",
      "suggested_fix": "<what should change if any>",
      "cited_synthesizer_paragraphs": ["<excerpt 1>", "<excerpt 2>"]
    }}
  ],
  "criteria_verdicts": [
    {{
      "criterion_id": "C1_HEADLINE_TRACE" | "C2_NO_CONTRADICTION" | "C3_US_SITUS_ESTATE" | "C4_SECTION_102" | "C5_FI_BOTH_BASES" | "C6_NO_FALSE_CERTAINTY",
      "verdict": "PASS" | "FAIL",
      "impact": "CRITICAL" | "MAJOR" | "N/A",
      "evidence": "<specific evidence — a cited figure, section, or raw-data row>"
    }}
    // one entry per criterion, six total, always
  ],
  "headline_number_audit": [
    {{
      "metric": "<e.g. us_situs_estate_nis | net_worth_nis | nvda_weight_pct | fi_target_nis>",
      "independent_value": <YOUR figure, re-derived from the raw holdings>,
      "claimed_value": <the pipeline's claimed figure>,
      "formula": "<how you derived independent_value>",
      "raw_rows_used": ["<raw holding rows you summed>"],
      "status": "MATCH" | "DIVERGES" | "UNVERIFIABLE"
    }}
  ],
  "agreement_with_argosy": {{
    "agrees_with_risk_verdict": true | false | "partial",
    "novel_concerns_argosy_missed": ["<list>"]
  }},
  "user_directive_respected": true | false
}}

You MUST emit one ``headline_number_audit`` row for EVERY recomputable headline \
number (net worth, US-situs estate, NVDA weight, FI target). ``MATCH`` only when \
your independent value is within tolerance of the claim — tolerance = \
max(₪5,000, 0.25% of the figure). ``DIVERGES`` when it is outside tolerance. \
``UNVERIFIABLE`` when the raw holdings do not contain enough to re-derive it \
(e.g. pensions, real-estate equity, or spend not present in the holdings). \
ANY row with ``DIVERGES`` or ``UNVERIFIABLE`` for a load-bearing figure forces \
``overall_assessment="BLOCK"`` and a matching BLOCKER finding — record it ALSO \
as a ``criteria_verdicts`` entry with ``criterion_id="C1_HEADLINE_TRACE"``, \
``verdict="FAIL"``. This \
headline-number audit OVERRIDES ``agreement_with_argosy`` and the user \
directive: a mathematical divergence is a BLOCKER even if the analysts, risk \
officers, synthesizer, or user directive appear to accept the number.

``overall_assessment="BLOCK"`` if and only if AT LEAST ONE ``criteria_verdicts`` \
entry is ``verdict="FAIL"`` (which includes the C1_HEADLINE_TRACE case above). \
A finding you consider serious but that fails none of the six criteria goes in \
``findings`` with severity AMBER or YELLOW, contributes to \
``overall_assessment="APPROVE_WITH_CONDITIONS"`` at most, and must NEVER by \
itself produce ``overall_assessment="BLOCK"``.

Respect the user_directive in the same way the fund manager is told to: \
the user's AGREED stances are NOT to be re-raised; their DISAGREED \
counter-positions should be evaluated on the merits of the synthesizer's \
response, not the original concern; DEFERRED items are open for fresh \
evaluation. Set ``user_directive_respected=false`` ONLY if the \
synthesizer has clearly ignored or violated a load-bearing user stance.

HEADLINE-NUMBER AUDIT BY INDEPENDENT RE-DERIVATION (your most important job \
— this plan drives a REAL retirement decision, so be adversarial about the \
math). The cardinal rule: DO NOT trust the pipeline's numbers because they \
are internally consistent. A multi-agent pipeline can agree with itself on a \
WRONG number — every surface inherits the same upstream error. Your value is \
that you re-derive from the RAW INPUTS with your OWN logic, blind to how the \
pipeline computed anything.

  0. RE-DERIVE FIRST, READ THE MANIFEST SECOND. Before you even look at the \
     PIPELINE-CLAIMED HEADLINE NUMBERS block, use the RAW PORTFOLIO HOLDINGS \
     block (and the analyst spend/budget figures) to INDEPENDENTLY compute, \
     from scratch and showing your work:
       (a) Net worth — sum the holdings (USD positions × FX + NIS-native), \
           and state explicitly what you included/excluded (real estate? \
           pensions?).
       (b) US-situs estate exposure — classify EACH holding by INSTRUMENT \
           DOMICILE, NOT by which broker holds it: a US-domiciled security \
           (NVDA, SCHD, VOO, AMD, QQQM, …) is US-situs whether held at a US \
           broker or an Israeli one; Irish/London UCITS funds, Israeli \
           trackers, and cash are NOT US-situs. Sum the US-situs USD × FX.
       (c) NVDA concentration — compute NVDA ÷ (a denominator you NAME \
           explicitly: tradeable book? net worth incl. real estate?). If the \
           plan quotes a different NVDA % elsewhere, that is a denominator \
           inconsistency to flag.
       (d) FI target — permanent-equivalent spend ÷ the perpetual real SWR.
  1. ONLY NOW compare your independent figures to the PIPELINE-CLAIMED \
     HEADLINE NUMBERS block. Treat that block as a CLAIM to reproduce, NOT as \
     truth. ANY claimed headline number you cannot reproduce from the raw \
     inputs, or that diverges from your independent derivation by more than a \
     rounding tolerance, is a BLOCKER — say whether it is a DERIVATION error \
     (the pipeline computed it wrong from the data) or a FABRICATION (the \
     prose states a number the pipeline never derived). Cite the raw rows and \
     show both figures. The same NVDA weight or estate figure appearing \
     consistently across the plan is NOT evidence it is correct.
  2. Critique the FI METHODOLOGY itself, not just the arithmetic. Is the \
     spend basis the permanent-equivalent spend (incl. amortized life events \
     — car cadence, healthcare ramp, home upgrades), or just the current \
     tracked burn? Is the yield a defensible perpetual real safe-withdrawal \
     rate (~2.4–3.5% after-tax for a 90+yr 0%-principal-drawdown mandate), or \
     is it the aggressive expected RETURN? An indefensible or internally \
     inconsistent methodology is at least an AMBER, a BLOCKER if it would \
     materially mis-state the FI date.
  3. Flag any headline claim that cites NO source, or whose prose contradicts \
     your re-derivation (e.g. \"comfortably past FI\" / \"capital sufficiency \
     reached\" on a razor-thin margin that is itself dominated by one volatile \
     concentrated position held at full value).
  Only if EVERY claimed headline number reproduces from the raw inputs within \
  tolerance AND the methodology is defensible may you say the math is sound — \
  and say so explicitly in agreement_with_argosy, naming the figures you \
  reproduced.

=== RAW PORTFOLIO HOLDINGS (your raw inputs — re-derive from THESE FIRST) ===
{raw_holdings_block}

>>> STOP. Before reading anything below, do your headline_number_audit from \
the RAW HOLDINGS above: independently compute net worth, US-situs estate \
exposure (by instrument domicile), and NVDA weight. Write down your figures. \
The blocks below (the pipeline's claimed numbers and the synthesizer's prose) \
contain the SAME headline numbers — they will anchor you to the pipeline's \
answer if you read them before deriving your own. Derive first; compare second.

=== PIPELINE-CLAIMED HEADLINE NUMBERS (a CLAIM to reproduce — NOT truth) ===
{derived_numbers_block}

=== SYNTHESIZER DRAFT (Phase 3 output) ===
{synth_draft_json}

=== ANALYST REPORTS (Phase 1) ===
{analyst_reports_text}

=== HORIZON DEBATES (Phase 2) ===
{debate_outcomes_text}

=== RISK VERDICT (Phase 4, consolidated) ===
{risk_verdict_text}

=== USER DIRECTIVE ===
{user_directive_block}

Produce the JSON now. No prose, no markdown fences — just the JSON object.
"""


def _build_prompt(
    *,
    synth_draft_json: str,
    analyst_reports_text: str,
    debate_outcomes_text: str,
    risk_verdict_text: str,
    user_directive: str,
    derived_numbers_block: str = "",
    raw_holdings_block: str = "",
) -> str:
    """Render the full codex prompt with all evidence blocks inlined.

    The user_directive block is either the verbatim directive or a
    sentinel string when none was passed — so the model never gets a
    bare placeholder it has to ignore. ``derived_numbers_block`` is the
    pipeline's CLAIMED headline numbers — codex re-derives the
    recomputable ones from ``raw_holdings_block`` and flags divergence,
    rather than treating the manifest as truth. A sentinel is used for
    either block when it could not be built.
    """
    user_directive_block = (
        user_directive.strip() if user_directive and user_directive.strip()
        else "(no user directive on this run)"
    )
    numbers_block = (
        derived_numbers_block.strip()
        if derived_numbers_block and derived_numbers_block.strip()
        else "(pipeline-claimed numbers unavailable on this run — re-derive the "
        "math from the raw holdings + analyst reports directly)"
    )
    holdings_block = (
        raw_holdings_block.strip()
        if raw_holdings_block and raw_holdings_block.strip()
        else "(raw holdings unavailable on this run — re-derive what you can "
        "from the analyst reports and flag the figures you could not verify)"
    )
    return _PROMPT_TEMPLATE.format(
        synth_draft_json=synth_draft_json,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        risk_verdict_text=risk_verdict_text,
        user_directive_block=user_directive_block,
        derived_numbers_block=numbers_block,
        raw_holdings_block=holdings_block,
        criteria_block=render_criteria_block(),
    )


# ----------------------------------------------------------------------
# Parsing — strict → lenient → synthetic "unparseable" fallback so the
# FM still sees SOMETHING from codex even when the model emits prose.
# ----------------------------------------------------------------------


def _enforce_headline_audit(opinion: CodexSecondOpinion) -> CodexSecondOpinion:
    """Structural backstop for the headline-number audit.

    The prompt tells codex that any ``DIVERGES`` headline-audit row must force
    ``overall_assessment="BLOCK"``. Don't rely on the model obeying — enforce
    it in code. If a load-bearing figure the reviewer independently re-derived
    diverges from the pipeline's claim, the verdict is BLOCK regardless of what
    the model wrote, and a BLOCKER finding is synthesized if one is missing.
    This is what makes the blind re-derivation a real gate, not a prompt wish.
    """
    diverged = [a for a in opinion.headline_number_audit if a.status == "DIVERGES"]
    if not diverged:
        return opinion
    opinion.overall_assessment = "BLOCK"
    has_blocker = any(f.severity == "BLOCKER" for f in opinion.findings)
    if not has_blocker:
        metrics = ", ".join(
            f"{a.metric}: independent {a.independent_value} vs claimed "
            f"{a.claimed_value}" for a in diverged
        )
        opinion.findings.insert(0, CodexFinding(
            severity="BLOCKER",
            topic="[C1_HEADLINE_TRACE] headline_number_divergence",
            detail=(
                "Independent re-derivation from the raw holdings diverged from "
                f"the pipeline's claimed headline number(s): {metrics}. A "
                "headline figure that cannot be reproduced from the raw data is "
                "a derivation error or fabrication — blocking promotion."
            ),
            suggested_fix="Re-derive the diverging figure from raw holdings and "
                          "fix the upstream computation before promotion.",
        ))
    return opinion


def _enforce_finite_criteria(opinion: CodexSecondOpinion) -> CodexSecondOpinion:
    """Bound ``overall_assessment=BLOCK`` to the finite criteria list.

    Two structural guarantees, not a prompt wish:

    1. A ``findings`` entry with ``severity="BLOCKER"`` whose ``topic`` does
       not reference a recognized ``ACCEPTANCE_CRITERIA`` id is downgraded to
       ``"AMBER"`` — an out-of-list objection cannot masquerade as a blocker.
    2. If, after that downgrade, NO ``criteria_verdicts`` entry is
       ``verdict="FAIL"`` and the headline-number audit did not already force
       a block, ``overall_assessment`` cannot remain ``"BLOCK"`` — it is
       relaxed to ``"APPROVE_WITH_CONDITIONS"`` so leftover advisory findings
       still surface without silently stalling promotion.

    This mirrors ``_enforce_headline_audit``'s existing pattern (a prior
    round already treats the math re-derivation as a real gate, not a
    prompt wish) and extends the same discipline to the rest of the finite
    criteria list.
    """
    known_ids = tuple(ACCEPTANCE_CRITERIA_BY_ID)
    for f in opinion.findings:
        if f.severity != "BLOCKER":
            continue
        if not any(cid in f.topic for cid in known_ids):
            f.severity = "AMBER"

    any_fail = any(v.verdict == "FAIL" for v in opinion.criteria_verdicts)
    headline_diverged = any(
        a.status == "DIVERGES" for a in opinion.headline_number_audit
    )
    if opinion.overall_assessment == "BLOCK" and not any_fail and not headline_diverged:
        opinion.overall_assessment = "APPROVE_WITH_CONDITIONS"
    return opinion


def _parse_codex_verdict(text: str) -> CodexSecondOpinion:
    """Parse codex's raw text into a ``CodexSecondOpinion``.

    Strategy:
      1. Strict ``model_validate_json`` on the entire text.
      2. Lenient: locate the first ``{`` and try ``JSONDecoder.raw_decode``.
      3. Synthetic "unparseable" opinion so callers still get a typed
         object (with a YELLOW finding flagging the parse failure).

    A successful parse is passed through ``_enforce_headline_audit`` so a
    divergent re-derivation forces BLOCK even if the model didn't.
    """
    if text:
        # Strip a fenced ```json block if the model added one despite
        # the prompt's instructions.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            return _enforce_finite_criteria(_enforce_headline_audit(
                CodexSecondOpinion.model_validate_json(cleaned)
            ))
        except Exception:
            pass

        # Lenient: find first { and try to raw_decode from there.
        first_brace = cleaned.find("{")
        if first_brace >= 0:
            try:
                decoder = json.JSONDecoder(strict=False)
                obj, _ = decoder.raw_decode(cleaned[first_brace:])
                return _enforce_finite_criteria(_enforce_headline_audit(
                    CodexSecondOpinion.model_validate(obj)
                ))
            except Exception:
                pass

    # Synthetic fallback — preserves the codex review row in the audit trail
    # even when the model didn't return clean JSON (or timed out → empty text).
    # FAIL CLOSED: this is a MATH gate, so a reviewer that did not actually
    # run / re-derive must NOT yield a passing verdict. A timeout or unparseable
    # output is a BLOCK, not a soft APPROVE_WITH_CONDITIONS — otherwise a
    # non-verdict silently waves the plan through (exactly what a timed-out
    # reviewer did on run 101). See feedback: fail loud on critical-agent failure.
    excerpt = (text or "")[:400]
    timed_out = not (text or "").strip()
    log.warning(
        "codex_second_opinion.unparseable",
        raw_excerpt=excerpt,
        timed_out=timed_out,
    )
    reason = (
        "Codex returned NO output (timeout / dispatch failure) — the "
        "independent headline-number re-derivation did not run."
        if timed_out else
        "Codex returned non-JSON output and the lenient parse fallback "
        f"couldn't recover a verdict. Raw excerpt (first 400 chars): {excerpt}"
    )
    return CodexSecondOpinion(
        overall_assessment="BLOCK",
        findings=[CodexFinding(
            severity="BLOCKER",
            topic="codex_review_unavailable",
            detail=(
                f"{reason} The independent math gate could not verify the "
                "headline numbers, so the plan is fail-closed (BLOCK) rather "
                "than soft-passed. Re-run the reviewer (often a transient "
                "timeout under load) or escalate for manual numeric review."
            ),
            suggested_fix="Re-dispatch codex with no competing load; if it "
                          "still fails, manually re-derive net worth / estate / "
                          "NVDA weight / FI target from the raw holdings.",
            cited_synthesizer_paragraphs=[],
        )],
        agreement_with_argosy=CodexAgreement(),
        user_directive_respected=None,
    )


# ----------------------------------------------------------------------
# Idempotency — check for an existing codex row before re-dispatching.
# ----------------------------------------------------------------------


async def _load_existing_codex_opinion(
    *, decision_audit_token: str, user_id: str, prompt: str,
) -> CodexSecondOpinion | None:
    """Return a previously-persisted codex opinion for this decision_id.

    Used by the resume path so a re-run after a Phase 5 failure doesn't
    re-dispatch codex (~$0.50 wasted + 1-3 min latency). Returns None on
    no row, parse failure, or any unexpected error (synthesis falls back
    to a fresh codex dispatch in that case, which is safe).

    Async because the only caller (``run_codex_second_opinion``) already
    runs inside a live asyncio loop; the DB read is awaited directly so we
    never nest ``asyncio.run`` (which raises "cannot be called from a
    running event loop" and silently disables idempotency).
    """
    try:
        from sqlalchemy import select
        from argosy.state import db as db_mod
        from argosy.state.models import AgentReport as AgentReportORM

        # Best-effort read via a fresh session — any failure here just
        # means we re-dispatch codex.
        async with db_mod.get_session() as session:
            row = await session.execute(
                select(AgentReportORM.response_text)
                .where(AgentReportORM.user_id == user_id)
                .where(AgentReportORM.decision_id == decision_audit_token)
                .where(AgentReportORM.agent_role == "codex_second_opinion")
                .where(AgentReportORM.user_prompt == prompt)
                .order_by(AgentReportORM.id.desc())
                .limit(1)
            )
            text = row.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "codex_second_opinion.idempotency_lookup_failed",
            decision_audit_token=decision_audit_token,
            error=str(exc),
        )
        return None

    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "advisory_review" in payload:
            return None
        return CodexSecondOpinion.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "codex_second_opinion.idempotency_parse_failed",
            decision_audit_token=decision_audit_token,
            error=str(exc),
        )
        return None


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def _resolve_codex_scripts_dir() -> Path:
    """Locate ``tools/codex-tandem/scripts`` relative to this file.

    The package layout is ``<repo>/argosy/orchestrator/flows/plan_synthesis/codex_second_opinion.py``;
    walking 4 parents reaches ``<repo>``. Computed at call time so test
    monkey-patches or alternative checkouts work.
    """
    return Path(__file__).resolve().parents[4] / "tools" / "codex-tandem" / "scripts"


async def run_codex_second_opinion(
    *,
    synth_draft_json: str,
    analyst_reports_text: str,
    debate_outcomes_text: str,
    risk_verdict_text: str,
    user_directive: str,
    decision_run_id: int,
    user_id: str,
    derived_numbers_block: str = "",
    raw_holdings_block: str = "",
) -> tuple[CodexSecondOpinion | None, AgentReport | None, GateOutcome]:
    """Dispatch codex as an independent second opinion. Fail-CLOSED.

    Returns ``(parsed_opinion, agent_report_row, gate_outcome)``. The third
    element is ALWAYS a ``GateOutcome`` with gate name ``"codex_math"``:

    * ``PASS``        — codex ran and returned APPROVE or APPROVE_WITH_CONDITIONS.
    * ``BLOCK``       — codex ran and returned BLOCK (math diverged or BLOCKER finding).
    * ``DID_NOT_RUN`` — codex could not run (env var disabled, kit missing, timeout,
                        exception). The ``detail`` names the specific cause.

    A missing independent opinion can still carry an advisory or failed-dispatch
    report for the fund manager, operator, and replay. A successful Claude review
    cannot discharge the required independent Codex check.

    Idempotency: when an existing ``codex_second_opinion`` agent_report
    row exists for ``decision_run_id``, its persisted verdict is
    returned and codex is NOT re-dispatched.
    """
    decision_audit_token = f"plan-synth-{decision_run_id}"

    # ------------------------------------------------------------------
    # Kill switches first — cheaper than any subprocess work.
    # ------------------------------------------------------------------
    if os.environ.get("ARGOSY_CODEX_REVIEW_ENABLED", "1") != "1":
        log.info(
            "codex_second_opinion.skipped_by_env_var",
            decision_run_id=decision_run_id,
        )
        return None, None, GateOutcome.did_not_run(
            "codex_math",
            detail="codex math gate disabled via ARGOSY_CODEX_REVIEW_ENABLED != '1'",
        )
    # DELIBERATE TEST SHORT-CIRCUIT: when running under pytest we skip the
    # live codex dispatch so tests don't require the codex-tandem kit.
    # The gate still reports DID_NOT_RUN (not a pass) so any caller that
    # checks blocks_promotion() will see the gate did not execute — this is
    # the correct fail-closed semantics even in tests.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info(
            "codex_second_opinion.skipped_under_pytest",
            decision_run_id=decision_run_id,
        )
        return None, None, GateOutcome.did_not_run(
            "codex_math",
            detail="codex math gate skipped under pytest (PYTEST_CURRENT_TEST set)",
        )

    # ------------------------------------------------------------------
    # Idempotency — if a row already exists, return it without dispatch.
    # ------------------------------------------------------------------
    prompt = _build_prompt(
        synth_draft_json=synth_draft_json,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        risk_verdict_text=risk_verdict_text,
        user_directive=user_directive,
        derived_numbers_block=derived_numbers_block,
        raw_holdings_block=raw_holdings_block,
    )

    existing = await _load_existing_codex_opinion(
        decision_audit_token=decision_audit_token, user_id=user_id, prompt=prompt,
    )
    if existing is not None and not codex_contributed(decision_run_id):
        log.info(
            "codex_second_opinion.idempotent_skip",
            decision_run_id=decision_run_id,
        )
        # No fresh row — the existing DB row is the source of truth.
        # Derive the gate from the persisted opinion's assessment.
        if existing.overall_assessment == "BLOCK":
            _blocker_topics = [
                f.topic for f in existing.findings if f.severity == "BLOCKER"
            ]
            _gate_detail = (
                f"codex blocked on prior run: "
                f"{', '.join(_blocker_topics[:2]) or 'see findings'}"
            )
            _existing_gate = GateOutcome.blocked("codex_math", detail=_gate_detail)
        else:
            _existing_gate = GateOutcome.passed(
                "codex_math",
                detail=f"codex ran on prior run: {existing.overall_assessment}",
            )
        return existing, None, _existing_gate

    from argosy.config import get_settings

    node_dir = (
        get_settings().home / "logs" / "synthesis" / "codex_zigzag"
        / f"run_{decision_run_id}_{uuid.uuid4().hex[:8]}"
    )
    try:
        response = await dispatch_plan_review(
            scripts_dir=_resolve_codex_scripts_dir(), node_dir=node_dir, role="codex_second_opinion",
            prompt=prompt, decision_run_id=decision_run_id, user_id=user_id,
            timeout_s=480, ceiling_s=_HARD_CEILING_S,
        )
    except ReviewUnavailable as exc:
        log.warning("codex_second_opinion.dispatch_failed", decision_run_id=decision_run_id, error=str(exc))
        row = exc.failed_report(role="codex_second_opinion", user_id=user_id, decision_run_id=decision_run_id)
        return None, row, GateOutcome.did_not_run("codex_math", detail=str(exc))

    parsed = _parse_codex_verdict(response.text)
    row = response.report(
        parsed=parsed, role="codex_second_opinion", user_id=user_id,
        decision_run_id=decision_run_id, prompt=prompt,
    )
    if response.advisory_reason:
        log.warning("codex_second_opinion.advisory_only", decision_run_id=decision_run_id,
                    reviewer=response.provider, reason=response.advisory_reason)
        return None, row, GateOutcome.did_not_run("codex_math", detail=response.advisory_reason)

    # Derive the GateOutcome from the parsed assessment so callers can call
    # blocks_promotion([gate]) to refuse promotion on any BLOCK or DID_NOT_RUN.
    if parsed.overall_assessment == "BLOCK":
        _blocker_topics = [
            f.topic for f in parsed.findings if f.severity == "BLOCKER"
        ]
        _gate_block_detail = (
            f"codex blocked: {', '.join(_blocker_topics[:2]) or 'see findings'}"
        )
        gate = GateOutcome.blocked("codex_math", detail=_gate_block_detail)
    else:
        gate = GateOutcome.passed(
            "codex_math",
            detail=f"codex ran: {parsed.overall_assessment}",
        )

    return parsed, row, gate


__all__ = [
    "CodexAgreement",
    "CodexFinding",
    "CodexSecondOpinion",
    "GateOutcome",
    "GateStatus",
    "_build_prompt",
    "_enforce_finite_criteria",
    "_parse_codex_verdict",
    "run_codex_second_opinion",
]
