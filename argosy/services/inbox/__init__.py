"""Inbox — the back-office "what needs me now?" feed.

The inbox is the server-owned, ranked, typed projection of every source that
can put an action on the user. The UI is a PURE projection: it receives an
ordered list of typed items + quiet-state metadata and renders them, branching
only on ``item.kind`` to pick a body component. It computes no queue
membership, rank, materiality, or rank-reason — those are domain decisions and
live here.

Layers:
  * ``types``   — the typed envelope (``InboxItem`` discriminated union),
                  semantic actions, source/trace refs, priority buckets,
                  the assembled ``InboxFeed``.
  * ``policy``  — the versioned, content-hashed attention-ordering policy:
                  bucket assignment, deterministic sort key, and the
                  server-computed plain-English ``rank_reason``.
  * ``service`` — ``build_inbox``: adapt each canonical source into items,
                  dedupe + gate materiality + suppress shadow, then rank.

This is deliberately separate from ``decision_funnel``: the funnel decides
INVESTMENT ACTION; the inbox decides HUMAN ATTENTION ORDER across investments,
plan tasks, cash, notes, and blockers.
"""

from argosy.services.inbox.types import (
    InboxAction,
    InboxFeed,
    InboxItem,
    InboxLiveness,
    PriorityBucket,
    SourceRef,
    TraceRef,
)

__all__ = [
    "InboxAction",
    "InboxFeed",
    "InboxItem",
    "InboxLiveness",
    "PriorityBucket",
    "SourceRef",
    "TraceRef",
]
