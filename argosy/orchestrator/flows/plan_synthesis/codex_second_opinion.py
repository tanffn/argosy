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

**Fail-soft:**
ANY codex error (kit missing, subprocess fail, timeout, parse error)
returns ``(None, None)`` and synthesis proceeds. The FM tolerates
``codex_second_opinion=None`` gracefully (no codex section in the
prompt).

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

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import AgentReport
from argosy.logging import get_logger

log = get_logger(__name__)


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
    findings: list[CodexFinding] = Field(default_factory=list)
    headline_number_audit: list[HeadlineNumberAudit] = Field(
        default_factory=list,
        description="Independent re-derivation of each recomputable headline "
        "number. Any DIVERGES/UNVERIFIABLE row forces overall_assessment=BLOCK.",
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

Required structured output (return as JSON — and ONLY JSON, no prose \
before or after the JSON block):

{{
  "overall_assessment": "APPROVE" | "APPROVE_WITH_CONDITIONS" | "BLOCK",
  "findings": [
    {{
      "severity": "BLOCKER" | "AMBER" | "YELLOW",
      "topic": "<short>",
      "detail": "<long>",
      "suggested_fix": "<what should change if any>",
      "cited_synthesizer_paragraphs": ["<excerpt 1>", "<excerpt 2>"]
    }}
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
``overall_assessment="BLOCK"`` and a matching BLOCKER finding. This \
headline-number audit OVERRIDES ``agreement_with_argosy`` and the user \
directive: a mathematical divergence is a BLOCKER even if the analysts, risk \
officers, synthesizer, or user directive appear to accept the number.

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
            topic="headline_number_divergence",
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
            return _enforce_headline_audit(
                CodexSecondOpinion.model_validate_json(cleaned)
            )
        except Exception:
            pass

        # Lenient: find first { and try to raw_decode from there.
        first_brace = cleaned.find("{")
        if first_brace >= 0:
            try:
                decoder = json.JSONDecoder(strict=False)
                obj, _ = decoder.raw_decode(cleaned[first_brace:])
                return _enforce_headline_audit(
                    CodexSecondOpinion.model_validate(obj)
                )
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
    *, decision_audit_token: str, user_id: str,
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
        return CodexSecondOpinion.model_validate_json(text)
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
) -> tuple[CodexSecondOpinion | None, AgentReport | None]:
    """Dispatch codex as an independent second opinion. Fail-soft.

    Returns ``(parsed_opinion, agent_report_row)``. Both ``None`` when:
      * the ``ARGOSY_CODEX_REVIEW_ENABLED`` env var is anything other than ``"1"``
      * running under pytest (``PYTEST_CURRENT_TEST`` is set)
      * the codex-tandem kit isn't importable (e.g. fresh checkout
        without the kit)
      * codex's subprocess raises / times out

    On a successful dispatch with unparseable output, a synthetic
    ``CodexSecondOpinion`` (YELLOW finding flagging the parse failure)
    is returned so the FM still sees a codex row.

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
        return None, None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info(
            "codex_second_opinion.skipped_under_pytest",
            decision_run_id=decision_run_id,
        )
        return None, None

    # ------------------------------------------------------------------
    # Idempotency — if a row already exists, return it without dispatch.
    # ------------------------------------------------------------------
    existing = await _load_existing_codex_opinion(
        decision_audit_token=decision_audit_token, user_id=user_id,
    )
    if existing is not None:
        log.info(
            "codex_second_opinion.idempotent_skip",
            decision_run_id=decision_run_id,
        )
        # No fresh row — the existing DB row is the source of truth.
        return existing, None

    # ------------------------------------------------------------------
    # Resolve the kit. The codex-tandem scripts dir is on
    # ``sys.path`` only after we add it here — no top-level import of
    # ``engine_codex`` so the rest of argosy doesn't pay the cost or
    # require the kit to be present.
    # ------------------------------------------------------------------
    scripts_dir = _resolve_codex_scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from engine_codex import run_codex  # type: ignore[import-not-found]
    except ImportError as exc:
        log.warning(
            "codex_second_opinion.kit_unavailable",
            scripts_dir=str(scripts_dir),
            error=str(exc),
        )
        return None, None

    prompt = _build_prompt(
        synth_draft_json=synth_draft_json,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        risk_verdict_text=risk_verdict_text,
        user_directive=user_directive,
        derived_numbers_block=derived_numbers_block,
        raw_holdings_block=raw_holdings_block,
    )

    # ------------------------------------------------------------------
    # Dispatch. ``run_codex`` is sync (subprocess); push it into the
    # default executor so we don't block the orchestrator's asyncio
    # loop. ``node_dir`` is a fresh tmpdir under the project so codex's
    # ``result.md`` write doesn't collide with concurrent runs.
    # ------------------------------------------------------------------
    from argosy.config import get_settings

    settings = get_settings()
    node_dir = (
        settings.home / "logs" / "synthesis" / "codex_zigzag"
        / f"run_{decision_run_id}_{uuid.uuid4().hex[:8]}"
    )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_codex(
                node_dir=node_dir,
                prompt=prompt,
                agent_name=f"codex_second_opinion_run_{decision_run_id}",
                role="codex_second_opinion",
                # 480s headroom: a first review observed at 233s, and the
                # post-reconcile re-review can approach/exceed 300s under
                # self-load — a 300s cap fail-closed a slow-but-valid
                # review. True hangs still time out (just later).
                timeout_s=480,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft on any dispatch error
        log.warning(
            "codex_second_opinion.dispatch_failed",
            decision_run_id=decision_run_id,
            error=str(exc),
        )
        return None, None

    log.info(
        "codex_second_opinion.dispatched",
        decision_run_id=decision_run_id,
        exit_code=getattr(result, "exit_code", None),
        tokens=getattr(result, "tokens", 0),
        wall_s=getattr(result, "wall_s", 0.0),
    )

    verdict_text = getattr(result, "verdict_text", "") or ""
    parsed = _parse_codex_verdict(verdict_text)

    # ------------------------------------------------------------------
    # Cost + token telemetry.
    #
    # The real ``CodexResult`` dataclass (engine_codex.CodexResult)
    # exposes a single flat ``tokens: int`` (total -- no in/out split)
    # and NO ``cost`` field. The kit computes cost externally via
    # ``engine_stats.estimate_cost_usd(model, tokens)`` against a
    # ``models.toml`` price table. Until this fix, we hardcoded
    # ``cost_usd=0.0`` on the AgentReport row, which is why run #31's
    # codex row shows $0 despite ~53k tokens of real GPT-5 spend.
    #
    # Defensive layering -- highest fidelity first:
    #   1. If the result object carries explicit ``cost`` /
    #      ``tokens_in`` / ``tokens_out`` attributes (e.g. a future
    #      kit version, or a test stub), honour them.
    #   2. Otherwise call ``estimate_cost_usd("codex-gpt-5-5", tokens)``
    #      using the kit's own price table -- best available estimate
    #      given that codex only emits a total token count to stderr.
    #   3. On ANY failure (kit module gone, models.toml missing, weird
    #      values), log a warning and fall through to cost=0.0 +
    #      tokens=0 rather than crashing.
    #
    # A defensive upper bound ($10) guards against a runaway estimate
    # surfacing a misleading dollar in the UI -- a single codex review
    # has never come close to $1 in practice; anything above $10 is
    # almost certainly a price-table glitch.
    # ------------------------------------------------------------------
    _COST_CAP_USD = 10.0
    total_tokens = int(getattr(result, "tokens", 0) or 0)
    tokens_in = int(getattr(result, "tokens_in", 0) or 0)
    tokens_out_attr = getattr(result, "tokens_out", None)
    if tokens_out_attr is None:
        # No explicit split available -- mirror the legacy convention of
        # parking the total under tokens_out (the kit never exposes input
        # tokens separately).
        tokens_out = total_tokens
    else:
        tokens_out = int(tokens_out_attr or 0)

    explicit_cost = getattr(result, "cost", None)
    if explicit_cost is not None:
        try:
            cost_usd = float(explicit_cost)
        except (TypeError, ValueError) as exc:
            log.warning(
                "codex_second_opinion.cost_attr_unparseable",
                decision_run_id=decision_run_id,
                raw=repr(explicit_cost),
                error=str(exc),
            )
            cost_usd = 0.0
    else:
        # Fall back to the kit's own cost estimator. The scripts_dir is
        # already on sys.path from the run_codex import above.
        try:
            from engine_stats import estimate_cost_usd  # type: ignore[import-not-found]
            cost_usd = float(
                estimate_cost_usd(model="codex-gpt-5-5", tokens=total_tokens)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "codex_second_opinion.cost_estimate_failed",
                decision_run_id=decision_run_id,
                tokens=total_tokens,
                error=str(exc),
            )
            cost_usd = 0.0

    if cost_usd < 0 or cost_usd > _COST_CAP_USD:
        log.warning(
            "codex_second_opinion.cost_out_of_range",
            decision_run_id=decision_run_id,
            raw_cost_usd=cost_usd,
            cap_usd=_COST_CAP_USD,
        )
        cost_usd = 0.0 if cost_usd < 0 else _COST_CAP_USD

    # ------------------------------------------------------------------
    # Build an AgentReport dataclass so the existing phase-recorder /
    # JSONL forensic-trail path can persist this row alongside Argosy's
    # native agent rows. ``output`` carries the parsed verdict so the
    # downstream replay UI can render it; ``response_text`` carries the
    # raw codex output for manual review.
    # ------------------------------------------------------------------
    row = AgentReport(
        agent_role="codex_second_opinion",
        user_id=user_id,
        model="gpt-5-codex",
        # Persist BOTH the raw verdict (for manual review) and the parsed
        # JSON so the FM's prompt builder can re-parse identically off
        # the DB row.
        response_text=parsed.model_dump_json(indent=2),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        prompt_hash="",
        confidence=None,
        output=parsed,
        decision_id=decision_audit_token,
        run_correlation_id=str(uuid.uuid4()),
        system_prompt="",  # codex prompt is a single user message
        user_prompt=prompt,
    )

    return parsed, row


__all__ = [
    "CodexAgreement",
    "CodexFinding",
    "CodexSecondOpinion",
    "_build_prompt",
    "_parse_codex_verdict",
    "run_codex_second_opinion",
]
