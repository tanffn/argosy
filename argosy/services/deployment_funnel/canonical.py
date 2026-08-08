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

Pure orchestration — no network. Integrity exclusions are resolved by the
caller (sync Session or async await) and passed in; this module never opens
an aiosqlite engine synchronously.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

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
    exposure_aware: bool = True,
    blocked_tickers: set[str] | None = None,
    integrity_exclusions: list[dict[str, str]] | None = None,
    session: Session | None = None,
) -> tuple[DeploymentPlan, Any | None]:
    """Assemble the plan-bound deploy list, then reconcile it against the plan's
    own numbers (preflight) and redirect any over-cap / funded-reserve overflow
    into the plan's zero-NVDA diversifier ETFs.

    Returns ``(plan, preflight_result)``. ``preflight_result`` is ``None`` when the
    funnel is disabled or there is no accepted plan (``doc is None``) — the caller
    then has only the raw assembled plan, never invented instruments.

    When ``session`` is provided and ``blocked_tickers`` is None, open
    remediations are resolved against that sync Session (period-directive /
    FastAPI ``get_db`` paths). Async callers should await
    ``exclusions_for_open_remediations_async`` and pass the results explicitly.
    """
    blocked = blocked_tickers
    exclusions = list(integrity_exclusions or [])
    if blocked is None and session is not None:
        from argosy.services.decision_integrity.exclusions import (
            exclusions_for_open_remediations,
            merge_exclusion_dicts,
        )

        # Symbols we might emit — plan instruments + discovery BUY tickers.
        candidate_syms: list[str] = []
        if doc is not None:
            for cls in getattr(doc, "classes", []) or []:
                for inst in getattr(cls, "instruments", []) or []:
                    sym = getattr(inst, "symbol", None)
                    if sym:
                        candidate_syms.append(str(sym))
        try:
            from argosy.api.routes.portfolio import _load_discovery_state

            picks, _e, _l = _load_discovery_state(user_id)
            candidate_syms.extend(
                (p.ticker or "")
                for p in picks
                if (p.verdict or "").upper() == "BUY" and p.ticker
            )
        except Exception:  # noqa: BLE001
            pass
        resolved = exclusions_for_open_remediations(
            session, user_id=user_id, tickers=candidate_syms,
        )
        exclusions = merge_exclusion_dicts(resolved)
        blocked = {e["ticker"] for e in exclusions}

    plan = assemble_deployment_plan(
        doc=doc, holdings=holdings, deploy_amount_usd=deploy_amount_usd,
        as_of=as_of, market_context=market_context, sleeve_pct=sleeve_pct,
        use_high_potential=use_high_potential, user_id=user_id,
        exposure_aware=exposure_aware,
        blocked_tickers=blocked,
        integrity_exclusions=exclusions,
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
    plan2, redirect_note = redirect_overflow_to_diversifiers(
        plan, result, doc, holdings=holdings if exposure_aware else None)
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


def deploy_plan_to_buy_list(
    plan: DeploymentPlan,
    doc,
    *,
    user_id: str = "ariel",
    blocked_tickers: set[str] | None = None,
    integrity_exclusions: list[dict[str, str]] | None = None,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    """Project the canonical plan into the inbox buy list.

    Stream A — drop instruments with open remediation_requests so a
    plan-synthesis integrity warning cannot become an actionable inbox
    buy. Exclusions are returned as rows with ``excluded=True`` + reason
    (never a silent absence).

    Callers must pass ``blocked_tickers`` / ``session`` — this function no
    longer opens an async engine from sync code.
    """
    exclusions = list(integrity_exclusions or [])
    if blocked_tickers is not None:
        blocked = {t.upper() for t in blocked_tickers}
    elif session is not None:
        from argosy.services.decision_integrity.exclusions import (
            exclusions_for_open_remediations,
            merge_exclusion_dicts,
        )

        symbols = [line.symbol for tier in plan.tiers for line in tier.lines]
        resolved = exclusions_for_open_remediations(
            session, user_id=user_id, tickers=symbols,
        )
        exclusions = merge_exclusion_dicts(resolved)
        blocked = {e["ticker"] for e in exclusions}
    else:
        # Fail closed on the plan's own exclusion sidecar when caller forgot.
        blocked = {
            (e.get("ticker") or "").upper()
            for e in (getattr(plan, "integrity_exclusions", ()) or ())
            if e.get("ticker")
        }
        exclusions = [
            dict(e) for e in (getattr(plan, "integrity_exclusions", ()) or ())
        ]

    reason_by = {
        (e.get("ticker") or "").upper(): e
        for e in exclusions
        if e.get("ticker")
    }
    for e in getattr(plan, "integrity_exclusions", ()) or ():
        t = (e.get("ticker") or "").upper()
        if t and t not in reason_by:
            reason_by[t] = dict(e)
            blocked.add(t)

    rows: list[dict[str, Any]] = []
    for tier in plan.tiers:
        for line in tier.lines:
            sleeve_sym = line.symbol
            for cite in getattr(line, "cites", ()) or ():
                if cite.startswith("plan_target:"):
                    sleeve_sym = cite.split(":", 1)[1]
                    break
            sym_u = (line.symbol or "").upper()
            if sym_u in blocked:
                info = reason_by.get(sym_u) or {
                    "ticker": sym_u,
                    "reason": f"open remediation blocks {sym_u}",
                    "blocked_by": "open_remediation",
                }
                rows.append(
                    {
                        "instrument": line.symbol,
                        "asset_class": _class_label_for_symbol(doc, sleeve_sym),
                        "amount_usd": 0.0,
                        "tier": line.tier,
                        "rationale": line.rationale,
                        "excluded": True,
                        "exclusion_reason": info.get("reason") or "",
                        "blocked_by": info.get("blocked_by") or "open_remediation",
                    }
                )
                continue
            rows.append(
                {
                    "instrument": line.symbol,
                    "asset_class": _class_label_for_symbol(doc, sleeve_sym),
                    "amount_usd": round(line.amount_usd, 2),
                    "tier": line.tier,
                    "rationale": line.rationale,
                    "excluded": False,
                }
            )
    # Surface exclusions that never made it onto a line (sleeve dropped earlier).
    present = {(r.get("instrument") or "").upper() for r in rows}
    for t, info in reason_by.items():
        if t and t not in present:
            rows.append(
                {
                    "instrument": t,
                    "asset_class": "integrity_exclusion",
                    "amount_usd": 0.0,
                    "tier": "excluded",
                    "rationale": "",
                    "excluded": True,
                    "exclusion_reason": info.get("reason") or "",
                    "blocked_by": info.get("blocked_by") or "open_remediation",
                }
            )
    return rows


__all__ = ["build_canonical_deploy_plan", "deploy_plan_to_buy_list"]
