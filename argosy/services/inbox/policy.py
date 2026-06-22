"""Inbox attention-ordering policy — versioned, deterministic, legible.

The crux of the inbox is that priority is NOT a mystery score: order is an
explicit, explainable policy and every item states WHY it is ranked where it
is. This module is that policy, single-sourced and content-hashed so every
feed records exactly which thresholds it ranked against, and a change is one
auditable edit (mirrors ``decision_funnel/policy.py`` — but a SEPARATE concern:
the funnel decides investment action, this decides human attention order).

Responsibilities:
  * ``assign_bucket`` — map an item's kind + signals to exactly one
    ``PriorityBucket`` (or ``None`` to suppress it from the queue).
  * ``sort_key`` — a deterministic within-bucket tiebreak (soonest deadline →
    largest dollars → highest severity/tier → stable id).
  * ``rank_reason`` — a server-computed plain-English sentence built from the
    item's real signals (days overdue, dollars affected, …). The client shows
    it verbatim; it never reconstructs meaning from enums.

All thresholds are conservative — this is a long-hold book, and the inbox's
job is to be QUIET unless something genuinely needs the user.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from argosy.services.inbox.types import InboxItem, PriorityBucket

# Sentinel for "no deadline" so dated items sort ahead of undated ones within a
# bucket (ISO strings compare lexically; this sorts after any real date).
_NO_DEADLINE = "~"  # '~' (0x7e) sorts after digits and uppercase letters


@dataclass(frozen=True)
class InboxPolicy:
    """Versioned attention-ordering thresholds. Frozen + content-hashed."""

    # --- materiality: below the bar → not surfaced (drops to audit/history) ---
    # Idle cash below this dollar overage isn't worth a queue slot.
    material_cash_usd: float = 5_000.0
    # System notes only surface at these severities; ``info`` notes are audit,
    # not action (spec: low-risk observations only if material).
    note_surface_severities: tuple[str, ...] = ("warning", "critical")

    # --- "expiring/blocking" window: a trade this close to expiry jumps to the
    # top regardless of its kind, because inaction loses the window. ---
    expiring_soon_days: int = 3

    # --- trade actions that REDUCE risk (inaction has downside) → bucket 2.
    # Everything else trade-shaped is an opportunity (bucket 5). ---
    risk_reducing_actions: tuple[str, ...] = ("sell", "trim", "reduce", "rebalance")

    @property
    def version(self) -> str:
        """Short content hash — stamped on every feed."""
        blob = json.dumps(asdict(self), sort_keys=True, default=list)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
        return f"inbox-pol-{digest}"

    def to_dict(self) -> dict:
        return {**asdict(self), "version": self.version}


DEFAULT_POLICY = InboxPolicy()

# Severity / tier ranks for the within-bucket tiebreak (lower = more urgent).
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_TIER_RANK = {"T3": 0, "T2": 1, "T1": 2, "T0": 3}


# ---------------------------------------------------------------------------
# Bucket assignment
# ---------------------------------------------------------------------------


def assign_bucket(item: InboxItem, policy: InboxPolicy = DEFAULT_POLICY) -> PriorityBucket | None:
    """Return the item's bucket, or ``None`` to suppress it (not material).

    Reads ``item.kind`` + ``item.signals`` only — never user-visible copy.
    Recognised signals by kind:
      * plan_task : ``status`` ∈ {OVERDUE, TODAY, DUE_SOON, UPCOMING}
      * trade     : ``action``, ``speculative`` (bool), ``expiring_in_days`` (int|None)
      * cash_deploy: ``excess_usd`` (float)
      * note      : ``severity`` ∈ {info, warning, critical}, ``risk_kind`` (bool)
    """
    sig = item.signals
    kind = item.kind

    if kind == "plan_task":
        status = sig.get("status")
        if status == "OVERDUE":
            return PriorityBucket.OVERDUE_BLOCKING
        # TODAY / DUE_SOON / UPCOMING are all dated plan commitments.
        return PriorityBucket.PLAN_COMMITMENT

    if kind in ("trade", "discovery_buy", "switch"):
        expiring = sig.get("expiring_in_days")
        if expiring is not None and expiring <= policy.expiring_soon_days:
            return PriorityBucket.OVERDUE_BLOCKING
        action = str(sig.get("action", "")).lower()
        if action in policy.risk_reducing_actions or sig.get("risk_reducing"):
            return PriorityBucket.RISK_REDUCTION
        # Buys, discovery buys, switches, speculative ideas: an opportunity that
        # needs a decision but whose inaction has no downside.
        return PriorityBucket.OPPORTUNITY

    if kind == "cash_deploy":
        excess = float(sig.get("excess_usd", 0.0) or 0.0)
        if excess < policy.material_cash_usd:
            return None  # immaterial idle cash → not a queue item
        return PriorityBucket.MATERIAL_CASH

    if kind == "note":
        severity = str(sig.get("severity", "info")).lower()
        if severity not in policy.note_surface_severities:
            return None  # info-level notes are audit, not action
        # A critical risk-shaped note (drift / concentration / cash) is a
        # risk-reduction decision; otherwise it's an observation.
        if severity == "critical" and sig.get("risk_kind"):
            return PriorityBucket.RISK_REDUCTION
        return PriorityBucket.OBSERVATION

    # Unknown kind: be conservative and surface as an observation rather than
    # silently dropping (a new kind without a policy branch is a bug we want to
    # SEE, not hide).
    return PriorityBucket.OBSERVATION


# ---------------------------------------------------------------------------
# Within-bucket ordering
# ---------------------------------------------------------------------------


def sort_key(item: InboxItem) -> tuple:
    """Deterministic within-bucket tiebreak.

    Order of precedence:
      1. soonest deadline first (due_at, else expires_at, else sentinel),
      2. largest dollars first,
      3. highest severity then tier,
      4. stable id (so the order is total + reproducible).
    """
    sig = item.signals
    deadline = item.due_at or item.expires_at or _NO_DEADLINE
    amount = float(item.amount_usd or 0.0)
    severity_rank = _SEVERITY_RANK.get(str(sig.get("severity", "")).lower(), 9)
    tier_rank = _TIER_RANK.get(str(sig.get("tier", "")), 9)
    return (deadline, -amount, severity_rank, tier_rank, item.id)


# ---------------------------------------------------------------------------
# Rank reason — server-computed plain English
# ---------------------------------------------------------------------------


def _money(amount: float) -> str:
    """Compact plain-English dollars, e.g. $84k / $1.2M / $420."""
    a = abs(amount)
    if a >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 1_000:
        return f"${amount / 1_000:.0f}k"
    return f"${amount:.0f}"


def rank_reason(item: InboxItem, bucket: PriorityBucket) -> str:
    """A short, honest sentence explaining why the item ranks where it does.

    Built from the item's real signals so the user can trust the order
    (spec: "Top: overdue 3 days, affects $84k"). No internal enums leak.
    """
    sig = item.signals
    amount = item.amount_usd
    amount_clause = f" · affects {_money(amount)}" if amount else ""

    if bucket == PriorityBucket.OVERDUE_BLOCKING:
        if item.kind == "plan_task":
            days = sig.get("days_overdue")
            if isinstance(days, int) and days > 0:
                return f"Overdue by {days} day{'s' if days != 1 else ''}{amount_clause}."
            return f"Due now{amount_clause}."
        # trade-shaped, expiring
        exp = sig.get("expiring_in_days")
        if isinstance(exp, int):
            if exp <= 0:
                return f"Expires today — decide now{amount_clause}."
            return f"Expires in {exp} day{'s' if exp != 1 else ''}{amount_clause}."
        return f"Time-sensitive{amount_clause}."

    if bucket == PriorityBucket.RISK_REDUCTION:
        return f"Acting reduces downside risk{amount_clause}."

    if bucket == PriorityBucket.PLAN_COMMITMENT:
        status = sig.get("status")
        if status == "TODAY":
            return f"Due today{amount_clause}."
        days = sig.get("days_until")
        if isinstance(days, int) and days >= 0:
            return f"Due in {days} day{'s' if days != 1 else ''}{amount_clause}."
        return f"A commitment from your plan{amount_clause}."

    if bucket == PriorityBucket.MATERIAL_CASH:
        if amount:
            return f"{_money(amount)} idle cash above your plan target."
        return "Idle cash above your plan target."

    if bucket == PriorityBucket.OPPORTUNITY:
        conviction = str(sig.get("conviction", "")).upper()
        if conviction == "HIGH":
            return f"High-conviction idea{amount_clause}."
        if conviction == "LOW":
            return f"A lower-conviction idea to weigh{amount_clause}."
        return f"An opportunity to weigh{amount_clause}."

    # OBSERVATION
    severity = str(sig.get("severity", "")).lower()
    if severity == "critical":
        return f"Flagged as important{amount_clause}."
    return f"Worth a look{amount_clause}."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rank_items(
    items: list[InboxItem], policy: InboxPolicy = DEFAULT_POLICY
) -> tuple[list[InboxItem], list[InboxItem]]:
    """Assign bucket + sort_key + rank_reason, then order.

    Returns ``(surfaced, suppressed)``:
      * ``surfaced``   — items with a bucket, ordered (bucket asc, then sort_key),
                         each with ``rank_reason`` filled.
      * ``suppressed`` — items the policy dropped as immaterial (bucket is None),
                         returned so the caller can record them in the debug view.

    Mutates the surfaced items in place (sets ``bucket`` / ``sort_key`` /
    ``rank_reason``).
    """
    surfaced: list[InboxItem] = []
    suppressed: list[InboxItem] = []
    for it in items:
        bucket = assign_bucket(it, policy)
        if bucket is None:
            suppressed.append(it)
            continue
        it.bucket = bucket
        it.sort_key = sort_key(it)
        it.rank_reason = rank_reason(it, bucket)
        surfaced.append(it)
    surfaced.sort(key=lambda i: (int(i.bucket), i.sort_key))  # type: ignore[arg-type]
    return surfaced, suppressed


__all__ = [
    "DEFAULT_POLICY",
    "InboxPolicy",
    "assign_bucket",
    "rank_items",
    "rank_reason",
    "sort_key",
]
