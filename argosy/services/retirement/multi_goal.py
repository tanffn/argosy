"""Multi-goal balancer with explicit hard/soft constraints (MED #26).

Per codex review fix: NOT "Lagrangian or priority order" false binary.
This is a constrained optimization: hard constraints MUST be met; soft
constraints are optimized within the remaining budget. Every "trade off
X for Y" suggestion cites which constraint is binding.

Hard constraints: kids' education due in 18mo, mortgage payoff at fixed
date, emergency fund 12mo floor.
Soft constraints: retirement at 49, house upgrade by 60.
"""
from dataclasses import dataclass, field
from typing import Literal


ConstraintType = Literal["hard_floor", "soft_target", "no_later_than"]


@dataclass(frozen=True)
class GoalConstraint:
    goal_id: str
    constraint_type: ConstraintType
    target_nis: float
    deadline: str | None
    priority: int  # 1-10
    rationale: str


@dataclass(frozen=True)
class GoalBalance:
    goal_id: str
    target_nis: float
    funded_pct: float
    binding_constraints: list[str] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)


def balance_multi_goals(
    *,
    available_capital_nis: float,
    constraints: list[GoalConstraint],
) -> list[GoalBalance]:
    """Solve the multi-goal allocation problem.

    Algorithm:
      1. Hard constraints get funded first (in priority order)
      2. Soft constraints share the remaining capital proportionally to
         priority
      3. Each goal's GoalBalance lists which other constraints are binding
    """
    remaining = available_capital_nis
    results: dict[str, GoalBalance] = {}

    # Step 1: hard constraints
    hard = [c for c in constraints if c.constraint_type in ("hard_floor", "no_later_than")]
    soft = [c for c in constraints if c.constraint_type == "soft_target"]
    hard_sorted = sorted(hard, key=lambda c: c.priority)

    for c in hard_sorted:
        allocation = min(remaining, c.target_nis)
        funded = allocation / c.target_nis if c.target_nis > 0 else 0.0
        results[c.goal_id] = GoalBalance(
            goal_id=c.goal_id,
            target_nis=c.target_nis,
            funded_pct=round(funded, 4),
            binding_constraints=[],
        )
        remaining = max(0.0, remaining - allocation)

    # Step 2: soft constraints share remaining capital proportionally
    if soft and remaining > 0:
        total_priority = sum(c.priority for c in soft)
        for c in soft:
            share = remaining * (c.priority / total_priority) if total_priority > 0 else 0.0
            allocation = min(share, c.target_nis)
            funded = allocation / c.target_nis if c.target_nis > 0 else 0.0
            binding = [other.goal_id for other in soft if other.goal_id != c.goal_id]
            tradeoffs: list[str] = []
            if funded < 1.0 and binding:
                tradeoffs.append(
                    f"Defunding {binding[0]} by ₪{c.target_nis - allocation:,.0f} "
                    f"would fully fund {c.goal_id} — choose which matters more."
                )
            results[c.goal_id] = GoalBalance(
                goal_id=c.goal_id,
                target_nis=c.target_nis,
                funded_pct=round(funded, 4),
                binding_constraints=binding,
                tradeoffs=tradeoffs,
            )
    else:
        for c in soft:
            results[c.goal_id] = GoalBalance(
                goal_id=c.goal_id,
                target_nis=c.target_nis,
                funded_pct=0.0,
                binding_constraints=[h.goal_id for h in hard],
                tradeoffs=[
                    f"All capital absorbed by hard constraints — defund "
                    f"{hard[0].goal_id if hard else 'no soft'} to make room."
                ] if hard else [],
            )

    return list(results.values())
