"""Allocation-author output -> canonical validated order sheet.

The author owns judgment.  This module owns only the deterministic projection:
live facts into shares/notional, current holdings into BUY-vs-ADD and portfolio
weight, and exact funding conservation.  It never picks or substitutes a symbol.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from argosy.services.after_tax import AfterTaxSale
from argosy.services.allocation_author.proposal import AllocationProposal
from argosy.services.order_sheet import (
    ConstraintCost,
    DatedCatalyst,
    ExecutionClip,
    FundingSummary,
    MarketEvidence,
    NoActionLine,
    OrderAction,
    OrderLine,
    OrderSheet,
    OutcomeExpectation,
    ReviewResolution,
    SheetValidation,
    StagedExecutionPlan,
    VoiceVerdict,
    validate_order_sheet,
)


class ExecutionFacts(BaseModel):
    """Live/sourced facts for one selected symbol."""

    evidence: MarketEvidence
    venue: str = Field(min_length=1)
    instrument_type: Literal["stock", "etf", "option"] = "stock"
    estate_situs: Literal["US", "non_US", "unknown"] = "unknown"
    constraint_costs: list[ConstraintCost] = Field(default_factory=list)
    quantity_increment: float = Field(default=1.0, gt=0)


class BuiltOrderSheet(BaseModel):
    sheet: OrderSheet
    validation: SheetValidation


def build_order_sheet(
    proposal: AllocationProposal,
    *,
    user_id: str,
    new_cash_usd: float,
    holdings_usd: dict[str, float],
    book_usd: float,
    facts_by_symbol: dict[str, ExecutionFacts],
    no_action: list[NoActionLine],
    discovered_symbols: set[str] | None = None,
    voices_by_symbol: dict[str, list[VoiceVerdict]] | None = None,
    sale_resolutions: dict[str, AfterTaxSale] | None = None,
    staged_sell_policies: dict[str, dict] | None = None,
    horizon_years: tuple[int, int] = (1, 5),
    freshness_days: int = 3,
    generated_at: datetime | None = None,
    review_resolution: ReviewResolution | None = None,
) -> BuiltOrderSheet:
    """Build and validate one buy/sell sheet without inventing missing facts.

    Missing author intent or execution facts is a hard construction error.  The
    caller should surface it as an unavailable order sheet, never fall back to a
    prose allocation that looks executable.
    """

    generated = generated_at or datetime.now(UTC)
    held = {str(k).upper(): float(v) for k, v in holdings_usd.items()}
    discovered = {s.upper() for s in (discovered_symbols or set())}
    voice_map = {k.upper(): v for k, v in (voices_by_symbol or {}).items()}
    sale_map = {k.upper(): v for k, v in (sale_resolutions or {}).items()}
    staged_map = {
        k.upper(): v for k, v in (staged_sell_policies or {}).items()
    }
    gross_sells = sum(float(row.amount_usd) for row in proposal.sells)
    sell_tax = 0.0
    sell_costs = 0.0
    for sell in proposal.sells:
        symbol = sell.symbol.strip().upper()
        resolution = sale_map.get(symbol)
        if resolution is None or not resolution.eligible or resolution.tax is None:
            reasons = "; ".join(resolution.failures) if resolution else "missing resolution"
            raise ValueError(f"{symbol}: sale is not after-tax actionable: {reasons}")
        sell_tax += resolution.tax.estimated_tax_usd
        sell_costs += resolution.friction_usd
    denominator = (
        float(book_usd) + float(new_cash_usd) - sell_tax - sell_costs
    )
    lines: list[OrderLine] = []

    # Convert authored dollars to venue-valid quantities. The residual may only
    # be assigned to names the author already selected; security selection stays
    # with the judgment team while this layer supplies executable arithmetic.
    rounded_buys: dict[str, tuple[float, float]] = {}
    deployable = float(proposal.cash_to_deploy)
    for buy in proposal.buys:
        symbol = buy.symbol.strip().upper()
        facts = facts_by_symbol.get(symbol)
        if facts is None:
            continue
        unit_cost = facts.evidence.price_usd * facts.quantity_increment
        units = math.floor((float(buy.amount_usd) + 1e-9) / unit_cost)
        rounded_buys[symbol] = (
            units * facts.quantity_increment,
            units * unit_cost,
        )
    residual = deployable - sum(value[1] for value in rounded_buys.values())
    while rounded_buys:
        affordable: list[tuple[float, float, str]] = []
        for buy in proposal.buys:
            symbol = buy.symbol.strip().upper()
            if symbol not in rounded_buys:
                continue
            facts = facts_by_symbol[symbol]
            unit_cost = facts.evidence.price_usd * facts.quantity_increment
            if unit_cost <= residual + 0.005:
                _, actual = rounded_buys[symbol]
                shortfall = float(buy.amount_usd) - actual
                affordable.append((shortfall / unit_cost, -unit_cost, symbol))
        if not affordable:
            break
        _, _, symbol = max(affordable)
        facts = facts_by_symbol[symbol]
        shares, actual = rounded_buys[symbol]
        unit_cost = facts.evidence.price_usd * facts.quantity_increment
        rounded_buys[symbol] = (
            shares + facts.quantity_increment,
            actual + unit_cost,
        )
        residual -= unit_cost
    residual = max(0.0, residual)

    for buy in proposal.buys:
        symbol = buy.symbol.strip().upper()
        intent = buy.order_intent
        if intent is None:
            raise ValueError(f"{symbol}: missing authored order_intent")
        facts = facts_by_symbol.get(symbol)
        if facts is None:
            raise ValueError(f"{symbol}: missing live execution facts")
        action = OrderAction.ADD if symbol in held else OrderAction.BUY
        authored_amount = float(buy.amount_usd)
        shares, amount = rounded_buys[symbol]
        if shares <= 0:
            raise ValueError(
                f"{symbol}: authored ${authored_amount:,.2f} cannot buy one "
                f"venue increment ({facts.quantity_increment:g} share)"
            )
        after_value = held.get(symbol, 0.0) + amount
        post_weight = 100.0 * after_value / denominator if denominator > 0 else 0.0
        weighted_multiple = (
            sum(s.probability_pct * s.terminal_multiple for s in intent.outcome_scenarios)
            / 100.0
            if intent.outcome_scenarios
            else None
        )
        expected_contribution = (
            (weighted_multiple - 1.0) * amount / denominator * 100.0
            if weighted_multiple is not None and denominator > 0
            else None
        )
        voices = list(voice_map.get(symbol) or [])
        voices.append(
            VoiceVerdict(
                source="deployment_author",
                verdict=action.value,
                as_of=generated,
                rationale=buy.justification or intent.thesis,
            )
        )
        source = "discovery" if symbol in discovered and symbol not in held else "rebalance"
        lines.append(
            OrderLine(
                symbol=symbol,
                action=action,
                shares=shares,
                notional_usd=amount,
                authored_notional_usd=authored_amount,
                quantity_increment=facts.quantity_increment,
                venue=facts.venue,
                instrument_type=facts.instrument_type,
                thesis=intent.thesis,
                thesis_type=intent.thesis_type,
                falsifier=intent.falsifier,
                catalyst=DatedCatalyst(
                    description=intent.catalyst_description,
                    due_date=intent.catalyst_date,
                ),
                expectation=OutcomeExpectation(
                    statement=intent.expectation,
                    due_date=intent.expectation_due_date,
                    success_measure=intent.success_measure,
                ),
                evidence=facts.evidence,
                stance_source=source,
                voices=voices,
                post_trade_weight_pct=post_weight,
                expected_upside_multiple=intent.expected_upside_multiple,
                outcome_scenarios=intent.outcome_scenarios,
                probability_confidence=intent.probability_confidence,
                probability_basis=intent.probability_basis,
                probability_weighted_multiple=weighted_multiple,
                expected_portfolio_contribution_pct=expected_contribution,
                capital_at_risk_pct=(amount / denominator * 100.0 if denominator > 0 else None),
                constraint_costs=facts.constraint_costs,
                estate_situs=facts.estate_situs,
            )
        )

    for sell in proposal.sells:
        symbol = sell.symbol.strip().upper()
        intent = sell.order_intent
        if intent is None:
            raise ValueError(f"{symbol}: missing authored order_intent")
        facts = facts_by_symbol.get(symbol)
        if facts is None:
            raise ValueError(f"{symbol}: missing live execution facts")
        resolution = sale_map[symbol]
        held_value = held.get(symbol, 0.0)
        amount = float(sell.amount_usd)
        action = OrderAction.SELL if amount >= held_value - 1.0 else OrderAction.TRIM
        after_value = max(0.0, held_value - amount)
        post_weight = 100.0 * after_value / denominator if denominator > 0 else 0.0
        voices = list(voice_map.get(symbol) or [])
        voices.append(
            VoiceVerdict(
                source="deployment_author",
                verdict=action.value,
                as_of=generated,
                rationale=sell.reason or intent.thesis,
            )
        )
        staged_execution = None
        if sell.execution_style == "staged_tranche":
            policy = staged_map.get(symbol)
            if policy is None:
                raise ValueError(f"{symbol}: missing staged sell policy")
            if sell.execute_by is None or sell.next_review_date is None:
                raise ValueError(f"{symbol}: missing staged execution dates")
            staged_execution = StagedExecutionPlan(
                execute_by=sell.execute_by,
                next_review_date=sell.next_review_date,
                tranche_reason=sell.tranche_reason,
                maximum_current_tranche_usd=float(
                    policy["maximum_current_tranche_usd"]
                ),
                remaining_glide_quarters=int(
                    policy.get("remaining_glide_quarters") or 1
                ),
                next_waypoint_date=(
                    policy.get("next_waypoint_date")
                    or policy["execute_no_later_than"]
                ),
                next_waypoint_weight_pct=policy.get("next_waypoint_weight_pct"),
                shares_to_sell_by_next_waypoint=int(
                    policy.get("shares_to_sell_by_next_waypoint") or 0
                ),
                glide_base_shares_to_sell_by_next_waypoint=int(
                    policy.get("glide_base_shares_to_sell_by_next_waypoint")
                    or policy.get("shares_to_sell_by_next_waypoint")
                    or 0
                ),
                tax_denominator_adjustment_shares=int(
                    policy.get("tax_denominator_adjustment_shares") or 0
                ),
                estimated_post_trade_weight_pct=policy.get(
                    "estimated_post_trade_weight_pct"
                ),
                estimated_post_trade_direct_nvda_weight_pct=policy.get(
                    "estimated_post_trade_direct_nvda_weight_pct",
                    policy.get("estimated_post_trade_weight_pct"),
                ),
                estimated_post_trade_effective_nvda_weight_pct=policy.get(
                    "estimated_post_trade_effective_nvda_weight_pct"
                ),
                clips=[
                    ExecutionClip.model_validate(row)
                    for row in policy.get("clips", [])
                ],
                reassess_after_fill=bool(
                    policy.get("reassess_after_fill", True)
                ),
            )
        lines.append(
            OrderLine(
                symbol=symbol,
                action=action,
                shares=resolution.quantity,
                notional_usd=amount,
                authored_notional_usd=amount,
                quantity_increment=facts.quantity_increment,
                venue=facts.venue,
                instrument_type=facts.instrument_type,
                thesis=intent.thesis,
                thesis_type=intent.thesis_type,
                falsifier=intent.falsifier,
                catalyst=DatedCatalyst(
                    description=intent.catalyst_description,
                    due_date=intent.catalyst_date,
                ),
                expectation=OutcomeExpectation(
                    statement=intent.expectation,
                    due_date=intent.expectation_due_date,
                    success_measure=intent.success_measure,
                ),
                evidence=facts.evidence,
                stance_source=(
                    "portfolio_review"
                    if staged_execution is not None
                    else "funding_switch"
                ),
                voices=voices,
                post_trade_weight_pct=post_weight,
                expected_upside_multiple=intent.expected_upside_multiple,
                outcome_scenarios=intent.outcome_scenarios,
                probability_confidence=intent.probability_confidence,
                probability_basis=intent.probability_basis,
                tax=resolution.tax,
                constraint_costs=facts.constraint_costs,
                estate_situs=facts.estate_situs,
                staged_execution=staged_execution,
            )
        )

    # Recompute both concentration views over the complete current order list.
    # The glide target is explicitly direct NVDA; diversified-fund look-through
    # remains visible as a separate diagnostic and is never substituted for it.
    from argosy.services.deployment_funnel.look_through import effective_nvda_usd

    buy_lines = [
        line for line in lines if line.action in (OrderAction.BUY, OrderAction.ADD)
    ]
    for line in lines:
        if line.staged_execution is None or line.symbol != "NVDA":
            continue
        policy = staged_map.get("NVDA") or {}
        direct_start = float(
            policy.get("current_direct_nvda_value_usd")
            or held.get("NVDA", 0.0)
        )
        effective_start_raw = policy.get("current_effective_nvda_value_usd")
        effective_start = (
            float(effective_start_raw)
            if effective_start_raw is not None
            else direct_start
        )
        direct_after = max(0.0, direct_start - line.notional_usd) + sum(
            buy.notional_usd for buy in buy_lines if buy.symbol == "NVDA"
        )
        effective_after = max(0.0, effective_start - line.notional_usd) + sum(
            effective_nvda_usd(buy.symbol, buy.notional_usd)
            for buy in buy_lines
        )
        if denominator > 0:
            direct_pct = round(100.0 * direct_after / denominator, 4)
            effective_pct = round(100.0 * effective_after / denominator, 4)
            line.staged_execution.estimated_post_trade_weight_pct = direct_pct
            line.staged_execution.estimated_post_trade_direct_nvda_weight_pct = direct_pct
            line.staged_execution.estimated_post_trade_effective_nvda_weight_pct = effective_pct

    funding = FundingSummary(
        new_cash_usd=float(new_cash_usd),
        gross_sell_proceeds_usd=gross_sells,
        sell_tax_usd=sell_tax,
        sell_costs_usd=sell_costs,
        reserve_usd=float(proposal.cash_to_reserve),
        rounding_residual_usd=round(residual, 2),
        available_to_buy_usd=round(
            sum(line.notional_usd for line in lines if line.action in (OrderAction.BUY, OrderAction.ADD)),
            2,
        ),
    )
    sheet = OrderSheet(
        user_id=user_id,
        generated_at=generated,
        horizon_years_min=horizon_years[0],
        horizon_years_max=horizon_years[1],
        freshness_days=freshness_days,
        portfolio_symbols=sorted(held),
        funding=funding,
        lines=lines,
        no_action=no_action,
        candidate_comparisons=proposal.candidate_comparisons,
        review_resolution=review_resolution,
        pending_research=proposal.pending_research,
        rationale=proposal.rationale,
    )
    return BuiltOrderSheet(sheet=sheet, validation=validate_order_sheet(sheet))


def build_cash_order_sheet(
    proposal: AllocationProposal,
    **kwargs,
) -> BuiltOrderSheet:
    """Compatibility wrapper for callers that explicitly prohibit funding sells."""

    if proposal.sells:
        raise ValueError("cash-only order sheet cannot contain sells")
    return build_order_sheet(proposal, sale_resolutions={}, **kwargs)


__all__ = [
    "BuiltOrderSheet",
    "ExecutionFacts",
    "build_order_sheet",
    "build_cash_order_sheet",
]
