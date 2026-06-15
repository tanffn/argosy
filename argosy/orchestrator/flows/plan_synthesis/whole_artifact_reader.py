"""Whole-artifact adversarial reader — the holistic final-stage review.

Argosy's agent fabric produces each section of a plan in isolation; no stage
reads the OWN finished output AS A WHOLE. A single fresh LLM prompt, handed the
assembled document and told to find holes, keeps catching REAL coherence holes
the fabric misses — precisely because it reads the finished bytes the client
will read, blind to how they were produced.

This module institutionalizes that as a final adversarial reader. It is fed:
  - the FULL assembled artifact (``AssembledArtifact.full_text`` — the exact
    bytes the user reads),
  - a fresh-external-context packet (today's date + any market/event context
    the caller passes),
  - the prior plan's text to diff against (regression detection).

Its job is COHERENCE OF THE WHOLE — contradictions, claims undercut by other
sections, staleness, and regressions — NOT re-deriving numbers. The codex
second-opinion gate (``codex_second_opinion.py``) owns the math; this reader
owns the prose-level integrity of the assembled document.

Design contract — mirrors ``codex_second_opinion.py``:
  - dispatched via ``engine_codex.run_codex(...)`` from the ``tools/codex-tandem``
    kit (``_resolve_codex_scripts_dir`` + ``sys.path.insert``), in an executor,
    ``timeout_s=300``;
  - strict → lenient → **fail-closed-to-BLOCK** parse (a timeout / unparseable
    output is a BLOCK, not a soft pass — the S21 lesson);
  - kill switches (``ARGOSY_CODEX_REVIEW_ENABLED != "1"`` and
    ``PYTEST_CURRENT_TEST``);
  - fail-soft dispatch (``(None, None)`` on kit-missing / dispatch error);
  - one ``agent_reports`` row with ``agent_role="whole_artifact_reader"``.

Simplifications vs ``codex_second_opinion.py`` (intentional, Task 6):
  - **No idempotency lookup.** The codex helper re-reads an existing
    ``agent_reports`` row to avoid re-dispatching on resume. This reader omits
    that: a fresh dispatch each run is safe and the lookup added DB-coupling
    risk for no load-bearing benefit at this task. Note for a future task if
    resume-cost becomes a concern.
  - **Simpler cost telemetry.** Tokens read defensively via ``getattr``; cost
    is best-effort via the kit's ``estimate_cost_usd`` and falls back to 0.0 on
    any failure (no ``_COST_CAP`` / explicit-cost-attr layering). The reader's
    verdict is the load-bearing output, not its dollar figure.
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
# Output schema
# ----------------------------------------------------------------------


class CoherenceFinding(BaseModel):
    """One whole-document coherence finding."""

    kind: Literal["contradiction", "cross_surface", "fragile_claim", "stale", "other"]
    severity: Literal["BLOCKER", "AMBER", "YELLOW"]
    detail: str
    surfaces_cited: list[str] = Field(
        default_factory=list,
        description="Verbatim excerpts from the document that conflict / "
        "anchor this finding.",
    )


class WholeArtifactVerdict(BaseModel):
    """The full structured whole-artifact coherence verdict."""

    overall_assessment: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "BLOCK"]
    findings: list[CoherenceFinding] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------


_PROMPT_TEMPLATE = """\
You are a HOSTILE, SKEPTICAL reader of a complete financial plan document — \
the exact bytes a client will read. You are BLIND to how this document was \
produced (which agent wrote which section, what the pipeline computed). Read \
the WHOLE document end-to-end and hunt for failures of COHERENCE OF THE WHOLE:

  (1) CONTRADICTIONS — the same concept stated with DIFFERENT values, or with \
      OPPOSITE conclusions, in two places. Quote BOTH conflicting passages \
      VERBATIM into ``surfaces_cited``.
  (2) FRAGILE CLAIMS — any headline claim that the document's OWN other \
      sections undercut. Example: a "financial independence reached" / capital-\
      sufficiency claim that a stated concentration, tail-risk, or thin margin \
      elsewhere in the SAME document makes fragile. Quote the claim AND the \
      undercutting section.
  (3) STALE content — anything inconsistent with today's date or the fresh \
      external-context packet (e.g. a "as of" date months old, an event the \
      context says has already happened, a price the context contradicts).
  (4) REGRESSIONS — content that got WORSE relative to the prior plan (a \
      section dropped, a hedge removed, a previously-stated risk no longer \
      acknowledged, a number that moved without explanation).

