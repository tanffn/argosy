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


class TraderProposal(BaseModel):
    """Concrete proposal produced by the trader.

    Mirrors SDD Appendix B.3 schema exactly.
    """

    ticker: str
    action: Literal["buy", "sell", "hold"]
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
        description="2-3 sentence rationale, with a citation to the debate "
        "outcome or the analyst report driving the call."
    )
    expected_impact: ExpectedImpact = Field(default_factory=ExpectedImpact)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Citations from analyst reports / debate outcome / "
        "domain_knowledge files. Required.",
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
                model = "claude-opus-4-7"
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
                "  - Cite analyst reports that drive the call. Citations "
                "are required.\n"
                "  - **CONFLICT OVERRIDE**: if the bull/bear debate "
                "outcome or any analyst text uses tactical-timing "
                "language (MACD, MA-cross, entry-confirmation, "
                "stop-loss placement, FX-hedge gating), these long-hold "
                "rules OVERRIDE that language. Do not let upstream "
                "tactical framing pull your verdict toward HOLD-on-"
                "chart-conditions reasoning.\n\n"
                "OUTPUT must be a JSON object conforming to this schema:\n"
                f"{TraderProposal.model_json_schema()}\n"
            )
        else:
            system = (
                "You are the trader on the Argosy fleet. You synthesize analyst "
                "reports and the researcher debate outcome into a concrete "
                "proposal.\n\n"
                "Rules:\n"
                "  - Never invent prices or sizes; derive them from the inputs.\n"
                "  - If you cannot produce a confident proposal, return "
                "`action='hold'` with a cited explanation.\n"
                "  - Cite the analyst report and/or debate-outcome lines that "
                "drive the call.\n"
                "  - For limit/stop orders, set the corresponding price field; "
                "for market orders, leave both null.\n\n"
                "OUTPUT must be a JSON object conforming to this schema:\n"
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
