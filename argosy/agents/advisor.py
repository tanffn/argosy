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

from typing import Any, Literal

from pydantic import Field

from argosy.agents.advisor_amendment_types import AmendmentIntent
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

    amendment: AmendmentIntent | None = Field(
        default=None,
        description=(
            "Wave 4: optional plan-amendment classification. Populated only "
            "when the system prompt's amendment-classification instructions "
            "are active (i.e., the user has a current plan) AND the latest "
            "user message asks to change something about that plan. The "
            "API route reads this and dispatches accordingly."
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
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

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
        has_current_plan: bool = False,
        image_attachments: list[Any] | None = None,
        pdf_attachments: list[Any] | None = None,
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
            has_current_plan: Wave 4. When True, the advisor has a current
                plan to amend; the system prompt grows an AMENDMENT INTENT
                DETECTION block instructing the model to classify any
                plan-change request into small/medium/large tiers. Defaults
                False so legacy intake-only flows are unchanged.
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
            "QUESTION FORMAT — each sub-question MUST start its own line with "
            "the literal prefix `Q#N — ` where N is a 1-based counter that "
            "increments per sub-question across the turn (so the first is "
            "`Q#1 — `, second `Q#2 — `, etc.). After the prefix, lead with a "
            "short bolded label (e.g. `**Capital-loss carryforwards:**`), then "
            "the question text. Example of the full line shape:\n"
            "  `Q#1 — **Capital-loss carryforwards:** Do you have any from prior years?`\n"
            "The UI detects the `Q#N —` prefix and renders each sub-question "
            "in a distinct visual block; without it they blend into the "
            "surrounding context-update notes. Do NOT use plain markdown "
            "numbered lists (`1. `, `2. `) or bare bolded labels for the "
            "questions themselves — only the `Q#N —` form. Cap at 4 "
            "sub-questions per turn; more overwhelms the user.\n\n"
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

        if image_attachments:
            system = system + (
                "\n\nIMAGE ATTACHMENT HANDLING\n\n"
                f"The user has attached {len(image_attachments)} image(s) to "
                "this turn. Examine each carefully. Common kinds and how to "
                "handle them:\n"
                "  - Brokerage statement screenshots: extract holdings, "
                "balances, account class. Emit `context_updates` for the "
                "matching `identity.brokerage_accounts` / "
                "`identity.bank_accounts` fields.\n"
                "  - News article screenshots: read the article, decide if "
                "it's material to the user's positions or plan, and answer "
                "any embedded question. Cite the publication if visible.\n"
                "  - Plan / portfolio diagrams: treat as supplementary "
                "context for the conversation; describe what you see and "
                "ask follow-up questions if the user's intent isn't obvious.\n"
                "  - Charts: read the axis labels, identify the trend, and "
                "respond accordingly.\n"
                "If the image isn't relevant to financial planning, "
                "acknowledge it neutrally and move on. Never invent details "
                "the image doesn't show — set confidence=LOW on any "
                "context_updates derived from images and say so plainly.\n"
            )

        if pdf_attachments:
            system = system + (
                "\n\nPDF ATTACHMENT HANDLING\n\n"
                f"The user has attached {len(pdf_attachments)} PDF(s) to "
                "this turn. They arrive as native ``document`` blocks — read "
                "the full text, tables, and any embedded images. Common kinds:\n"
                "  - Brokerage / pension statements: extract holdings, "
                "balances, account class. Emit `context_updates` for the "
                "matching `identity.brokerage_accounts` / "
                "`identity.bank_accounts` / pension fields.\n"
                "  - Tax filings / pay stubs: extract gross/net income, "
                "withholding totals, and any deduction lines that imply "
                "kupat-gemel / קרן השתלמות contributions.\n"
                "  - Wealth plans / IPS documents: treat as a candidate "
                "baseline. If the user is asking you to ingest it as their "
                "plan, say so plainly and ask them to confirm before any "
                "structural advice is given against it.\n"
                "  - Research reports / prospectuses: summarise the key "
                "claims relevant to the user's positions.\n"
                "Cite the PDF filename in `cited_sources` whenever you "
                "extract a figure from it. If a value looks ambiguous "
                "(handwritten, scanned poorly, smudged), set confidence=LOW "
                "on the resulting context update and flag the ambiguity in "
                "your message to the user.\n"
            )

        if has_current_plan:
            system = system + (
                "\n\nAMENDMENT INTENT DETECTION\n\n"
                "If the user's latest message asks to change something about their current "
                "plan (a target, theme, action, or speculative candidate), classify it:\n"
                "  small  - strict tightening of one specific target/action they reference\n"
                "           directly. Direction must be \"tighten\" (lowers risk surface);\n"
                "           \"loosen\" or \"ambiguous\" - use medium instead.\n"
                "  medium - theme shift on one horizon, multi-target tweak, loosening, or\n"
                "           any change that involves cross-target reasoning.\n"
                "  large  - structural rethink, cross-horizon, \"re-evaluate everything\",\n"
                "           \"run synthesis\", or any request that asks the fleet to\n"
                "           reconsider.\n\n"
                "Emit the classification in the `amendment` field of your structured output.\n"
                "For small with direction=tighten, also emit a fully-formed `proposed_delta`\n"
                "with item_id, item_kind, horizon, change_kind, summary, prior, proposed,\n"
                "rationale, and accepted=true.\n\n"
                "Be conservative: when in doubt, classify as medium. The user can always say\n"
                "\"do a full synthesis\" to escalate to large; they cannot easily reverse a\n"
                "hasty small Delta.\n"
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
