from __future__ import annotations

from typing import Callable

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
    PlanGap,
    PreflightResult,
)
from argosy.services.deployment_funnel.enrich import (
    PriceProvider,
    build_history_features,
    news_sentiment_for,
)
from argosy.services.deployment_funnel.gates import GateInputs, classify_candidate
from argosy.services.deployment_funnel.look_through import effective_nvda_usd

# Statuses whose dollars count toward the "kept" (deployable) total.
_KEPT = {CandidateStatus.APPROVE, CandidateStatus.CAP_AT_PCT}


def run_preflight(
    candidates: list[AllocationCandidate],
    *,
    symbol_of: Callable[[AllocationCandidate], str],
    gate_inputs: GateInputs,
    provider: PriceProvider,
    signals_by_symbol: dict[str, str],
    deployable_usd: float,
) -> PreflightResult:
    """Deterministic, no-LLM preflight. Enriches + classifies each candidate and
    collects typed plan gaps. Pure given its inputs. Shadow-only: it computes and
    returns a result; it never persists or executes."""
    enriched: list[EnrichedCandidate] = []
    plan_gaps: list[PlanGap] = []
    kept_total = 0.0

    for cand in candidates:
        symbol = symbol_of(cand)
        hf = build_history_features(symbol, provider)
        sentiment = news_sentiment_for(symbol, signals_by_symbol)
        status, reason, cap_pct = classify_candidate(
            cand, symbol, hf, sentiment, gate_inputs
        )
        eff_nvda = effective_nvda_usd(symbol, cand.total_notional_usd)

        enriched.append(
            EnrichedCandidate(
                candidate=cand, symbol=symbol, effective_nvda_usd=eff_nvda,
                news_sentiment=sentiment, history=hf, status=status,
                reason=reason, cap_pct=cap_pct,
            )
        )

        if status is CandidateStatus.REQUIRES_PLAN_CHANGE:
            cls = gate_inputs.class_of.get(symbol.upper(), "unknown")
            plan_gaps.append(
                PlanGap(
                    asset_class=cls, current_target_pct=0.0,
                    proposed_target_pct=None,
                    reason_refs=(f"{symbol} implies '{cls}', absent from the plan",),
                    blocked_amount_usd=cand.total_notional_usd,
                )
            )
        elif status in _KEPT:
            frac = (
                (cap_pct / 100.0)
                if (status is CandidateStatus.CAP_AT_PCT and cap_pct is not None)
                else 1.0
            )
            kept_total += cand.total_notional_usd * frac

    kept_total = round(min(kept_total, deployable_usd), 2)
    return PreflightResult(
        deployable_usd=deployable_usd,
        enriched=tuple(enriched),
        plan_gaps=tuple(plan_gaps),
        kept_total_usd=kept_total,
    )
