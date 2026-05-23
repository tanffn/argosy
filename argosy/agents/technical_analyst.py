"""Technical analyst agent (SDD §3.1, Phase 7).

Inputs: pre-computed indicator dict per ticker (RSI, MACD, MA crossings,
support/resistance levels) computed by the caller using ta-lib-style
helpers. Output: `TechnicalReport` with per-ticker indicator echo +
signal classification (`entry|hold|exit`). **Haiku** — cheap,
deterministic-ish.

Per SDD §3.1: "agent role is interpretation, not calculation".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand

Signal = Literal["entry", "hold", "exit"]


class TickerTechnicals(BaseModel):
    ticker: str
    indicators: dict[str, float] = Field(
        default_factory=dict,
        description="Echo of the input indicator values used. Keys vary "
        "(rsi_14, macd, macd_signal, ma_50, ma_200, atr_14, ...).",
    )
    signal: Signal = "hold"
    rationale: str = Field(default="", description="One-sentence reasoning.")
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Source of OHLC data (e.g., 'yfinance:NVDA:1d').",
    )


class TechnicalReport(BaseModel):
    per_ticker: dict[str, TickerTechnicals] = Field(default_factory=dict)
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(default_factory=list)


class TechnicalAnalystAgent(BaseAgent[TechnicalReport]):
    """Haiku-class technical analyst. Classifies entry/hold/exit per ticker."""

    agent_role = "technical"
    output_model = TechnicalReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        tickers: list[str],
        indicators_payload: dict[str, dict[str, Any]],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Args:
            tickers: ordered list of tickers in scope.
            indicators_payload: per-ticker dict of pre-computed indicators.
                Expected keys (any subset OK): rsi_14, macd, macd_signal,
                ma_50, ma_200, ma_cross_50_200 ('golden'|'death'|'none'),
                atr_14, support, resistance, source.

        Wave A: returns ``(system, user, sources)``. Each ticker's
        pre-computed indicator block becomes a Citations API document
        with source_id ``indicators/{ticker}`` so the model's output
        can carry character-offset citations back into the inputs.
        Tickers with no indicators are mentioned inline in the user
        prompt (no source body to attach).
        """
        system = (
            "You are the technical analyst on the Argosy fleet. The caller "
            "has already computed RSI, MACD, moving averages, etc. — your "
            "job is to classify the ticker as entry / hold / exit and "
            "explain in one sentence.\n\n"
            "Heuristic guides (not hard rules; use judgement):\n"
            "  - RSI < 30 + bullish MACD cross → 'entry'.\n"
            "  - RSI > 70 + bearish MACD cross → 'exit'.\n"
            "  - Mixed signals → 'hold'.\n"
            "  - Death cross (50d below 200d) tilts toward 'exit' even on "
            "neutral RSI.\n\n"
            "Rules:\n"
            "  - Cite the OHLC data source on every per-ticker entry. "
            "The pre-computed indicators for each ticker are attached as "
            "document blocks titled `indicators/<ticker>`; cite those "
            "source_ids in `cited_sources` alongside the OHLC vendor "
            "reference (e.g. `yfinance:NVDA:1d`) from the `source` key.\n"
            "  - Echo the indicator values you used — do not invent new ones.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{TechnicalReport.model_json_schema()}\n"
        )

        sources: list[tuple[str, str]] = []
        missing: list[str] = []
        for t in tickers:
            data = indicators_payload.get(t, {})
            if not data:
                missing.append(t)
                continue
            lines = [f"## {t}"]
            for k, v in data.items():
                lines.append(f"  - {k}: {v}")
            sources.append((f"indicators/{t}", "\n".join(lines)))

        ticker_refs = ", ".join(f"`{sid}`" for sid, _ in sources)
        missing_line = (
            ("Tickers with no indicators payload (skip or hold): "
             + ", ".join(missing) + "\n\n")
            if missing else ""
        )
        user = (
            f"Tickers in scope: {', '.join(tickers) if tickers else '(none)'}\n\n"
            + missing_line
            + (
                "PRE-COMPUTED INDICATORS are attached as document blocks: "
                f"{ticker_refs}.\n\n"
                if ticker_refs
                else "(no pre-computed indicator documents attached)\n\n"
            )
            + "Produce a TechnicalReport JSON now. signal must be one "
            "of entry|hold|exit per ticker."
        )
        return system, user, sources


__all__ = [
    "Signal",
    "TechnicalAnalystAgent",
    "TechnicalReport",
    "TickerTechnicals",
]
