"""The ONE canonical deploy-plan builder.

Both ``GET /api/portfolio/deploy-cash`` (the client-initiated "act now" fallback)
and the inbox period directive (the proactive path) MUST produce the same buy
list for the same ``(doc, holdings, cash, amount)`` — divergent buy engines across
surfaces is the recurring "one canonical source" defect. This function is that
single deterministic assembler:

    assemble_deployment_plan  ->  run_preflight_for_plan  ->  redirect_overflow_to_diversifiers

It deliberately EXCLUDES the expensive, opt-in fleet disposition (the live
RiskOfficer/FundManager call): that stays in the route's ``fleet_review=true``
branch. The proactive inbox path uses the deterministic core only.

Pure orchestration — no DB, no settings, no network. The caller loads
``doc``/``holdings``/``cash`` (and any ``market_context``/``snapshot_prices``) and
passes ``funnel_enabled`` explicitly, so this is unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from argosy.services.deployment_advisor import DeploymentPlan, assemble_deployment_plan
from argosy.services.deployment_funnel.from_plan import (
    redirect_overflow_to_diversifiers,
    run_preflight_for_plan,
)


def build_canonical_deploy_plan(
    *,
    doc,
    holdings: dict[str, float],
    cash_usd: float,
    deploy_amount_usd: float,
    as_of: date,
    market_context: Any = None,
    sleeve_pct: float = 5.0,
    use_high_potential: bool = True,
    user_id: str = "ariel",
    snapshot_prices: dict[str, float] | None = None,
    fleet_available: bool = False,
    funnel_enabled: bool = True,
) -> tuple[DeploymentPlan, Any | None]:
    """Assemble the plan-bound deploy list, then reconcile it against the plan's
    own numbers (preflight) and redirect any over-cap / funded-reserve overflow
    into the plan's zero-NVDA diversifier ETFs.

    Returns ``(plan, preflight_result)``. ``preflight_result`` is ``None`` when the
    funnel is disabled or there is no accepted plan (``doc is None``) — the caller
    then has only the raw assembled plan, never invented instruments.
    """
    plan = assemble_deployment_plan(
        doc=doc, holdings=holdings, deploy_amount_usd=deploy_amount_usd,
        as_of=as_of, market_context=market_context, sleeve_pct=sleeve_pct,
        use_high_potential=use_high_potential, user_id=user_id,
    )
    if doc is None or not funnel_enabled:
        return plan, None

    result = run_preflight_for_plan(
        plan, doc=doc, holdings_usd=holdings, cash_usd=cash_usd,
        deployable_usd=deploy_amount_usd, snapshot_prices=snapshot_prices,
        fleet_available=fleet_available,
    )

    # Deterministic redirect (no LLM, no plan change): overflow the funnel won't
    # place in its natural sleeve flows into the plan's own diversifier ETFs. When
    # it fires we re-run the preflight against the REDIRECTED plan so the surfaced
    # facts describe the plan the user actually sees.
    plan2, redirect_note = redirect_overflow_to_diversifiers(plan, result, doc)
    if redirect_note:
        plan = replace(plan2, caveats=tuple(plan2.caveats) + (redirect_note,))
        result = run_preflight_for_plan(
            plan, doc=doc, holdings_usd=holdings, cash_usd=cash_usd,
            deployable_usd=deploy_amount_usd, snapshot_prices=snapshot_prices,
            fleet_available=fleet_available,
        )

    return plan, result


def _class_label_for_symbol(doc, symbol: str) -> str:
    """The plan class whose instruments include ``symbol`` (the asset class the
    buy fills), or the high-potential sleeve label as a fallback."""
    if doc is not None:
        for cls in getattr(doc, "classes", []) or []:
            for inst in getattr(cls, "instruments", []) or []:
                if getattr(inst, "symbol", None) == symbol:
                    return getattr(cls, "label", "") or ""
    return "High-growth potential"


def deploy_plan_to_buy_list(plan: DeploymentPlan, doc) -> list[dict[str, Any]]:
    """Project the canonical plan into the inbox buy list: one row per deployed
    BUY line across ALL tiers (core / medium / high sleeve / …), carrying the
    instrument, the asset class it fills, the dollar amount, the tier, and the
    rationale. The rows sum to ``plan.deployed_total_usd`` — every deployed dollar
    is shown exactly once, none dropped or double-counted."""
    rows: list[dict[str, Any]] = []
    for tier in plan.tiers:
        for line in tier.lines:
            rows.append(
                {
                    "instrument": line.symbol,
                    "asset_class": _class_label_for_symbol(doc, line.symbol),
                    "amount_usd": round(line.amount_usd, 2),
                    "tier": line.tier,
                    "rationale": line.rationale,
                }
            )
    return rows


__all__ = ["build_canonical_deploy_plan", "deploy_plan_to_buy_list"]
