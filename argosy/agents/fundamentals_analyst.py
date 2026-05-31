"""Fundamentals analyst agent (SDD §3.1, Appendix B.1, Phase 7).

Inputs: per-ticker fundamentals payload (yfinance + SEC EDGAR derived
metrics, fed in via the cache adapter / dependency injection — same
pattern as the news analyst). Output: `FundamentalsReport` with one
`TickerFundamentals` entry per ticker (PE/PEG/EV-EBITDA, growth rates,
balance sheet quality, fair-value estimate, confidence). **Sonnet**.

The agent role is interpretation, NOT calculation. The caller (loop /
CLI) computes the metrics and hands them in; the agent reasons over
them. This mirrors the news analyst's payload-injection design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand
from argosy.agents.remediation import RemediationRequest


class TickerFundamentals(BaseModel):
    ticker: str
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    ev_ebitda: float | None = None
    revenue_growth_yoy: float | None = Field(
        default=None, description="Year-over-year revenue growth, decimal (0.10 = 10%)."
    )
    earnings_growth_yoy: float | None = None
    debt_to_equity: float | None = None
    balance_sheet_quality: str = Field(
        default="unknown",
        description="Short tag: 'strong' | 'adequate' | 'weak' | 'unknown'.",
    )
    fair_value_estimate_usd: float | None = Field(
        default=None,
        description="Per-share fair-value estimate driven by the supplied "
        "metrics; null if insufficient inputs.",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    notes: str = Field(default="")
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Per-ticker citations: yfinance / SEC EDGAR file paths or URLs.",
    )


class FundamentalsReport(BaseModel):
    """Top-level fundamentals report. One entry per ticker."""

    per_ticker: dict[str, TickerFundamentals] = Field(default_factory=dict)
    summary: str = Field(default="", description="One-paragraph narrative across the fleet.")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct citations.",
    )
    remediation_requests: list[RemediationRequest] = Field(
        default_factory=list,
        description=(
            "Structured requests back to the orchestrator when the "
            "input data has detectable quality issues (stale price, "
            "missing payload, etc.). The orchestrator dispatches each "
            "+ re-runs this analyst with fresh data — DO NOT write "
            "data-refresh recommendations into ``summary`` prose. "
            "Per [[feedback_agents_talk_to_each_other]] the fleet "
            "resolves these internally, never punts to the user."
        ),
    )


class FundamentalsAnalystAgent(BaseAgent[FundamentalsReport]):
    """Sonnet-class fundamentals analyst.

    Reads pre-computed per-ticker metrics (PE/PEG/EV-EBITDA, growth,
    balance-sheet quality flags) and produces a structured report with
    fair-value estimates + confidence per ticker.
    """

    agent_role = "fundamentals"
    output_model = FundamentalsReport
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        tickers: list[str],
        fundamentals_payload: dict[str, dict[str, Any]],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Args:
            tickers: ordered list of tickers in scope.
            fundamentals_payload: per-ticker dict carrying the metric
                inputs. Expected keys per ticker (any subset OK):
                pe_ratio, peg_ratio, ev_ebitda, revenue_growth_yoy,
                earnings_growth_yoy, debt_to_equity, current_price,
                source_url (the SEC filing or yfinance reference).

        Returns:
            ``(system, user, sources)``. Each ticker's pre-computed
            payload is emitted as a document source with id
            ``fundamentals/<TICKER>`` so the Citations API can attribute
            individual numeric claims back to it. Tickers absent from
            the payload contribute no source.
        """
        system = (
            "You are the fundamentals analyst on the Argosy fleet. You "
            "interpret pre-computed valuation metrics — you do NOT recompute "
            "them. For each ticker, classify balance-sheet quality, derive a "
            "fair-value estimate (anchored to the supplied multiples and "
            "growth), and report confidence per ticker.\n\n"
            "Rules:\n"
            "  - Cite the source (SEC EDGAR URL, yfinance reference, or "
            "domain_knowledge file) for every numeric claim you keep. The "
            "per-ticker payloads are attached as document sources with id "
            "`fundamentals/<TICKER>`; reference them by that id.\n"
            "  - If a ticker has no attached `fundamentals/<TICKER>` source, "
            "or the attached payload is missing data needed for an estimate, "
            "set `fair_value_estimate_usd=null` and `confidence=LOW`. Never "
            "fabricate a multiple that wasn't in the input.\n"
            "  - balance_sheet_quality: 'strong' (low D/E + ample liquidity), "
            "'adequate' (mid D/E), 'weak' (high D/E or thin liquidity), "
            "'unknown' if inputs are absent.\n"
            "  - DATA QUALITY: if the supplied price/payload looks "
            "materially inconsistent (e.g. current_price is stale or "
            "pre-split, fundamentals payload is empty), DO NOT write "
            "'recommend refresh' into ``summary`` prose. Instead emit a "
            "structured ``RemediationRequest`` on the report's "
            "``remediation_requests`` list — the orchestrator will "
            "re-fetch + re-run this analyst before any downstream agent "
            "sees the report. Use kind='price_stale' for stale prices, "
            "'fundamentals_stale' for missing/empty fundamentals, or "
            "'data_refresh' if uncertain. Include the affected ticker "
            "and a one-sentence reason.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{FundamentalsReport.model_json_schema()}\n"
        )

        sources: list[tuple[str, str]] = []
        missing: list[str] = []
        for t in tickers:
            data = fundamentals_payload.get(t, {})
            if not data:
                missing.append(t)
                continue
            lines: list[str] = []
            for key in (
                "pe_ratio",
                "peg_ratio",
                "ev_ebitda",
                "revenue_growth_yoy",
                "earnings_growth_yoy",
                "debt_to_equity",
                "current_price",
                "source_url",
            ):
                if key in data:
                    lines.append(f"  - {key}: {data[key]}")
            sources.append((f"fundamentals/{t}", "\n".join(lines)))

        ref_list = (
            ", ".join(sid for sid, _ in sources) if sources else "(none)"
        )
        missing_line = (
            ""
            if not missing
            else (
                "\n\nNo fundamentals payload was attached for: "
                f"{', '.join(missing)}. Emit per-ticker entries for them with "
                "`fair_value_estimate_usd=null`, `balance_sheet_quality='unknown'`, "
                "and `confidence=LOW`."
            )
        )

        user = (
            f"Tickers in scope: {', '.join(tickers) if tickers else '(none)'}\n"
            f"Attached fundamentals sources: {ref_list}\n\n"
            "The per-ticker pre-computed metrics are attached as document "
            "sources (one per ticker). Treat them as data already computed by "
            "the ingestion layer — do NOT recompute. Cite the matching "
            "`fundamentals/<TICKER>` source on every per-ticker entry that "
            "carries any numeric data."
            f"{missing_line}\n\n"
            "Produce a FundamentalsReport JSON now."
        )
        return system, user, sources


__all__ = [
    "FundamentalsAnalystAgent",
    "FundamentalsReport",
    "TickerFundamentals",
]
