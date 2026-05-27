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
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        fx_payload: dict[str, dict[str, float]],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Args:
            fx_payload: dict of {pair: {spot, pct_change_30d,
                pct_change_90d, source}}, e.g.:
                {"USD/NIS": {"spot": 3.65, "pct_change_30d": -1.2,
                             "pct_change_90d": 0.4,
                             "source": "fred:DEXISUS"}}

        Returns:
            ``(system, user, sources)`` where ``sources`` is a list of
            ``(source_id, content)`` document blocks — one per currency
            pair — keyed by ``fx/rates/<pair>`` (slash in pair preserved,
            e.g. ``fx/rates/USD/NIS``). The user prompt references the
            source_ids rather than inlining the payload bodies so the
            Citations API can attribute each per-pair claim back to its
            document block.
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
            "  - CITATION FORMAT (STRICT): Per-pair FX snapshots are "
            "attached as document blocks whose source_id is "
            "`fx/rates/<pair>` — the `fx/` bucket prefix is REQUIRED. "
            "Cite each pair using the FULL source_id (e.g. "
            "`fx/rates/USD/NIS`, NOT `rates/USD/NIS` and NOT `USD/NIS`). "
            "The same string must appear verbatim in both the per-pair "
            "`cited_sources` list and in any `response_text` mentions. "
            "Also include the vendor reference (e.g. `fred:DEXISUS`, "
            "`boi:USD_NIS_DAILY`) from each block's `source` field "
            "alongside the bucket-prefixed source_id.\n"
            "  - Hedging recommendations are optional; do not propose them "
            "unless the 90d move exceeds 3% in either direction.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{FXReport.model_json_schema()}\n"
        )

        sources: list[tuple[str, str]] = []
        for pair, data in sorted(fx_payload.items()):
            body = (
                f"pair: {pair}\n"
                f"spot: {data.get('spot')}\n"
                f"pct_change_30d: {data.get('pct_change_30d')}\n"
                f"pct_change_90d: {data.get('pct_change_90d')}\n"
                f"source: {data.get('source', '')}"
            )
            sources.append((f"fx/rates/{pair}", body))

        if sources:
            pair_refs = ", ".join(f"`{sid}`" for sid, _ in sources)
            user = (
                "PER-PAIR FX SNAPSHOTS are attached as document blocks: "
                f"{pair_refs}.\n\n"
                "When citing in `cited_sources` and `response_text`, use "
                "the FULL source_id WITH the `fx/` bucket prefix exactly "
                "as listed above (e.g. `fx/rates/USD/NIS`). Do NOT drop "
                "the `fx/` prefix.\n\n"
                "Produce an FXReport JSON now."
            )
        else:
            user = (
                "FX TIME-SERIES SNAPSHOT:\n"
                "  (no FX data supplied)\n\n"
                "Produce an FXReport JSON now."
            )
        return system, user, sources


__all__ = ["FXAnalystAgent", "FXReport", "PairLevels", "TrendDirection"]
