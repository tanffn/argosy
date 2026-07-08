"""Plan slice synthesizer — stage B of sliced full synthesis (phase 3).

Design: docs/design/sliced_full_synthesis.md §2.B.

Six parallel expansion calls: one per ``HorizonSection`` (long / medium /
short) plus one section batch per horizon. Each call receives — in this
exact order, so the shared prefix is BYTE-IDENTICAL across all six calls
(prompt-cache-shared; the siblings pay cache-read, not cache-creation, for
the corpus):

  1. the full HARD FACTS corpus (portfolio snapshot, analyst reports,
     debate outcomes) — sections need verbatim analyst text for
     ``Citation.extract``s, so no lossy per-slice excerpting;
  2. the ``DERIVED HEADLINE NUMBERS`` block;
  3. the COMPLETE skeleton verbatim (the coherence contract — every slice
     sees all three postures + the whole targets table, so cross-horizon
     prose references never guess a sibling's numbers);
  4. LAST (the varying suffix): this slice's assignment + the prior-items
     index rows and corrective corrections relevant to it.

Output schemas are the EXISTING pydantic types (``HorizonSection`` /
``Section``) — no new artifact schema; ``SectionBatch`` below is only a
thin list envelope so the structured-output path has a root object.

The contract mirrors the patch agents': the slice EXPANDS a decided
skeleton, it does not decide. Assembly byte-restores every locked field
from the skeleton regardless of what the slice emits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import HorizonSection, Section

# The core expansion contract (design §2.B, verbatim).
_EXPAND_CONTRACT = (
    "You are EXPANDING a decided skeleton, not deciding. Target values, "
    "delta identities, statuses, and rosters are LOCKED — deviating only "
    "wastes your output, because deterministic assembly restores them. "
    "Your job is rationale, posture prose, action detail/how_to/done_when, "
    "body_md, and evidence whose extracts literally contain the "
    "skeleton's values."
)

_SHARED_RULES = (
    "RULES:\n"
    "  - The PLAN SKELETON in the user message is the coherence contract: "
    "all three horizons' statuses, postures, targets (with the decided "
    "numbers), theme/action/delta/section rosters, and speculative "
    "candidates. When your prose references another horizon or a headline "
    "figure, use the skeleton's exact values — never re-derive, round, or "
    "carry a number forward from history.\n"
    "  - When a DERIVED HEADLINE NUMBERS block is present it is "
    "AUTHORITATIVE: use its exact values verbatim.\n"
    "  - Expand EVERY roster entry in your assignment; assembly fails "
    "LOUDLY on an omitted entry. Do NOT add items, sections, or deltas "
    "that are not in the roster — inventions are dropped.\n"
    "  - Preserve every label / item_id / section_id / ticker verbatim "
    "(they are the assembly join keys).\n"
    "  - CITATIONS for every numeric or directional claim: "
    "`agent_report:<id>` for analyst evidence, `decision_run:<id>` for "
    "prior synthesis lineage, `domain_kb:<path>` for jurisdiction rules, "
    "`plan_section:<heading>` for baseline references.\n"
    "  - Prose is forward-looking only. Do NOT narrate revisions — no "
    "`prior`, `previous`, `earlier`, `revised from`, `preserved from`, "
    "`lineage to`, `draft #N`, `synth #N`, `wave N`, `retracted`, "
    "`superseded` — those words are gate-banned and will block "
    "publication.\n"
    "  - TECHNICAL-READING DISCIPLINE: any symbol-level technical reading "
    "you state (RSI, MACD, moving average, price) MUST come from the "
    "CURRENT technical payload in this run's analyst outputs — never from "
    "the prior plan's prose. If the payload lacks the reading, describe "
    "the signal qualitatively.\n"
    "  - FI FRAMING DISCIPLINE: never restate a retired/superseded numeric "
    "value even to say it is dropped; keep the PERPETUITY BASE and the "
    "TOTAL CAPITAL TARGET distinct and never conflate them.\n\n"
    "RESPONSE FORMAT: emit the JSON object directly — no markdown code "
    "fences, no preamble. Your response MUST start with `{` and end "
    "with `}`."
)

# Evidence rubric for the section batch (condensed from the monolith's
# EvidencePerSection contract — the shape validators + the gate enforce it).
_EVIDENCE_RULES = (
    "EVIDENCE DISCIPLINE (gate-enforced):\n"
    "  1. Every section emits SectionEvidence; facts and missing_data "
    "cannot BOTH be empty.\n"
    "  2. Every FactClaim is covered by >=1 Citation whose "
    "supports_fact_index points at its slot.\n"
    "  3. Every numeric FactClaim's supporting Citation.extract MUST "
    "contain the value as a LITERAL substring — mirror the value format "
    "in the extract (value=277000 needs '277000' or '277,000' in the "
    "extract; '₪277K' FAILS).\n"
    "  4. Categorical/policy/qualitative facts: the extract must share "
    ">=3 content tokens with the fact text.\n"
    "  5. Distillate-derived facts use source_locator "
    "`distillate.<field_name>[<index>]`.\n"
    "  6. Any inference/agent_baseline/assumption_register citation "
    "requires >=1 documented Assumption.\n"
    "  7. Unavailable data goes into missing_data as SPECIFIC items.\n"
    "  8. FactClaim.text must be >=12 chars after strip — a complete "
    "claim, never a single token.\n"
    "  Each assigned section's key_facts (from the skeleton roster) are "
    "its content contract: state each key fact, with its value, backed "
    "by evidence."
)


class SectionBatch(BaseModel):
    """Envelope for one horizon's expanded ``Section`` list — a root
    object for the structured-output path, NOT a new artifact schema
    (the ``Section`` entries are the existing type, spliced into
    ``PlanSynthesisOutput.sections`` at assembly)."""

    sections: list[Section] = Field(default_factory=list)


def build_slice_shared_prefix(
    *,
    user_directive: str,
    portfolio_snapshot_summary: str,
    analyst_reports_text: str,
    debate_outcomes_text: str,
    resolved_numbers_block: str,
    skeleton_json: str,
) -> str:
    """The byte-identical shared prefix for all six expansion calls.

    Ordering per design §2.B: shared content first (directive is shared
    across all six calls, then the hard-facts corpus, then the derived
    numbers, then the complete skeleton); the caller appends the varying
    slice assignment LAST so prompt-cache sharing covers the corpus.
    """
    parts: list[str] = []
    if user_directive:
        parts.append(
            "=== USER DIRECTIVE (authoritative human input on this run) "
            "===\n" + user_directive
        )
    parts.extend([
        "================ HARD FACTS — GROUND TRUTH ================\n"
        "The corpus below is shared by every expansion slice; evidence "
        "extracts must quote it verbatim.",
        "=== PORTFOLIO SNAPSHOT (current holdings) ===\n"
        + portfolio_snapshot_summary,
        "=== ANALYST REPORTS (Phase 1 outputs) ===\n" + analyst_reports_text,
        "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n"
        + debate_outcomes_text,
    ])
    if resolved_numbers_block:
        parts.append(
            "=== DERIVED HEADLINE NUMBERS (AUTHORITATIVE — USE VERBATIM) "
            "===\n" + resolved_numbers_block
        )
    parts.append(
        "=== PLAN SKELETON (the decided contract — every locked field is "
        "byte-restored at assembly) ===\n" + skeleton_json
    )
    return "\n\n".join(parts)


class PlanHorizonSliceSynthesizerAgent(BaseAgent[HorizonSection]):
    """Expand ONE horizon (long / medium / short) of the skeleton into a
    full ``HorizonSection``."""

    agent_role = "plan_synthesizer"  # same Opus-class model resolution
    output_model = HorizonSection
    # The slice writes prose around locked decisions; the top-level
    # citation check is enforced by the prompt + downstream full-artifact
    # gates (same reasoning as the patch agents).
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(
        self,
        *,
        shared_prefix: str,
        assignment_block: str,
    ) -> tuple[str, str]:
        system = (
            "You are a plan SLICE synthesizer on the Argosy fleet — one of "
            "six parallel expansion calls over a decided plan skeleton "
            f"(sliced phase-3 synthesis).\n\n{_EXPAND_CONTRACT}\n\n"
            "Output schema: ONE HorizonSection JSON object for the horizon "
            "named in your assignment (the SDK enforces the schema "
            "strictly). Reproduce the skeleton's horizon / status / "
            "freshness_expected / targets (with their numbers) / themes / "
            "actions / speculative candidates / deltas EXACTLY, and write "
            "the prose: posture (full essay expanding the skeleton's "
            "posture_summary), rationale, per-target rationale, theme "
            "rationale + cited_sources, action detail + rationale + how_to "
            "+ done_when (concrete steps pointing at the right Argosy "
            "surface, and a crisp checkable completion bar — no invented "
            "numbers), delta rationale/prior/proposed/citations, and "
            "speculative-candidate thesis_summary + exit_trigger + "
            "sourced_from.\n\n"
            + _SHARED_RULES
        )
        usr = (
            shared_prefix
            + "\n\n=== YOUR SLICE ASSIGNMENT (varying — expand ONLY this) "
            "===\n" + assignment_block
            + "\n\nEmit the expanded HorizonSection JSON now."
        )
        return system, usr


class PlanSectionBatchSliceSynthesizerAgent(BaseAgent[SectionBatch]):
    """Expand one horizon's section-roster entries into full evidence-
    bearing ``Section``s."""

    agent_role = "plan_synthesizer"
    output_model = SectionBatch
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(
        self,
        *,
        shared_prefix: str,
        assignment_block: str,
    ) -> tuple[str, str]:
        system = (
            "You are a plan SLICE synthesizer on the Argosy fleet — one of "
            "six parallel expansion calls over a decided plan skeleton "
            f"(sliced phase-3 synthesis).\n\n{_EXPAND_CONTRACT}\n\n"
            "Output schema: {\"sections\": Section[]} — one Section per "
            "roster entry in your assignment, in roster order (the SDK "
            "enforces the schema strictly). Section = {section_id, "
            "horizon, title, body_md, evidence: SectionEvidence}. "
            "section_id/horizon must match the roster entry EXACTLY; "
            "body_md is forward-looking prose (no agent names, no revision "
            "history) that states the entry's key_facts with the "
            "skeleton's values.\n\n"
            + _EVIDENCE_RULES + "\n\n" + _SHARED_RULES
        )
        usr = (
            shared_prefix
            + "\n\n=== YOUR SLICE ASSIGNMENT (varying — expand ONLY this) "
            "===\n" + assignment_block
            + "\n\nEmit the {\"sections\": [...]} JSON now."
        )
        return system, usr


__all__ = [
    "PlanHorizonSliceSynthesizerAgent",
    "PlanSectionBatchSliceSynthesizerAgent",
    "SectionBatch",
    "build_slice_shared_prefix",
]
