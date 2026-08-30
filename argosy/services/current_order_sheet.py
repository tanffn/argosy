"""Canonical lookup for the one currently controlling order sheet.

The repository still contains legacy ``Proposal`` rows and stance-registry
verdicts.  They are useful history and inputs, but while a validated unified
sheet is open (or has been accepted and is executing) every user-facing trade
projection must use that sheet.  This module gives Inbox and Portfolio one
shared lookup instead of letting each surface invent precedence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from argosy.services.order_sheet import OrderSheet, SheetValidation, validate_order_sheet
from argosy.services.order_sheet_materializer import order_sheet_fingerprint
from argosy.state.models import ActionProposal, Proposal


@dataclass(frozen=True)
class CurrentOrderSheet:
    directive: ActionProposal
    sheet: OrderSheet
    fingerprint: str
    materialized_proposal_ids: frozenset[int]
    validation: SheetValidation


def _payload(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_current_order_sheet(db: Any, user_id: str) -> CurrentOrderSheet | None:
    """Return the latest parseable open/accepted unified sheet, if one controls.

    A rejected/deferred/superseded latest directive deliberately returns
    ``None``; an older sheet must not spring back to life.  An expired OPEN
    sheet also stops controlling, while an accepted sheet remains current
    through its order/fill lifecycle.  A sheet made invalid by a newer
    validator still controls display precedence: the UI must show that one
    blocked artifact, not resurrect contradictory legacy proposals.  Callers
    use ``validation`` to decide whether it may be approved or projected as a
    portfolio verdict.
    """

    directive = db.execute(
        select(ActionProposal)
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == f"period_directive:{user_id}",
        )
        .order_by(ActionProposal.surfaced_at.desc(), ActionProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if directive is None or directive.status not in {"open", "accepted"}:
        return None
    now = datetime.now(UTC)
    expires_at = directive.expires_at
    if (
        directive.status == "open"
        and expires_at is not None
        and (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)) < now
    ):
        return None

    payload = _payload(directive.suggested_payload)
    raw_sheet = payload.get("order_sheet")
    if payload.get("artifact_type") != "validated_order_sheet" or not isinstance(raw_sheet, dict):
        return None
    try:
        sheet = OrderSheet.model_validate(raw_sheet)
    except ValueError:
        return None
    validation = validate_order_sheet(sheet)
    fingerprint = order_sheet_fingerprint(sheet)
    if payload.get("order_sheet_fingerprint") != fingerprint:
        return None

    proposal_ids = frozenset(
        int(value)
        for value in db.execute(
            select(Proposal.id).where(
                Proposal.user_id == user_id,
                Proposal.source == "order_sheet",
                Proposal.expected_impact_json.like(f"%{fingerprint}%"),
            )
        ).scalars()
    )
    return CurrentOrderSheet(
        directive=directive,
        sheet=sheet,
        fingerprint=fingerprint,
        materialized_proposal_ids=proposal_ids,
        validation=validation,
    )


def overlay_position_theses(
    rows: list[dict[str, Any]], current: CurrentOrderSheet
) -> list[dict[str, Any]]:
    """Make held-position verdicts describe the current sheet, not old inputs."""

    actions = {line.symbol: line for line in current.sheet.lines}
    no_actions = {line.symbol: line for line in current.sheet.no_action}
    projected: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        symbol = str(row.get("ticker") or "").strip().upper()
        action = actions.get(symbol)
        no_action = no_actions.get(symbol)
        if action is not None:
            row.update(
                {
                    "verdict": action.action.value,
                    "reasoning_md": (
                        f"Current validated order sheet: {action.thesis}\n\n"
                        f"Falsifier: {action.falsifier}"
                    ),
                    "decision_basis": "CURRENT_ORDER_SHEET",
                    "decision_inputs": [
                        {
                            "source": "order_sheet",
                            "fingerprint": current.fingerprint,
                            "stance_source": action.stance_source,
                        }
                    ],
                    "falsifier_state": "armed",
                    "falsifiers": [action.falsifier],
                    "next_validation": action.expectation.due_date.isoformat(),
                    "last_fleet_check_at": current.sheet.generated_at.isoformat(),
                    "analysis_state": "analysed",
                }
            )
            cited = list(row.get("cited_sources") or [])
            for citation in (action.evidence.price_source, *(v.source for v in action.voices)):
                if citation and citation not in cited:
                    cited.append(citation)
            row["cited_sources"] = cited
        elif no_action is not None:
            row.update(
                {
                    "verdict": "HOLD",
                    "reasoning_md": (
                        f"Current validated order sheet: NO ACTION. {no_action.reason}"
                    ),
                    "decision_basis": "CURRENT_ORDER_SHEET",
                    "decision_inputs": [
                        {
                            "source": "order_sheet",
                            "fingerprint": current.fingerprint,
                            "decision": "NO_ACTION",
                        }
                    ],
                    "last_fleet_check_at": current.sheet.generated_at.isoformat(),
                }
            )
        projected.append(row)
    return projected


__all__ = [
    "CurrentOrderSheet",
    "load_current_order_sheet",
    "overlay_position_theses",
]
