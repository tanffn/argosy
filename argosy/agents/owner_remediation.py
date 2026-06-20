"""OwnerRemediationAgent — the node OWNER's targeted fix for a reader objection.

Phase 3 routes a whole-artifact reader finding to the node's owner (compliance
routes, never rewrites). The owner must then propose a CONCRETE remediation,
grounded in the node's derivation + the finding — choosing among:

  * set_value  — the FIGURE is wrong; propose a corrected value for the node.
  * prose_fix  — the figure is RIGHT; the surrounding PROSE drifted from it. Say
                 what to change (the surgical editor applies it, no figure change).
  * decline    — the finding is not actionable / wrong; say why (recorded).

The set_value vs prose_fix distinction is the crux: a finding about a figure
subject (e.g. "headline age 46 vs withdrawal funds spend through 48") usually
means the NARRATIVE is inconsistent with a correct figure, NOT that the figure is
wrong — so prose_fix, not a value change. Opus, per accuracy-over-cost; grounded
in the node's derivation, not outside memory (require_citations False).
"""
from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class OwnerRemediationVerdict(BaseModel):
    """The owner's proposed remediation for one reader objection.

    Validation aliases accept the field names the model naturally emits (a live
    run returned ``decision``/``reasoning`` instead of ``remediation``/
    ``reasoning_md``) — robust to the LLM's word choice without losing the schema.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    remediation: Literal["set_value", "prose_fix", "decline"] = Field(
        validation_alias=AliasChoices("remediation", "decision", "kind", "action"),
        description=(
            "set_value = the figure itself is wrong; propose a corrected value. "
            "prose_fix = the figure is correct but the surrounding prose drifted "
            "from it; describe the prose change (no figure change). "
            "decline = the finding is not actionable or is wrong; explain why."
        ),
    )
    proposed_value: float | None = Field(
        default=None,
        validation_alias=AliasChoices("proposed_value", "value", "new_value"),
        description=(
            "REQUIRED only when remediation=set_value: the corrected numeric value "
            "for the node, in the node's own unit (fraction for pct, integer year "
            "for year, NIS for money, age in years). null otherwise."
        ),
    )
    instruction: str = Field(
        default="",
        validation_alias=AliasChoices("instruction", "prose_change", "fix", "reason"),
        description=(
            "For prose_fix: precisely what the prose must say to match the figure "
            "(quote the drifted surface). For decline: why no change is warranted."
        ),
    )
    reasoning_md: str = Field(
        default="",
        validation_alias=AliasChoices("reasoning_md", "reasoning", "rationale", "explanation"),
        description=(
            "150-300 words. Reason FROM the node's derivation + the finding. State "
            "whether the FIGURE or the PROSE is at fault and why. Quote the current "
            "value + the cited surfaces verbatim."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class OwnerRemediationAgent(BaseAgent[OwnerRemediationVerdict]):
    """The owner's targeted remediation (set_value / prose_fix / decline) for a
    reader objection against a node it owns."""

    agent_role = "owner_remediation"
    output_model = OwnerRemediationVerdict
    require_citations = False

    def build_prompt(
        self,
        *,
        node_key: str,
        owner_role: str,
        current_value: str,
        derivation_md: str,
        finding_detail: str,
        surfaces_cited: str = "",
    ) -> tuple[str, str]:
        system = (
            f"You are the {owner_role or 'accountable'} OWNER of one node in Ariel's "
            f"living financial plan — `{node_key}`. The plan reviewer raised a "
            "finding that touches your node. Propose the TARGETED fix, choosing:\n"
            "  - set_value: the FIGURE is wrong -> give the corrected value.\n"
            "  - prose_fix: the figure is RIGHT but the surrounding PROSE drifted "
            "from it -> describe exactly what the prose must say (no figure change).\n"
            "  - decline: the finding is not actionable / is wrong -> say why.\n\n"
            "CRUCIAL: a finding that quotes contradictory PROSE usually means the "
            "NARRATIVE is inconsistent with a CORRECT figure — prefer prose_fix in "
            "that case. Only set_value when the figure itself is genuinely wrong.\n\n"
            "PRIME DIRECTIVE: Argosy maximizes the family's financial position + the "
            "earliest SAFE retirement. Fix the contradiction on the merits of the "
            "derivation — never parameter-fit a figure to a nicer conclusion. Ground "
            "every judgment in the node's DERIVATION below, not outside memory."
        )
        user_parts = [
            f"NODE: {node_key}",
            f"CURRENT VALUE: {current_value}",
            "",
            "DERIVATION (recipe + inbound values):",
            derivation_md or "(no derivation supplied)",
            "",
            "REVIEWER FINDING (the contradiction to resolve):",
            finding_detail or "(no detail supplied)",
        ]
        if surfaces_cited.strip():
            user_parts += ["", "SURFACES CITED (verbatim excerpts):", surfaces_cited.strip()]
        user_parts += [
            "",
            "Return a JSON object with EXACTLY these keys: "
            '"remediation" (one of set_value|prose_fix|decline), "proposed_value" '
            "(number or null), \"instruction\" (string), \"reasoning_md\" (string), "
            '"confidence" (HIGH|MEDIUM|LOW). Reason from the derivation and quote '
            "the current value + cited surfaces.",
        ]
        return system, "\n".join(user_parts)


__all__ = ["OwnerRemediationAgent", "OwnerRemediationVerdict"]
