"""Plan patch synthesizer — patch-mode phase 3 (corrective patch-synthesis).

Design: docs/design/corrective_patch_synthesis.md §2.C.

One call per implicated SLICE: the agent receives the prior slice VERBATIM
(the base being edited), ONLY the corrections/directives implicating that
slice, the authoritative DERIVED HEADLINE NUMBERS block, the slice's
prior-items index rows (ID-stability contract), and a short HARD FACTS
excerpt. It emits an EDITED SLICE in the existing pydantic schema
(``HorizonSection`` / ``Section``) — not patch ops.

The deterministic merge in the orchestrator byte-restores every item whose
``item_id`` is not implicated by a correction, so the model physically
cannot perturb an unimplicated item — deviating only wastes its own output.

Both agent classes use ``agent_role = "plan_synthesizer"`` so model
resolution (Opus class, accuracy-over-cost) matches the full synthesizer —
a distinct agent class, not a mode flag, so the prompt stays single-purpose.

``require_citations`` is False: the patch edits an already-gated artifact
and the merged output goes through the SAME full-artifact gates (rewriter
invariants, phases 4/4.5/5, whole-artifact reader, corrections-landed
floor) as any draft; a slice-local top-level citation check would reject
valid prose-only edits (``Section`` has no ``cited_sources`` field at all).
"""

from __future__ import annotations

from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import HorizonSection, Section

# The core editing contract (design §2.C, verbatim).
_EDIT_CONTRACT = (
    "You are EDITING, not drafting. Change ONLY what a correction requires: "
    "the implicated items, the Delta entries for those items, and any "
    "sentence in posture/rationale/body_md that states a corrected figure. "
    "Reproduce everything else byte-for-byte. Unimplicated items are "
    "restored from the base regardless of what you emit — deviating only "
    "wastes your own output. Update each edited item's evidence so extracts "
    "literally contain the new values."
)

_SHARED_RULES = (
    "RULES:\n"
    "  - The BASE SLICE in the user message is the artifact being edited. "
    "Start from it; your output must be the SAME slice with only the "
    "correction-required edits applied.\n"
    "  - The CORRECTIONS block lists every correction/directive implicating "
    "this slice, each with its canonical value + derivation and the wrong "
    "values that must be ABSENT from your output.\n"
    "  - When a DERIVED HEADLINE NUMBERS block is present it is "
    "AUTHORITATIVE: use its exact values; never a rounded or carried-forward "
    "number.\n"
    "  - Preserve every item_id / section_id / horizon verbatim (the "
    "ID-stability contract). Do NOT add or remove items or sections.\n"
    "  - Prose is forward-looking only. Never narrate the correction "
    "('previously', 'revised from', 'was X', 'corrected to') — state the "
    "corrected value as load-bearing fact. Gate-banned words: prior, "
    "previous, earlier, revised from, retracted, superseded, draft #N.\n"
    "  - You integrate supplied canonical values; you do NOT re-derive "
    "figures. If a correction's canonical value is missing, leave the "
    "surface's derivation intact and change only what the correction "
    "explicitly states.\n\n"
    "RESPONSE FORMAT: emit the JSON object directly — no markdown code "
    "fences, no preamble. Your response MUST start with `{` and end "
    "with `}`."
)


def _user_prompt(
    *,
    slice_label: str,
    base_slice_json: str,
    corrections_block: str,
    resolved_numbers_block: str,
    prior_items_block: str,
    hard_facts_excerpt: str,
) -> str:
    parts: list[str] = []
    if resolved_numbers_block:
        parts.append(
            "=== DERIVED HEADLINE NUMBERS (AUTHORITATIVE — USE VERBATIM) ===\n"
            + resolved_numbers_block
        )
    parts.append(
        "=== CORRECTIONS IMPLICATING THIS SLICE (each must be resolved) ===\n"
        + corrections_block
    )
    if hard_facts_excerpt:
        parts.append(
            "=== HARD FACTS EXCERPT (portfolio snapshot summary) ===\n"
            + hard_facts_excerpt
        )
    if prior_items_block:
        parts.append(
            "=== PRIOR ITEMS INDEX [reference — item_id stability only] ===\n"
            + prior_items_block
        )
    parts.append(
        f"=== BASE SLICE ({slice_label}) — EDIT THIS, verbatim JSON ===\n"
        + base_slice_json
    )
    parts.append(
        "Emit the EDITED slice JSON now — the same slice with only the "
        "correction-required edits applied."
    )
    return "\n\n".join(parts)


class PlanHorizonPatchSynthesizerAgent(BaseAgent[HorizonSection]):
    """Patch one HorizonSection slice (long / medium / short)."""

    agent_role = "plan_synthesizer"
    output_model = HorizonSection
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(
        self,
        *,
        slice_label: str,
        base_slice_json: str,
        corrections_block: str,
        resolved_numbers_block: str = "",
        prior_items_block: str = "",
        hard_facts_excerpt: str = "",
    ) -> tuple[str, str]:
        system = (
            "You are the plan PATCH synthesizer on the Argosy fleet — a "
            "corrective patch pass over ONE horizon slice of the current "
            f"plan.\n\n{_EDIT_CONTRACT}\n\n"
            "Output schema: the SAME HorizonSection JSON shape as the base "
            "slice (the SDK enforces the schema strictly).\n\n"
            + _SHARED_RULES
        )
        return system, _user_prompt(
            slice_label=slice_label,
            base_slice_json=base_slice_json,
            corrections_block=corrections_block,
            resolved_numbers_block=resolved_numbers_block,
            prior_items_block=prior_items_block,
            hard_facts_excerpt=hard_facts_excerpt,
        )


class PlanSectionPatchSynthesizerAgent(BaseAgent[Section]):
    """Patch one Section slice (by ``(section_id, horizon)``)."""

    agent_role = "plan_synthesizer"
    output_model = Section
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(
        self,
        *,
        slice_label: str,
        base_slice_json: str,
        corrections_block: str,
        resolved_numbers_block: str = "",
        prior_items_block: str = "",
        hard_facts_excerpt: str = "",
    ) -> tuple[str, str]:
        system = (
            "You are the plan PATCH synthesizer on the Argosy fleet — a "
            "corrective patch pass over ONE evidence-bearing Section of the "
            f"current plan.\n\n{_EDIT_CONTRACT}\n\n"
            "Output schema: the SAME Section JSON shape as the base slice "
            "(the SDK enforces the schema strictly). The evidence subtree "
            "must stay consistent: every numeric FactClaim's supporting "
            "extract must contain the value as a literal substring.\n\n"
            + _SHARED_RULES
        )
        return system, _user_prompt(
            slice_label=slice_label,
            base_slice_json=base_slice_json,
            corrections_block=corrections_block,
            resolved_numbers_block=resolved_numbers_block,
            prior_items_block=prior_items_block,
            hard_facts_excerpt=hard_facts_excerpt,
        )


__all__ = [
    "PlanHorizonPatchSynthesizerAgent",
    "PlanSectionPatchSynthesizerAgent",
]
