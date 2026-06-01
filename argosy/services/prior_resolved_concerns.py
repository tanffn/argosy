"""Prior-resolved FM-objection fetcher (wave 7 piece B).

Produces the ``list[PriorResolvedConcern]`` that the orchestrator
threads into ``FundManagerAgent.build_prompt`` for the next FM
plan-revision call. Each entry corresponds to one prior-draft FM
objection the user actively answered (AGREE or DISAGREE — DEFER
is filtered upstream).

Selection rule: pick the MOST RECENT prior plan_version (by
``created_at``) that belongs to this user, is NOT
``current_plan_version_id``, and has at least one
``fm_objection_user_state`` row with a non-DEFER stance.

The fetcher reads three tables:

  * ``plan_versions``: scope by user + recency, excluding current.
  * ``fm_objection_user_state``: pull AGREE/DISAGREE rows for the
    chosen prior plan.
  * ``agent_reports``: parse the prior plan's ``fund_manager``
    response_text so each row's ``objection_index`` resolves to a
    concrete topic + detail + severity. Falls back to a generic
    label when the index is out of range (defensive — should be
    impossible if the prior fm verdict was written normally).

Pure function (no writes). Caller is responsible for threading
the result into the FM build_prompt kwargs.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.agents.fund_manager import PriorResolvedConcern
from argosy.api.routes.plan import _classify_severity, _parse_fm_response, _split_reason
from argosy.state.models import AgentReport, FMObjectionUserState, PlanVersion

log = logging.getLogger(__name__)


def _find_most_recent_prior_with_resolutions(
    session: Session,
    *,
    user_id: str,
    current_plan_version_id: int | None,
) -> PlanVersion | None:
    """Most recent plan_version (excluding ``current_plan_version_id``
    when given) that has at least one AGREE/DISAGREE row in
    ``fm_objection_user_state``.

    ``current_plan_version_id=None`` means "no draft-in-progress to
    exclude" — used at phase 5 dispatch time when the new draft hasn't
    been persisted yet.
    """
    stmt = (
        select(PlanVersion)
        .join(
            FMObjectionUserState,
            FMObjectionUserState.plan_version_id == PlanVersion.id,
        )
        .where(
            PlanVersion.user_id == user_id,
            FMObjectionUserState.user_id == user_id,
            FMObjectionUserState.stance.in_(("AGREE", "DISAGREE")),
        )
        .order_by(desc(PlanVersion.imported_at))
        .limit(1)
    )
    if current_plan_version_id is not None:
        stmt = stmt.where(PlanVersion.id != current_plan_version_id)
    return session.execute(stmt).scalar_one_or_none()


def _objection_text_by_index(
    response_text: str,
) -> list[tuple[str, str, str]]:
    """Parse the fund_manager response_text into a list of
    ``(topic, detail, severity)`` by objection index. Reuses the
    same shape decoders the /api/plan/draft/objections route uses
    so prior-resolved entries match what the user originally saw."""
    payload = _parse_fm_response(response_text)
    reasons = payload.get("reasons", []) or []
    out: list[tuple[str, str, str]] = []
    for r in reasons:
        if not isinstance(r, str):
            continue
        topic, detail = _split_reason(r)
        severity = _classify_severity(topic, detail)
        out.append((topic, detail, severity))
    return out


def get_prior_resolved_concerns(
    session: Session,
    *,
    user_id: str,
    current_plan_version_id: int | None,
) -> list[PriorResolvedConcern]:
    """Return AGREE/DISAGREE concerns from the user's most recent
    prior draft, as :class:`PriorResolvedConcern` items ready to
    feed into the FM plan-revision prompt.

    Empty list when:
      * No prior plan_version exists.
      * The prior exists but has no AGREE/DISAGREE rows (all DEFER
        or no rows).
      * The prior's fund_manager agent_report can't be located or
        produces no parseable reasons.
    """
    prior = _find_most_recent_prior_with_resolutions(
        session,
        user_id=user_id,
        current_plan_version_id=current_plan_version_id,
    )
    if prior is None:
        return []

    rows = (
        session.execute(
            select(FMObjectionUserState)
            .where(
                FMObjectionUserState.user_id == user_id,
                FMObjectionUserState.plan_version_id == prior.id,
                FMObjectionUserState.stance.in_(("AGREE", "DISAGREE")),
            )
            .order_by(FMObjectionUserState.objection_index)
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    fm_report = (
        session.execute(
            select(AgentReport)
            .where(
                AgentReport.user_id == user_id,
                AgentReport.agent_role == "fund_manager",
                AgentReport.decision_id == f"plan-synth-{prior.id}",
            )
            .order_by(desc(AgentReport.id))
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if fm_report is None:
        log.info(
            "prior_resolved.no_fm_report",
            extra={"prior_plan_version_id": prior.id},
        )
        return []

    indexed = _objection_text_by_index(fm_report.response_text)
    if not indexed:
        return []

    out: list[PriorResolvedConcern] = []
    for row in rows:
        if 0 <= row.objection_index < len(indexed):
            topic, detail, severity = indexed[row.objection_index]
        else:
            # Defensive fallback — shouldn't fire in practice; the
            # FM verdict that produced the row should have had the
            # corresponding objection.
            topic = f"(prior objection #{row.objection_index} — text unavailable)"
            detail = "Prior FM verdict could not be re-parsed for this index."
            severity = "YELLOW"
        out.append(
            PriorResolvedConcern(
                topic=topic,
                detail=detail,
                severity=severity,
                stance=row.stance,
                counter_position=row.counter_position,
            )
        )
    return out


__all__ = ["get_prior_resolved_concerns"]
