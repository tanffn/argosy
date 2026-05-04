"""Advisor agent — persistent gap-tracker + free-form Q&A.

This subclasses `IntakeAgent` and adds two operating modes:

- **gap_driven**: same as legacy intake — the agent asks the next
  missing or stale gap (the route picks the highest-priority one and
  the agent batches related ones).
- **user_driven**: the user asked a question or made a statement.
  The agent's job is to (a) answer, citing domain_knowledge files
  where relevant, (b) log any factual updates from the user's
  message as `context_updates`, and (c) optionally surface a related
  gap as a follow-up.

The route chooses the mode (see `argosy.api.routes.advisor`):

  - last_user_message ends with "?"        → user_driven
  - last_user_message empty (page loaded)  → gap_driven (greet + ask)
  - statement (no "?")                     → user_driven
                                             (acknowledge + log + ask follow-up)

Both modes share the `ALREADY ANSWERED` / `STILL NEEDED` blocks built
from the gap tracker, so the agent never re-asks fresh fields and
always knows what's missing.

The original `IntakeAgent` keeps working unchanged; the AdvisorAgent
is a strict superset (new optional `mode` parameter on build_prompt;
new optional `mode` field on output).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from argosy.agents.intake import (
    INTAKE_STAGES,
    STAGE_PURPOSE,
    IntakeAgent,
    IntakeTurnOutput,
)


class AdvisorTurnOutput(IntakeTurnOutput):
    """IntakeTurnOutput + a `mode` discriminator so the UI can render
    user-driven turns (Q&A bubbles) differently from gap-driven turns
    (the agent asks; the user fills in).
    """

    mode: Literal["gap_driven", "user_driven"] = Field(
        default="gap_driven",
        description=(
            "Which path produced this turn. The route sets this based on "
            "request shape; the agent echoes it back so downstream callers "
            "can branch without re-deriving."
        ),
    )


class AdvisorAgent(IntakeAgent):
    """Persistent advisor.

    Same agent_role/model defaults as IntakeAgent so we don't perturb
    cost-tracking or settings YAMLs. The role is renamed to "advisor"
    in the audit log so we can distinguish reframed turns from legacy
    intake turns when slicing reports.
    """

    agent_role = "advisor"
    output_model = AdvisorTurnOutput
    require_citations = False
    max_tokens = 2048

    def build_prompt(  # type: ignore[override]
        self,
        *,
        current_stage: str,
        accumulated_context: str = "",
        last_user_message: str = "",
        history_excerpt: str = "",
        answered_fields: list[str] | None = None,
        missing_fields: list[str] | None = None,
        mode: Literal["gap_driven", "user_driven"] = "gap_driven",
        target_field: str | None = None,
    ) -> tuple[str, str]:
        """Construct (system_addendum, user_prompt) for one advisor turn.

        Args:
            current_stage: stage_1..stage_6 (kept for stage-aware prompts
                even though the advisor is no longer strictly stage-gated;
                priority hints still derive from stage purpose).
            mode: gap_driven (agent asks) or user_driven (agent answers).
            target_field: optional dotted-path the route wants the agent
                to focus on (e.g., the user clicked a sidebar row). Only
                meaningful for gap_driven mode.
        """
        if current_stage not in STAGE_PURPOSE:
            raise ValueError(f"Unknown advisor stage: {current_stage!r}")

        stage_purpose = STAGE_PURPOSE[current_stage]
        stage_index = INTAKE_STAGES.index(current_stage) + 1

        if mode == "gap_driven":
            mode_addendum = (
                "MODE: gap_driven — the user just opened the panel (or has "
                "nothing to ask). Your job is to ask 2-4 RELATED sub-questions "
                "drawn from the STILL NEEDED list below, batched into one "
                "message. Do NOT answer a question unless one was actually "
                "asked. Greet briefly only on the very first turn."
            )
        else:
            mode_addendum = (
                "MODE: user_driven — the user has asked a question or made a "
                "statement. Your job is, in order:\n"
                "  1. ANSWER the question concisely. If the answer depends on "
                "jurisdiction-specific rules (Israeli tax brackets, "
                "kupat-gemel ceiling, US PFIC, etc.), cite the relevant "
                "`domain_knowledge/...` file in `cited_sources`.\n"
                "  2. LOG any factual updates the user gave you as a "
                "`context_updates` entry — even if they buried it inside a "
                "question (e.g., 'now that I'm earning 750k NIS, should I…' "
                "→ that's a salary update). Use the same dotted-key shape as "
                "the STILL NEEDED list.\n"
                "  3. Optionally append ONE related follow-up question drawn "
                "from STILL NEEDED, but only if it flows naturally. Don't "
                "force a follow-up just to fill space.\n"
                "Render the answer and any follow-up in the SAME "
                "`question_for_user` field, separated by a blank line. The "
                "field name is a legacy artifact; treat it as 'message to "
                "the user'."
            )

        system = (
            "You are the advisor agent on the Argosy fleet — a persistent "
            "financial-context companion. Each turn round-trips through the "
            "model and takes ~10 seconds, so be efficient. Conversational, "
            "calm, professional. Prioritize critical info first (tax "
            "residency, family, income, assets, savings rate).\n\n"
            f"Current stage: {current_stage} ({stage_index} of {len(INTAKE_STAGES)}).\n"
            f"Stage purpose: {stage_purpose}\n\n"
            f"{mode_addendum}\n\n"
            "BATCHING RULE — when in gap_driven mode, ask 2-4 RELATED "
            "sub-questions per turn whenever the fields naturally cluster:\n"
            "  - household: spouse status + spouse residency + children + ages\n"
            "  - citizenship: own + spouse's + any others (single turn)\n"
            "  - income: gross salary + RSU vesting + bonus structure together\n"
            "  - brokerages: which brokers + account types + rough balance per\n"
            "  - real estate: how many properties + mortgage status + rental P/L\n"
            "  - pensions (Israeli context): קרן השתלמות + קופת גמל + פנסיה\n"
            "Render as a numbered list inside the single `question_for_user` "
            "string. Do NOT ask >4 sub-questions in one turn — that overwhelms "
            "the user.\n\n"
            "CONSTRAINTS:\n"
            "  - DO NOT re-ask anything in ALREADY ANSWERED. If a value is "
            "stale (it'll be in STILL NEEDED with a 'stale:' marker), it's "
            "fine to ask the user to confirm/refresh, but acknowledge the "
            "prior value.\n"
            "  - Never invent facts. If a number is unavailable, set "
            "confidence=LOW on the resulting context update and proceed.\n"
            "  - If the user reveals a contradiction with an earlier answer, "
            "flag it explicitly and ask which is current.\n"
            "  - In gap_driven mode, when the STILL NEEDED list is empty for "
            "the current stage, set stage_complete=true and name the "
            "next_stage; do not pad with another question.\n\n"
            "OUTPUT must conform exactly to this JSON schema:\n"
            f"{AdvisorTurnOutput.model_json_schema()}\n"
        )

        if answered_fields is None:
            answered_block = "  (route did not compute — derive from YAML below as best you can)"
        elif not answered_fields:
            answered_block = "  (none yet — this is early in the relationship)"
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

        target_hint = ""
        if target_field and mode == "gap_driven":
            target_hint = (
                "\nFOCUS HINT: the user clicked the sidebar row for "
                f"`{target_field}` — prioritize that field (and any 1-3 "
                "sibling fields that cluster naturally) for this turn.\n"
            )

        user_message_block = last_user_message or (
            "(no message — page just loaded, greet briefly and start asking gaps)"
        )

        user = (
            "Information gathered so far (YAML, may be empty):\n"
            "```yaml\n"
            f"{accumulated_context}\n"
            "```\n\n"
            "ALREADY ANSWERED — DO NOT ASK ABOUT ANY OF THESE AGAIN:\n"
            f"{answered_block}\n\n"
            "STILL NEEDED — missing or stale; ask ONLY about these (in batched "
            "form when 2-4 cluster):\n"
            f"{missing_block}\n"
            f"{target_hint}\n"
            "When you emit a context_updates entry, the yaml_patch should use "
            "the SAME dotted-key shape as the lists above (e.g., "
            "`spouse_citizenship: Israeli` for `identity.spouse_citizenship`).\n\n"
            "Recent conversation (last few turns; may be empty):\n"
            f"{history_excerpt or '(no prior turns)'}\n\n"
            "User's most recent message:\n"
            f"<user_message>{user_message_block}</user_message>\n\n"
            f"Produce the next advisor turn in `{mode}` mode as JSON conforming "
            "to the AdvisorTurnOutput schema above. Set the `mode` field of "
            f"your output to `{mode}`. If the STILL NEEDED list is empty AND "
            "you're in gap_driven mode, set stage_complete=true and name "
            "the next stage."
        )
        return system, user


__all__ = [
    "AdvisorAgent",
    "AdvisorTurnOutput",
]
