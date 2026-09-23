"""Fail-closed enforcement for decision runs without execution authority."""
from __future__ import annotations

from sqlalchemy import select

from argosy.services.chat_advisor.contracts import ExecutionPolicy
from argosy.state.models import DecisionRun, Proposal


class AnalysisOnlyViolation(PermissionError):
    """Raised when a non-executable analysis reaches a money-moving sink."""


async def proposal_execution_policy(session, proposal_id: int) -> str:
    """Return the canonical run policy for a proposal (legacy rows are normal)."""
    value = (
        await session.execute(
            select(DecisionRun.execution_policy)
            .join(Proposal, Proposal.decision_run_id == DecisionRun.id)
            .where(Proposal.id == proposal_id)
        )
    ).scalar_one_or_none()
    return str(value or ExecutionPolicy.NORMAL.value)


async def assert_proposal_can_mutate(
    session, proposal_id: int | None, *, operation: str
) -> None:
    """Reject approvals, broker actions, and fill receipts for analysis-only runs."""
    if proposal_id is None:
        return
    policy = await proposal_execution_policy(session, proposal_id)
    if policy == ExecutionPolicy.ANALYSIS_ONLY.value:
        raise AnalysisOnlyViolation(
            f"proposal {proposal_id} belongs to an analysis-only decision run; "
            f"{operation} is forbidden"
        )


__all__ = [
    "AnalysisOnlyViolation",
    "assert_proposal_can_mutate",
    "proposal_execution_policy",
]
