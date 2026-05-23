"""Intake extractor agent — pre-populate `user_context` from an uploaded plan.

The user uploads `Jacobs_Wealth_Plan.md` (or any plan markdown) on the
`/intake` page. Rather than asking the user 30 questions whose answers are
already documented in the plan, this agent reads the plan once and emits a
structured `IntakeExtraction` payload. The intake route merges the
extracted YAML into `user_context.{identity,goals,constraints}_yaml`
**additively** (existing values win), and the existing turn-by-turn intake
loop then asks only about the gaps.

Design notes:

- This is a **one-shot** extractor (not turn-based like `IntakeAgent`).
  Inputs: the plan markdown + the user's accumulated_context so the agent
  can avoid re-stating already-known facts. Output: `IntakeExtraction`.
- **No fabrication.** The prompt explicitly forbids inferring fields the
  plan does not mention. Missing fields go on `fields_missing` and the
  intake loop fills them in conversationally afterwards.
- **No citations required.** The source IS the user's plan — there is no
  external authority to cite. The schema does carry per-field
  `source_excerpt` strings (which sentence in the plan supported the
  extraction), but the base-class citation gate is disabled.
- **Haiku default.** Light reasoning over a single document; matches the
  intake-agent pricing/latency profile.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


# ----------------------------------------------------------------------
# Output schema
# ----------------------------------------------------------------------


class ExtractedField(BaseModel):
    """One extracted field from the plan, with the supporting excerpt.

    `value` is the extracted value as a free-text or YAML-scalar string
    (e.g., "israel", "2032", "1.2M NIS"). `source_excerpt` is 1-2 sentences
    quoted from the plan that justify the extraction; lets the user
    eyeball the agent's reasoning without re-reading the whole document.
    """

    value: str
    source_excerpt: str = ""
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class IntakeExtraction(BaseModel):
    """Structured extraction over an uploaded plan markdown.

    The three `*_yaml` strings are what the route merges into
    `user_context`. They MUST be valid YAML even when individual fields
    are None (skip the field, do not emit `null`).
    """

    # ---- Stage 1: identity ---------------------------------------------
    tax_residency: ExtractedField | None = None
    citizenship: list[str] | None = None  # could be multiple
    family: ExtractedField | None = None  # spouse + children mentioned
    employment: ExtractedField | None = None  # employer + years

    # ---- Stage 2: goals ------------------------------------------------
    retirement_target_year: ExtractedField | None = None
    target_annual_income: ExtractedField | None = None
    near_term_spending: ExtractedField | None = None

    # ---- Stage 3-4: financial picture / brokerages (high level only;
    # detailed positions come via the TSV ingest path) ------------------
    primary_brokers: list[str] | None = None
    bank_diversification_preference: ExtractedField | None = None

    # ---- Constraints / preferences -------------------------------------
    risk_tolerance: ExtractedField | None = None
    constraints_other: list[str] = Field(default_factory=list)

    # ---- Synthesis -----------------------------------------------------
    identity_yaml: str = Field(
        default="",
        description="YAML serialization of identity-shaped fields. Valid YAML "
        "even when fields are None (skip them, do not emit `null`).",
    )
    goals_yaml: str = Field(
        default="",
        description="YAML serialization of goals fields.",
    )
    constraints_yaml: str = Field(
        default="",
        description="YAML serialization of constraints fields.",
    )

    # ---- Honest reporting ----------------------------------------------
    fields_extracted: list[str] = Field(
        default_factory=list,
        description="Names of fields that had actual content in the plan.",
    )
    fields_missing: list[str] = Field(
        default_factory=list,
        description="Names of fields the plan did NOT address — the intake "
        "loop will ask about these next.",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    notes: str = Field(
        default="",
        description="Synthesizer notes — anything the agent noticed (e.g., "
        "'plan is v2.0 from Feb 2026, may be stale on FX').",
    )


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------


class IntakeExtractorAgent(BaseAgent[IntakeExtraction]):
    """Reads a plan markdown and produces structured `IntakeExtraction`."""

    agent_role = "intake_extractor"
    output_model = IntakeExtraction
    # The source IS the user's plan, not an external authority — there is
    # nothing to cite. We DO carry per-field `source_excerpt` strings so
    # the user can audit the agent's reasoning, but the base citation
    # gate is disabled.
    require_citations = False
    max_tokens = 4096

    def build_prompt(
        self,
        *,
        plan_markdown: str,
        accumulated_context: str = "",
        plan_filename: str = "plan.md",
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Construct ``(system_addendum, user_prompt, sources)`` for the extraction.

        Args:
            plan_markdown: full markdown text of the plan.
            accumulated_context: serialized user_context-so-far (YAML);
                lets the extractor avoid re-stating already-known facts.
            plan_filename: filename of the uploaded plan (used as the
                terminal segment of the source_id; defaults to ``plan.md``
                when the caller has no filename to thread through).

        Wave A: returns ``(system, user, sources)``. The plan markdown is
        extracted into a single Citations API document block titled
        ``intake/plan_markdown/<filename>`` rather than inlined into the
        user prompt, so the model's output can carry character-offset
        citations back into the underlying plan text. The
        ``source_excerpt`` strings on each ``ExtractedField`` remain a
        human-readable audit trail; Citations are the machine-checkable
        spans.
        """
        source_id = f"intake/plan_markdown/{plan_filename}"

        system = (
            "You are the intake-extractor agent on the Argosy fleet. Your job: "
            "read a financial plan document the user has uploaded and produce a "
            "structured extraction the intake interview can use to skip questions "
            "the plan already answers.\n\n"
            "ABSOLUTE RULES:\n"
            "1. If the plan does not address a field, leave it None and add the "
            "field name to fields_missing. Do not infer from outside knowledge. "
            "Do not fabricate. The downstream intake loop will ask about missing "
            "fields conversationally — that is the SAFE outcome when in doubt.\n"
            "2. For every field you DO populate, set `source_excerpt` to 1-2 "
            "sentences quoted directly from the plan that support the value. If "
            "you cannot quote a supporting excerpt, do not populate the field. "
            f"The plan text is attached as a document block titled `{source_id}`.\n"
            "3. Per-field confidence: HIGH = stated explicitly and unambiguously; "
            "MEDIUM = stated but qualified or implicit; LOW = inferred from "
            "context (use sparingly, prefer leaving the field None).\n"
            "4. The three *_yaml strings MUST be valid YAML even when fields are "
            "None — skip the field entirely, do not emit `null` lines. Empty YAML "
            "(\"\") is valid.\n"
            "5. fields_extracted and fields_missing together cover the schema "
            "fields you considered. The intake loop reads fields_missing to "
            "decide which questions still need to be asked.\n"
            "6. Output strictly conforms to the IntakeExtraction JSON schema. No "
            "extra commentary outside the schema.\n\n"
            "OUTPUT JSON SCHEMA:\n"
            f"{IntakeExtraction.model_json_schema()}\n"
        )

        user = (
            "User context already gathered (YAML; may be empty). Avoid contradicting "
            "or redundantly re-stating these facts:\n"
            "```yaml\n"
            f"{accumulated_context or '(empty)'}\n"
            "```\n\n"
            f"Plan markdown to extract from: see document `{source_id}`.\n\n"
            "Produce the IntakeExtraction JSON. Remember: when in doubt, leave the "
            "field None and list it in fields_missing. The intake loop will follow "
            "up — fabrication is the worst outcome."
        )

        sources: list[tuple[str, str]] = [(source_id, plan_markdown)]
        return system, user, sources


__all__ = ["ExtractedField", "IntakeExtraction", "IntakeExtractorAgent"]
