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


# Stable mapping from snapshot keys → (provider, series_id). Used to build
# Citations API source_ids of the form `macro/<PROVIDER>/<SERIES>`. Keys
# not in this map fall through to `macro/UNKNOWN/<key>` so the document
# block is still emitted (the model just gets a less informative title).
_SNAPSHOT_SOURCE_MAP: dict[str, tuple[str, str]] = {
    "vix":       ("FRED", "VIXCLS"),
    "fred_10y":  ("FRED", "DGS10"),
    # The synthesis input assembler fetches WTI (DCOILWTICO) and labels
    # it `oil_wti` (see inputs.py::_gather_macro_snapshot). Brent
    # (DCOILBRENTEU) is also valid if the fetcher ever switches; both
    # mappings live here so either label resolves to a real FRED series
    # rather than falling through to `macro/UNKNOWN/...`.
    "oil_wti":   ("FRED", "DCOILWTICO"),
    "oil_brent": ("FRED", "DCOILBRENTEU"),
    "dxy":       ("FRED", "DTWEXBGS"),
    "usd_nis":   ("BOI",  "USD_NIS"),
    "boi_rate":  ("BOI",  "POLICY_RATE"),
}


def _source_id_for(key: str) -> str:
    """Return the canonical source_id for a snapshot key.

    Known keys map through ``_SNAPSHOT_SOURCE_MAP`` to provider+series IDs;
    unknown keys keep the raw key under ``macro/UNKNOWN/`` so the document
    block is still emitted (the model just gets a less informative title).
    """
    if key in _SNAPSHOT_SOURCE_MAP:
        provider, series = _SNAPSHOT_SOURCE_MAP[key]
        return f"macro/{provider}/{series}"
    return f"macro/UNKNOWN/{key}"


class MacroAnalystAgent(BaseAgent[MacroReport]):
    """Sonnet-class macro analyst. Classifies risk regime + names drivers."""

    agent_role = "macro"
    output_model = MacroReport
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        macro_snapshot: dict[str, float],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Args:
            macro_snapshot: numeric snapshot. Expected keys (any subset OK):
                vix, usd_nis, boi_rate, fred_10y, oil_brent, dxy.

        Wave A: returns ``(system, user, sources)``. Each snapshot reading
        becomes a Citations API document block titled
        ``macro/<PROVIDER>/<SERIES>`` (e.g. ``macro/FRED/VIXCLS``) so the
        model's output can carry character-offset citations back into the
        per-indicator inputs. The user prompt references the document
        source_ids instead of inlining the numeric values.
        """
        system = (
            "You are the macro analyst on the Argosy fleet. Classify the "
            "current cross-asset regime (risk_on / neutral / risk_off) and "
            "name the top 2-4 drivers in one short bullet each.\n\n"
            "Rules:\n"
            "  - Cite the source for any specific numeric claim. The "
            "per-indicator readings are attached as document blocks titled "
            "`macro/<PROVIDER>/<SERIES>` (e.g. `macro/FRED/VIXCLS`, "
            "`macro/BOI/USD_NIS`); use those source_ids in `cited_sources`.\n"
            "  - Do not predict the future; characterize the present.\n"
            "  - If too few inputs are present to call a regime, set regime "
            "to 'neutral' and confidence to LOW.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{MacroReport.model_json_schema()}\n"
        )

        # Build one document source per snapshot reading. Each source body
        # is the indicator key + value (a single line); the source_id
        # carries the provider+series identifier so the model can cite
        # cleanly without the value being inlined in the user prompt.
        sources: list[tuple[str, str]] = []
        roster_lines: list[str] = []
        for k, v in sorted(macro_snapshot.items()):
            source_id = _source_id_for(k)
            sources.append((source_id, f"{k}: {v}"))
            roster_lines.append(f"  - {k}: see document `{source_id}`")

        roster_block = "\n".join(roster_lines) or "  (snapshot empty)"
        user = (
            "MACRO SNAPSHOT (per-indicator readings attached as document "
            "blocks; cite by source_id):\n"
            f"{roster_block}\n\n"
            "Produce a MacroReport JSON now."
        )
        return system, user, sources


__all__ = ["MacroAnalystAgent", "MacroReport"]
