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
    ) -> tuple[str, str]:
        tier = tier or self.tier

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
