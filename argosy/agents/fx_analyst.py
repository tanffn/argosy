"""FX analyst agent (SDD §3.1, Phase 7).

Inputs: USD/NIS/EUR time series from FRED + Bank of Israel (already
fetched by the caller). Output: `FXReport` with current levels, recent
trend direction, FX-aware position-sizing notes, and hedging
recommendations. **Haiku** — cheap, deterministic-ish.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand

TrendDirection = Literal["strengthening", "weakening", "flat"]


class PairLevels(BaseModel):
    pair: str = Field(description="e.g., 'USD/NIS', 'USD/EUR', 'EUR/NIS'.")
    spot: float
    trend_30d: TrendDirection = "flat"
    pct_change_30d: float = 0.0
    pct_change_90d: float = 0.0
    cited_sources: list[str] = Field(default_factory=list)


class FXReport(BaseModel):
    pairs: list[PairLevels] = Field(default_factory=list)
    position_sizing_notes: list[str] = Field(
        default_factory=list,
        description="One-line FX-aware position-sizing observations "
        "(e.g., 'NIS-denominated wages → smaller USD purchases this month').",
    )
    hedging_recommendations: list[str] = Field(
        default_factory=list,
        description="Optional; e.g., 'Consider 50% hedge on EUR rental income "
        "given EUR weakening 4% in 90d'.",
    )
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(default_factory=list)


class FXAnalystAgent(BaseAgent[FXReport]):
    """Haiku-class FX analyst. Reads pre-fetched FX time series."""

    agent_role = "fx"
    output_model = FXReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        fx_payload: dict[str, dict[str, float]],
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            fx_payload: dict of {pair: {spot, pct_change_30d,
                pct_change_90d, source}}, e.g.:
                {"USD/NIS": {"spot": 3.65, "pct_change_30d": -1.2,
                             "pct_change_90d": 0.4,
                             "source": "fred:DEXISUS"}}
        """
        system = (
            "You are the FX analyst on the Argosy fleet. The caller has "
            "fetched recent USD/NIS, USD/EUR, and EUR/NIS data. Your job is "
            "to characterize the trend and produce FX-aware position-sizing "
            "notes for an Israeli-resident multi-currency investor.\n\n"
            "Rules:\n"
            "  - trend_30d='strengthening' if 30d move >= +1% (the base "
            "currency, e.g. USD in USD/NIS, has strengthened); "
            "'weakening' if <= -1%; else 'flat'.\n"
            "  - Cite the source for each pair (e.g., 'fred:DEXISUS', "
            "'boi:USD_NIS_DAILY').\n"
            "  - Hedging recommendations are optional; do not propose them "
            "unless the 90d move exceeds 3% in either direction.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{FXReport.model_json_schema()}\n"
        )

        if fx_payload:
            lines = []
            for pair, data in sorted(fx_payload.items()):
                lines.append(
                    f"  - {pair}: spot={data.get('spot')}; "
                    f"30d={data.get('pct_change_30d')}%; "
                    f"90d={data.get('pct_change_90d')}%; "
                    f"source={data.get('source', '')}"
                )
            block = "\n".join(lines)
        else:
            block = "  (no FX data supplied)"

        user = (
            "FX TIME-SERIES SNAPSHOT:\n"
            f"{block}\n\n"
            "Produce an FXReport JSON now."
        )
        return system, user


__all__ = ["FXAnalystAgent", "FXReport", "PairLevels", "TrendDirection"]
