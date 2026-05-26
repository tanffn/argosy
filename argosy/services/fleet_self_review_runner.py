"""Fleet self-review runner — composes + persists the report.

Two public entry points:

  * ``generate_fleet_self_review(db, user_id, scope_kind, ...)``
    runs all detectors, composes markdown, persists a row in
    ``fleet_self_review_reports``, emits the WS event
    ``fleet_self_review.completed``.  Returns the persisted row.

  * ``schedule_post_synthesis_review(...)`` is the
    fire-and-forget background-thread variant fired from the
    plan_synthesis orchestrator after the FM verdict + draft are
    persisted.  Mirrors the pattern of
    ``_schedule_fm_objection_translation_precompute`` — daemon
    thread, fresh sessionmaker bound to the orchestrator's engine,
    failures swallowed.

Why split out from ``fleet_self_review.py``? Pure-function
detectors should stay free of DB-write / event-bus side effects so
they're trivially testable.  This module owns side effects.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.services.fleet_self_review import (
    ALL_DETECTORS,
    Finding,
    ReviewScope,
    finding_to_dict,
    run_all_detectors,
    severity_counts,
)
from argosy.state.models import FleetSelfReviewReport

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Markdown composition
# ----------------------------------------------------------------------


_SEVERITY_ICON = {"RED": "[RED]", "AMBER": "[AMBER]", "YELLOW": "[YELLOW]"}
_SEVERITY_ORDER = ("RED", "AMBER", "YELLOW")


def _render_finding(f: Finding) -> str:
    """One Finding → markdown block."""
    icon = _SEVERITY_ICON.get(f.severity, f"[{f.severity}]")
    head = f"### {icon} {f.detector} · {f.title}"
    lines = [head, ""]
    lines.append(f"**Category:** {f.category} · **Severity:** {f.severity}")
    lines.append("")
    if f.evidence:
        lines.append("**Evidence:**")
        lines.append("```json")
        lines.append(json.dumps(f.evidence, indent=2, default=str))
        lines.append("```")
    if f.suggested_fix:
        lines.append("")
        lines.append(f"**Suggested fix:** {f.suggested_fix}")
    lines.append("")
    return "\n".join(lines)


def compose_markdown(
    findings: list[Finding],
    stats: list[dict],
    *,
    scope: ReviewScope,
    scope_kind: str,
) -> str:
    """Deterministic markdown report — NO LLM, fully reproducible.

    Section order:
      1. TL;DR — severity counts
      2. Findings grouped by severity (RED first)
      3. Detector run-stats footer
    """
    counts = severity_counts(findings)
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# Fleet self-review · {scope_kind} · {now}")
    lines.append("")
    if scope.decision_run_id is not None:
        lines.append(f"Triggered by decision_run #{scope.decision_run_id}.")
    else:
        lines.append(
            f"Daily sweep, lookback {scope.lookback_days}d, "
            f"user `{scope.user_id}`."
        )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- **RED:** {counts['RED']} (must fix)"
    )
    lines.append(
        f"- **AMBER:** {counts['AMBER']} (should investigate)"
    )
    lines.append(
        f"- **YELLOW:** {counts['YELLOW']} (informational)"
    )
    lines.append("")
    if not findings:
        lines.append(
            "Detectors found no anomalies in scope.  Either the fleet is "
            "behaving cleanly or we're missing detector coverage — review "
            "the run-stats footer to see which detectors had any data to "
            "evaluate."
        )
        lines.append("")

    by_severity: dict[str, list[Finding]] = {"RED": [], "AMBER": [], "YELLOW": []}
    for f in findings:
        if f.severity in by_severity:
            by_severity[f.severity].append(f)

    for sev in _SEVERITY_ORDER:
        bucket = by_severity[sev]
        if not bucket:
            continue
        lines.append(f"## {sev} findings ({len(bucket)})")
        lines.append("")
        # Sort within severity by detector id for deterministic output.
        for f in sorted(bucket, key=lambda x: (x.detector, x.id)):
            lines.append(_render_finding(f))

    # Detector run-stats footer
    lines.append("## Detector run-stats")
    lines.append("")
    lines.append("| Detector | Name | OK | Count | Error |")
    lines.append("|---|---|---|---|---|")
    for s in stats:
        ok_cell = "yes" if s["ok"] else "no"
        err = s.get("error") or ""
        lines.append(
            f"| {s['detector']} | {s['name']} | {ok_cell} | {s['count']} | {err} |"
        )
    lines.append("")
    lines.append(
        f"_Self-review surface — see "
        f"`argosy/services/fleet_self_review.py` for detector "
        f"implementations.  Adding a detector: add one function + one "
        f"line in `ALL_DETECTORS`._"
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Public sync entry point
# ----------------------------------------------------------------------


def generate_fleet_self_review(
    db: Session,
    *,
    user_id: str,
    scope_kind: str = "manual",
    decision_run_id: int | None = None,
    lookback_days: int = 14,
) -> FleetSelfReviewReport:
    """Run every detector, compose markdown, persist + emit event.

    Args:
      db: open sync ``Session`` bound to the same engine that holds
          decision_runs / agent_reports / decision_phases.
      user_id: tenant scope.
      scope_kind: one of ``'post_synthesis' | 'daily' | 'manual'``.
          The DB CHECK constraint enforces this set.
      decision_run_id: when ``scope_kind='post_synthesis'``, the run
          we just finished.  Stored for the UI back-link.
      lookback_days: rolling window for the daily-sweep detectors.

    Returns:
      The persisted ``FleetSelfReviewReport`` row.

    Failure mode: a detector that raises is logged + skipped (its
    failure shows in the report's run-stats footer).  Persistence
    failure is logged + re-raised — the caller (orchestrator hook,
    daily-brief loop) wraps generation in its own try/except.
    """
    scope = ReviewScope(
        user_id=user_id,
        decision_run_id=decision_run_id,
        lookback_days=lookback_days,
    )

    findings, stats = run_all_detectors(db, scope)
    content_md = compose_markdown(
        findings, stats, scope=scope, scope_kind=scope_kind,
    )
    counts = severity_counts(findings)

    row = FleetSelfReviewReport(
        user_id=user_id,
        generated_at=datetime.now(timezone.utc),
        scope_kind=scope_kind,
        decision_run_id=decision_run_id,
        content_md=content_md,
        findings_json=json.dumps(
            [finding_to_dict(f) for f in findings], default=str,
        ),
        severity_summary_json=json.dumps(counts),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    log.info(
        "fleet_self_review.persisted",
        user_id=user_id,
        scope_kind=scope_kind,
        decision_run_id=decision_run_id,
        report_id=row.id,
        red=counts["RED"], amber=counts["AMBER"], yellow=counts["YELLOW"],
        findings_total=len(findings),
        detectors_total=len(ALL_DETECTORS),
    )

    # Best-effort WS event — the home page badge subscribes to this so
    # the user sees the badge populate without a refresh.  Failure is
    # logged + swallowed.
    try:
        from argosy.api.events import publish_event_threadsafe

        publish_event_threadsafe(
            "fleet_self_review.completed",
            {
                "user_id": user_id,
                "report_id": row.id,
                "scope_kind": scope_kind,
                "decision_run_id": decision_run_id,
                "severity_counts": counts,
            },
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "fleet_self_review.event_publish_failed",
            report_id=row.id, error=str(exc),
        )

    return row


# ----------------------------------------------------------------------
# Background thread scheduler — fired from plan_synthesis orchestrator.
# ----------------------------------------------------------------------


def schedule_post_synthesis_review(
    *,
    session: Session,
    user_id: str,
    decision_run_id: int,
) -> None:
    """Fire-and-forget background self-review after a synthesis run.

    Spawns a daemon thread that opens a fresh session bound to the
    orchestrator's engine.  Pattern mirrors
    ``_schedule_fm_objection_translation_precompute`` —
    the orchestrator's session is closed by its caller after
    ``run_synthesis`` returns, so the background thread can't reuse it.

    Failures are logged + swallowed: a self-review that crashes must
    NEVER block the synthesis flow (the user reads the draft + FM
    objections from the existing /plan path; the self-review is a
    separate surface).
    """
    engine = session.get_bind()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    thread = threading.Thread(
        target=_post_synthesis_worker,
        kwargs={
            "session_factory": session_factory,
            "user_id": user_id,
            "decision_run_id": decision_run_id,
        },
        daemon=True,
        name=f"fleet-self-review-{decision_run_id}",
    )
    thread.start()
    log.info(
        "fleet_self_review.scheduled",
        user_id=user_id,
        decision_run_id=decision_run_id,
        thread_name=thread.name,
    )


def _post_synthesis_worker(
    *,
    session_factory,
    user_id: str,
    decision_run_id: int,
) -> None:
    """Background worker that produces one post-synthesis self-review."""
    db = session_factory()
    try:
        generate_fleet_self_review(
            db,
            user_id=user_id,
            scope_kind="post_synthesis",
            decision_run_id=decision_run_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "fleet_self_review.post_synthesis_failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
        )
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover — defensive
            pass


__all__ = [
    "compose_markdown",
    "generate_fleet_self_review",
    "schedule_post_synthesis_review",
]
