"""In-process job registry (Spec A — `docs/superpowers/specs/2026-05-29-jobs-registry-design.md`).

Sprint A commit #3a lands the shell:

* :class:`JobRegistry` — per-job audit recorder + lock owner. Composes
  the existing :class:`argosy.orchestrator.scheduler.Scheduler` via
  :class:`RegisteredScheduler` (sub-class composition, §1.6 — codex
  BLOCKER #5).
* :class:`RegisteredScheduler` — subclass that wraps every
  ``_fire_once`` with the registry's ``_open_job_run`` / ``_close_job_run``
  helpers. Ordering invariant (§1.7): ``_close_job_run`` runs BEFORE
  ``_record_tick``.
* :class:`RetryConfig` + :func:`retry_transient` — bounded transient
  retry for transport-only errors (§1.8). Business-rule + LLM-content
  errors hard-fail.

Lifecycle wiring into ``argosy/api/main.py`` lands in commit #3b. This
module is constructible today; nothing in here auto-runs.
"""

from __future__ import annotations

from argosy.services.jobs.registered_scheduler import RegisteredScheduler
from argosy.services.jobs.registry import (
    AlreadyRunning,
    JobMetadata,
    JobRegistry,
    JobView,
)
from argosy.services.jobs.retry import RetryConfig, retry_transient

__all__ = [
    "AlreadyRunning",
    "JobMetadata",
    "JobRegistry",
    "JobView",
    "RegisteredScheduler",
    "RetryConfig",
    "retry_transient",
]
