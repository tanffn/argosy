"""Derive a job's *outcome* status from the work it reported.

The fail-open bug this closes: a cadence tick that returns without
raising was closed ``status="ok"`` even when its ``output_summary``
plainly said the work failed (``adapter_errors>0``, a non-empty
``errors`` list, every per-item ``status:"error"``, zero work done,
...). "Did the tick raise?" is the wrong question — "did the work
succeed?" is. This module answers the second one.

The **summary failure contract** (documented, small, opt-in): a loop's
``tick()`` may put any of the following in its returned ``output_summary``
dict to signal that work failed. The scheduler close path
(:mod:`argosy.services.jobs.registered_scheduler` and the base
:class:`~argosy.orchestrator.scheduler.Scheduler`) inspects the summary
and DERIVES the close status from it:

* ``status`` — a top-level string in :data:`_FAILED_STATUS_STRINGS`
  (``"error"`` / ``"failed"`` / ``"degraded"``).
* ``adapter_errors`` — int ``> 0`` (the prediction-evaluator / adapter
  convention: N upstream fetches failed this tick).
* ``errors`` — a **non-empty** list/tuple of error entries. An empty
  ``[]`` is the healthy sentinel and is ignored.
* ``error`` — a non-empty top-level scalar or container. Several cadence
  loops deliberately catch an exception so the scheduler stays alive and
  return ``{"error": "..."}``; that is a failed tick, not a green run.
* ``stages`` — an explicitly declared mapping of stage names to work
  summaries or terminal status strings (``ok`` / ``skipped`` or a failure).
  Summaries are recursively evaluated by this same contract. Unrelated nested
  business payloads are not inspected for incidental error-like fields.
* any of the count keys in :data:`_FAILURE_COUNT_KEYS` — int ``> 0``
  (``failures`` / ``failed`` / ``failure_count`` / ``error_count`` /
  ``failed_streams`` / ``streams_failed`` / ``failed_count``).
* per-item mappings under ``streams`` (dict) or ``items`` / ``results``
  (list): if **any** child item carries a failed ``status``, the tick
  did not fully succeed → degrade. (Partial failure is still a failure
  the operator must see — SDD "make failures visible".)
* **zero-work-done**: an ``attempted`` / ``total`` / ``items_total``
  count ``> 0`` while the matching success count
  (``succeeded`` / ``ok`` / ``ok_count`` / ``items_ok``) is ``0`` — every
  unit of declared work failed.

Because the ``job_runs.status`` CHECK enum is
``running/ok/error/skipped/cancelled`` (migration 0048) there is no
distinct ``"degraded"`` value to persist; both *partial* and *total*
failure map to :data:`FAILURE_STATUS` (``"error"``) so the run renders
non-green everywhere (UI, watchdog, retention). The distinction is kept
in the human-readable *reason* the deriver returns, and a future
migration can widen the enum to split degraded from error without
touching this contract.

The deriver is intentionally conservative: it only fires on the keys
above with the shapes above, so a loop that returns unrelated payload
keys is never mis-flagged.
"""

from __future__ import annotations

from typing import Any

#: The close status used for any derived failure. Constrained to the
#: ``job_runs.status`` CHECK enum (migration 0048) — no ``"degraded"``.
FAILURE_STATUS = "error"
OK_STATUS = "ok"

#: Top-level ``status`` strings that mean "this tick failed".
_FAILED_STATUS_STRINGS = frozenset({"error", "failed", "degraded", "fail"})

#: Integer count keys where ``value > 0`` signals failure.
_FAILURE_COUNT_KEYS = (
    "adapter_errors",
    "failures",
    "failed",
    "failure_count",
    "failed_count",
    "error_count",
    "failed_streams",
    "streams_failed",
)

#: Keys under which a total-attempted count may live (zero-work check).
_ATTEMPTED_KEYS = ("attempted", "total", "items_total", "processed")
#: Matching success-count keys.
_SUCCEEDED_KEYS = ("succeeded", "ok", "ok_count", "items_ok", "success")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _item_status_failed(item: Any) -> bool:
    if isinstance(item, dict):
        st = item.get("status")
        return isinstance(st, str) and st.lower() in _FAILED_STATUS_STRINGS
    return False


