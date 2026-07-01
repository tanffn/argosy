from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from argosy.services.contracts import AllocationCandidate


class CandidateStatus(str, Enum):
    APPROVE = "approve_candidate"
    VETO = "veto"
    DEFER = "defer"
    REQUIRES_PLAN_CHANGE = "requires_plan_change"
    CAP_AT_PCT = "cap_at_pct"


CANDIDATE_STATUSES = tuple(s.value for s in CandidateStatus)


@dataclass(frozen=True)
class HistoryFeatures:
    """Price-history FEATURES for a candidate symbol. Recorded for judgment;
    NEVER a gate on their own (gold-at-ATH is evidence, not a rule)."""

    last_price: float | None
    ath: float | None
    pct_below_ath: float | None       # 0 == at ATH; 12.0 == 12% below
    zscore_vs_window: float | None
    drawdown_pct: float | None
    stale: bool = False


@dataclass(frozen=True)
class PlanGap:
    asset_class: str
    current_target_pct: float
    proposed_target_pct: float | None
    reason_refs: tuple[str, ...]
    blocked_amount_usd: float


@dataclass(frozen=True)
class EnrichedCandidate:
    candidate: AllocationCandidate
    symbol: str
    effective_nvda_usd: float          # incl. index look-through
    news_sentiment: str | None         # None => "no recent ingested signal"
    history: HistoryFeatures
    status: CandidateStatus
    reason: str
    cap_pct: float | None = None       # set when status is CAP_AT_PCT


@dataclass(frozen=True)
class PreflightResult:
    deployable_usd: float
    enriched: tuple[EnrichedCandidate, ...]
    plan_gaps: tuple[PlanGap, ...]
    kept_total_usd: float
    notes: tuple[str, ...] = ()
