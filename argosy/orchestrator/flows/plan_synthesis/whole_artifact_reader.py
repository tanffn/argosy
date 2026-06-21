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
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY

log = get_logger(__name__)

# The arbitrated-subject taxonomy the reader classifies each finding against,
# sourced from the SAME registry the deliberation router uses so the prompt and
# the router cannot drift. Sorted for a stable prompt hash.
_SUBJECT_TAXONOMY_STR = ", ".join(sorted(SUBJECT_REGISTRY.keys()))


# ----------------------------------------------------------------------
# Hard dispatch ceiling. ``run_codex``'s own ``timeout_s`` does NOT
# reliably hard-kill a fully-stuck codex subprocess on Windows (it relies
# on the subprocess/CLI self-terminating); a hung codex left
# ``await loop.run_in_executor(...)`` blocking synthesis for 6+ HOURS in a
# live run. ``run_in_executor`` has no cancellation/timeout of its own, so
# wrap the await in ``asyncio.wait_for`` as a BACKSTOP: ``timeout_s`` (300)
# + a 60s grace margin so a slow-but-valid reader still completes, but a
# true hang raises ``asyncio.TimeoutError`` and hits the existing
# dispatch-failure path (→ (None, None), reader simply doesn't run and
# synthesis proceeds). The grace margin is identical to the codex
# second-opinion ceiling (60s) for consistency; 300 + 60 = 360.
#
# CAVEAT: ``wait_for`` cancels the AWAIT, but the underlying thread running
# ``run_codex`` keeps going (you can't kill a thread). Acceptable — the
# orphaned thread may linger but no longer blocks synthesis. We do NOT try
# to kill the thread.
#
# The codex timeout is env-configurable (ARGOSY_READER_CODEX_TIMEOUT_S, default
# 540) because a large assembled artifact (~100k chars) legitimately needs more
# than 5 minutes for the reviewer to read — a too-tight timeout reads as a
# dispatch failure (the fail-closed synthetic BLOCK) even though nothing is hung.
# The old 300s default fail-closed real-but-slow reads in EVERY ~100k-artifact
# run (drun 117 logged three 0-token synthetic BLOCKs before the real APPROVE),
# so 540 is the default. Clamped to run_codex's own 600s subprocess ceiling; the
# hard backstop adds the same 60s grace margin (540 + 60 = 600).
_DEFAULT_READER_CODEX_TIMEOUT_S = 540


def _reader_codex_timeout_s() -> int:
    """codex ``timeout_s`` for the reader, from env (clamped to [60, 600])."""
    try:
        v = int(os.environ.get("ARGOSY_READER_CODEX_TIMEOUT_S",
                               str(_DEFAULT_READER_CODEX_TIMEOUT_S)))
    except (TypeError, ValueError):
        v = _DEFAULT_READER_CODEX_TIMEOUT_S
    return max(60, min(v, 600))


def _hard_ceiling_s() -> int:
    """asyncio backstop = codex timeout + 60s grace."""
    return _reader_codex_timeout_s() + 60


# ----------------------------------------------------------------------
# Output schema
# ----------------------------------------------------------------------


class CoherenceFinding(BaseModel):
    """One whole-document coherence finding."""

    kind: Literal[
        "contradiction", "cross_surface", "fragile_claim", "stale", "regression",
        "other", "new_dispute", "ruling_divergence", "ruling_defect",
    ]
    severity: Literal["BLOCKER", "AMBER", "YELLOW"]
    detail: str
    surfaces_cited: list[str] = Field(
        default_factory=list,
        description="Verbatim excerpts from the document that conflict / "
        "anchor this finding.",
    )
    subject_type: str = Field(
        default="",
        description="The arbitrated subject this finding pertains to (e.g. a "
        "settled-ruling subject_type).",
    )
    field_path: str = Field(
        default="",
        description="Dotted path to the structured field the claim maps to.",
    )
    normalized_claim: str = Field(
        default="",
        description="The normalized form of the claim made by the cited surface.",
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
      acknowledged, a number that moved without explanation). \
      NOT a regression: a deliberate change away from the prior plan that \
      BETTER serves the client's standing goal (maximize wealth + earliest SAFE \
      retirement; over-conservatism that costs retirement years is the explicit \
      anti-goal) AND whose rationale is stated IN this document — e.g. acting on \
      a gate the document shows is now satisfied, or no longer headlining an \
      over-conservative scenario it still discloses as a what-if. The prior \
      plan is being REPLACED, not ratified; only flag a regression when a \
      load-bearing caveat/risk is dropped WITHOUT a stated, goal-consistent \
      reason. When the document gives such a reason, treat it as an improvement.

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

