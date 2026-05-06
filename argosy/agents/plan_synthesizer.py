"""Plan synthesizer — Phase 3 of plan_synthesis_flow.

Inputs (assembled by the orchestrator):
  - baseline distillate (markdown)
  - prior current plan (markdown — or empty on first synthesis)
  - 9 analyst reports concatenated (text)
  - 3 debate outcomes (one per horizon)
  - portfolio snapshot summary
  - recent fills + decisions summary

Output: PlanSynthesisOutput (long, medium, short HorizonSections + inputs
provenance).

Default model: Opus. Per user preference (accuracy over cost), the
synthesizer is given the most capable model in the fleet.
"""

from __future__ import annotations

from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput


class PlanSynthesizerAgent(BaseAgent[PlanSynthesisOutput]):
    """Phase 3 of plan_synthesis_flow."""

    agent_role = "plan_synthesizer"
    output_model = PlanSynthesisOutput
    require_citations = True
    max_tokens = 16384

    def build_prompt(
        self,
        *,
        baseline_distillate_md: str,
        prior_current_md: str,
        analyst_reports_text: str,
        debate_outcomes_text: str,
        portfolio_snapshot_summary: str,
        recent_fills_summary: str,
    ) -> tuple[str, str]:
        system = (
            "You are the plan synthesizer on the Argosy fleet — Phase 3 of the "
            "monthly synthesis flow.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
            "Your job: produce three HorizonSection documents (long, medium, "
            "short) from the inputs below. The medium horizon is the strategic "
            "centerpiece — that is where the firm earns its fee. Long is mostly "
            "stable; short is mostly mechanical.\n\n"
            "Per-horizon character:\n"
            "  - long (5+ years): posture-heavy, few targets, directional "
            "    actions, status=no_change is the common case.\n"
            "  - medium (1-2 years): tactical targets, themed actions, "
            "    parameterized triggers (\"if VIX > 30: accelerate\").\n"
            "  - short (~30 days): dated, concrete, replaced every monthly "
            "    cycle. Includes speculative_candidates.\n\n"
            "STATUS values:\n"
            "  - no_change: nothing material moved; honest, evidence-backed.\n"
            "  - minor_revision: targets nudged or actions refined.\n"
            "  - major_revision: structural target/posture change.\n\n"
            "DELTAS: every change vs. the prior current plan must produce a "
            "Delta entry with a stable item_id (e.g. 'medium.targets.nvda'), "
            "rationale, and citations. Per-delta accept/reject relies on these.\n\n"
            "CITATIONS REQUIRED for every numeric or directional claim. Use "
            "the format `agent_report:<id>` for analyst evidence, "
            "`decision_run:<id>` for prior synthesis lineage, "
            "`domain_kb:<path>` for jurisdiction rules, "
            "`plan_section:<heading>` for baseline references, "
            "`prior_current:<id>` for diff context.\n\n"
            "OUTPUT must be a JSON object conforming to:\n"
            f"{PlanSynthesisOutput.model_json_schema()}\n"
        )

        usr = "\n\n".join([
            "=== BASELINE DISTILLATE ===\n" + (baseline_distillate_md or "(no baseline)"),
            "=== PRIOR CURRENT PLAN ===\n" + (prior_current_md or "(no prior current — first synthesis)"),
            "=== ANALYST REPORTS (Phase 1 outputs) ===\n" + analyst_reports_text,
            "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n" + debate_outcomes_text,
            "=== PORTFOLIO SNAPSHOT ===\n" + portfolio_snapshot_summary,
            "=== RECENT FILLS + DECISIONS (last 90 days) ===\n" + recent_fills_summary,
            "Produce the PlanSynthesisOutput JSON now. Honor the medium-horizon "
            "centerpiece framing. If status=no_change for a horizon, deltas_from_prior "
            "must be empty AND the rationale must explicitly justify why nothing changed.",
        ])
        return system, usr


__all__ = ["PlanSynthesizerAgent"]
