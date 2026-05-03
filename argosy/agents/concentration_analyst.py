"""Concentration analyst agent (SDD §3.1, Phase 2).

Inputs: positions snapshot summary + plan target weights + NVDA pace
data. Output: `ConcentrationReport` with breaches (vs caps) + per-class
deltas vs target + NVDA pace tracking. Haiku-class (deterministic-ish,
cheap).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class Breach(BaseModel):
    category: str = Field(description="e.g., 'NVDA' or 'Tech sector' or 'Single position cap'.")
    actual_pct: float = Field(description="Actual portfolio share, 0-100.")
    cap_pct: float = Field(description="Configured cap, 0-100.")
    severity: str = Field(
        default="warning",
        description="'warning' (over cap by <5pp) | 'breach' (>=5pp)",
    )
    note: str = Field(default="", description="One-line context.")


class NvdaPace(BaseModel):
    shares_sold_ytd: int = 0
    target_shares_ytd: int = 0
    delta_shares: int = Field(
        default=0,
        description="shares_sold_ytd - target_shares_ytd; negative means behind plan.",
    )
    on_track: bool = True


class ConcentrationReport(BaseModel):
    breaches: list[Breach] = Field(default_factory=list)
    deltas_vs_target: dict[str, float] = Field(
        default_factory=dict,
        description="Per-category {actual_pct - target_pct}; positive means over target.",
    )
    nvda_pace: NvdaPace = Field(default_factory=NvdaPace)
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.HIGH
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Plan / portfolio sources backing the deltas. Required for citation gate.",
    )


class ConcentrationAnalystAgent(BaseAgent[ConcentrationReport]):
    """Haiku-class concentration analyst. Cheap. Deterministic-ish."""

    agent_role = "concentration"
    output_model = ConcentrationReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        positions_summary: str,
        plan_targets: dict[str, float],
        nvda_shares_sold_ytd: int = 0,
        nvda_target_shares_ytd: int = 0,
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            positions_summary: human-readable summary text from the
                portfolio TSV ingest (`PortfolioSnapshot.summary_text()`).
            plan_targets: {category: target_pct} from the plan
                (e.g., 'NVDA': 15, 'Growth': 20).
            nvda_shares_sold_ytd: actual; from NVDA sale history.
            nvda_target_shares_ytd: plan target; from plan annual schedule.
        """
        system = (
            "You are the concentration analyst on the Argosy fleet. Your "
            "single job is to compute deltas vs plan and flag breaches.\n\n"
            "Rules:\n"
            "  - For each category in plan_targets, compute "
            "(actual - target). Report this in `deltas_vs_target`.\n"
            "  - A 'breach' is over-cap by ≥5 percentage points. A "
            "'warning' is over by <5pp. Under-target is not a breach.\n"
            "  - Cite the snapshot source path and plan version for each "
            "claim.\n"
            "  - Compute NVDA pace as shares_sold_ytd minus target. on_track "
            "is True iff delta_shares >= 0.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{ConcentrationReport.model_json_schema()}\n"
        )

        target_lines = "\n".join(
            f"  - {cat}: target {pct}%" for cat, pct in sorted(plan_targets.items())
        ) or "  (no plan targets supplied)"

        user = (
            "PORTFOLIO SNAPSHOT SUMMARY:\n"
            f"{positions_summary}\n\n"
            "PLAN TARGETS (percent of portfolio):\n"
            f"{target_lines}\n\n"
            "NVDA PACE:\n"
            f"  shares_sold_ytd: {nvda_shares_sold_ytd}\n"
            f"  target_shares_ytd: {nvda_target_shares_ytd}\n\n"
            "Produce a ConcentrationReport JSON now."
        )
        return system, user


__all__ = [
    "Breach",
    "ConcentrationAnalystAgent",
    "ConcentrationReport",
    "NvdaPace",
]
