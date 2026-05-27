"""Plan-distiller agent — extract a durable, LLM-suited distillate from a
user-imported plan markdown.

See SDD §6.10 / spec docs/superpowers/specs/2026-05-05-plan-distillate-design.md §3.

Inputs: plan markdown (already in the DB as ``plan_versions.raw_markdown``).
Output: a structured ``PlanDistillate`` capturing principles, targets-as-stated,
decision rules, constraints, goals, risk priorities, and stress tolerance.

EXPLICIT EXCLUSIONS (enforced in the system prompt):
  - Current portfolio percentages (66% NVDA today, etc.)
  - Current FX rates (3.09 NIS/USD)
  - Specific dollar amounts at point-in-time ($430k proceeds)
  - Dated tranche schedules (Q1 2026 sells 2,500 shares)
  - Share counts (12,748 NVDA shares)
  - "Next 30/90 days" implementation roadmap sections

These will be re-derived monthly by ``plan_synthesis_flow`` from current
state — the distillate must NOT bake them in.
"""

from __future__ import annotations

from argosy.agents.base import BaseAgent
from argosy.agents.plan_distiller_types import PlanDistillate

EXCLUSION_LIST = [
    "current portfolio percentages (66% NVDA today, 19% defensive, etc.)",
    "current FX rates (e.g. 3.09 NIS/USD)",
    "specific dollar amounts at a point in time (e.g. $430k proceeds, $171.81/share)",
    "dated tranche schedules (Q1 2026 sells 2,500 shares)",
    "share counts (12,748 NVDA shares, etc.)",
    "implementation roadmap 'next 30/90 days' sections — those belong "
    "in the synthesized short-horizon plan, not in the distillate",
]


class PlanDistillerAgent(BaseAgent[PlanDistillate]):
    """Extracts a durable structured distillate from a plan markdown."""

    agent_role = "plan_distiller"
    output_model = PlanDistillate
    # Citations not required — the source IS the user's plan, not an
    # external authority. Each extracted item still carries
    # ``source_section`` so the UI can click-through to the heading.
    require_citations = False
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8192).

    def build_prompt(
        self,
        *,
        plan_label: str,
        plan_markdown: str,
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Wave A: returns ``(system, user, sources)``. The plan markdown body
        is attached as a Citations API document block titled
        ``plan/baseline_markdown`` so the model's output can carry
        character-offset citations back into the plan text. The user prompt
        references the document source_id instead of inlining the plan body.
        """
        exclusions = "\n".join(f"  - {item}" for item in EXCLUSION_LIST)
        system = (
            "You are the plan-distiller agent on the Argosy fleet.\n\n"
            "Your job: extract a DURABLE, structured distillate from the "
            "user's imported plan. The distillate is the only representation "
            "of the baseline that downstream synthesis ever consumes; the "
            "raw plan stays available for forensic lookups, but is NOT "
            "injected into agent prompts.\n\n"
            "The plan markdown is attached as a document block titled "
            "`plan/baseline_markdown`; cite that source_id for every "
            "extracted item via ``source_section`` plus any cited_sources.\n\n"
            "WHAT TO EXTRACT (durable):\n"
            "  - goals: retirement target year, target annual income, FI status, "
            "    employment horizon, lifestyle aspirations\n"
            "  - principles: investment philosophy (UCITS-first for estate "
            "    safety, real-returns framework, NIS-USD natural hedge, "
            "    concentration is the load-bearing risk)\n"
            "  - risk_priorities: ordered list of top risks the user cares "
            "    about; the first item dominates\n"
            "  - decision_rules: actionable rules the user has committed to "
            "    (bracket-aware RSU sales, gap-weighted deployment, no "
            "    Defensive above cap, never panic-convert)\n"
            "  - targets: numeric targets WITH explicit stated_at + "
            "    revisit_after dates; treat them as working assumptions, "
            "    not eternal truths\n"
            "  - constraints: things the user has explicitly opted in/out "
            "    of (no consolidate brokers, UCITS preferred, speculation "
            "    cap)\n"
            "  - stress_tolerance: free text on willingness to ride "
            "    drawdowns / sequence-of-returns risk tolerance\n\n"
            "EXPLICIT EXCLUSIONS — DO NOT EXTRACT (these decay; the "
            "monthly synthesis flow will derive them fresh from current "
            "state):\n"
            f"{exclusions}\n\n"
            "PROVENANCE: every extracted item must carry a ``source_section`` "
            "pointing to the plan heading or sub-heading where it appears, "
            "so the UI can click-through. Use the plan's own heading text.\n\n"
            "DO NOT INFER. If a category has no clear evidence in the plan, "
            "leave the list empty. The user can fill gaps conversationally.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{PlanDistillate.model_json_schema()}\n"
        )

        user = (
            f"PLAN LABEL: {plan_label}\n\n"
            "The plan body is attached as document block "
            "`plan/baseline_markdown`. Distill it now.\n\n"
            "Produce the PlanDistillate JSON now. Respect the exclusion "
            "list strictly: if the plan says 'NVDA is currently 66%', "
            "you do NOT record 66% as a target. You may record the "
            "stated target value (e.g. 15%) since that is durable."
        )
        sources: list[tuple[str, str]] = [
            ("plan/baseline_markdown", plan_markdown),
        ]
        return system, user, sources


__all__ = ["PlanDistillerAgent", "EXCLUSION_LIST"]