Quote conflicting passages VERBATIM in ``surfaces_cited`` so a human can \
locate them. DO NOT re-derive numbers from scratch — a SEPARATE gate owns the \
math. Your job is the COHERENCE OF THE WHOLE: does this document, read as one \
artifact, hang together and tell a consistent, current, non-regressed story?

Severity: BLOCKER = a contradiction / fragile-headline that would mislead the \
client on a load-bearing decision; AMBER = a real coherence gap that should be \
fixed before sending; YELLOW = a minor inconsistency / polish issue.

overall_assessment: BLOCK if ANY BLOCKER finding; APPROVE_WITH_CONDITIONS if \
only AMBER/YELLOW findings; APPROVE only if the document is fully coherent, \
current, and non-regressed.

Return ONLY the JSON object below — no prose before or after, no markdown \
fences:

{{
  "overall_assessment": "APPROVE" | "APPROVE_WITH_CONDITIONS" | "BLOCK",
  "findings": [
    {{
      "kind": "contradiction" | "cross_surface" | "fragile_claim" | "stale" | "other",
      "severity": "BLOCKER" | "AMBER" | "YELLOW",
      "detail": "<explanation in your own words>",
      "surfaces_cited": ["<verbatim excerpt 1>", "<verbatim excerpt 2>"]
    }}
  ]
}}

