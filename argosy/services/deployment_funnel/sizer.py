"""Deterministic sizer — turns per-candidate statuses (the bounded actions the
gates, and later the risk officer, may emit) into FINAL dollar amounts under
hard constraints. The LLM never emits dollars (codex correction 1): it chooses
a bounded action; THIS module computes the money.

Constraints enforced here:
- cash-only, no sells;
- Σ final ≤ deployable cash (freed cash from vetoes is NOT force-redeployed —
  it becomes remainder, so a vetoed US-index line can't quietly re-inflate the
  sleeves being trimmed);
- CAP_AT_PCT lines sized to notional × cap_pct;
- MOVE_TO_RESERVE / VETO / DEFER / REQUIRES_PLAN_CHANGE size to $0;
- a minimum ticket drops dust lines to remainder.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
)

DEFAULT_MIN_TICKET_USD = 500.0


@dataclass(frozen=True)
class SizedLine:
    symbol: str
    final_usd: float
    status: str
    reason: str


@dataclass(frozen=True)
class SizedPlan:
    lines: tuple[SizedLine, ...]
    deployed_usd: float
    undeployed_remainder_usd: float
    reserve_parked_usd: float


def _target_usd(e: EnrichedCandidate) -> float:
    notional = e.candidate.total_notional_usd
    if e.status is CandidateStatus.APPROVE:
        return notional
    if e.status is CandidateStatus.CAP_AT_PCT and e.cap_pct is not None:
        return round(notional * e.cap_pct / 100.0, 2)
    return 0.0


def size_deployment(
    enriched: list[EnrichedCandidate] | tuple[EnrichedCandidate, ...],
    *,
    deployable_usd: float,
    min_ticket_usd: float = DEFAULT_MIN_TICKET_USD,
) -> SizedPlan:
    """Compute the final sized buy list. Deterministic; pure. Never deploys more
    than ``deployable_usd`` — if kept targets exceed cash, they are water-filled
    pro-rata (largest first, running budget so cents can't overshoot)."""
    # Desired dollars per kept line (pre-budget).
    desired: list[tuple[EnrichedCandidate, float]] = []
    reserve_parked = 0.0
    for e in enriched:
        if e.status is CandidateStatus.MOVE_TO_RESERVE:
            reserve_parked += e.candidate.total_notional_usd
            continue
        want = _target_usd(e)
        if want > 0.0:
            desired.append((e, want))

    total_want = round(sum(w for _, w in desired), 2)
    scale = 1.0 if total_want <= deployable_usd else deployable_usd / total_want

    lines: list[SizedLine] = []
    remaining = round(deployable_usd, 2)
    deployed = 0.0
    # Largest first; running budget absorbs rounding so Σ can never exceed cash.
    for e, want in sorted(desired, key=lambda ew: (-ew[1], ew[0].symbol)):
        amount = min(round(want * scale, 2), remaining)
        if amount < min_ticket_usd:
            continue  # dust -> remainder
        remaining = round(remaining - amount, 2)
        deployed = round(deployed + amount, 2)
        lines.append(
            SizedLine(
                symbol=e.symbol, final_usd=amount,
                status=e.status.value, reason=e.reason,
            )
        )

    return SizedPlan(
        lines=tuple(lines),
        deployed_usd=deployed,
        undeployed_remainder_usd=round(max(0.0, deployable_usd - deployed), 2),
        reserve_parked_usd=round(reserve_parked, 2),
    )


__all__ = ["SizedLine", "SizedPlan", "size_deployment", "DEFAULT_MIN_TICKET_USD"]
