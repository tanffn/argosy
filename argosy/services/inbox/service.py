"""``build_inbox`` — assemble the ranked feed from today's canonical sources.

Each source has a small adapter that maps its rows into ``InboxItem``s with the
raw policy ``signals`` attached. The service then dedupes overlapping needs,
hands everything to the policy for bucketing + ranking, and wraps the result
with quiet-state liveness metadata.

Design rules honoured here:
  * Shadow-mode proposals NEVER reach the feed (they are recorded for
    calibration only) — the trade adapter filters them at the query.
  * A resolved / self-verified plan task DISAPPEARS from the queue (the client
    is in the loop only when something needs them) — the plan adapter drops
    acknowledged + Argosy-verified items.
  * A single failing source must not blank the whole inbox — each adapter is
    isolated; a failure is logged and recorded in ``dropped`` (debug), and the
    rest of the feed still renders.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argosy.services.inbox.policy import DEFAULT_POLICY, InboxPolicy, rank_items
from argosy.services.inbox.types import (
    InboxAction,
    InboxFeed,
    InboxItem,
    InboxLiveness,
    PriorityBucket,
    SourceRef,
)

_log = logging.getLogger(__name__)

# Trade-proposal statuses that genuinely need the user. Everything else
# (draft / cooling / executed / rejected / expired / …) is either pre-surface
# or already resolved.
_AWAITING_STATUSES = ("awaiting_human",)
_READY_TO_EXECUTE_STATUSES = ("approved",)
_ACTIONABLE_TRADE_STATUSES = _AWAITING_STATUSES + _READY_TO_EXECUTE_STATUSES

# Note kinds that describe an idle-cash need — deduped away when the dedicated
# cash-deployment item is present (one decision, not two competing ones).
_CASH_NOTE_HINT = "cash"

# Action-proposal kinds whose downside-of-inaction makes a critical flag a
# risk-reduction decision rather than a passive observation.
_RISK_NOTE_HINTS = ("drift", "concentration", "cash", "exposure", "risk", "rebalance")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _adapt_trades(db: Session, user_id: str, today: date) -> list[InboxItem]:
    from argosy.state.models import Proposal as ProposalRow

    # Shadow proposals are recorded but never surfaced. Treat NULL (pre-migration
    # rows) as not-shadow. Same gate the public /api/proposals list applies.
    shadow_clause = func.coalesce(ProposalRow.shadow, 0) == 0
    stmt = (
        select(ProposalRow)
        .where(ProposalRow.user_id == user_id)
        .where(ProposalRow.status.in_(_ACTIONABLE_TRADE_STATUSES))
        .where(shadow_clause)
        .order_by(ProposalRow.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    items: list[InboxItem] = []
    for r in rows:
        speculative = r.account_class == "limited"
        action = (r.action or "").lower()
        expiring_in_days: int | None = None
        if r.expires_at is not None:
            expiring_in_days = (r.expires_at.date() - today).days
        ready = r.status in _READY_TO_EXECUTE_STATUSES

        if ready:
            primary = InboxAction("execute", "Execute now", "primary", requires_confirmation=True)
            secondary = [InboxAction("view_reasoning", "See the reasoning", "secondary")]
        else:
            primary = InboxAction("approve", "Approve", "primary")
            secondary = [
                InboxAction("reject", "Reject", "danger"),
                InboxAction("ask_deeper_review", "Ask for a deeper review", "secondary"),
                InboxAction("view_reasoning", "See the reasoning", "secondary"),
            ]

        verb = action.capitalize() if action else "Trade"
        body: dict[str, Any] = {
            "rationale": r.rationale_summary or "",
            "order_line": f"{r.size_shares_or_currency:g} {r.size_units} · {r.order_type}",
            "instrument": r.instrument,
            "speculative": speculative,
        }
        if r.confidence:
            body["conviction"] = r.confidence

        items.append(
            InboxItem(
                id=f"trade:{r.id}",
                kind="trade",
                title=f"{verb} {r.ticker}",
                why_now=_trim(r.rationale_summary) or f"A {verb.lower()} decision is waiting on you.",
                primary_action=primary,
                secondary_actions=secondary,
                body=body,
                expires_at=r.expires_at.isoformat() if r.expires_at else None,
                source_refs=[SourceRef("trade_proposal", str(r.id))],
                signals={
                    "action": action,
                    "speculative": speculative,
                    "expiring_in_days": expiring_in_days,
                    "tier": r.tier,
                    "status": r.status,
                    "conviction": r.confidence,
                    "ready_to_execute": ready,
                },
            )
        )
    return items


def _adapt_notes(db: Session, user_id: str) -> list[InboxItem]:
    from argosy.services.action_proposals import list_open_action_proposals, to_view

    rows = list_open_action_proposals(db, user_id)
    items: list[InboxItem] = []
    for row in rows:
        v = to_view(row)
        kind_lc = (v.kind or "").lower()
        risk_kind = any(h in kind_lc for h in _RISK_NOTE_HINTS)
        items.append(
            InboxItem(
                id=f"note:{v.id}",
                kind="note",
                title=v.summary or "Something to look at",
                why_now=_trim(v.rationale_md) or "Argosy flagged this while watching your portfolio.",
                primary_action=InboxAction("accept", "Accept", "primary"),
                secondary_actions=[
                    InboxAction("defer", "Defer", "secondary"),
                    InboxAction("dismiss", "Dismiss", "secondary"),
                ],
                body={"detail": v.rationale_md or ""},
                expires_at=v.expires_at or None,
                source_refs=[SourceRef("action_proposal", str(v.id))],
                signals={
                    "severity": v.severity,
                    "note_kind": v.kind,
                    "risk_kind": risk_kind,
                    "is_cash_note": _CASH_NOTE_HINT in kind_lc,
                },
            )
        )
    return items


def _adapt_plan_tasks(db: Session, user_id: str, today: date) -> list[InboxItem]:
    # Reuse the exact home-page action-item collection so the inbox and the home
    # checklist agree item-for-item. These helpers live in the plan route module.
    from argosy.api.routes.plan import _collect_action_items, _load_action_acks
    from argosy.state.queries import get_current_plan, get_pending_draft

    pv = get_pending_draft(db, user_id) or get_current_plan(db, user_id)
    if pv is None:
        return []
    acked = _load_action_acks(db, user_id)
    withholding_status: dict[str, Any] | None = None
    try:
        from argosy.services.payslip_ingest import withholding_action_status

        withholding_status = withholding_action_status(user_id, db)
    except Exception:  # noqa: BLE001 — optional enrichment, never blocks the feed
        withholding_status = None

    collected = _collect_action_items(
        pv,
        today=today,
        window_days=14,
        acked=acked,
        withholding_status=withholding_status,
    )
    items: list[InboxItem] = []
    for a in collected:
        # The client is in the loop ONLY when something needs them: a task the
        # user already marked done, or one Argosy itself verified, DISAPPEARS.
        if a.acknowledged or a.argosy_verified:
            continue
        days_until = a.days_until
        days_overdue = -days_until if (days_until is not None and days_until < 0) else None
        items.append(
            InboxItem(
                id=f"plan_task:{a.item_id}",
                kind="plan_task",
                title=a.label,
                why_now=_trim(a.detail) or "A commitment from your plan.",
                primary_action=InboxAction("mark_done", "Mark done", "primary"),
                secondary_actions=[],
                body={
                    "detail": a.detail,
                    "how_to": a.how_to,
                    "done_when": a.done_when,
                    "content_fingerprint": a.content_fingerprint,
                },
                due_at=a.dated.isoformat() if a.dated else None,
                source_refs=[SourceRef("plan_action_item", a.item_id)],
                signals={
                    "status": a.status,
                    "days_until": days_until,
                    "days_overdue": days_overdue,
                },
            )
        )
    return items


def _adapt_cash(db: Session, user_id: str, today: date) -> list[InboxItem]:
    from argosy.services.unallocated_cash_detector import detect_unallocated_cash_overage

    event = detect_unallocated_cash_overage(db, user_id=user_id, today=today)
    if event is None or event.excess_usd <= 0:
        return []
    buy_list = [
        {
            "instrument": p.instrument,
            "asset_class": p.asset_class,
            "amount_usd": round(p.amount_usd, 2),
            "rationale": p.rationale,
        }
        for p in event.proposals
    ]
    return [
        InboxItem(
            id="cash_deploy",
            kind="cash_deploy",
            title="Put your idle cash to work",
            why_now=_trim(event.headline) or "You have cash sitting above your plan target.",
            primary_action=InboxAction("review_cash", "See where it goes", "primary"),
            secondary_actions=[InboxAction("defer", "Not now", "secondary")],
            body={"headline": event.headline, "buy_list": buy_list},
            amount_usd=event.excess_usd,
            source_refs=[SourceRef("cash_detector", event.snapshot_date or "current")],
            signals={"excess_usd": event.excess_usd},
        )
    ]


_ADAPTERS = {
    "trades": _adapt_trades,
    "notes": lambda db, user_id, today: _adapt_notes(db, user_id),
    "plan_tasks": _adapt_plan_tasks,
    "cash": _adapt_cash,
}


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def _dedupe(items: list[InboxItem]) -> tuple[list[InboxItem], list[InboxItem]]:
    """Drop overlapping needs so the user sees ONE decision, not two.

    Today's only overlap: the dedicated cash-deployment item subsumes any
    "cash piling up" note. Returns ``(kept, dropped)``.
    """
    has_cash_item = any(i.kind == "cash_deploy" for i in items)
    kept: list[InboxItem] = []
    dropped: list[InboxItem] = []
    for it in items:
        if has_cash_item and it.kind == "note" and it.signals.get("is_cash_note"):
            dropped.append(it)
        else:
            kept.append(it)
    return kept, dropped


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_inbox(
    db: Session,
    *,
    user_id: str,
    policy: InboxPolicy = DEFAULT_POLICY,
    today: date | None = None,
) -> InboxFeed:
    """Build the ranked inbox feed for ``user_id`` from today's sources."""
    today = today or datetime.now(timezone.utc).date()
    raw: list[InboxItem] = []
    dropped: list[dict[str, Any]] = []

    for name, adapter in _ADAPTERS.items():
        try:
            raw.extend(adapter(db, user_id, today))
        except Exception:  # noqa: BLE001 — isolate a failing source; never blank the feed
            _log.exception("inbox.adapter_failed", extra={"adapter": name, "user_id": user_id})
            dropped.append({"id": f"<adapter:{name}>", "reason": "source_error", "kind": name})

    deduped, dedup_dropped = _dedupe(raw)
    for d in dedup_dropped:
        dropped.append({"id": d.id, "reason": "deduped_into_cash_deploy", "kind": d.kind})

    surfaced, suppressed = rank_items(deduped, policy)
    for s in suppressed:
        dropped.append({"id": s.id, "reason": "below_materiality", "kind": s.kind})

    # Quiet-state liveness signals (all positive-framed).
    open_approvals = sum(1 for i in surfaced if i.kind == "trade")
    cash_within_band = not any(i.kind == "cash_deploy" for i in surfaced)
    no_overdue = not any(i.bucket == PriorityBucket.OVERDUE_BLOCKING for i in surfaced)
    future_due = sorted(
        i.due_at for i in surfaced if i.kind == "plan_task" and i.due_at and i.due_at >= today.isoformat()
    )
    liveness = InboxLiveness(
        last_checked=_utcnow_iso(),
        pending_decisions=len(surfaced),
        open_approvals=open_approvals,
        cash_within_band=cash_within_band,
        no_overdue_tasks=no_overdue,
        next_review=future_due[0] if future_due else None,
    )

    return InboxFeed(
        items=surfaced,
        liveness=liveness,
        policy_version=policy.version,
        generated_at=_utcnow_iso(),
        dropped=dropped,
    )


__all__ = ["build_inbox"]
