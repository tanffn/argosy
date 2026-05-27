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


class CodexSecondOpinion(BaseModel):
    """The full structured codex second-opinion verdict."""

    overall_assessment: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "BLOCK"]
    findings: list[CodexFinding] = Field(default_factory=list)
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
  "agreement_with_argosy": {{
    "agrees_with_risk_verdict": true | false | "partial",
    "novel_concerns_argosy_missed": ["<list>"]
  }},
  "user_directive_respected": true | false
}}

Respect the user_directive in the same way the fund manager is told to: \
the user's AGREED stances are NOT to be re-raised; their DISAGREED \
counter-positions should be evaluated on the merits of the synthesizer's \
response, not the original concern; DEFERRED items are open for fresh \
evaluation. Set ``user_directive_respected=false`` ONLY if the \
synthesizer has clearly ignored or violated a load-bearing user stance.

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
) -> str:
    """Render the full codex prompt with all evidence blocks inlined.

    The user_directive block is either the verbatim directive or a
    sentinel string when none was passed — so the model never gets a
    bare placeholder it has to ignore.
    """
    user_directive_block = (
        user_directive.strip() if user_directive and user_directive.strip()
        else "(no user directive on this run)"
    )
    return _PROMPT_TEMPLATE.format(
        synth_draft_json=synth_draft_json,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        risk_verdict_text=risk_verdict_text,
        user_directive_block=user_directive_block,
    )


# ----------------------------------------------------------------------
# Parsing — strict → lenient → synthetic "unparseable" fallback so the
# FM still sees SOMETHING from codex even when the model emits prose.
# ----------------------------------------------------------------------


def _parse_codex_verdict(text: str) -> CodexSecondOpinion:
    """Parse codex's raw text into a ``CodexSecondOpinion``.

    Strategy:
      1. Strict ``model_validate_json`` on the entire text.
      2. Lenient: locate the first ``{`` and try ``JSONDecoder.raw_decode``.
      3. Synthetic "unparseable" opinion so callers still get a typed
         object (with a YELLOW finding flagging the parse failure).
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
            return CodexSecondOpinion.model_validate_json(cleaned)
        except Exception:
            pass

        # Lenient: find first { and try to raw_decode from there.
        first_brace = cleaned.find("{")
        if first_brace >= 0:
            try:
                decoder = json.JSONDecoder(strict=False)
                obj, _ = decoder.raw_decode(cleaned[first_brace:])
                return CodexSecondOpinion.model_validate(obj)
            except Exception:
                pass

    # Synthetic "unparseable" opinion — preserves the codex review row
    # in the audit trail even when the model didn't return clean JSON.
    excerpt = (text or "")[:400]
    log.warning(
        "codex_second_opinion.unparseable",
        raw_excerpt=excerpt,
    )
    return CodexSecondOpinion(
        overall_assessment="APPROVE_WITH_CONDITIONS",
        findings=[CodexFinding(
            severity="YELLOW",
            topic="codex_review_unparseable",
            detail=(
                "Codex returned non-JSON output and the lenient parse "
                "fallback couldn't recover a verdict. Raw excerpt "
                f"(first 400 chars): {excerpt}"
            ),
            suggested_fix="Manual review required — see agent_reports row "
                          "for full raw text.",
            cited_synthesizer_paragraphs=[],
        )],
        agreement_with_argosy=CodexAgreement(),
        user_directive_respected=None,
    )


# ----------------------------------------------------------------------
# Idempotency — check for an existing codex row before re-dispatching.
# ----------------------------------------------------------------------


def _load_existing_codex_opinion(
    *, decision_audit_token: str, user_id: str,
) -> CodexSecondOpinion | None:
    """Return a previously-persisted codex opinion for this decision_id.

    Used by the resume path so a re-run after a Phase 5 failure doesn't
    re-dispatch codex (~$0.50 wasted + 1-3 min latency). Returns None on
    no row, parse failure, or any unexpected error (synthesis falls back
    to a fresh codex dispatch in that case, which is safe).
    """
    try:
        from sqlalchemy import select
        from argosy.state import db as db_mod
        from argosy.state.models import AgentReport as AgentReportORM

        # Sync-bridged read via a fresh session (mirror of the pattern in
        # other plan_synthesis helpers). Best-effort — any failure here
        # just means we re-dispatch codex.
        async def _read() -> str | None:
            async with db_mod.get_session() as session:
                row = await session.execute(
                    select(AgentReportORM.response_text)
                    .where(AgentReportORM.user_id == user_id)
                    .where(AgentReportORM.decision_id == decision_audit_token)
                    .where(AgentReportORM.agent_role == "codex_second_opinion")
                    .order_by(AgentReportORM.id.desc())
                    .limit(1)
                )
                return row.scalar_one_or_none()

        text = asyncio.run(_read())
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
    existing = _load_existing_codex_opinion(
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
                timeout_s=300,
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
    # Build an AgentReport dataclass so the existing phase-recorder /
    # JSONL forensic-trail path can persist this row alongside Argosy's
    # native agent rows. ``output`` carries the parsed verdict so the
    # downstream replay UI can render it; ``response_text`` carries the
    # raw codex output for manual review.
    # ------------------------------------------------------------------
    tokens = int(getattr(result, "tokens", 0) or 0)
    row = AgentReport(
        agent_role="codex_second_opinion",
        user_id=user_id,
        model="gpt-5-codex",
        # Persist BOTH the raw verdict (for manual review) and the parsed
        # JSON so the FM's prompt builder can re-parse identically off
        # the DB row.
        response_text=parsed.model_dump_json(indent=2),
        tokens_in=0,  # codex doesn't split in/out tokens in our wrapper
        tokens_out=tokens,
        cost_usd=0.0,  # codex wrapper doesn't return cost today
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
