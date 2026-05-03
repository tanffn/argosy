"""Macro analyst agent (SDD §3.1, Phase 2).

Inputs: macro snapshot dict (VIX, USD/NIS, BoI rate, FRED 10Y, oil).
Output: `MacroReport` with regime classification + key drivers. Sonnet.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class MacroReport(BaseModel):
    regime: Literal["risk_on", "neutral", "risk_off"] = "neutral"
    drivers: list[str] = Field(
        default_factory=list,
        description="Short bullet labels for the top drivers of the regime call.",
    )
    key_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="The numeric values used in the call (echo the input snapshot).",
    )
    summary: str = Field(default="", description="Two-sentence narrative for the dashboard.")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Source URLs / file paths backing the regime call (e.g. FRED series links).",
    )


class MacroAnalystAgent(BaseAgent[MacroReport]):
    """Sonnet-class macro analyst. Classifies risk regime + names drivers."""

    agent_role = "macro"
    output_model = MacroReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        macro_snapshot: dict[str, float],
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            macro_snapshot: numeric snapshot. Expected keys (any subset OK):
                vix, usd_nis, boi_rate, fred_10y, oil_brent, dxy.
        """
        system = (
            "You are the macro analyst on the Argosy fleet. Classify the "
            "current cross-asset regime (risk_on / neutral / risk_off) and "
            "name the top 2-4 drivers in one short bullet each.\n\n"
            "Rules:\n"
            "  - Cite the source for any specific numeric claim "
            "(`fred:DGS10`, `fred:VIXCLS`, `boi:USD_NIS`, etc.).\n"
            "  - Do not predict the future; characterize the present.\n"
            "  - If too few inputs are present to call a regime, set regime "
            "to 'neutral' and confidence to LOW.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{MacroReport.model_json_schema()}\n"
        )

        snap_lines = "\n".join(
            f"  - {k}: {v}" for k, v in sorted(macro_snapshot.items())
        ) or "  (snapshot empty)"

        user = (
            "MACRO SNAPSHOT:\n"
            f"{snap_lines}\n\n"
            "Produce a MacroReport JSON now."
        )
        return system, user


__all__ = ["MacroAnalystAgent", "MacroReport"]
