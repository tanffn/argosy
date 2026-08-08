"""Shared choke for user-visible actionable BUY recommendations.

DecisionFlow is not the only road from fundamentals to a buy the user
sees. Discovery grades FleetPick(BUY) → deployment_advisor labels them
``fleet_validated`` → deploy_funnel/canonical builds inbox buy lists.
Plan-synthesis fundamentals feed the accepted plan that those deploy
paths consume. Any of those paths must refuse an actionable BUY when
provenance/open remediations would block DecisionFlow green_light.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.services.decision_integrity.gates import (
    IntegrityGateResult,
    evaluate_green_light_integrity,
)


def evaluate_actionable_buy_integrity(
    session: Session | None,
    *,
    user_id: str,
    ticker: str,
    fundamentals_fields: dict[str, Any] | None = None,
    analyst_reports: Sequence[AgentReport | dict[str, Any]] | None = None,
    skip_db: bool = False,
) -> IntegrityGateResult:
    """Same integrity bar as DecisionFlow green_light for actionable buys."""
    return evaluate_green_light_integrity(
        session,
        user_id=user_id,
        ticker=ticker,
        fundamentals_fields=fundamentals_fields,
        analyst_reports=analyst_reports or [],
        skip_db=skip_db,
        require_fundamentals_provenance=True,
    )


def filter_tickers_with_open_remediations(
    session: Session,
    *,
    user_id: str,
    tickers: Sequence[str],
) -> set[str]:
    """Return uppercased tickers that currently have an open remediation."""
    from argosy.services.decision_integrity.remediation_store import (
        list_open_remediations,
    )

    blocked: set[str] = set()
    for t in tickers:
        if not t:
            continue
        key = str(t).strip().upper()
        if list_open_remediations(session, user_id=user_id, ticker=key):
            blocked.add(key)
    return blocked


__all__ = [
    "evaluate_actionable_buy_integrity",
    "filter_tickers_with_open_remediations",
]