For EACH finding, set ``subject_type`` to the ONE taxonomy key below that best \
names the arbitrated subject the finding is about, so a downstream deliberation \
step can route it without re-classifying your prose. Use "" ONLY if no key \
fits. Taxonomy: {subject_taxonomy}.

Return ONLY the JSON object below — no prose before or after, no markdown \
fences:

{{
  "overall_assessment": "APPROVE" | "APPROVE_WITH_CONDITIONS" | "BLOCK",
  "findings": [
    {{
      "kind": "contradiction" | "cross_surface" | "fragile_claim" | "stale" | "regression" | "other",
      "severity": "BLOCKER" | "AMBER" | "YELLOW",
      "detail": "<explanation in your own words>",
      "surfaces_cited": ["<verbatim excerpt 1>", "<verbatim excerpt 2>"],
      "subject_type": "<one taxonomy key above, or \\"\\" if none fits>"
    }}
  ]
}}

=== ASSEMBLED PLAN ARTIFACT (the exact bytes the user reads) ===
{assembled_artifact}
=== REVIEWER-ONLY CANONICAL RECONCILIATION ANCHOR (NOT client-facing) ===
This section is NOT part of the client-facing plan. Use it ONLY as the canonical
registry reference (one owner per figure) for checking whether the client-facing
plan prose above AGREES with it. Do NOT critique this section itself, do NOT treat
it as a stale/regression client surface, and do NOT average its figures with the
prose. When a prose figure disagrees with this anchor, emit a finding citing the
prose excerpt (the anchor is the source of truth, not the disagreement).
{canonical_anchor}
=== FRESH EXTERNAL CONTEXT (today + any market/event context) ===
{external_context}
=== PRIOR PLAN (diff against this — flag regressions) ===
{prior_plan}
Produce the JSON now. No prose, no markdown fences — just the JSON object.
"""


def build_settled_rulings_block(settled_rulings: list[dict]) -> str:
    """Render the settled-ruling contract injected into the reader prompt."""
    if not settled_rulings:
        return ""
    lines = [
        "SETTLED RULINGS — these questions are arbitrated. Do NOT re-litigate the "
        "preferred answer. You MUST still verify every surface against the ruling. "
        "Emit `ruling_divergence` if a surface disagrees with a ruling; emit "
        "`ruling_defect` if a ruling itself is stale, overbroad, unsupported, wrongly "
        "scoped, or violates the authority order; emit `new_dispute` for anything not "
        "covered below.",
    ]
    for r in settled_rulings:
        lines.append(f"- [{r.get('subject_type','')}] {r.get('ruling','')}")
    return "\n".join(lines)


def _build_prompt(
    *,
    assembled_artifact: str,
    external_context: str,
    prior_plan_text: str,
    settled_rulings: list[dict] | None = None,
    canonical_anchor: str | None = None,
) -> str:
    """Render the prompt with the evidence blocks inlined.

    Sentinel strings stand in for an empty external-context / prior-plan /
    canonical-anchor block so the model never gets a bare placeholder it must
    guess at. The canonical anchor (when present) is a reviewer-only oracle —
    the prompt section header tells the model not to critique it.
    """
    artifact_block = (
        assembled_artifact.strip()
        if assembled_artifact and assembled_artifact.strip()
        else "(assembled artifact unavailable on this run — this is itself a "
        "coherence failure: BLOCK with a BLOCKER finding noting the empty plan)"
    )
    anchor_block = (
        canonical_anchor.strip()
        if canonical_anchor and canonical_anchor.strip()
        else "(no canonical registry anchor on this run — judge coherence from "
        "the artifact's own stated figures)"
    )
    context_block = (
        external_context.strip()
        if external_context and external_context.strip()
        else "(no fresh external-context packet on this run — assess staleness "
        "against the dates stated inside the document itself)"
    )
    rulings_block = build_settled_rulings_block(settled_rulings or [])
    if rulings_block:
        context_block = f"{context_block}\n\n{rulings_block}"
    prior_block = (
        prior_plan_text.strip()
        if prior_plan_text and prior_plan_text.strip()
        else "(no prior plan to diff against — this is the first plan, so there "
        "are no regressions to flag; focus on internal coherence + staleness)"
    )
    return _PROMPT_TEMPLATE.format(
        assembled_artifact=artifact_block,
        canonical_anchor=anchor_block,
        external_context=context_block,
        prior_plan=prior_block,
        subject_taxonomy=_SUBJECT_TAXONOMY_STR,
    )


# ----------------------------------------------------------------------
# Parsing — strict → lenient → fail-closed synthetic BLOCK.
# ----------------------------------------------------------------------


_ALLOWED_ASSESSMENTS = {"APPROVE", "APPROVE_WITH_CONDITIONS", "BLOCK"}
_ALLOWED_KINDS = {
    "contradiction", "cross_surface", "fragile_claim", "stale", "regression", "other",
}
_ALLOWED_SEVERITIES = {"BLOCKER", "AMBER", "YELLOW"}


def _coerce_verdict_dict(obj: dict) -> dict:
    """Defensively coerce a recovered verdict dict into a schema-valid shape.

    A structurally-valid JSON verdict must NOT be discarded wholesale just
    because one finding carries an out-of-enum ``kind`` or ``severity``. This
    salvages such a verdict by coercing unknown enum values to safe defaults,
    preserving ``overall_assessment`` and ALL findings. Only a genuinely
    unrecoverable (non-dict / no-JSON) input falls through to the synthetic
    fail-closed BLOCK in ``_parse_verdict``.

    Coercion rules:
      - ``overall_assessment`` not in the allowed set → "BLOCK" (fail-closed on
        the load-bearing field — a missing/invalid assessment must not pass);
      - per finding: unknown ``kind`` → "other"; unknown ``severity`` → "AMBER"
        (defensive — surface it rather than drop it); ``detail`` coerced to str;
        ``surfaces_cited`` coerced to a list.
    """
    assessment = obj.get("overall_assessment")
    if assessment not in _ALLOWED_ASSESSMENTS:
        assessment = "BLOCK"

    coerced_findings: list[dict] = []
    raw_findings = obj.get("findings")
    if isinstance(raw_findings, list):
        for f in raw_findings:
            if not isinstance(f, dict):
                continue
            kind = f.get("kind")
            if kind not in _ALLOWED_KINDS:
                kind = "other"
            severity = f.get("severity")
            if severity not in _ALLOWED_SEVERITIES:
                severity = "AMBER"
            detail = f.get("detail")
            detail = detail if isinstance(detail, str) else str(detail) if detail is not None else ""
            surfaces = f.get("surfaces_cited")
            if not isinstance(surfaces, list):
                surfaces = []
            # Preserve the structured routing fields the deliberation step reads
            # (subject_type especially) — the lenient salvage path must NOT drop
            # them, or a recovered verdict loses its arbitration routing.
            coerced_findings.append({
                "kind": kind,
                "severity": severity,
                "detail": detail,
                "surfaces_cited": surfaces,
                "subject_type": f.get("subject_type") if isinstance(f.get("subject_type"), str) else "",
                "field_path": f.get("field_path") if isinstance(f.get("field_path"), str) else "",
                "normalized_claim": f.get("normalized_claim") if isinstance(f.get("normalized_claim"), str) else "",
            })

    return {"overall_assessment": assessment, "findings": coerced_findings}


def _parse_verdict(text: str) -> WholeArtifactVerdict:
    """Parse the reader's raw text into a ``WholeArtifactVerdict``.

    Strategy:
      1. Strict ``model_validate_json`` (after stripping ```json fences).
      2. Lenient: locate the first ``{`` and try ``JSONDecoder.raw_decode`` to
         recover a dict. Any recovered dict is SALVAGED via
         ``_coerce_verdict_dict`` (unknown enum values coerced, all findings
         preserved) — a single out-of-enum ``kind``/``severity`` must NOT
         discard a structurally-valid verdict.
      3. **Fail closed** — only if NO JSON object can be recovered at all: a
         synthetic BLOCK verdict with one BLOCKER ``other`` finding explaining
         the timeout / unparseable output.

    The fail-closed default is the S21 lesson: a reviewer that did not
    actually run (timeout / dispatch failure → empty text) or whose output
    can't be recovered must NOT silently wave the plan through. A non-verdict
    is a BLOCK, not a soft APPROVE. But a recoverable verdict with one odd
    enum value is real signal and must be preserved, not thrown away.
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

        # Recover a dict — first via strict json.loads, then via lenient
        # raw_decode from the first ``{``. Any recovered dict is salvaged
        # (out-of-enum values coerced) rather than discarded.
        recovered: dict | None = None
        try:
            loaded = json.loads(cleaned)
            if isinstance(loaded, dict):
                recovered = loaded
        except Exception:
            pass
        if recovered is None:
            first_brace = cleaned.find("{")
            if first_brace >= 0:
                try:
                    decoder = json.JSONDecoder(strict=False)
                    obj, _ = decoder.raw_decode(cleaned[first_brace:])
                    if isinstance(obj, dict):
                        recovered = obj
                except Exception:
                    pass
        if recovered is not None:
            try:
                return WholeArtifactVerdict.model_validate(
                    _coerce_verdict_dict(recovered)
                )
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
    settled_rulings: list[dict] | None = None,
    canonical_anchor: str | None = None,
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
    # Deterministic leakage precheck — runs BEFORE any codex dispatch (cheaper)
    # and catches a class the LLM reader misses: an artifact that still carries
    # unrendered placeholders / leaked emission scaffolding (``[derivation pending]``,
    # ``EMIT AS``, ``{{fact:``). The client must never see those, so this is a
    # fail-closed BLOCK with no LLM judgement. The downstream reconcile loop's
    # owner path cannot prose-fix a render artifact, so it falls through to a
    # re-render / re-synth — exactly the right repair for a leak.
    # ------------------------------------------------------------------
    from argosy.quality.leakage_gate import scan_leakage

    _leaks = scan_leakage(assembled_artifact)
    if _leaks:
        log.warning(
            "whole_artifact_reader.leakage_blocked",
            decision_run_id=decision_run_id, leaks=_leaks,
        )
        _leak_verdict = WholeArtifactVerdict(
            overall_assessment="BLOCK",
            findings=[CoherenceFinding(
                kind="other",
                severity="BLOCKER",
                detail=(
                    "Artifact-integrity BLOCK: the assembled plan still contains "
                    "unrendered placeholders / leaked emission scaffolding — every "
                    "figure must render before the plan can ship. Leak tokens: "
                    + "; ".join(_leaks)
                ),
                surfaces_cited=_leaks,
                subject_type="artifact_integrity",
            )],
        )
        _leak_row = AgentReport(
            agent_role="whole_artifact_reader",
            user_id=user_id,
            model="deterministic-leakage-gate",
            response_text=_leak_verdict.model_dump_json(indent=2),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="",
            confidence=None,
            output=_leak_verdict,
            decision_id=decision_audit_token,
            run_correlation_id=str(uuid.uuid4()),
            system_prompt="",
            user_prompt="(deterministic leakage precheck — no LLM dispatch)",
        )
        return _leak_verdict, _leak_row

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
        settled_rulings=settled_rulings,
        canonical_anchor=canonical_anchor,
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
        # Wrap the executor await in ``asyncio.wait_for`` so a hung codex
        # subprocess (which run_codex's own timeout_s does not reliably
        # kill — see _hard_ceiling_s) can't block synthesis indefinitely.
        # A timeout raises asyncio.TimeoutError, caught by the except below
        # and handled exactly like any other dispatch failure: (None, None),
        # so the reader simply doesn't run and synthesis proceeds.
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: run_codex(
                    node_dir=node_dir,
                    prompt=prompt,
                    agent_name=f"whole_artifact_reader_run_{decision_run_id}",
                    role="whole_artifact_reader",
                    timeout_s=_reader_codex_timeout_s(),
                ),
            ),
            timeout=_hard_ceiling_s(),
        )
    except asyncio.TimeoutError:
        # Hard ceiling tripped — a stuck codex subprocess. The orphaned
        # executor thread may linger but no longer blocks synthesis. This is
        # a DISPATCH failure (the reader never produced output), so it takes
        # the fail-soft (None, None) path — NOT the parse fail-closed-to-BLOCK
        # path (that one fires only when codex DID return empty/garbage text).
        log.warning(
            "whole_artifact_reader.dispatch_failed",
            decision_run_id=decision_run_id,
            error=f"hard ceiling exceeded ({_hard_ceiling_s()}s) — codex hung",
            timed_out=True,
        )
        return None, None
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
        # Dispatched via run_codex with no ``--model``, so the real model is the
        # codex CLI default (gpt-5.5). Label it accurately, not "gpt-5-codex".
        model="gpt-5.5",
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
    "build_settled_rulings_block",
    "_build_prompt",
    "_coerce_verdict_dict",
    "_parse_verdict",
    "run_whole_artifact_review",
]
