"""Fund/ETF vehicle analyst agent (collective-instrument verdict path).

Gap this closes: the per-stock fleet (fundamentals / news / sentiment /
technical) asks equity questions (PE ratio, moat, earnings, insider flow)
that are MEANINGLESS for an ETF or index fund. This agent asks the CORRECT
fund-vehicle questions so collective instruments can earn a settled verdict
with defensible falsifiers, not a stock-shaped misfire.

Five questions the agent must address:

  1. DOMICILE — identify the vehicle's domicile, not its holdings' countries.
     Apply the supplied household tax status and accepted constraints; Israeli
     residency alone does not establish non-US-person status.

  2. TER / TOTAL EXPENSE RATIO — Is the fund cheap relative to its mandate
     and available alternatives?  If the exact TER is not in the supplied
     packet, the agent MUST state that and produce a LOW-conviction input
     for that dimension rather than fabricating a number.

  3. INDEX / MANDATE FIT — Does the fund's index still implement the sleeve
     role the plan assigned it (e.g. FWRA covers "global equity" but is 62%
     US — does that still serve the plan's intended diversification)?

  4. NVDA LOOK-THROUGH — use dated fund-specific holdings, funding and the
     actual portfolio denominator. Absolute indirect exposure can increase
     while portfolio concentration falls. Incidental exposure is not a veto.

  5. OVERLAP — Does this fund duplicate exposure already provided by another
     sleeve?  Given FWRA (62% US) + CSPX (100% US) both in the book, the
     non-US diversification benefit of FWRA is diluted; the agent should name
     the overlap instruments if they are in the household context.

The agent outputs a ``FundVehicleReport`` that carries:
  - a HOLD / TRIM / SELL verdict (BUY is not in scope for a fund-vehicle
    review — the plan controls position sizing separately);
  - conviction (HIGH / MEDIUM / LOW) — LOW when material data is missing;
  - reasoning_md — 2-4 sentence advisor-voice paragraph;
  - falsifiers — concrete sentences that, if TRUE, would invalidate this verdict;
  - revisit_triggers — typed trigger structures (metric_condition, dated_event).

Falsifiers and revisit_triggers must be CONCRETE (e.g. "tracking difference
exceeds 20 bps over 12m vs the index" is concrete; "performance deteriorates"
is degenerate and will not pass the verdict registry's substance floor).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class FundVehicleReport(BaseModel):
    """Structured fund/ETF vehicle verdict from the analyst."""

    # Defaulted, NOT required. We hand the ticker to the agent; making the model
    # echo it back turns a perfectly good analysis into a schema error. The live
    # FWRA run failed exactly this way — the agent returned a reasoned TRIM and
    # it was discarded because the envelope lacked a field the caller already
    # knew. The orchestrator stamps the authoritative value after validation.
    ticker: str = Field(default="", description="Fund ticker / identifier analysed.")
    verdict: str = Field(
        description="HOLD | TRIM | SELL. BUY is not in scope for a vehicle review."
    )
    conviction: ConfidenceBand = ConfidenceBand.LOW
    reasoning_md: str = Field(
        default="",
        description=(
            "2-4 sentence advisor-voice explanation of the verdict. "
            "Must reference the domicile assessment, the index-fit judgment, "
            "and any data gaps. Do NOT invent TER or tracking-error numbers "
            "that were not in the input packet or actually retrieved primary evidence."
        ),
    )
    domicile_ok: bool = Field(
        description=(
            "True when the fund's domicile is UCITS (Irish/EU) or otherwise "
            "not US-situs for this NRA household. False for US-domiciled funds."
        ),
    )
    ter_known: bool = Field(
        default=False,
        description="True only when TER was supplied or verified in a retrieved dated primary document.",
    )
    ter_bps: float | None = Field(
        default=None,
        description=(
            "Total expense ratio in basis points, IF known from the input. "
            "NULL if neither supplied nor verified in a retrieved dated primary document — do NOT fabricate."
        ),
    )
    nvda_lookahead_weight_pct: float | None = Field(
        default=None,
        description=(
            "Estimated NVDA look-through as % of THIS fund's NAV "
            "(e.g. if the fund tracks S&P 500 and NVDA is ~5.5% of S&P, "
            "this is 5.5). NULL if the fund index is not known or NVDA weight "
            "is not derivable."
        ),
    )
    overlap_instruments: list[str] = Field(
        default_factory=list,
        description=(
            "Other household holdings that duplicate this fund's core exposure "
            "(e.g. XZEW for a second S&P tracker)."
        ),
    )
    falsifiers: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete falsifier sentences. Each must name a measurable threshold "
            "or a specific event. Example: 'Tracking difference to FTSE All-World "
            "exceeds 20 bps over any 12-month window.' Minimum 2 non-degenerate "
            "falsifiers required for HIGH/MED conviction."
        ),
    )
    revisit_triggers: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Typed trigger objects. Each must have 'kind' in "
            "{price_below, price_above, metric_condition, dated_event}. "
            "Example: {kind: dated_event, label: 'annual TER review', "
            "date: '2027-01-01'}."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.LOW
    cited_sources: list[str] = Field(
        default_factory=list,
        description="domain_knowledge/ file paths or external URLs cited.",
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "Dimensions where the agent lacked input data and could not "
            "derive a value (e.g. 'TER not supplied', 'AUM unknown'). "
            "Listed so the caller knows which dimensions need data enrichment."
        ),
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FundVehicleAnalystAgent(BaseAgent[FundVehicleReport]):
    """Opus-class fund/ETF vehicle analyst.

    Receives a pre-assembled context packet (fund metadata, plan role,
    position context, domain_knowledge excerpts) and produces a structured
    fund-vehicle verdict: HOLD / TRIM / SELL with concrete falsifiers and
    revisit triggers.

    Judgment belongs to the agent (LLM-team architecture). The caller
    assembles the packet; this agent interprets it.
    """

    agent_role = "fund_vehicle_analyst"
    output_model = FundVehicleReport
    require_citations = True
    use_structured_output = True
    # Adaptive thinking at "high" effort — same band as the trader/risk_officer.
    # Fund-vehicle judgments involve multi-dimensional reasoning (domicile +
    # mandate fit + overlap + concentration look-through) and have direct
    # portfolio consequence (leading to a settled verdict).
    claude_code_allowed_tools: tuple[str, ...] = ("WebSearch", "WebFetch")
    claude_code_max_turns = 10
    claude_code_keep_tool_stream_open = True
    claude_code_public_documents = True

    def build_prompt(
        self,
        *,
        ticker: str,
        fund_context: dict[str, Any],
        domain_knowledge: str = "",
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the fund-vehicle analysis prompt.

        Args:
            ticker: the fund symbol (upper-cased).
            fund_context: dict with all available fund facts. Expected keys
                (any subset OK, missing keys are noted as data_gaps):
                  - structure: "ETF" | "fund" | "bond" | "reit"
                  - asset_class: from instrument_reference
                  - sector: from instrument_reference (e.g. "Broad Index")
                  - region: from instrument_reference (e.g. "Global")
                  - estate_safe: bool from instrument_reference
                  - us_weight: float fraction of US equity (0..1), if known
                  - us_weight_source: provenance string
                  - plan_role: str describing the plan's stated role
                  - position_weight_pct: current book weight %
                  - position_usd_value: USD value
                  - other_book_holdings: list[str] of other ticker symbols
                  - ter_bps: float | None
                  - index_name: str | None
                  - domicile_country: str | None (e.g. "IE", "US")
            domain_knowledge: concatenated excerpts from relevant
                domain_knowledge/ files (estate_tax_nonresidents.md,
                nonresident_withholding.md, etc.). Attached as a source.

        Returns:
            (system, user, sources) tuple.
        """
        tk = (ticker or "").upper()
        ctx = fund_context or {}

        # Build a human-readable context block for the user prompt.
        ctx_lines = [f"FUND: {tk}"]
        for key, label in [
            ("structure", "Structure"),
            ("asset_class", "Asset class"),
            ("sector", "Sector/Style"),
            ("region", "Region"),
            ("estate_safe", "Estate-safe (not US-situs)"),
            ("domicile_country", "Domicile country"),
            ("index_name", "Index tracked"),
            ("us_weight", "US-equity weight (fraction)"),
            ("us_weight_source", "US-weight source"),
            ("ter_bps", "TER (basis points)"),
            ("plan_role", "Plan role"),
            ("position_weight_pct", "Current portfolio weight %"),
            ("position_usd_value", "Current USD value"),
            ("other_book_holdings", "Other holdings in book"),
            ("research_reserve_usd", "Shared pending-research reserve USD"),
            ("live_execution_evidence", "Live execution evidence with source timestamps"),
        ]:
            val = ctx.get(key)
            if val is not None:
                ctx_lines.append(f"  {label}: {val}")
            else:
                ctx_lines.append(f"  {label}: NOT SUPPLIED")
        context_block = "\n".join(ctx_lines)

        sources: list[tuple[str, str]] = [
            (f"fund_context/{tk}", context_block),
        ]
        if domain_knowledge.strip():
            sources.append(("domain_knowledge/tax", domain_knowledge))

        system = (
            "You are the fund-vehicle analyst on the Argosy investment fleet. "
            "Your job is to evaluate a COLLECTIVE INSTRUMENT (ETF, index fund, bond "
            "fund) and produce a structured verdict: HOLD, TRIM, or SELL. BUY is "
            "NOT in scope — position sizing is the plan's job.\n\n"
            "Derive household constraints and current holdings from the supplied "
            "context and attributed domain evidence. Do not infer US citizenship "
            "or tax status from Israeli residency. Distinguish instrument domicile "
            "from the countries of its underlying holdings. Missing facts remain "
            "data gaps, not guessed household facts.\n\n"
            "RULES:\n"
            "  - Cite every claim with a source id from the attached documents "
            "(fund_context/<TICKER> or domain_knowledge/tax) or an exact primary "
            "document URL actually retrieved, including its date.\n"
            "  - If TER / tracking-error / AUM / domicile is NOT in the input packet "
            "or verified in a dated primary document, "
            "add it to data_gaps and carry LOW confidence for that dimension — "
            "NEVER fabricate a number.\n"
            "  - DOMICILE: derive estate_safe from the supplied 'Estate-safe' flag "
            "and 'Domicile country'. Irish domicile (IE) → estate_safe=True. "
            "US domicile → estate_safe=False under the supplied non-US-person "
            "estate framework. A risk flag alone is not a sell mandate: assess "
            "documented constraints, accepted exposures and available alternatives.\n"
            "  - OVERLAP: if two holdings track the same or nearly the same index "
            "(e.g. FWRA + ACWD are both FTSE/MSCI All-World variants), flag the "
            "duplicates in overlap_instruments as ticker STRINGS only, never "
            "objects. Put overlap explanations in reasoning_md.\n"
            "  - NVDA look-through: use dated fund-specific holdings evidence. "
            "nvda_lookahead_weight_pct is NVDA as a percent of the fund's entire "
            "NAV; do not multiply an already whole-fund weight by US weight again. "
            "If unavailable, return null and a data gap, not a remembered index weight. "
            "Separate absolute dollars of indirect exposure from portfolio weight. "
            "An ETF containing NVDA can dilute concentration when funded from NVDA "
            "or when its NVDA weight is below the existing portfolio's. Evaluate "
            "the actual funding and before/after portfolio; incidental NVDA exposure "
            "alone is not grounds to reject a diversified fund. If funding or current "
            "portfolio exposure is absent, state that rather than inventing it.\n"
            "  - FALSIFIERS must be CONCRETE. 'Performance deteriorates' is "
            "degenerate. 'Tracking difference to its stated index exceeds 20 bps "
            "over any rolling 12-month window' is concrete.\n"
            "  - REVISIT TRIGGERS must be typed. Minimum: one dated_event trigger "
            "(e.g. annual TER review) and one metric_condition trigger where "
            "applicable.\n"
            "  - VERDICT logic:\n"
            "      HOLD: fund fits the plan mandate, domicile is OK, no better "
            "UCITS alternative is available, TER is competitive (or unknown).\n"
            "      TRIM: fund has a marginal issue (domicile tolerated per plan, "
            "or moderate overlap) but should be reduced rather than replaced.\n"
            "      SELL: an evidenced structural risk is incompatible with the "
            "documented household mandate, OR it materially duplicates another "
            "sleeve AND a better vehicle exists, "
            "OR it fails the plan mandate.\n"
            "  - CONVICTION:\n"
            "      HIGH: all five dimensions (domicile, TER, fit, overlap, NVDA "
            "look-through) are resolved from the packet or dated primary evidence.\n"
            "      MEDIUM: most dimensions resolved, some data gaps.\n"
            "      LOW: critical data missing (TER unknown, domicile ambiguous, "
            "plan role unclear).\n"
        )

        from argosy.agents.research_guidance import targeted_research_guidance
        system += targeted_research_guidance(ctx.get("research_question") or (
            "Resolve material missing fund facts using official issuer documents: "
            "domicile, current TER, mandate, dated holdings and tracking difference. "
            "Use the exact share class; distinguish tracking difference from tracking error."
        ))

        user = (
            f"Analyse fund/ETF {tk} using the supplied context and domain knowledge. "
            f"Produce a FundVehicleReport with: verdict, conviction, reasoning_md "
            f"(2-4 sentences, advisor voice), domicile_ok, ter_known, ter_bps (null "
            f"if neither supplied nor verified from dated primary evidence), "
            f"nvda_lookahead_weight_pct (null if not derivable), "
            f"overlap_instruments, falsifiers (≥2 concrete sentences), "
            f"revisit_triggers (typed), data_gaps, cited_sources.\n\n"
            f"Context is attached as source 'fund_context/{tk}'. "
            f"Domain knowledge is attached as source 'domain_knowledge/tax'.\n\n"
            f"Do NOT fabricate TER, AUM, or tracking-error numbers. "
            f"Missing data → add to data_gaps + lower conviction accordingly.\n\n"
            f"Return JSON matching this exact output schema:\n"
            f"{json.dumps(FundVehicleReport.model_json_schema(), ensure_ascii=False)}"
        )

        return (system, user, sources)


__all__ = ["FundVehicleAnalystAgent", "FundVehicleReport"]
