"""Argosy cadence orchestrator (Phase 2).

The orchestrator runs registered loops on independent schedules. Each
loop is a `CadenceLoop` subclass that exposes async `tick()` and a
schedule descriptor. Phase 2 ships ONE loop wired to live execution —
`daily_brief` — but the architecture accommodates the others
(minute/hour/weekly/monthly/quarterly/annual) without redesign.

See SDD §5.
"""

from __future__ import annotations

from argosy.orchestrator.scheduler import Scheduler

__all__ = ["Scheduler"]
