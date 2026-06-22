"""Typed envelope for the inbox feed.

One discriminated union (``InboxItem``) over heterogeneous bodies, sharing a
single attention contract: a plain-language title, a one-line ``why_now``, a
server-computed ``rank_reason``, a primary action + secondary actions (Defer /
Dismiss / …), and an expandable typed ``body``. Actions are SEMANTIC
(``intent`` / ``label`` / ``style``) — never a raw API path or internal enum,
so the client never reconstructs meaning from mechanics.

Nothing in the user-visible fields (``title`` / ``why_now`` / ``rank_reason`` /
action ``label`` / ``body``) may leak internal jargon — no ``T2``,
``account_class``, proposal ids, raw statuses, or "escalate tier". Internal
plumbing (the raw status, source ids, signals the policy reads) lives in
``signals`` / ``source_refs`` and is only echoed in the DEBUG view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Literal, Optional

# The kinds of item the inbox can hold. ``trade``/``cash_deploy``/``plan_task``/
# ``note`` exist over today's sources; ``discovery_buy``/``switch`` are reserved
# for the funding-aware / discovery-driven follow-on (they slot into this union
# without a contract change).
InboxKind = Literal[
    "trade",
    "cash_deploy",
    "plan_task",
    "note",
    "discovery_buy",
    "switch",
]

ActionStyle = Literal["primary", "secondary", "danger"]

# Semantic action intents. The client maps an intent → the concrete handler
# (which API call to make); the wire never carries the path itself.
ActionIntent = Literal[
    "approve",
    "reject",
    "ask_deeper_review",
    "execute",
    "defer",
    "dismiss",
    "mark_done",
    "review_cash",
    "customize",
    "view_reasoning",
]


class PriorityBucket(IntEnum):
    """The explicit, legible attention order (spec §Prioritisation).

    Lower ordinal = higher up the queue. Each item is assigned exactly one
    bucket by the policy; within a bucket the deterministic ``sort_key`` breaks
    ties. The bucket's plain-English name is what the client may show as a
    section label.
    """

    OVERDUE_BLOCKING = 1  # overdue / expiring / blocking — act or lose the window
    RISK_REDUCTION = 2  # sell / rebalance / concentration / drift — inaction has downside
    PLAN_COMMITMENT = 3  # dated plan to-dos, required info
    MATERIAL_CASH = 4  # idle cash above the plan band
    OPPORTUNITY = 5  # opportunity / speculative — only if it needs a decision
    OBSERVATION = 6  # low-risk observations, only if material

    @property
    def label(self) -> str:
        return {
            PriorityBucket.OVERDUE_BLOCKING: "Overdue or expiring",
            PriorityBucket.RISK_REDUCTION: "Reduce risk",
            PriorityBucket.PLAN_COMMITMENT: "Plan commitments",
            PriorityBucket.MATERIAL_CASH: "Put cash to work",
            PriorityBucket.OPPORTUNITY: "Opportunities",
            PriorityBucket.OBSERVATION: "Worth a look",
        }[self]


@dataclass(frozen=True)
class InboxAction:
    """A semantic action the user can take on an item.

    ``intent`` is the verb the client maps to a handler; ``label`` is the
    plain-English button text; ``style`` drives emphasis. ``requires_confirmation``
    asks the client to confirm before firing (used for money-moving / execute).
    """

    intent: ActionIntent
    label: str
    style: ActionStyle = "secondary"
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "label": self.label,
            "style": self.style,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class SourceRef:
    """Stable pointer back to the canonical source row an item came from.

    Used for dedupe (two sources describing the same need), for the action
    handlers (which row to act on), and for the debug view. Not shown in
    user-visible copy.
    """

    source: str  # "trade_proposal" | "action_proposal" | "plan_action_item" | "cash_detector" | …
    ref_id: str  # stable id within that source (str so plan item_ids fit)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "ref_id": self.ref_id}


@dataclass(frozen=True)
class TraceRef:
    """Links to the decision-funnel trace, when the item originated there."""

    funnel_run_id: Optional[str] = None
    decision_snapshot_id: Optional[str] = None
    inbox_run_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "funnel_run_id": self.funnel_run_id,
            "decision_snapshot_id": self.decision_snapshot_id,
            "inbox_run_id": self.inbox_run_id,
        }


@dataclass
class InboxItem:
    """One item in the feed.

    Adapters build the envelope + ``signals`` (the raw inputs the policy reads:
    severity, tier, days-until, risk-reducing, speculative, …) and leave
    ``bucket`` / ``sort_key`` / ``rank_reason`` unset. The policy fills those.
    Anything the policy needs to decide rank goes in ``signals``; anything the
    user reads goes in the envelope/body.
    """

    id: str  # stable composite id, e.g. "trade:123" / "plan_task:rsu_withholding"
    kind: InboxKind
    title: str
    why_now: str
    primary_action: Optional[InboxAction] = None
    secondary_actions: list[InboxAction] = field(default_factory=list)
    body: dict[str, Any] = field(default_factory=dict)
    due_at: Optional[str] = None  # ISO date/datetime — soonest sorts first
    expires_at: Optional[str] = None
    amount_usd: Optional[float] = None
    source_refs: list[SourceRef] = field(default_factory=list)
    trace: Optional[TraceRef] = None
    signals: dict[str, Any] = field(default_factory=dict)

    # --- set by the policy (not by adapters) ---
    bucket: Optional[PriorityBucket] = None
    sort_key: tuple = ()
    rank_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Client-facing projection. Excludes ``signals`` and the raw
        ``sort_key`` (debug-only); the debug view serializes those separately."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "why_now": self.why_now,
            "rank_reason": self.rank_reason,
            "bucket": int(self.bucket) if self.bucket is not None else None,
            "bucket_label": self.bucket.label if self.bucket is not None else None,
            "primary_action": self.primary_action.to_dict() if self.primary_action else None,
            "secondary_actions": [a.to_dict() for a in self.secondary_actions],
            "body": self.body,
            "due_at": self.due_at,
            "expires_at": self.expires_at,
            "amount_usd": self.amount_usd,
            "source_refs": [s.to_dict() for s in self.source_refs],
            "trace": self.trace.to_dict() if self.trace else None,
        }

    def to_debug_dict(self) -> dict[str, Any]:
        """Debug projection — includes the policy inputs + raw sort key."""
        d = self.to_dict()
        d["signals"] = self.signals
        d["sort_key"] = list(self.sort_key)
        return d


@dataclass
class InboxLiveness:
    """Small "Argosy is watching" signals for the quiet/steady state.

    All booleans are POSITIVE-framed (true = the reassuring case), so the
    client can render them as a row of green checks without re-deriving.
    """

    last_checked: str  # ISO timestamp
    pending_decisions: int
    open_approvals: int
    cash_within_band: bool
    no_overdue_tasks: bool
    next_review: Optional[str] = None  # ISO date or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_checked": self.last_checked,
            "pending_decisions": self.pending_decisions,
            "open_approvals": self.open_approvals,
            "cash_within_band": self.cash_within_band,
            "no_overdue_tasks": self.no_overdue_tasks,
            "next_review": self.next_review,
        }


@dataclass
class InboxFeed:
    """The assembled, ranked feed + metadata returned by ``build_inbox``."""

    items: list[InboxItem]
    liveness: InboxLiveness
    policy_version: str
    generated_at: str
    # Items the policy considered but did NOT surface (suppressed by materiality,
    # shadow-mode, or dedupe), each with a reason. Empty in the client projection;
    # populated only for the debug view.
    dropped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return len(self.items) == 0

    def to_dict(self, *, debug: bool = False) -> dict[str, Any]:
        return {
            "items": [
                it.to_debug_dict() if debug else it.to_dict() for it in self.items
            ],
            "quiet": self.quiet,
            "needs_you_count": len(self.items),
            "liveness": self.liveness.to_dict(),
            "policy_version": self.policy_version,
            "generated_at": self.generated_at,
            "dropped": self.dropped if debug else [],
        }


__all__ = [
    "ActionIntent",
    "ActionStyle",
    "InboxAction",
    "InboxFeed",
    "InboxItem",
    "InboxKind",
    "InboxLiveness",
    "PriorityBucket",
    "SourceRef",
    "TraceRef",
]