def derive_run_status(summary: Any) -> tuple[str, str | None]:
    """Return ``(status, reason)`` for a tick's ``output_summary``.

    ``status`` is :data:`OK_STATUS` when no failure signal is present,
    else :data:`FAILURE_STATUS`. ``reason`` is a short human string when
    a failure was derived (for ``error_message`` / logging), else
    ``None``. Non-dict / ``None`` summaries are treated as ``ok`` (there
    is nothing to contradict a raise-free tick).
    """
    if not isinstance(summary, dict):
        return OK_STATUS, None

    # 1. Explicit top-level status.
    top = summary.get("status")
    if isinstance(top, str) and top.lower() in _FAILED_STATUS_STRINGS:
        return FAILURE_STATUS, f"summary.status={top!r}"

    if "stages" in summary:
        stages = summary["stages"]
        if not isinstance(stages, dict):
            return FAILURE_STATUS, "summary.stages must be a mapping"
        for name, stage in stages.items():
            if isinstance(stage, str):
                terminal = stage.strip().lower()
                if terminal in {"ok", "skipped"}:
                    continue
                # Unknown/in-progress states do not prove finished work.
                return FAILURE_STATUS, f"stage {name!r}: status={stage!r}"
            if not isinstance(stage, dict):
                return FAILURE_STATUS, f"stage {name!r} must be a work summary"
            status, reason = derive_run_status(stage)
            if status != OK_STATUS:
                return status, f"stage {name!r}: {reason}"

    # 2/4. Integer failure counts.
    for key in _FAILURE_COUNT_KEYS:
        n = _as_int(summary.get(key))
        if n is not None and n > 0:
            return FAILURE_STATUS, f"{key}={n}"

    # 3. Non-empty errors list.
    errors = summary.get("errors")
    if isinstance(errors, (list, tuple)) and len(errors) > 0:
        return FAILURE_STATUS, f"errors[{len(errors)}]"

    # 3a. A caught exception reported as one top-level error.  Keep the
    # predicate shape-aware so the common healthy sentinels (None, "", [], {})
    # remain green.
    error = summary.get("error")
    if error not in (None, "", [], {}, ()):
        return FAILURE_STATUS, "summary.error is non-empty"

    # 5. Per-item mappings — any failed child degrades the whole tick.
    streams = summary.get("streams")
    if isinstance(streams, dict) and streams:
        failed = [k for k, v in streams.items() if _item_status_failed(v)]
        if failed:
            reason = (
                "all streams failed"
                if len(failed) == len(streams)
                else f"{len(failed)}/{len(streams)} streams failed"
            )
            return FAILURE_STATUS, f"{reason}: {', '.join(map(str, failed))}"
    for list_key in ("items", "results"):
        seq = summary.get(list_key)
        if isinstance(seq, (list, tuple)) and seq:
            failed = [i for i in seq if _item_status_failed(i)]
            if failed:
                reason = (
                    f"all {list_key} failed"
                    if len(failed) == len(seq)
                    else f"{len(failed)}/{len(seq)} {list_key} failed"
                )
                return FAILURE_STATUS, reason

    # 6. Zero-work-done: attempted > 0 but succeeded == 0.
    for a_key in _ATTEMPTED_KEYS:
        attempted = _as_int(summary.get(a_key))
        if attempted is None or attempted <= 0:
            continue
        for s_key in _SUCCEEDED_KEYS:
            succeeded = _as_int(summary.get(s_key))
            if succeeded is None:
                continue
            if succeeded == 0:
                return (
                    FAILURE_STATUS,
                    f"zero work done ({a_key}={attempted}, {s_key}=0)",
                )
            break

    return OK_STATUS, None


def summary_signals_failure(summary: Any) -> bool:
    """Convenience boolean wrapper over :func:`derive_run_status`."""
    return derive_run_status(summary)[0] != OK_STATUS


__all__ = [
    "FAILURE_STATUS",
    "OK_STATUS",
    "derive_run_status",
    "summary_signals_failure",
]
