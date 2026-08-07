"""Trader agent (SDD §3.3, Appendix B.3, Phase 3).

Synthesizes analyst reports + the researcher debate outcome + positions
+ user constraints into a concrete `TraderProposal`. Default model is
Opus for T2/T3 (synthesis under contradiction) and Sonnet for T0/T1
(routine).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class ExpectedImpact(BaseModel):
    concentration_delta: str = Field(
        default="",
        description="Free-text describing concentration change, e.g., "
        "'NVDA share goes 68% → 65%'.",
    )
    cash_delta: str = Field(default="", description="Cash effect, e.g., '+$8.2K'.")
    tax_estimate: str = Field(
        default="",
        description="Free-text tax estimate, e.g., '~$1.6K Israeli CGT @25% on $6.3K LTCG'.",
    )


_FALSIFIER_RULE = (
    "  - **AUTHOR FALSIFIERS (required for every verdict, including HOLD).** "
    "Emit 2-4 ``falsifiers``: specific, checkable statements that would prove "
    "THIS thesis wrong and force a re-review, each tied to the ACTUAL reason "
    "for your verdict. Never generic ('stock drops', 'market falls') — a "
    "generic falsifier that any bearish headline would trip is WORSE than "
    "none, because it re-opens a DEFENDED position on noise. Examples: HOLD "
    "ORCL — 'TTM free cash flow turns sustainably positive with debt stable'; "
    "BUY grower — 'two consecutive quarters of gross-margin compression below "
    "55%'; SELL — 'dividend restored and coverage back above 1.3x'. For EACH "
    "mechanically-checkable falsifier, pair a typed ``revisit_trigger`` (see "
    "schema): a price level uses kind=price_below or price_above with a "
    "numeric price; a fundamental uses kind=metric_condition with "
    "metric/op/value (metric names like fcf_ttm, eps, revenue_growth, "
    "de_ratio, nrr); a catalyst date uses kind=dated_event with an ISO date "
    "and a label. A qualitative falsifier with no clean trigger is fine — "
    "omit it from revisit_triggers. A HOLD is a DEFENDED position: state what "
    "would move you off it. Never emit an empty falsifiers list.\n"
)


class RevisitTrigger(BaseModel):
    """One typed tripwire the fleet arms alongside a verdict.

    Serialized via ``model_dump(exclude_none=True)`` to the dict shape
    ``verdict_registry.write_verdict`` / ``evaluate_triggers`` read. The four
    ``kind`` values match ``verdict_registry.VALID_TRIGGER_KINDS`` exactly.
    """

    kind: Literal["price_below", "price_above", "metric_condition", "dated_event"]
    # price_below / price_above
    price: float | None = None
    # metric_condition
    metric: str | None = None
    op: Literal[">=", ">", "<=", "<", "=="] | None = None
    value: float | None = None
    # dated_event (ISO YYYY-MM-DD)
    date: str | None = None
    # shared human label (metric_condition / dated_event matching + UI)
    label: str | None = None


class TraderProposal(BaseModel):
    """Concrete proposal produced by the trader.

    Mirrors SDD Appendix B.3 schema exactly.
    """

    ticker: str
    action: Literal["buy", "sell", "hold", "insufficient_data"] = Field(
        description=(
            "buy / sell / hold are the standard verdicts. "
            "``insufficient_data`` (2026-05-31) is for cases where the "
            "trader cannot complete the analysis because load-bearing "
            "inputs are missing or flagged-unusable AFTER the orchestrator "
            "has already attempted remediation. Distinct from HOLD: HOLD "
            "means 'analysis completed, recommendation is to wait'; "
            "INSUFFICIENT_DATA means 'analysis aborted because we couldn't "
            "see what we needed'. Surfaces in the UI as a separate verdict "
            "state so the user knows the system didn't fail-soft into HOLD."
        ),
    )
    size_shares_or_currency: float = Field(
        description="Numeric size; interpret per `size_units`. For shares, "
        "this is share count; for currency, this is the notional in the "
        "proposal currency."
    )
    size_units: Literal["shares", "currency"] = "shares"
    instrument: Literal["stock", "etf", "option"] = "stock"
    order_type: Literal["market", "limit", "stop", "stop-limit"] = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"
    rationale_summary: str = Field(
        description="Rationale for the call, citing the debate outcome or "
        "the analyst report driving it. In LONG-HOLD mode this MUST be "
        "structured markdown: one section per line, blank line between "
        "sections, using bold labels in this order — '**Verdict:**', "
        "'**Quality read:**', '**Price read:**', '**Thesis fit:**', "
        "'**Data quality:**' (only if there are caveats), "
        "'**Recommendation:**' — ending with a final '**Sources:**' line "
        "carrying ALL citations (report names and URLs, comma-separated). "
        "Never inline bracket citations or raw URLs mid-sentence, and never "
        "one run-on paragraph. In tactical mode: 2-3 sentences."
    )
    expected_impact: ExpectedImpact = Field(default_factory=ExpectedImpact)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Citations from analyst reports / debate outcome / "
        "domain_knowledge files. Required.",
    )
    falsifiers: list[str] = Field(
        default_factory=list,
        description="2-4 specific, checkable statements that would prove THIS "
        "verdict wrong and force a re-review (required for every verdict, "
        "including HOLD). Thesis-specific — tied to the actual reason for the "
        "call — never generic ('price drops', 'market falls').",
    )
    revisit_triggers: list[RevisitTrigger] = Field(
        default_factory=list,
        description="Typed tripwires, ideally one per mechanically-checkable "
        "falsifier. Qualitative falsifiers may have no trigger.",
    )


class TraderAgent(BaseAgent[TraderProposal]):
    """Trader. Default Opus on T2/T3; Sonnet on T0/T1.

    The model defaults are picked at construction time using the `tier`
    kwarg, so a single class serves both regimes. Tests override
    `_call_model` and the model id to canned values either way.
    """

    agent_role = "trader"
    output_model = TraderProposal
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000).

    def __init__(
        self,
        *,
        user_id: str,
        tier: str = "T2",
        model: str | None = None,
    ) -> None:
        # Pick a sensible default per tier per SDD §3.3 if no override given.
        if model is None:
            t = (tier or "").upper()
            if t in ("T0", "T1"):
                model = "claude-sonnet-4-6"
            else:
                model = "claude-opus-4-8"
        super().__init__(user_id=user_id, model=model)
        self.tier = tier

    def build_prompt(
        self,
        *,
        analyst_reports: list[dict],
        debate_outcome: dict,
        positions_snapshot: str,
        user_constraints: str,
        tier: str | None = None,
        ticker: str = "",
        mode: Literal["tactical_trade", "long_hold"] = "tactical_trade",
    ) -> tuple[str, str]:
        """Build the trader's prompt.

        ``mode`` (2026-05-31, /consult long-hold variant):
        - ``tactical_trade`` (default) — original SDD §3.3 trader
          synthesizing analyst reports + debate into a concrete trade
          proposal with order_type / time_in_force / limit / stop.
          Weighs technical entry timing + FX sizing alongside
          fundamentals + news.
        - ``long_hold`` — long-horizon investor framing per
          [[user_long_hold_investor]]. Weighs thesis fit, dividend
          record, sector position, multi-year fundamentals; explicitly
          DOES NOT gate on MACD/RSI/MA-cross chart timing or FX
          hedging for USD-into-USD-stock decisions. Output schema is
          unchanged (still a ``TraderProposal``) but
          ``time_in_force=GTC`` is the natural default and
          ``order_type=market`` is preferred over limit/stop chart
          entries.
        """
        tier = tier or self.tier

        if mode == "long_hold":
            system = (
                "You are the trader on the Argosy fleet, evaluating an "
                "ad-hoc per-ticker consultation in LONG-HOLD MODE. The "
                "user is a long-horizon investor (5+ year intended "
                "holding) — they are NOT timing a trade. Your job is to "
                "answer: should this company be owned for the long term, "
                "and at what conviction?\n\n"
                "**WRITE FOR A NON-INVESTOR.** ``rationale_summary`` "
                "must be readable by someone who is NOT a Wall Street "
                "analyst. Rules:\n"
                "  - Spell out every acronym on first use, then use it: "
                "'price-to-earnings ratio (P/E)', 'enterprise value to "
                "EBITDA (EV/EBITDA) — a valuation multiple that adjusts "
                "for debt and non-cash items', 'debt-to-equity (D/E)', "
                "'return on equity (RoE)', etc.\n"
                "  - Translate jargon. Don't say 'multiple compression'; "
                "say 'the price would have to fall for earnings to "
                "justify it'. Don't say 'margin of safety'; say 'a "
                "buffer between what we'd pay and what we think it's "
                "actually worth — so we're protected if our estimate is "
                "wrong'.\n"
                "  - **Explain apparent contradictions in the data.** If "
                "revenue grew 22% but earnings only grew 2.3%, the "
                "rationale MUST explain why — that means costs grew "
                "faster than sales (margin compression), so the company "
                "is selling more but keeping less per sale. Don't "
                "assume the reader will spot the gap.\n"
                "  - Use short sentences.\n"
                "  - **FORMAT ``rationale_summary`` AS MARKDOWN "
                "SECTIONS.** Each section goes on its OWN line with a "
                "blank line between sections, in this order (omit a "
                "section only when you truly have nothing for it):\n"
                "      **Verdict:** the one-sentence call.\n\n"
                "      **Quality read:** how good the business is.\n\n"
                "      **Price read:** valuation vs a fair-value "
                "estimate and the buffer (or lack of one).\n\n"
                "      **Thesis fit:** how it fits the user's plan and "
                "existing book, when relevant.\n\n"
                "      **Data quality:** caveats — only if there are "
                "any.\n\n"
                "      **Recommendation:** what to do, concretely.\n\n"
                "      **Sources:** every citation, comma-separated.\n"
                "    NEVER one run-on paragraph.\n"
                "  - **CITATIONS GO IN THE FINAL ``**Sources:**`` LINE "
                "ONLY** — report names (e.g. 'fundamentals/TICKER') and "
                "URLs, comma-separated. Do NOT embed bracket citations "
                "or raw URLs mid-sentence inside the prose sections; "
                "the prose must read cleanly to a human.\n\n"
                "Rules:\n"
                "  - Weight fundamentals (PE, EV/EBITDA, dividend yield, "
                "RoE, debt/equity, revenue/earnings growth, free cash "
                "flow, sector position), durable competitive position, "
                "and long-horizon news (earnings trajectory, structural "
                "changes, regulatory shifts).\n"
                "  - DO NOT gate on chart timing. MACD crossings, RSI "
                "readings, MA-50 / MA-200 distances, ATR ranges, and "
                "other tactical-entry indicators are NOT relevant to a "
                "long-hold decision. If the technical analyst is in the "
                "analyst reports, treat its timing language as "
                "secondary context only.\n"
                "  - DO NOT cite FX direction as a hedging argument. The "
                "user holds USD and is allocating USD into a USD-listed "
                "equity; per-ticker FX exposure is a portfolio-level "
                "concern, not a per-decision entry signal. If the FX "
                "analyst is in the analyst reports, ignore its hedging "
                "recommendations.\n"
                "  - For BUY: emit ``order_type='market'``, "
                "``time_in_force='GTC'``, no ``limit_price``, no "
                "``stop_price``. Long-hold investors don't time entries.\n"
                "  - For HOLD: return ``action='hold'`` only if the "
                "fundamental thesis is broken or the company isn't a "
                "long-hold candidate — NOT because the chart hasn't "
                "confirmed an entry.\n"
                "  - For SELL: only if the thesis breaks (deteriorating "
                "fundamentals, dividend cut, sector decline, "
                "concentration cap exceeded).\n"
                "  - **INSUFFICIENT_DATA vs HOLD**: if you cannot "
                "compute a fair-value estimate AND that's the "
                "load-bearing missing piece for a long-hold decision "
                "(i.e. you'd need it to judge whether the entry price "
                "leaves a buffer against your estimate), return "
                "``action='insufficient_data'`` — NOT HOLD. The "
                "rationale must state plainly what specific input was "
                "missing or flagged unusable AFTER remediation, and "
                "what would unblock a real recommendation (e.g. "
                "'clean fundamentals payload from SEC EDGAR or a "
                "configured Finnhub key'). Reserve HOLD for cases "
                "where the analysis DID complete and the answer is "
                "'wait at this valuation' or 'keep the existing "
                "position'. HOLD says 'I evaluated and recommend "
                "patience'; INSUFFICIENT_DATA says 'I couldn't "
                "evaluate'.\n"
                "  - Cite analyst reports that drive the call. Citations "
                "are required — in ``cited_sources`` and the final "
                "``**Sources:**`` line, never mid-sentence.\n"
                "  - **CONFLICT OVERRIDE**: if the bull/bear debate "
                "outcome or any analyst text uses tactical-timing "
                "language (MACD, MA-cross, entry-confirmation, "
                "stop-loss placement, FX-hedge gating), these long-hold "
                "rules OVERRIDE that language. Do not let upstream "
                "tactical framing pull your verdict toward HOLD-on-"
                "chart-conditions reasoning.\n"
                "  - **NEVER RECOMMEND AGENT REFRESHES**. The "
                "orchestrator has already run remediation on flagged "
                "data quality issues BEFORE you see the analyst "
                "reports. If a piece of data is still missing or "
                "flagged, it means remediation was attempted and "
                "didn't resolve — produce a best-effort answer noting "
                "the specific limitation (e.g. 'fair-value estimate "
                "unavailable because the fundamentals payload remains "
                "incomplete after refresh'). Do NOT emit prose like "
                "'recommend the Domain Refresh agent re-pull X' or "
                "'have the news pipeline retry'. The fleet handles "
                "its own remediation internally — your job is to "
                "produce the verdict with whatever inputs landed.\n"
                "  - **HOLD WORDING**: HOLD is ambiguous for users who "
                "don't own the ticker. If ``positions_snapshot`` "
                "indicates the user does NOT hold this ticker, your "
                "HOLD rationale should explicitly say 'do not "
                "initiate a position' rather than 'hold the position'. "
                "If they do hold it, say 'keep the existing position'. "
                "If positions context is empty, default to the 'do "
                "not initiate' framing — /consult is most often used "
                "to evaluate new candidates.\n\n"
                + _FALSIFIER_RULE
                + "OUTPUT must be a JSON object conforming to this schema:\n"
                f"{TraderProposal.model_json_schema()}\n"
            )
        else:
            system = (
                "You are the trader on the Argosy fleet. You synthesize analyst "
                "reports and the researcher debate outcome into a concrete "
                "proposal.\n\n"
                "**WRITE FOR A NON-INVESTOR.** ``rationale_summary`` must be "
                "readable by someone who is NOT a Wall Street analyst. Spell "
                "out acronyms on first use ('P/E ratio', 'EV/EBITDA', 'D/E', "
                "'RoE'). Translate jargon. Explain apparent contradictions in "
                "the data (e.g. high revenue growth with low earnings growth "
                "means margin compression — costs grew faster than sales).\n\n"
                "Rules:\n"
                "  - Never invent prices or sizes; derive them from the inputs.\n"
                "  - If you cannot produce a confident proposal AND the "
                "inputs you have ARE complete enough to reason on, return "
                "`action='hold'` with a cited explanation.\n"
                "  - **INSUFFICIENT_DATA vs HOLD** (2026-05-31): if you "
                "cannot complete the analysis because load-bearing inputs "
                "are missing or flagged unusable AFTER remediation (e.g. "
                "price feed stale + indicators payload incomplete), "
                "return ``action='insufficient_data'`` — NOT HOLD. The "
                "rationale must state plainly what specific input was "
                "missing AFTER remediation. Reserve HOLD for completed "
                "analysis with a 'wait at this entry' recommendation. "
                "HOLD says 'I evaluated and recommend patience'; "
                "INSUFFICIENT_DATA says 'I couldn't evaluate'.\n"
                "  - Cite the analyst report and/or debate-outcome lines that "
                "drive the call.\n"
                "  - For limit/stop orders, set the corresponding price field; "
                "for market orders, leave both null.\n"
                "  - **NEVER RECOMMEND AGENT REFRESHES**. The orchestrator "
                "has already run remediation on flagged data quality "
                "issues BEFORE you see the analyst reports. If a piece "
                "of data is still missing or flagged, it means "
                "remediation was attempted and didn't resolve — produce "
                "a best-effort answer noting the specific limitation, "
                "and DO NOT emit prose like 'recommend the Domain "
                "Refresh agent re-pull X' or 'have the news pipeline "
                "retry'. The fleet handles its own remediation "
                "internally — your job is to produce the verdict with "
                "whatever inputs landed.\n\n"
                + _FALSIFIER_RULE
                + "OUTPUT must be a JSON object conforming to this schema:\n"
                f"{TraderProposal.model_json_schema()}\n"
            )

        report_blocks: list[str] = []
        for r in analyst_reports:
            role = r.get("agent_role") or r.get("role") or "?"
            payload = {k: v for k, v in r.items() if k not in ("agent_role", "role")}
            report_blocks.append(f"### Analyst: {role}\n{payload}")

        user = (
            f"Tier: {tier}\n"
            f"Ticker: {ticker or '(infer from analyst reports if unambiguous)'}\n\n"
            "USER CONSTRAINTS:\n"
            f"{user_constraints}\n\n"
            "POSITIONS SNAPSHOT:\n"
            f"{positions_snapshot}\n\n"
            "ANALYST REPORTS:\n\n"
            + "\n\n".join(report_blocks)
            + "\n\nDEBATE OUTCOME:\n"
            f"{debate_outcome}\n\n"
            "Produce the TraderProposal JSON now."
        )
        return system, user


__all__ = ["ExpectedImpact", "TraderAgent", "TraderProposal"]
