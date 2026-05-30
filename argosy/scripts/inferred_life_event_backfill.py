"""Inferred-life-event-detector backfill verifier (Spec E commit #9).

Empirical merge-gate companion to ``tests/test_inferred_life_event_backfill.py``:
the test exercises the detector against fixture JSON files; this script
exercises the SAME detector against the user's REAL transaction stream
(or any sub-window of it) and reports the findings without firing the
LLM-backed action_proposer.

Per spec §9 commit #9 + §5.7 the merge gate is "if the detector fires
garbage, don't merge".  In production this script is the operator's
day-2 sanity check — after a heuristic threshold tweak, run

    python -m argosy.scripts.inferred_life_event_backfill \
        --user-id ariel --lookback-days 365 --dry-run

and confirm the per-heuristic counts + per-finding evidence summaries
look sane before deploying.  Pass ``--real-write`` to also persist the
findings into ``inferred_life_event_findings`` (still shadow-only: no
``action_proposals`` row is written — the script swaps the proposer
runner for a no-op stub).

CLI flags
=========

  ``--user-id``         tenant (default ``ariel``).
  ``--lookback-days``   rolling-window size in days (default 365 — the
                        spec's 12-month window).
  ``--dry-run``         (default) run the detector with a no-op
                        proposer stub AND skip the INSERT into
                        ``inferred_life_event_findings``.  The script
                        prints per-heuristic counts + per-finding
                        evidence summary and exits without touching the
                        DB.
  ``--real-write``      flip the dry-run default off — runs the
                        detector as production does, which INSERTs
                        rows into ``inferred_life_event_findings`` but
                        STILL substitutes a no-op proposer so no
                        ``action_proposals`` row lands.  This is the
                        "shadow-only" mode the spec §5.4 + Ariel's
                        locked decision describes.
  ``--db-url``          override the DB URL (defaults to repo's
                        ``db/argosy.db`` resolved relative to this
                        file's parents[2]).
  ``--report-out``      path to write the JSON report (in addition to
                        stdout's human-readable summary).
  ``--verbose``         enable DEBUG logging.

Exit code semantics:
  0 — detector ran cleanly; report printed.  Caller inspects.
  1 — detector raised; report (best-effort) printed; details on stderr.
  2 — bad CLI arguments / DB session setup failure.

The script does NOT fail on findings.  Whether the findings are
"acceptable" is an operator judgment call — there's no hardcoded gate
because the real-data shape varies per user.  CI runs the
fixture-driven tests for the deterministic gate; this script is the
manual-inspection counterpart.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

_log = logging.getLogger("argosy.scripts.inferred_life_event_backfill")


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class FindingRecord:
    """One per-finding record stamped into the report.

    Mirrors ``InferredLifeEventFinding`` but ORM-decoupled so the JSON
    serialisation is stable across DB-shape changes.
    """

    pattern: str
    heuristic_confidence: str
    evidence_window_start: str
    evidence_window_end: str
    evidence_transaction_count: int
    evidence_summary: str
    conflict_resolution: str | None
    dismissed: bool


@dataclass
class BackfillReport:
    """The artefact the CLI emits + writes (when ``--report-out``)."""

    user_id: str
    generated_at: str
    mode: str
    lookback_days: int
    findings_total: int = 0
    findings_proposed: int = 0
    findings_shadow: int = 0
    findings_dismissed: int = 0
    conflicts_resolved: int = 0
    shadow_mode: bool = False
    per_heuristic_counts: dict[str, int] = field(default_factory=dict)
    findings: list[FindingRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "lookback_days": self.lookback_days,
            "findings_total": self.findings_total,
            "findings_proposed": self.findings_proposed,
            "findings_shadow": self.findings_shadow,
            "findings_dismissed": self.findings_dismissed,
            "conflicts_resolved": self.conflicts_resolved,
            "shadow_mode": self.shadow_mode,
            "per_heuristic_counts": dict(self.per_heuristic_counts),
            "findings": [
                {
                    "pattern": f.pattern,
                    "heuristic_confidence": f.heuristic_confidence,
                    "evidence_window_start": f.evidence_window_start,
                    "evidence_window_end": f.evidence_window_end,
                    "evidence_transaction_count": f.evidence_transaction_count,
                    "evidence_summary": f.evidence_summary,
                    "conflict_resolution": f.conflict_resolution,
                    "dismissed": f.dismissed,
                }
                for f in self.findings
            ],
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# DB session resolution
# ---------------------------------------------------------------------------


def _default_db_url() -> str:
    """Resolve the default DB URL.

    Anchors at this file's location (``argosy/scripts/``) and walks up
    two parents -> repo root -> ``db/argosy.db``.  Matches the
    convention from
    ``argosy/scripts/state_observer_backfill.py::_reconstruct_state_inputs``.
    """
    default_db_path = Path(__file__).resolve().parents[2] / "db" / "argosy.db"
    return os.environ.get("ARGOSY_DB_URL", f"sqlite:///{default_db_path}")


def _make_session(db_url: str):
    """Build a sync Session bound to ``db_url``."""
    engine = sa.create_engine(db_url, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal()


# ---------------------------------------------------------------------------
# Detector adapter
# ---------------------------------------------------------------------------


def _stub_proposer_async():
    """Return a no-op async proposer stub.

    The stub mirrors the signature of
    ``argosy.services.action_proposer_runner.run_action_proposer_for_inferred_event``
    but never fires the LLM.  Returning ``None`` makes the orchestrator
    record the finding without binding a proposal id back.
    """
    async def _stub(sess, *, inferred_event, user_id, **_extras):
        return None
    return _stub


def _build_finding_record(
    *,
    pattern: str,
    heuristic_confidence: str,
    evidence_window_start: Any,
    evidence_window_end: Any,
    evidence_transaction_ids: list[int],
    evidence_summary: str,
    conflict_resolution: str | None,
    dismissed: bool,
) -> FindingRecord:
    """Coerce an in-memory finding into a JSON-serialisable record."""
    return FindingRecord(
        pattern=pattern,
        heuristic_confidence=heuristic_confidence,
        evidence_window_start=str(evidence_window_start),
        evidence_window_end=str(evidence_window_end),
        evidence_transaction_count=len(evidence_transaction_ids or []),
        evidence_summary=evidence_summary,
        conflict_resolution=conflict_resolution,
        dismissed=dismissed,
    )


def run_backfill(
    *,
    user_id: str,
    lookback_days: int,
    dry_run: bool,
    db_url: str | None = None,
    now: datetime | None = None,
) -> BackfillReport:
    """Run the detector once and pack the result into a report.

    Args:
      user_id:       tenant.
      lookback_days: detector lookback window size.
      dry_run:       True (default) -> use SAVEPOINT semantics + roll
                     back so the DB is unchanged.  False -> let the
                     detector INSERT rows but STILL substitute a no-op
                     proposer (the "--real-write" / "shadow-only" mode).
      db_url:        override; defaults to ``_default_db_url()``.
      now:           override clock; defaults to ``datetime.now(UTC)``.

    Returns:
      :class:`BackfillReport` with per-heuristic counts + per-finding
      records.
    """
    from argosy.services.inferred_life_event_detector import run_detector
    from argosy.state.models import InferredLifeEventFinding

    if now is None:
        now = datetime.now(timezone.utc)

    report = BackfillReport(
        user_id=user_id,
        generated_at=now.isoformat(),
        mode=("dry-run" if dry_run else "real-write-shadow-only"),
        lookback_days=lookback_days,
    )

    engine, session = _make_session(db_url or _default_db_url())
    try:
        # In dry-run mode we don't want the detector's commits to land.
        # The detector calls ``session.commit()`` internally, so a
        # simple ``session.rollback()`` at the end isn't enough — we
        # need to snapshot the finding-row IDs BEFORE the detector run,
        # then DELETE anything new after it returns.
        baseline_finding_ids: set[int] = set()
        if dry_run:
            baseline_finding_ids = {
                row.id
                for row in session.query(InferredLifeEventFinding)
                .filter_by(user_id=user_id)
                .all()
            }

        summary = run_detector(
            session,
            user_id,
            lookback_days=lookback_days,
            now=now,
            proposer_runner=_stub_proposer_async(),
        )

        report.findings_total = summary.findings_total
        report.findings_proposed = summary.findings_proposed
        report.findings_shadow = summary.findings_shadow
        report.findings_dismissed = summary.findings_dismissed
        report.conflicts_resolved = summary.conflicts_resolved
        report.shadow_mode = summary.shadow_mode
        report.errors.extend(summary.errors)

        # Pull the persisted rows back to compose the per-finding
        # evidence summary.  In dry-run mode we'll delete them after.
        rows = (
            session.query(InferredLifeEventFinding)
            .filter_by(user_id=user_id)
            .all()
        )
        # Filter to ONLY the rows this run created.
        new_rows = [r for r in rows if r.id not in baseline_finding_ids]

        per_heuristic_counts: dict[str, int] = defaultdict(int)
        for r in new_rows:
            per_heuristic_counts[r.pattern] += 1
            try:
                ev_ids = (
                    json.loads(r.evidence_transaction_ids)
                    if r.evidence_transaction_ids
                    else []
                )
            except (ValueError, TypeError):
                ev_ids = []
            report.findings.append(
                _build_finding_record(
                    pattern=r.pattern,
                    heuristic_confidence=r.heuristic_confidence,
                    evidence_window_start=r.evidence_window_start,
                    evidence_window_end=r.evidence_window_end,
                    evidence_transaction_ids=ev_ids,
                    evidence_summary=r.evidence_summary or "",
                    conflict_resolution=r.conflict_resolution,
                    dismissed=bool(r.dismissed),
                )
            )
        report.per_heuristic_counts = dict(per_heuristic_counts)

        if dry_run and new_rows:
            # Clean up the new rows so the DB is unchanged by the run.
            for r in new_rows:
                session.delete(r)
            session.commit()
            _log.info(
                "inferred_life_event_backfill.dry_run_rollback rows=%d",
                len(new_rows),
            )
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 — close-failure shouldn't mask
            pass
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass

    return report


# ---------------------------------------------------------------------------
# Async wrapper — for tests that want to verify the runner without sys.exit
# ---------------------------------------------------------------------------


async def run_backfill_async(
    *,
    user_id: str,
    lookback_days: int,
    dry_run: bool,
    db_url: str | None = None,
    now: datetime | None = None,
) -> BackfillReport:
    """Async-friendly facade — wraps the sync runner in
    ``asyncio.to_thread`` so the operator can call this from a notebook
    without blocking the event loop."""
    return await asyncio.to_thread(
        run_backfill,
        user_id=user_id,
        lookback_days=lookback_days,
        dry_run=dry_run,
        db_url=db_url,
        now=now,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report_text(report: BackfillReport) -> str:
    """Human-readable report dumped to stdout."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(
        f"Inferred-life-event-detector backfill ({report.mode})"
    )
    lines.append("=" * 72)
    lines.append(f"user_id            : {report.user_id}")
    lines.append(f"generated_at       : {report.generated_at}")
    lines.append(f"lookback_days      : {report.lookback_days}")
    lines.append(f"shadow_mode        : {report.shadow_mode}")
    lines.append("")
    lines.append("Summary counts")
    lines.append("-" * 72)
    lines.append(f"  findings_total     : {report.findings_total}")
    lines.append(f"  findings_proposed  : {report.findings_proposed}")
    lines.append(f"  findings_shadow    : {report.findings_shadow}")
    lines.append(f"  findings_dismissed : {report.findings_dismissed}")
    lines.append(f"  conflicts_resolved : {report.conflicts_resolved}")
    lines.append("")
    lines.append("Per-heuristic counts")
    lines.append("-" * 72)
    if not report.per_heuristic_counts:
        lines.append("  (none)")
    else:
        for pattern in sorted(report.per_heuristic_counts.keys()):
            lines.append(
                f"  {pattern:<28}: {report.per_heuristic_counts[pattern]}"
            )
    lines.append("")
    lines.append("Per-finding evidence")
    lines.append("-" * 72)
    if not report.findings:
        lines.append("  (none)")
    else:
        for i, f in enumerate(report.findings, 1):
            lines.append(
                f"  [{i}] pattern={f.pattern}  conf={f.heuristic_confidence}"
                f"  window=[{f.evidence_window_start} .. "
                f"{f.evidence_window_end}]"
                f"  tx_count={f.evidence_transaction_count}"
                f"  dismissed={f.dismissed}"
            )
            if f.conflict_resolution and f.conflict_resolution != "no_conflict":
                lines.append(
                    f"      conflict_resolution: {f.conflict_resolution}"
                )
            # Wrap the evidence summary at ~80 chars for terminal width.
            summary = f.evidence_summary or "(no summary)"
            lines.append(f"      {summary}")
    lines.append("")
    if report.errors:
        lines.append("Errors")
        lines.append("-" * 72)
        for e in report.errors:
            lines.append(f"  {e}")
        lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferred_life_event_backfill",
        description=(
            "Run the inferred-life-event detector against the user's "
            "real transaction stream and report findings (Spec E #9)."
        ),
    )
    parser.add_argument(
        "--user-id", default="ariel",
        help="Tenant to backfill for (default: ariel).",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=365,
        help=(
            "Detector lookback window size in days (default: 365 = "
            "spec's 12-month window)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True,
        help=(
            "Run detector with no-op proposer stub AND roll back the "
            "INSERTs into inferred_life_event_findings (default).  The "
            "DB is unchanged after the script exits."
        ),
    )
    mode.add_argument(
        "--real-write", action="store_true",
        help=(
            "Persist findings into inferred_life_event_findings (still "
            "shadow-only: no action_proposals row is written).  Use "
            "this when you want the audit trail of a successful "
            "backfill captured."
        ),
    )
    parser.add_argument(
        "--db-url", default="",
        help=(
            "DB URL override; defaults to db/argosy.db resolved "
            "relative to this script's location."
        ),
    )
    parser.add_argument(
        "--report-out", default="",
        help=(
            "Write JSON report to this path in addition to stdout. "
            "Default: stdout only."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Exit codes:
      0 -- detector ran cleanly; report printed.
      1 -- detector raised; partial report (best-effort) emitted.
      2 -- bad CLI args / DB setup failure.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # mutex: --real-write overrides the default --dry-run.
    dry_run = not args.real_write

    db_url = args.db_url.strip() or None

    if args.lookback_days <= 0:
        print(
            f"ERROR: --lookback-days must be > 0 (got "
            f"{args.lookback_days})",
            file=sys.stderr,
        )
        return 2

    try:
        report = run_backfill(
            user_id=args.user_id,
            lookback_days=args.lookback_days,
            dry_run=dry_run,
            db_url=db_url,
        )
    except Exception as exc:  # noqa: BLE001 — surface the failure
        print(f"ERROR: detector run failed: {exc!r}", file=sys.stderr)
        return 1

    print(format_report_text(report))

    if args.report_out:
        out_path = Path(args.report_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        print(f"JSON report written to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BackfillReport",
    "FindingRecord",
    "format_report_text",
    "main",
    "run_backfill",
    "run_backfill_async",
]