=== ASSEMBLED PLAN ARTIFACT (the exact bytes the user reads) ===
{assembled_artifact}
=== FRESH EXTERNAL CONTEXT (today + any market/event context) ===
{external_context}
=== PRIOR PLAN (diff against this — flag regressions) ===
{prior_plan}
Produce the JSON now. No prose, no markdown fences — just the JSON object.
"""


def _build_prompt(
    *,
    assembled_artifact: str,
    external_context: str,
    prior_plan_text: str,
) -> str:
    """Render the prompt with the three evidence blocks inlined.

    Sentinel strings stand in for an empty external-context / prior-plan
    block so the model never gets a bare placeholder it must guess at.
    """
    artifact_block = (
        assembled_artifact.strip()
        if assembled_artifact and assembled_artifact.strip()
        else "(assembled artifact unavailable on this run — this is itself a "
        "coherence failure: BLOCK with a BLOCKER finding noting the empty plan)"
    )
    context_block = (
        external_context.strip()
        if external_context and external_context.strip()
        else "(no fresh external-context packet on this run — assess staleness "
        "against the dates stated inside the document itself)"
    )
    prior_block = (
        prior_plan_text.strip()
        if prior_plan_text and prior_plan_text.strip()
        else "(no prior plan to diff against — this is the first plan, so there "
        "are no regressions to flag; focus on internal coherence + staleness)"
    )
    return _PROMPT_TEMPLATE.format(
        assembled_artifact=artifact_block,
        external_context=context_block,
        prior_plan=prior_block,
    )


# ----------------------------------------------------------------------
# Parsing — strict → lenient → fail-closed synthetic BLOCK.
# ----------------------------------------------------------------------


def _parse_verdict(text: str) -> WholeArtifactVerdict:
    """Parse the reader's raw text into a ``WholeArtifactVerdict``.

    Strategy:
      1. Strict ``model_validate_json`` (after stripping ```json fences).
      2. Lenient: locate the first ``{`` and try ``JSONDecoder.raw_decode``.
      3. **Fail closed** — a synthetic BLOCK verdict with one BLOCKER
         ``other`` finding explaining the timeout / unparseable output.

    The fail-closed default is the S21 lesson: a reviewer that did not
    actually run (timeout / dispatch failure → empty text) or whose output
    can't be recovered must NOT silently wave the plan through. A non-verdict
    is a BLOCK, not a soft APPROVE.
    """
    if text:
        cleaned = text.strip()
        # Strip a fenced ```json block if the model added one despite the
        # prompt's instructions.
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            return WholeArtifactVerdict.model_validate_json(cleaned)
        except Exception:
            pass

        # Lenient: find the first { and raw_decode from there.
        first_brace = cleaned.find("{")
        if first_brace >= 0:
            try:
                decoder = json.JSONDecoder(strict=False)
                obj, _ = decoder.raw_decode(cleaned[first_brace:])
                return WholeArtifactVerdict.model_validate(obj)
            except Exception:
                pass

    # Fail closed — synthetic BLOCK so the holistic gate cannot be silently
    # waved through by a timed-out / unparseable reader.
    excerpt = (text or "")[:400]
    timed_out = not (text or "").strip()
    log.warning(
        "whole_artifact_reader.unparseable",
        raw_excerpt=excerpt,
        timed_out=timed_out,
    )
    reason = (
        "The whole-artifact reader returned NO output (timeout / dispatch "
        "failure) — the holistic coherence review did not run."
        if timed_out else
        "The whole-artifact reader returned non-JSON output and the lenient "
        f"parse fallback couldn't recover a verdict. Raw excerpt (first 400 "
        f"chars): {excerpt}"
    )
    return WholeArtifactVerdict(
        overall_assessment="BLOCK",
        findings=[CoherenceFinding(
            kind="other",
            severity="BLOCKER",
            detail=(
                f"{reason} The holistic coherence gate could not read the "
                "assembled artifact, so the plan is fail-closed (BLOCK) rather "
                "than soft-passed. Re-run the reader (often a transient timeout "
                "under load) or escalate for manual whole-document review."
            ),
            surfaces_cited=[],
        )],
    )


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def _resolve_codex_scripts_dir() -> Path:
    """Locate ``tools/codex-tandem/scripts`` relative to this file.

    Layout: ``<repo>/argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py``;
    four parents reach ``<repo>``. Computed at call time so test monkey-patches
    or alternative checkouts work.
    """
    return Path(__file__).resolve().parents[4] / "tools" / "codex-tandem" / "scripts"


async def run_whole_artifact_review(
    *,
    assembled_artifact: str,
    external_context: str,
    prior_plan_text: str,
    decision_run_id: int,
    user_id: str,
) -> tuple[WholeArtifactVerdict | None, AgentReport | None]:
    """Dispatch the whole-artifact adversarial reader. Fail-soft.

    Returns ``(parsed_verdict, agent_report_row)``. Both ``None`` when:
      * ``ARGOSY_CODEX_REVIEW_ENABLED`` is anything other than ``"1"``,
      * running under pytest (``PYTEST_CURRENT_TEST`` set),
      * the codex-tandem kit isn't importable (fresh checkout), or
      * the dispatch raises / times out.

    On a successful dispatch with unparseable output, a synthetic BLOCK
    ``WholeArtifactVerdict`` (one BLOCKER finding) is returned so the caller
    still sees a row and the gate fails closed.
    """
    decision_audit_token = f"plan-synth-{decision_run_id}"

    # ------------------------------------------------------------------
    # Kill switches first — cheaper than any subprocess work.
    # ------------------------------------------------------------------
    if os.environ.get("ARGOSY_CODEX_REVIEW_ENABLED", "1") != "1":
        log.info(
            "whole_artifact_reader.skipped_by_env_var",
            decision_run_id=decision_run_id,
        )
        return None, None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info(
            "whole_artifact_reader.skipped_under_pytest",
            decision_run_id=decision_run_id,
        )
        return None, None

    # ------------------------------------------------------------------
    # Resolve the kit. The codex-tandem scripts dir is only added to
    # ``sys.path`` here — no top-level import of ``engine_codex`` so the
    # rest of argosy doesn't require the kit to be present.
    # ------------------------------------------------------------------
    scripts_dir = _resolve_codex_scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from engine_codex import run_codex  # type: ignore[import-not-found]
    except ImportError as exc:
        log.warning(
            "whole_artifact_reader.kit_unavailable",
            scripts_dir=str(scripts_dir),
            error=str(exc),
        )
        return None, None

    prompt = _build_prompt(
        assembled_artifact=assembled_artifact,
        external_context=external_context,
        prior_plan_text=prior_plan_text,
    )

    # ------------------------------------------------------------------
    # Dispatch. ``run_codex`` is sync (subprocess); push it into the
    # default executor so we don't block the orchestrator's asyncio loop.
    # ``node_dir`` is a fresh tmpdir so codex's ``result.md`` write doesn't
    # collide with concurrent runs.
    # ------------------------------------------------------------------
    from argosy.config import get_settings

    settings = get_settings()
    node_dir = (
        settings.home / "logs" / "synthesis" / "whole_artifact_reader"
        / f"run_{decision_run_id}_{uuid.uuid4().hex[:8]}"
    )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_codex(
                node_dir=node_dir,
                prompt=prompt,
                agent_name=f"whole_artifact_reader_run_{decision_run_id}",
                role="whole_artifact_reader",
                timeout_s=300,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft on any dispatch error
        log.warning(
            "whole_artifact_reader.dispatch_failed",
            decision_run_id=decision_run_id,
            error=str(exc),
        )
        return None, None

    log.info(
        "whole_artifact_reader.dispatched",
        decision_run_id=decision_run_id,
        exit_code=getattr(result, "exit_code", None),
        tokens=getattr(result, "tokens", 0),
        wall_s=getattr(result, "wall_s", 0.0),
    )

    verdict_text = getattr(result, "verdict_text", "") or ""
    parsed = _parse_verdict(verdict_text)

    # ------------------------------------------------------------------
    # Cost + token telemetry (simplified — see module docstring). The kit's
    # ``CodexResult`` exposes a flat ``tokens`` total and no cost; park the
    # total under tokens_out, best-effort cost via the kit's estimator,
    # fall through to 0.0 on any failure.
    # ------------------------------------------------------------------
    total_tokens = int(getattr(result, "tokens", 0) or 0)
    try:
        from engine_stats import estimate_cost_usd  # type: ignore[import-not-found]
        cost_usd = float(estimate_cost_usd(model="codex-gpt-5-5", tokens=total_tokens))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "whole_artifact_reader.cost_estimate_failed",
            decision_run_id=decision_run_id,
            tokens=total_tokens,
            error=str(exc),
        )
        cost_usd = 0.0

    # ------------------------------------------------------------------
    # Build an AgentReport row so the existing phase-recorder / forensic
    # trail path can persist this alongside Argosy's native agent rows.
    # ``output`` carries the parsed verdict for the replay UI; ``response_text``
    # carries the parsed JSON for re-parse off the DB row.
    # ------------------------------------------------------------------
    row = AgentReport(
        agent_role="whole_artifact_reader",
        user_id=user_id,
        model="gpt-5-codex",
        response_text=parsed.model_dump_json(indent=2),
        tokens_in=0,
        tokens_out=total_tokens,
        cost_usd=cost_usd,
        prompt_hash="",
        confidence=None,
        output=parsed,
        decision_id=decision_audit_token,
        run_correlation_id=str(uuid.uuid4()),
        system_prompt="",  # single user message
        user_prompt=prompt,
    )

    return parsed, row


__all__ = [
    "CoherenceFinding",
    "WholeArtifactVerdict",
    "_build_prompt",
    "_parse_verdict",
    "run_whole_artifact_review",
]
