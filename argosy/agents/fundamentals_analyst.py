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
from argosy.services.decision_integrity.as_of import format_field_for_prompt


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


# Stable render order for the per-ticker payload source documents.
# Anchors first (the analyst needs price + EPS / share count to derive a
# per-share fair value), then ratios / growth / quality, then metadata.
# Keys NOT in this list still render (sorted, after these) — see
# build_prompt.
_PAYLOAD_KEY_ORDER: tuple[str, ...] = (
    "current_price",
    "market_cap",
    "market_cap_m",
    "shares_outstanding",
    "eps_ttm",
    "eps_forward",
    "pe_ratio",
    "pe_ratio_ttm",
    "pe_normalized_annual",
    "forward_pe",
    "peg_ratio",
    "ev_ebitda",
    "revenue_ttm",
    "revenue_growth_yoy",
    "revenue_per_share_ttm",
    "net_income_ttm",
    "earnings_growth_yoy",
    "free_cashflow",
    "dividend_yield",
    "payout_ratio",
    "gross_margin_ttm",
    "operating_margin_ttm",
    "net_margin_ttm",
    "debt_to_equity",
    "return_on_equity",
    "52w_high",
    "52w_low",
    "beta",
    "sector",
    "industry",
    "source_url",
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

    #: Live web access (decision_runs 127/128 finding): payload-only
    #: reasoning missed the ELF China-tariff margin hit and the CELH
    #: Texas AG probe. A few targeted WebSearch queries let the analyst
    #: catch catalysts (earnings, regulatory/legal, tariffs, M&A,
    #: guidance) the pre-gathered feed didn't carry. Payload metrics
    #: remain the arithmetic ground truth. WebFetch NOT enabled — see
    #: NewsAnalystAgent for rationale.
    claude_code_allowed_tools: tuple[str, ...] = ("WebSearch",)

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
                anchors — current_price, eps_ttm, eps_forward,
                shares_outstanding, market_cap / market_cap_m,
                revenue_ttm, net_income_ttm, free_cashflow; ratios —
                pe_ratio, peg_ratio, ev_ebitda, revenue_growth_yoy,
                earnings_growth_yoy, debt_to_equity, dividend_yield;
                plus source_url (the SEC filing or yfinance reference).
                Unknown keys render too (sorted, after the known set).

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
            "  - DATA QUALITY — be CONSERVATIVE about flagging staleness. "
            "**Trust the supplied data feed**. Do NOT flag a price as "
            "stale based on what you 'remember' the ticker trading at — "
            "stock splits, corporate actions, and market moves are real, "
            "and your training data ages. A 'low' or 'unfamiliar' price "
            "for a ticker IS NOT evidence of staleness. Only flag a "
            "remediation_request when you can DEMONSTRATE an internal "
            "inconsistency in the supplied data itself:\n"
            "    * ``market_cap`` is present AND ``shares_outstanding`` is "
            "present AND ``current_price`` is present, AND "
            "``abs(market_cap / shares_outstanding - current_price) / "
            "current_price > 0.10`` (the implied price diverges from the "
            "reported price by more than 10%). This means the fields "
            "are internally inconsistent — one of them is wrong. Emit "
            "``kind='price_stale'``.\n"
            "    * The entire ``fundamentals_payload`` for a ticker is "
            "empty (no PE, no growth, no market cap). Emit "
            "``kind='fundamentals_stale'``.\n"
            "If you cannot DEMONSTRATE one of these conditions with the "
            "supplied data, do NOT emit a remediation_request. Trust "
            "the inputs and proceed with the analysis. The whole point "
            "of having a structured data feed is that it is the source "
            "of truth — your training data is not.\n"
            "When you DO emit a remediation_request, populate it on the "
            "report's ``remediation_requests`` list — never write the "
            "recommendation into ``summary`` prose. Include the affected "
            "ticker and a one-sentence reason citing the specific "
            "inconsistency you observed. Use ``kind='data_integrity'`` "
            "for market_cap/price divergence (or other demonstrated "
            "internal inconsistency) and ``kind='price_stale'`` / "
            "``kind='fundamentals_stale'`` for the cases above. These "
            "requests are persisted as BLOCKING rows — do not rely on "
            "prose alone.\n"
            "  - AS-OF LABELS: numeric payload fields may arrive as "
            "``<value> (as of YYYY-MM-DD)``. Preserve that as_of label "
            "in ``notes`` / ``summary`` whenever you cite the figure. "
            "Never present a Q1 (or earlier) figure as current when an "
            "as_of label shows it predates a later earnings release.\n"
            "  - WEB SEARCH: you have the WebSearch tool. You SHOULD run "
            "1-3 targeted web searches for MATERIAL recent developments "
            "on the tickers in scope — earnings surprises, regulatory or "
            "legal actions, tariffs / trade policy, M&A, guidance changes "
            "— but ONLY when the attached payload is thin or clearly does "
            "not explain the valuation picture (e.g. a multiple that "
            "looks dislocated with no attached context). If the attached "
            "payload already covers the story for a ticker, do NOT spend "
            "a search on it. You MUST cite the URL of any web finding "
            "you use — web URLs join `cited_sources` exactly like "
            "`fundamentals/<TICKER>` payload sources. The attached "
            "payload metrics remain the ARITHMETIC GROUND TRUTH: web "
            "findings supply CONTEXT and catalysts (put them in `notes` "
            "/ `summary` and let them shape confidence), never numbers "
            "you compute a fair-value estimate from, and you must NEVER "
            "fabricate figures. If a search returns nothing material, "
            "say so briefly rather than padding.\n\n"
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
            rendered: set[str] = set()
            # Known keys first, in a stable analyst-friendly order —
            # anchors (price / EPS / share count / absolute financials)
            # ahead of ratios. Then ANY remaining payload keys, sorted,
            # so upstream gatherers never get silently dropped here
            # (the old fixed whitelist swallowed eps_ttm / market_cap /
            # free_cashflow — decision_run 126's "no per-share anchor").
            for key in _PAYLOAD_KEY_ORDER:
                if key in data and data[key] is not None:
                    lines.append(f"  - {key}: {format_field_for_prompt(data, key)}")
                    rendered.add(key)
            for key in sorted(data):
                if key not in rendered and data[key] is not None:
                    lines.append(f"  - {key}: {format_field_for_prompt(data, key)}")
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
