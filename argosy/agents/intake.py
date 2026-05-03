"""Intake agent — the conversational interview agent (SDD §6, Appendix B.7).

This is a *turn-based* agent: each `run(...)` produces a single next-question
plus an optional structured update to `user_context` derived from the
*previous* user answer. The CLI loop (`argosy intake`) drives the conversation.

Stages (per SDD §6.1):

  stage_1: identity & jurisdiction
  stage_2: goals & timeline
  stage_3: financial picture (income → bank → brokerage → pensions → real
           estate → insurance → tax filings)
  stage_4: brokerage connections
  stage_5: plan import & critique
  stage_6: operational preferences

The agent advances stages by emitting a STAGE_COMPLETE marker on the
`stage_complete` field of its output. The orchestrator (CLI) is responsible
for persisting `current_stage` to the DB on stage transitions.

Design notes:

- The agent is **side-effect free**: it does NOT touch the DB. The CLI is the
  orchestrator and writes back to `user_context` based on `context_updates`.
- The agent's output schema is strict pydantic so the CLI can rely on
  shape; tests verify the schema round-trips.
- Citations: this agent's *own* questions don't typically require citations,
  but when it makes a domain-knowledge claim (e.g., "the Israeli surtax kicks
  in around 721k NIS"), it cites a `domain_knowledge/...` file. The
  base-class citation gate is therefore **disabled** for this agent — set via
  `require_citations = False` — because most turns are pure questions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand

INTAKE_STAGES: list[str] = [
    "stage_1",
    "stage_2",
    "stage_3",
    "stage_4",
    "stage_5",
    "stage_6",
]

STAGE_PURPOSE: dict[str, str] = {
    "stage_1": (
        "Establish identity & jurisdiction: country of tax residence, "
        "citizenship, family status (spouse, children). This determines "
        "which domain_knowledge/tax/<jurisdiction>/ files apply downstream."
    ),
    "stage_2": (
        "Establish goals & timeline: retirement target year, target annual "
        "income, near-term spending events, kids' education, charitable plans."
    ),
    "stage_3": (
        "Build the financial picture in priority order: income (pay stubs, "
        "RSU schedule, bonus history) → bank balances → brokerage positions "
        "(with cost-basis lots if available) → pensions (קרן השתלמות, "
        "קופת גמל, קרן פנסיה) → real estate (mortgages, valuations, rental "
        "P&L) → insurance → tax filings (prior דוח שנתי, W-8BEN status). "
        "Ask for documentation when self-reported confidence is low."
    ),
    "stage_4": (
        "Brokerage connections: IBKR API key (limited acct), Schwab read-only "
        "export workflow, Leumi TSV upload schedule. All credentials encrypted."
    ),
    "stage_5": (
        "Plan import & critique: optional import of an existing plan document; "
        "the plan-critique agent then produces RED/YELLOW/GREEN findings. The "
        "user can keep, accept critique edits, or ask intake to draft anew."
    ),
    "stage_6": (
        "Operational preferences: tier override mode, execution mode (paper "
        "for first N weeks), model defaults, alert channels, cadence schedule."
    ),
}


# ----------------------------------------------------------------------
# Output schema
# ----------------------------------------------------------------------


class ContextUpdate(BaseModel):
    """A structured patch to user_context derived from the user's last answer.

    `target_section` chooses which YAML payload in `user_context` the patch
    applies to: identity / goals / constraints. `yaml_patch` is a YAML fragment
    the orchestrator merges into the existing payload.
    """

    target_section: Literal["identity", "goals", "constraints"]
    yaml_patch: str = Field(
        default="",
        description="YAML fragment to merge into the chosen section.",
    )
    rationale: str = Field(
        default="",
        description="One sentence: why the agent inferred this update.",
    )


class IntakeTurnOutput(BaseModel):
    """Output of one intake turn.

    `question_for_user` is the next question (or empty if stage is complete
    AND we've already delivered the wrap-up sentence).
    `stage_complete=True` signals the orchestrator to advance to the next
    stage. `next_stage` is the agent's recommended next stage; usually this
    is the next sequential stage but the agent can request a back-jump if
    the user reveals contradictory information.
    """

    stage: Literal[
        "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6"
    ]
    question_for_user: str = Field(
        default="",
        description="Exactly one question, or empty if stage just completed.",
    )
    context_updates: list[ContextUpdate] = Field(
        default_factory=list,
        description="Zero or more patches to user_context derived from the user's previous turn.",
    )
    stage_complete: bool = Field(
        default=False,
        description="True if the agent has gathered enough for this stage.",
    )
    next_stage: Literal[
        "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6", "complete"
    ] | None = None
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Optional. Cite domain_knowledge files if the question or rationale "
        "depends on jurisdiction-specific rules.",
    )
    notes_for_orchestrator: str = Field(
        default="",
        description="Free-text notes the CLI may surface to the user (e.g., 'attach "
        "your latest pay stub before the next session').",
    )


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------


class IntakeAgent(BaseAgent[IntakeTurnOutput]):
    """Intake interview agent. One question per turn. Stage-aware."""

    agent_role = "intake"
    output_model = IntakeTurnOutput
    # Citations not strictly required on every turn (most turns are pure
    # questions). When the agent does cite, it should still cite Tier-1.
    require_citations = False
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        current_stage: str,
        accumulated_context: str = "",
        last_user_message: str = "",
        history_excerpt: str = "",
    ) -> tuple[str, str]:
        """Construct (system_addendum, user_prompt) for one intake turn.

        Args:
            current_stage: one of `stage_1`..`stage_6`. Determines purpose.
            accumulated_context: serialized user_context-so-far (YAML).
            last_user_message: the user's most recent answer, or "" on the
                first turn of stage_1.
            history_excerpt: last few Q/A pairs as a single string for
                continuity. Optional; pass "" if not tracked.
        """
        if current_stage not in STAGE_PURPOSE:
            raise ValueError(f"Unknown intake stage: {current_stage!r}")

        stage_purpose = STAGE_PURPOSE[current_stage]
        stage_index = INTAKE_STAGES.index(current_stage) + 1

        system = (
            "You are the intake agent on the Argosy fleet, conducting a "
            "financial-context interview. One question at a time. "
            "Conversational, calm, professional. Prioritize critical info "
            "first (tax residency, family, income, assets, savings rate).\n\n"
            f"Current stage: {current_stage} ({stage_index} of 6).\n"
            f"Stage purpose: {stage_purpose}\n\n"
            "CONSTRAINTS:\n"
            "  - Ask exactly ONE question per turn.\n"
            "  - When the user provides data with low confidence, ask for "
            "documentation if it materially affects downstream decisions.\n"
            "  - When the user gives an illogical answer per established "
            "financial principles, challenge it directly with evidence — do "
            "not soften.\n"
            "  - When you have enough to advance, set stage_complete=true and "
            "name the next_stage; do not ask another question on this turn.\n"
            "  - If the user reveals a contradiction with an earlier stage, "
            "you may set next_stage to the earlier stage to revisit.\n"
            "  - Never invent facts. If a number is unavailable, set "
            "confidence=LOW on the resulting context update and proceed.\n\n"
            "OUTPUT must conform exactly to this JSON schema:\n"
            f"{IntakeTurnOutput.model_json_schema()}\n"
        )

        user = (
            "Information gathered so far (YAML, may be empty):\n"
            "```yaml\n"
            f"{accumulated_context}\n"
            "```\n\n"
            "Recent conversation (last few turns; may be empty):\n"
            f"{history_excerpt or '(no prior turns)'}\n\n"
            "User's most recent answer:\n"
            f"<user_answer>{last_user_message or '(this is the first turn — greet briefly and start the interview)'}</user_answer>\n\n"
            "Produce the next intake turn as JSON conforming to the IntakeTurnOutput "
            "schema above. If the previous answer materially advances the current "
            "stage, include a context_updates entry. If the stage is satisfied, set "
            "stage_complete=true and name the next stage."
        )
        return system, user


__all__ = [
    "INTAKE_STAGES",
    "STAGE_PURPOSE",
    "ContextUpdate",
    "IntakeAgent",
    "IntakeTurnOutput",
]
