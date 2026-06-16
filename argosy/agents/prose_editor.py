"""Cheap single-snippet prose corrector for the surgical-fix loop.

A deterministic re-render fixes number facts; it cannot fix authored free-text
CONTRADICTIONS (the FI-sufficiency claim, a liquidity-runway divergence, a
stale-date phrasing). This editor is handed ONLY the offending snippet + the
reader's description of the defect + the authoritative/canonical context, and
returns the MINIMALLY corrected snippet — the smallest edit that removes the
contradiction. It never rewrites the whole plan (that is full re-synth); it
edits one cited segment, which is what makes the fix converge instead of
reshuffle.

``correct_prose_site`` takes an injectable ``editor`` (tests pass a stub). The
default dispatch is a real cheap BaseAgent call (Sonnet, structured output).
Fail-safe: any editor error returns the original snippet unchanged (the
re-verify pass + whole-artifact reader remain the backstop).
"""
from __future__ import annotations

import logging
from typing import Callable

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent

log = logging.getLogger(__name__)

_PROMPT_SYSTEM = (
    "You are a precise copy-editor on a financial-advisory team. You are given "
    "ONE snippet of an existing plan that an adversarial reviewer flagged as "
    "wrong, stale, or self-contradictory, plus the reviewer's reason and the "
    "AUTHORITATIVE canonical facts. Your job: return the SAME snippet with the "
    "SMALLEST possible edit that makes it factually correct and removes the "
    "contradiction.\n\n"
    "Rules:\n"
    "- Make the SMALLEST possible change — reword or add a short qualifying "
    "clause; keep the original wording, structure, and length as close as "
    "possible. Do not rewrite the whole sentence.\n"
    "- Do NOT delete or hide a load-bearing claim to dodge the problem. If a "
    "headline claim (e.g. 'capital sufficiency reached') is fragile, QUALIFY it "
    "honestly in WORDS (e.g. 'reached only on a thin margin that a routine FX "
    "move could erase'), do not silently drop it.\n"
    "- CRITICAL: do NOT introduce any NEW number, figure, ticker, currency "
    "amount, age, or percentage that is not ALREADY in the offending snippet. "
    "Reconcile differences QUALITATIVELY (e.g. 'on a cash-only basis' vs 'on the "
    "full investable basis'), never by inserting new figures — injecting numbers "
    "creates fresh cross-surface contradictions.\n"
    "- Write ONLY client-facing prose. Never mention 'the prior plan', a previous "
    "version, revision history, or internal tokens like '[domain_knowledge/...]' "
    "or 'agent_report:'. If the correct content requires data not present in the "
    "snippet, make the MINIMAL honest qualifier and stop — do not fabricate it.\n"
    "Put the corrected snippet — and nothing else (no commentary, no preamble) — "
    "in the `corrected_text` field of your structured response."
)


class CorrectedSnippet(BaseModel):
    """The minimally-corrected snippet."""

    corrected_text: str = Field(
        description="The corrected snippet — same wording, smallest edit that "
        "fixes the flagged defect. No commentary.",
    )


class ProseEditorAgent(BaseAgent[CorrectedSnippet]):
    """Cheap, single-snippet structured corrector (Sonnet by default — slim
    scope, no extended thinking). No citations required."""

    agent_role = "prose_editor"
    output_model = CorrectedSnippet
    # A one-snippet copy-edit grounded in the supplied canonical facts — no
    # external sources to cite (BaseAgent defaults require_citations=True).
    require_citations = False

    def build_prompt(
        self,
        *,
        fact_id: str,
        canonical_value: object,
        offending_text: str,
        defect_reason: str = "",
    ) -> tuple[str, str]:
        user = (
            f"Canonical fact: {fact_id}\n"
            f"Correct/authoritative value or context: {canonical_value}\n"
            f"Reviewer's reason this snippet is wrong: {defect_reason or '(unspecified)'}\n\n"
            f"Offending snippet:\n\"\"\"{offending_text}\"\"\"\n\n"
            "Return the corrected snippet in the corrected_text field."
        )
        return _PROMPT_SYSTEM, user


def correct_prose_site(
    *,
    fact_id: str,
    canonical_value: object,
    offending_text: str,
    defect_reason: str = "",
    editor: Callable[[str], str] | None = None,
) -> str:
    """Return a minimal corrected snippet for an llm_prose site. With the default
    editor, dispatches ProseEditorAgent directly (structured). When an ``editor``
    callable is injected (tests / custom dispatch), it is handed a formatted
    prompt string and must return the corrected text. Fail-safe: returns
    ``offending_text`` unchanged on any error."""
    try:
        if editor is None:
            agent = ProseEditorAgent(user_id="ariel")
            report = agent.run_sync(
                fact_id=fact_id, canonical_value=canonical_value,
                offending_text=offending_text, defect_reason=defect_reason,
            )
            out = getattr(report, "output", None)
            corrected = (getattr(out, "corrected_text", "") or "").strip() if out else ""
            return corrected or offending_text
        prompt = (
            f"fact={fact_id} value={canonical_value} reason={defect_reason}\n"
            f"snippet: {offending_text}"
        )
        corrected = (editor(prompt) or "").strip()
        return corrected or offending_text
    except Exception as exc:  # noqa: BLE001 — fail-safe; re-verify is the backstop
        log.warning("prose_editor.failed fact=%s err=%s", fact_id, exc)
        return offending_text
