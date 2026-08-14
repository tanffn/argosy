"""ObjectionOwnerClassifierAgent — find which analyst owns an FM objection.

Called when ``_parse_analyst_refs_any_form`` returns nothing (no explicit
``agent_report:XAgent`` citation in the objection text). Rather than
silently dropping the objection, this lightweight Sonnet-class agent is
asked: "Which analyst domain owns this concern?"

Escalation-bar rule (from CLAUDE.md / HANDOVER.md):
  - Returns ``owner_role`` when a domain specialist can verify or rebut
    the FM's concern from their domain data. Two agents deriving different
    numbers in the same domain = derivation question → owner_role, never
    ``needs_user_input``.
  - Returns ``needs_user_input=True`` ONLY for genuine STRUCTURAL FORKS:
    (a) a user-expressed directive that conflicts with the model's
    derivation and only the user can say which governs (e.g. "your
    directive locks FI MET but the model derives FI NOT MET — which
    basis governs?"); or (b) a choice between valid paths that differs
    on personal values or risk preference (sell vs hold the core, adopt
    vs exit an asset class, goal changes).
  - NEVER ``needs_user_input`` for a number disagreement that a domain
    analyst can re-derive from raw data. That is a derivation question;
    the escalation-bar doctrine forbids routing it to the user.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class ObjectionOwnerClassification(BaseModel):
    """Routing decision for one FM objection with no explicit citation."""

    owner_role: Optional[str] = Field(
        default=None,
        description=(
            "The analyst role string (e.g. 'tax', 'concentration', "
            "'withdrawal_sequencer') that owns this objection. Null when "
            "the objection requires user input and no analyst can settle it."
        ),
    )
    needs_user_input: bool = Field(
        default=False,
        description=(
            "True ONLY for genuine structural forks: (a) a user-expressed "
            "directive that conflicts with the model's derivation; or (b) a "
            "choice between valid paths on personal values or risk preference. "
            "False when the objection is a derivation disagreement a domain "
            "analyst can resolve."
        ),
    )
    user_question: str = Field(
        default="",
        description=(
            "A CONCRETE question for the user, populated only when "
            "needs_user_input=True. Must name the specific numbers and the "
            "specific choice the user must make — not a generic 'synthesis "
            "failed' statement. E.g. 'Your directive locks the honest-liquid "
            "margin at +579,730 NIS (FI MET); the retirement model derives "
            "-186,670 NIS (FI NOT MET). Which basis governs — and if yours, "
            "what does the model have wrong?'"
        ),
    )
    rationale: str = Field(
        default="",
        description=(
            "One or two sentences explaining the routing decision. "
            "Cite the domain the objection touches."
        ),
    )


class ObjectionOwnerClassifierAgent(BaseAgent[ObjectionOwnerClassification]):
    """Lightweight Sonnet-class classifier: which analyst domain owns an FM objection?

    Called only when ``_parse_analyst_refs_any_form`` found no explicit
    ``agent_report:XAgent`` citation. Constrained to the canonical role
    list so it cannot hallucinate a non-existent specialist. Per-call cost
    target: ~$0.01-0.05.
    """

    agent_role = "objection_owner_classifier"
    output_model = ObjectionOwnerClassification
    require_citations = False

    def build_prompt(
        self,
        *,
        objection_topic: str,
        objection_detail: str,
        objection_severity: str,
        candidate_roles: list[str],
    ) -> tuple[str, str]:
        roles_block = "\n".join(f"  - {r}" for r in sorted(candidate_roles))
        system = (
            "You are a routing classifier for the Argosy financial-advisor fleet. "
            "A Fund Manager has raised an objection to a draft plan, but did NOT "
            "explicitly cite which analyst agent is responsible. Your job is to "
            "determine which analyst domain owns this objection so the right "
            "specialist can respond.\n\n"
            "### Escalation-bar rule — read carefully ###\n\n"
            "DERIVATION QUESTIONS stay in the fleet (never reach the user):\n"
            "  - Any objection where a domain analyst can re-derive the contested "
            "value from raw data.\n"
            "  - Two numbers disagreeing on the same quantity (e.g. NIS 580k vs "
            "NIS -187k, 12% vs 8%) is a derivation question. Assign owner_role to "
            "the analyst who owns that domain; needs_user_input must be False.\n\n"
            "STRUCTURAL FORKS reach the user — but ONLY these:\n"
            "  (a) A user-expressed directive that conflicts with the model's "
            "derivation AND only the user can adjudicate (e.g. 'your directive "
            "says FI is met but the retirement model says it is not — which "
            "governs?').\n"
            "  (b) A choice between valid paths that turns on personal values or "
            "risk preference (sell vs hold a single stock, adopt vs exit an asset "
            "class, change retirement goal). No analyst can resolve this from data.\n\n"
            "If in doubt, prefer assigning owner_role (a domain analyst) over "
            "escalating to the user. The user is NOT the expert; the fleet is.\n\n"
            "### Candidate analyst roles ###\n\n"
            f"{roles_block}\n\n"
            "### Output rules ###\n\n"
            "  - If you set owner_role, needs_user_input MUST be False and "
            "user_question MUST be empty.\n"
            "  - If needs_user_input is True, owner_role MUST be null and "
            "user_question MUST be a concrete, specific sentence naming the "
            "numbers and the exact choice the user must make. Generic text like "
            "'synthesis failed' or 'please clarify' is NOT acceptable.\n"
            "  - owner_role MUST be one of the candidate roles above or null. "
            "Never invent a role not on that list.\n\n"
            f"Output JSON conforming to: {ObjectionOwnerClassification.model_json_schema()}"
        )
        user = (
            "FM objection to classify:\n\n"
            f"TOPIC: {objection_topic}\n"
            f"SEVERITY: {objection_severity}\n"
            f"DETAIL:\n{objection_detail}\n\n"
            "Which analyst role owns this? Or does it require user input?\n"
            "Return the ObjectionOwnerClassification JSON now."
        )
        return system, user


__all__ = [
    "ObjectionOwnerClassification",
    "ObjectionOwnerClassifierAgent",
]
