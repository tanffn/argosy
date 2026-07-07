"""Critique-reconcile closer agent (the routing half of the critique loop).

After the weekly plan-critique lands, RED findings (and notable YELLOWs)
must not just sit in a panel — they get RECONCILED. This agent is the
closer that examines each triggered finding against the canonical plan
export and decides, per finding, which closer PATH resolves it:

* ``prose_edit``            — the finding is a stale/contradictory prose
  claim in an EDITABLE surface (the plan's authored raw markdown) that a
  minimal find/replace grounded in the canonical facts fixes.
* ``requires_resynthesis``  — the finding is real but lives in a surface
  DERIVED from the structured plan that cannot be edited in place (the
  horizon-row class: graph-authored prose only re-renders at synthesis).
  Honest outcome: flag for re-synthesis, never hand-patch.
* ``refresh_snapshot``      — the finding is about stale PORTFOLIO DATA
  (prices, FX, weights) that the snapshot-refresh service can reprice.
* ``needs_user_input``      — the finding needs data or a decision only
  the client can supply (route to the inbox as needs-info).
* ``dispute``               — the closer, re-deriving from the supplied
  plan + facts, believes the critique's claim is WRONG. Provide the
  evidence; the critique re-verifies blind and either withdraws the
  finding or upholds it (which escalates).

The agent ROUTES and evidences; the deterministic service applies. It
never invents numbers and never edits surfaces it was told are
unreachable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


CloserAction = Literal[
    "prose_edit",
    "requires_resynthesis",
    "refresh_snapshot",
    "needs_user_input",
    "dispute",
]


class FindingRoute(BaseModel):
    """The closer's routing decision for ONE critique finding."""

    finding_index: int = Field(
        description="Index of the finding in the FINDINGS list you were given.",
    )
    action: CloserAction
    rationale: str = Field(
        description="One or two sentences: why this path closes the finding.",
    )
    # prose_edit payload — only when action == "prose_edit".
    find: str | None = Field(
        default=None,
        description="EXACT verbatim substring of the editable plan markdown "
        "to replace. Copy it precisely. Only for action=prose_edit.",
    )
    replace: str | None = Field(
        default=None,
        description="Corrected text. Use ONLY numbers already present in the "
        "canonical facts or in `find`; never invent a figure. Only for "
        "action=prose_edit.",
    )
    # needs_user_input payload.
    question_for_user: str | None = Field(
        default=None,
        description="The concrete question / data request for the client. "
        "Only for action=needs_user_input.",
    )
    # dispute payload.
    rebuttal: str | None = Field(
        default=None,
        description="Concrete evidence (numbers, quotes from the plan or "
        "facts) showing the critique's claim is wrong. Only for "
        "action=dispute.",
    )


class CritiqueClosePlan(BaseModel):
    """Routing decisions for every triggered finding."""

    routes: list[FindingRoute] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="One line on the overall reconciliation approach.",
    )


class CritiqueCloserAgent(BaseAgent[CritiqueClosePlan]):
    """Routes weekly-critique findings to their closer paths (Opus 4.8)."""

    agent_role = "critique_closer"
    output_model = CritiqueClosePlan
    require_citations = False
    max_tokens = 16000

    def build_prompt(
        self,
        *,
        plan_label: str,
        plan_markdown: str,
        findings_block: str,
        raw_markdown_editable: bool,
    ) -> tuple[str, str]:
        editability = (
            "The plan carries an AUTHORED raw-markdown body that IS editable "
            "in place: `prose_edit` with an exact find/replace is allowed for "
            "prose-only contradictions."
            if raw_markdown_editable
            else "This plan is GRAPH-AUTHORED: every prose/horizon surface is "
            "DERIVED from the structured plan and re-renders only at "
            "synthesis. `prose_edit` is UNREACHABLE — findings about stale "
            "prose/horizon rows contradicting the structured allocation must "
            "route to `requires_resynthesis` (the honest outcome), never to "
            "a hand-patch."
        )
        system = (
            "You are the Argosy critique-reconcile closer. The plan-critique "
            "reader flagged findings against the client's plan; your job is to "
            "route EVERY finding you are given to the closer path that "
            "actually resolves it, with evidence.\n\n"
            "PATHS:\n"
            "- prose_edit: minimal find/replace in the EDITABLE plan markdown, "
            "grounded ONLY in numbers already present in the plan text. Never "
            "invent a figure.\n"
            "- requires_resynthesis: the defect is real but lives in a surface "
            "derived from the structured plan (stale horizon rows, prose that "
            "only re-renders at synthesis). Flag it; do not hand-patch.\n"
            "- refresh_snapshot: the defect is stale portfolio DATA (prices, "
            "FX, weights, balances) the snapshot-refresh service can reprice "
            "without the client.\n"
            "- needs_user_input: only the client can supply the data or the "
            "decision. State the concrete question.\n"
            "- dispute: you re-derived from the plan + facts and the "
            "critique's claim is WRONG. Give concrete evidence; a blind "
            "re-verification will adjudicate.\n\n"
            f"EDITABILITY CONTRACT (binding): {editability}\n\n"
            "Route every finding index you were given exactly once. Do not "
            "soften real findings into disputes; dispute only with concrete "
            "contrary evidence.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{CritiqueClosePlan.model_json_schema()}\n"
        )
        user = (
            f"PLAN LABEL: {plan_label}\n\n"
            f"FINDINGS TO ROUTE (index, severity, topic, summary, ref, "
            f"evidence):\n{findings_block}\n\n"
            f"=== PLAN (canonical export the critique reviewed) ===\n"
            f"{plan_markdown}\n\n"
            "Produce the CritiqueClosePlan JSON now — one route per finding "
            "index listed above."
        )
        return system, user


__all__ = [
    "CritiqueCloserAgent",
    "CritiqueClosePlan",
    "FindingRoute",
]
