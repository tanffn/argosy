"""Current autonomous recommendations as inputs to the unified order sheet.

The decision funnel, portfolio review, and verdict-trigger sweep write durable
``proposals``.  The daily holdings reviewer writes one audit row for every
holding and, for a confirmed action, a ``stock_decision`` ActionProposal.  Both
families used to stop in their own queues and never reach the deployment author.
This module defines the narrow handoff into the one unified order sheet.

They are *judgment inputs*, not orders and not mandatory legs.  The deployment
author must reconcile them with the whole book into one voice; deterministic
code only decides whether a current recommendation exists and carries its
recorded provenance/size without changing the judgment.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select

from argosy.state.models import ActionProposal, HoldingReview, Proposal

AUTONOMOUS_PROPOSAL_SOURCES = frozenset(
    {
        "decision_funnel",
        "portfolio_review",
        "verdict_trigger_sweep",
    }
)


def load_actionable_recommendations(
    db: Any,
    *,
    user_id: str,
    as_of: datetime | None = None,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    """Return current autonomous buy/sell judgments in stable creation order.

    ``expires_at`` remains authoritative when present.  Rows without an expiry
    still age out after ``max_age_days`` so legacy producers cannot keep a stale
    recommendation alive indefinitely.
    """

    moment = as_of or datetime.now(UTC)
    cutoff = moment - timedelta(days=max(1, int(max_age_days)))
    # Once recommendations have been reconciled into the one open period
    # directive, their component rows are cancelled so the client cannot
    # approve two competing artifacts. Keep carrying those exact rows while
    # the unified directive is open; its payload is the durable linkage.
    linked_ids: set[int] = set()
    open_directives = db.execute(
        select(ActionProposal.suggested_payload).where(
            ActionProposal.user_id == user_id,
            ActionProposal.kind == "allocate",
            ActionProposal.status == "open",
            ActionProposal.dedup_key.like("period_directive:%"),
        )
    ).scalars().all()
    for raw_payload in open_directives:
        try:
            payload = json.loads(raw_payload or "{}")
            linked_ids.update(int(value) for value in payload.get("source_proposal_ids", []))
        except (TypeError, ValueError):
            continue

    proposal_rows = db.execute(
        select(Proposal)
        .where(
            Proposal.user_id == user_id,
            or_(
                Proposal.status == "awaiting_human",
                and_(
                    Proposal.status == "cancelled",
                    Proposal.id.in_(linked_ids),
                ),
            ),
            Proposal.shadow == 0,
            Proposal.source.in_(AUTONOMOUS_PROPOSAL_SOURCES),
            Proposal.action.in_(("buy", "sell")),
            Proposal.size_shares_or_currency > 0,
            Proposal.created_at >= cutoff,
            or_(Proposal.expires_at.is_(None), Proposal.expires_at >= moment),
        )
        .order_by(Proposal.created_at.asc(), Proposal.id.asc())
    ).scalars().all()
    out = [
        {
            "recommendation_key": f"proposal:{int(row.id)}",
            "proposal_id": int(row.id),
            "ticker": row.ticker.strip().upper(),
            "action": row.action.lower(),
            "size": float(row.size_shares_or_currency),
            "size_units": row.size_units,
            "rationale": row.rationale_summary,
            "confidence": row.confidence,
            "source": row.source,
            "decision_run_id": row.decision_run_id,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }
        for row in proposal_rows
    ]

    # The per-holding reviewer is deliberately not allowed to size trades.  It
    # decides the verb from fresh evidence; the deployment author sizes and
    # funds the action against the whole book.  Carry the latest review per
    # symbol when it is confirmed (``proposed`` or ``dedup_skipped``) or disputed
    # by the blind re-derivation (``held_unverified``). ``dedup_skipped`` means an
    # open ActionProposal already owns the UI slot; it does NOT make the newer
    # reviewed judgment disappear from unified-plan synthesis. A later
    # HOLD/ABSTAIN supersedes an earlier action naturally because selection
    # happens before filtering.
    review_rows = db.execute(
        select(HoldingReview)
        .where(
            HoldingReview.user_id == user_id,
            HoldingReview.reviewed_at >= cutoff,
        )
        .order_by(
            HoldingReview.reviewed_at.desc(),
            HoldingReview.id.desc(),
        )
    ).scalars().all()
    latest_by_symbol: dict[str, HoldingReview] = {}
    for row in review_rows:
        symbol = (row.symbol or "").strip().upper()
        if symbol and symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = row
    for symbol, row in latest_by_symbol.items():
        verdict = (row.verdict or "").strip().upper()
        if verdict not in {"BUY", "SELL", "TRIM"}:
            continue
        if row.outcome not in {"proposed", "dedup_skipped", "held_unverified"}:
            continue
        disputed = row.outcome == "held_unverified"
        out.append(
            {
                "recommendation_key": f"holding_review:{int(row.id)}",
                "proposal_id": None,
                "holding_review_id": int(row.id),
                "ticker": symbol,
                "action": verdict.lower(),
                # Sizing is a portfolio-level judgment.  Zero here means
                # explicitly unsized input, never a $0 trade recommendation.
                "size": None,
                "size_units": None,
                "rationale": row.reason,
                "confidence": row.confidence,
                "source": "holdings_review",
                "verification_status": "disputed" if disputed else "confirmed",
                "decision_run_id": None,
                "created_at": row.reviewed_at.isoformat(),
                "expires_at": (row.reviewed_at + timedelta(days=max_age_days)).isoformat(),
            }
        )

    out.sort(key=lambda row: (str(row.get("created_at") or ""), row["recommendation_key"]))
    return out


def recommendation_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Canonical stable identity used by the period-directive dedup check."""

    return sorted({int(row["proposal_id"]) for row in rows if row.get("proposal_id") is not None})


def recommendation_keys(rows: list[dict[str, Any]]) -> list[str]:
    """Stable identities for every input, including unsized holding reviews."""

    return sorted(
        {
            str(row.get("recommendation_key") or f"proposal:{int(row['proposal_id'])}")
            for row in rows
            if row.get("recommendation_key") or row.get("proposal_id") is not None
        }
    )


__all__ = [
    "AUTONOMOUS_PROPOSAL_SOURCES",
    "load_actionable_recommendations",
    "recommendation_ids",
    "recommendation_keys",
]
