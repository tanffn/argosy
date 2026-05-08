"""Plan amendment flow — chat-borne plan changes (Wave 4).

Public API:
  - classify(intent) → ClassificationResult
  - run_small(session, *, user_id, message, intent) → AmendmentResultDTO
  - dispatch_async(session, *, user_id, message, tier, intent, cancel_existing) → AmendmentResultDTO
  - cancel(session, *, user_id, decision_run_id) → bool

Re-exports the monkeypatchable internals so tests' `from argosy.orchestrator.flows
import plan_amendment as flow; monkeypatch.setattr(flow, "_spawn_worker", ...)`
patterns work (mirrors Wave 2's plan_synthesis package convention).
"""

from __future__ import annotations

from argosy.orchestrator.flows.plan_amendment._types import (
    ClassificationResult,
    EffectiveTier,
)
from argosy.orchestrator.flows.plan_amendment.classifier import classify
from argosy.orchestrator.flows.plan_amendment.dispatcher import (
    cancel,
    dispatch_async,
    run_small,
    _spawn_worker,
)
from argosy.orchestrator.flows.plan_amendment.workers import (
    _large_worker,
    _medium_worker,
    _run_phase_3_synthesizer,
)

__all__ = [
    "ClassificationResult",
    "EffectiveTier",
    "_large_worker",
    "_medium_worker",
    "_run_phase_3_synthesizer",
    "_spawn_worker",
    "cancel",
    "classify",
    "dispatch_async",
    "run_small",
]
