"""Intake agent — the conversational interview agent (SDD §6, Appendix B.7).

This is a *turn-based* agent: each `run(...)` produces a single next-question
plus an optional structured update to `user_context` derived from the
*previous* user answer. The CLI loop (`argosy intake`) drives the conversation.

Stages (per SDD §6.1, expanded by Phase 2 CFP coverage and the
concentration-reduction follow-up):

  stage_1:  identity & jurisdiction
  stage_2:  goals & timeline
  stage_3:  financial picture (income → bank → brokerage → IL pensions
            per-vehicle → US retirement → real estate → expenses)
  stage_4:  brokerage connections
  stage_5:  plan import & critique
  stage_6:  operational preferences
  stage_7:  estate planning           (CFP)
  stage_8:  risk management/insurance (CFP)
  stage_9:  tax situation             (CFP)
  stage_10: education funding         (CFP)
  stage_11: special situations        (employer concentration, RSU plans,
            sector overweights — explicit acknowledgement + mitigation)

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
    "stage_7",
    "stage_8",
    "stage_9",
    "stage_10",
    "stage_11",
]

STAGE_PURPOSE: dict[str, str] = {
    "stage_1": (
        "Establish identity & jurisdiction: country of tax residence, "
        "citizenship, family status (spouse, children, dependents), date "
        "of birth, employment status. This determines which "
        "domain_knowledge/tax/<jurisdiction>/ files apply downstream."
    ),
    "stage_2": (
        "Establish goals & timeline: retirement target year, target annual "
        "income, near-term spending events, risk tolerance, time horizon, "
        "lifestyle aspirations, legacy/charitable intent."
    ),
    "stage_3": (
        "Build the financial picture in priority order: income (pay stubs, "
        "RSU schedule, bonus history, secondary income) → bank balances → "
        "brokerage positions (with cost-basis lots if available) → US "
        "retirement accounts (401k / IRA / Roth / HSA) → IL pensions "
        "(קרן השתלמות, קופת גמל, קרן פנסיה) → real estate (mortgages, "
        "valuations, rental P&L) → monthly expense breakdown → emergency "
        "fund → other debts → business interests / foreign assets. "
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
    "stage_7": (
        "Estate planning (CFP scope): will, living trust, durable power of "
        "attorney, healthcare directive, beneficiary designations on retirement "
        "and insurance accounts, guardianship designation for minor children. "
        "Annual review cadence — life events trigger out-of-band updates."
    ),
    "stage_8": (
        "Risk management / insurance (CFP scope): life, disability (short and "
        "long term), health (carrier + deductible + HSA-eligibility), long-term "
        "care, property & casualty (home/auto/renters), umbrella liability."
    ),
    "stage_9": (
        "Tax situation (CFP scope): filing status (US: MFJ/MFS/single/HoH; IL: "
        "individual/joint), prior-year AGI and effective rate, carryforwards "
        "(capital losses, AMT credit, foreign tax credit), tax-loss harvesting "
        "preference, planned charitable giving, estimated quarterly payments."
    ),
    "stage_10": (
        "Education funding (CFP scope): per-dependent target college year and "
        "cost, currency, education savings accounts (529 / Coverdell / "
        "חיסכון לכל ילד), funding strategy (full / partial / loans expected)."
    ),
    "stage_11": (
        "Single-employer equity concentration, RSU vest schedules, sector "
        "overweights, and other portfolio-risk factors that warrant explicit "
        "acknowledgement and a mitigation plan."
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
        "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6",
        "stage_7", "stage_8", "stage_9", "stage_10", "stage_11",
    ]
    question_for_user: str = Field(
        default="",
        description=(
            "A single message asking the user 2-4 RELATED sub-questions at "
            "once when the fields naturally cluster (e.g., spouse + children + "
            "spouse-residency in one message; brokerage names + account types + "
            "estimated balances together). Render as a numbered list inside the "
            "single string. Empty if the stage has just completed."
        ),
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
        "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6",
        "stage_7", "stage_8", "stage_9", "stage_10", "stage_11", "complete",
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
    """Intake interview agent.

    Batches 2-4 related sub-questions per turn (each agent round-trip
    takes ~10s on Haiku; one-question-at-a-time made the interview feel
    sluggish). Stage-aware. Avoids re-asking anything already present in
    `accumulated_context`.
    """

    agent_role = "intake"
    output_model = IntakeTurnOutput
    # Citations not strictly required on every turn (most turns are pure
    # questions). When the agent does cite, it should still cite Tier-1.
    require_citations = False
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        current_stage: str,
        accumulated_context: str = "",
        last_user_message: str = "",
        history_excerpt: str = "",
        answered_fields: list[str] | None = None,
        missing_fields: list[str] | None = None,
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
            "financial-context interview. Each turn round-trips through the "
            "model and takes ~10 seconds, so BATCH related sub-questions "
            "together to make the interview feel responsive. Conversational, "
            "calm, professional. Prioritize critical info first (tax "
            "residency, family, income, assets, savings rate).\n\n"
            f"Current stage: {current_stage} ({stage_index} of {len(INTAKE_STAGES)}).\n"
            f"Stage purpose: {stage_purpose}\n\n"
            "BATCHING RULE — ask 2-4 RELATED sub-questions per turn whenever "
            "the fields naturally cluster:\n"
            "  - household: spouse status + spouse residency + children + ages\n"
            "  - citizenship: own + spouse's + any others (single turn)\n"
            "  - income: gross salary + RSU vesting + bonus structure together\n"
            "  - brokerages: which brokers + account types + rough balance per\n"
            "  - real estate: how many properties + mortgage status + rental P/L\n"
            "  - pensions (Israeli context): קרן השתלמות + קופת גמל + פנסיה\n"
            "Render as a numbered list inside the single `question_for_user` "
            "string. Do NOT ask >4 sub-questions in one turn — that overwhelms "
            "the user. If a field needs disambiguation, defer it to the next "
            "turn; don't pad.\n\n"
            "CONSTRAINTS:\n"
            "  - DO NOT re-ask anything that's already in `accumulated_context`. "
            "If an answer is already there (even partially), accept it and move "
            "on; ask for the MISSING piece only.\n"
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

        # Render explicit answered / still-needed lists. These are the
        # AUTHORITATIVE source for what's done — the agent must NOT
        # re-derive this from the YAML (Haiku is bad at that, repeatedly
        # re-asking answered fields). The route computes these via
        # `argosy.agents.intake_fields.stage_status` and passes them in.
        if answered_fields is None:
            answered_block = "  (route did not compute — derive from YAML below as best you can)"
        elif not answered_fields:
            answered_block = "  (none yet — this is early in the stage)"
        else:
            answered_block = "\n".join(f"  - {f}" for f in answered_fields)

        if missing_fields is None:
            missing_block = "  (route did not compute — pick from stage purpose above)"
        elif not missing_fields:
            missing_block = (
                "  (none — set stage_complete=true and name next_stage; "
                "do NOT ask another question on this turn)"
            )
        else:
            missing_block = "\n".join(f"  - {f}" for f in missing_fields)

        user = (
            "Information gathered so far (YAML, may be empty):\n"
            "```yaml\n"
            f"{accumulated_context}\n"
            "```\n\n"
            "ALREADY ANSWERED — DO NOT ASK ABOUT ANY OF THESE AGAIN:\n"
            f"{answered_block}\n\n"
            "STILL NEEDED for this stage — ask ONLY about these fields, "
            "in BATCHED form (2-4 related at once when they cluster):\n"
            f"{missing_block}\n\n"
            "When you emit a context_updates entry, the yaml_patch should use "
            "the SAME dotted-key shape as the lists above (e.g., "
            "`spouse_citizenship: Israeli` for `identity.spouse_citizenship`; "
            "or nested `spouse: {citizenship: Israeli}` — both are accepted "
            "by the merge step, but flat is preferred for stability).\n\n"
            "Recent conversation (last few turns; may be empty):\n"
            f"{history_excerpt or '(no prior turns)'}\n\n"
            "User's most recent answer:\n"
            f"<user_answer>{last_user_message or '(this is the first turn — greet briefly and start the interview)'}</user_answer>\n\n"
            "Produce the next intake turn as JSON conforming to the IntakeTurnOutput "
            "schema above. If the previous answer materially advances the current "
            "stage, include a context_updates entry. If the STILL NEEDED list is "
            "empty, set stage_complete=true and name the next stage."
        )
        return system, user


__all__ = [
    "INTAKE_STAGES",
    "STAGE_PURPOSE",
    "ContextUpdate",
    "IntakeAgent",
    "IntakeTurnOutput",
]
