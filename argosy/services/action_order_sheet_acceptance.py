"""Turn an accepted schema-bearing action proposal into executable proposals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.order_sheet import OrderSheet
from argosy.services.order_sheet_materializer import (
    materialize_order_sheet,
    order_sheet_fingerprint,
    resolve_order_sheet_accounts,
)


def materialize_action_order_sheet(
    session: Session,
    action_proposal: Any,
    *,
    payload_override: dict[str, Any] | None = None,
    funding_account_id: str | None = None,
) -> tuple[list[Any], str | None]:
    """Stage executable rows for a validated unified order-sheet proposal.

    Non-order-sheet action proposals are intentionally a no-op. The caller
    commits these rows atomically with the parent's acceptance transition.
    """
    if getattr(action_proposal, "kind", None) != "allocate":
        return [], None
    if payload_override is None:
        try:
            payload = json.loads(action_proposal.suggested_payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    else:
        payload = payload_override
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_type") != "validated_order_sheet"
        or payload.get("validation_status") != "validated"
        or not isinstance(payload.get("order_sheet"), dict)
    ):
        return [], None

    sheet = OrderSheet.model_validate(payload["order_sheet"])
    recorded_fingerprint = str(payload.get("order_sheet_fingerprint") or "")
    actual_fingerprint = order_sheet_fingerprint(sheet)
    if recorded_fingerprint and recorded_fingerprint != actual_fingerprint:
        raise ValueError("order-sheet fingerprint does not match the accepted payload")

    configured = (funding_account_id or "").strip() or None
    if configured is None:
        from argosy.config import get_settings

        configured = get_settings().order_sheet_funding_account_id
    funding, sell_accounts = resolve_order_sheet_accounts(
        session,
        sheet,
        funding_account_id=configured,
    )
    rows = materialize_order_sheet(
        session,
        sheet,
        funding_account_id=funding,
        sell_accounts_by_symbol=sell_accounts,
        approved_by_unified_acceptance=True,
    )
    # Acceptance means this sheet becomes the one executable list.  Retain
    # older rows as audit history, but remove their ability to surface or
    # execute beside the accepted sheet.
    from argosy.state.models import Proposal, ProposalHistory

    session.flush()
    current_ids = {int(row.id) for row in rows if getattr(row, "id", None) is not None}
    legacy_rows = list(
        session.execute(
            select(Proposal).where(
                Proposal.user_id == sheet.user_id,
                Proposal.status.in_(("awaiting_human", "approved", "cooling")),
            )
        ).scalars()
    )
    now = datetime.now(UTC)
    for legacy in legacy_rows:
        if int(legacy.id) in current_ids:
            continue
        legacy.status = "cancelled"
        legacy.updated_at = now
        session.add(
            ProposalHistory(
                proposal_id=legacy.id,
                status="cancelled",
                transitioned_at=now,
                transitioned_by="order_sheet_acceptance",
                note=f"Superseded by validated order sheet {actual_fingerprint}",
            )
        )
    return rows, funding or None


__all__ = ["materialize_action_order_sheet"]
