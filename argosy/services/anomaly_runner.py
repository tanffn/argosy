"""Anomaly-detection runner (EX2).

Public entry points:

  * ``run_anomaly_check(user_id, db, *, triggered_by, source_statement_id,
    agent)``
        Pure-sync helper that loads watchlist + statements, calls the
        ``AnomalyDetectionAgent``, persists the resulting
        ``anomaly_reports`` row, and emits the ``anomaly.detected`` WS
        event when at least one Anomaly fired. Returns the persisted
        ORM row. Tests pass a stubbed agent here.

  * ``schedule_anomaly_check(*, user_id, triggered_by,
    triggering_source_file_id=None, source_statement_id=None)``
        Canonical fire-and-forget entry. Used by both the event-driven
        path (expense statement ingest) and the daily backstop. Gates
        on ``is_enabled_for_runtime()`` + pytest detection, spawns a
        daemon thread, opens its own fresh sync session inside the
        worker, persists the report row, and emits the WS event.
        Failure is logged + swallowed (never propagates).

  * ``schedule_event_driven_check(*, session, user_id, source_statement_id)``
        Legacy alias kept for the session-bound call shape. New
        callers should prefer ``schedule_anomaly_check``.

  * ``is_enabled_for_runtime()`` — gate on ``ARGOSY_ANOMALY_DETECTION_ENABLED``
    + ``PYTEST_CURRENT_TEST``. Same shape as the daily-brief gate so the
    test isolation contract is identical.

  * Watchlist loader (``load_watchlist_seed``) — reads
    ``argosy/data/watchlist_seed.yaml`` once per process. Pickable for
    monkeypatching in tests.

Cadence:
  - Event-driven: fires on Discount Bank statement ingest (statement
    that matches a watchlist entry's ``issuer_match`` + ``account_match``
    filter). This is the critical path — daily-only polling can miss
    a same-day fee-waiver disappearance for ~24h.
  - Daily backstop: fires from the daily-brief background loop at
    07:00 local for any watchlist entry that hasn't been touched
    today, so dormant accounts still get periodic verification.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.anomaly_detection import (
    AnomalyDetectionAgent,
    AnomalyDetectionReport,
)
from argosy.logging import get_logger
from argosy.state.models import (
    AnomalyReport,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
)

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Watchlist seed loading
# ----------------------------------------------------------------------


_WATCHLIST_SEED_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "watchlist_seed.yaml"
)


def load_watchlist_seed(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the watchlist seed YAML into a list of dicts.

    Returns ``[]`` on missing-file or parse-failure so the runner can
    proceed (with no entries to evaluate → produces a NORMAL report
    rather than crashing).

    Each entry has shape:
      {
        "name": str,
        "description": str,
        "account_match": str | None,
        "issuer_match": str | None,
        "expected_pattern": str,
        "alert_when": str,
        "severity": "RED" | "AMBER" | "YELLOW",
      }
    """
    path = path or _WATCHLIST_SEED_PATH
    try:
        import yaml  # PyYAML — already a dep via expense_ingest configs

        if not path.exists():
            log.warning("anomaly_runner.watchlist_seed_missing", path=str(path))
            return []
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("anomaly_runner.watchlist_seed_load_failed", error=str(exc))
        return []

    if not isinstance(raw, dict):
        return []
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict) or "name" not in e:
            continue
        out.append(
            {
                "name": str(e.get("name", "")),
                "description": str(e.get("description", "")),
                "account_match": (e.get("account_match") or None),
                "issuer_match": (e.get("issuer_match") or None),
                "expected_pattern": str(e.get("expected_pattern", "")),
                "alert_when": str(e.get("alert_when", "")),
                "severity": str(e.get("severity", "AMBER")).upper(),
            }
        )
    return out


# ----------------------------------------------------------------------
# Helpers — statement assembly
# ----------------------------------------------------------------------


def _format_transaction(tx: ExpenseTransaction) -> str:
    """One-line transaction dump for the LLM document block.

    Format: ``YYYY-MM-DD | merchant_raw | amount_nis | direction``.
    """
    occurred = tx.occurred_on.isoformat() if tx.occurred_on else "?"
    merchant = (tx.merchant_raw or "").strip()[:200]
    amount = tx.amount_nis if tx.amount_nis is not None else "(orig)"
    direction = tx.direction or "?"
    return f"{occurred} | {merchant} | {amount} | {direction}"


def _build_statement_block(
    db: Session, statement: ExpenseStatement
) -> dict[str, Any]:
    """Turn an ExpenseStatement ORM row into the dict shape the agent expects."""
    src = db.get(ExpenseSource, statement.source_id)
    account_label = (
        f"{src.issuer}/{src.external_id} ({src.display_name})"
        if src is not None
        else f"source#{statement.source_id}"
    )
    txs = (
        db.execute(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.statement_id == statement.id)
            .order_by(ExpenseTransaction.occurred_on)
        )
        .scalars()
        .all()
    )
    return {
        "statement_id": statement.id,
        "account_label": account_label,
        "period_start": statement.period_start.isoformat() if statement.period_start else "",
        "period_end": statement.period_end.isoformat() if statement.period_end else "",
        "transactions": [_format_transaction(tx) for tx in txs],
    }


def _filter_entries_for_statement(
    entries: list[dict[str, Any]],
    source: ExpenseSource | None,
) -> list[dict[str, Any]]:
    """Return entries whose account_match/issuer_match apply to this source.

    Empty / None filters mean "any". Substring match, case-insensitive.
    """
    if source is None:
        return entries
    out: list[dict[str, Any]] = []
    issuer = (source.issuer or "").lower()
    external_id = (source.external_id or "").lower()
    display_name = (source.display_name or "").lower()
    for e in entries:
        am = (e.get("account_match") or "").lower()
        im = (e.get("issuer_match") or "").lower()
        if im and im not in issuer:
            continue
        if am and am not in external_id and am not in display_name:
            continue
        out.append(e)
    return out


def _severity_summary(report: AnomalyDetectionReport) -> dict[str, int]:
    """Pre-join severity counts for the home-page banner."""
    counts: dict[str, int] = {"RED": 0, "AMBER": 0, "YELLOW": 0}
    for a in report.anomalies:
        sev = (a.severity or "").upper()
        if sev in counts:
            counts[sev] += 1
    return counts


# ----------------------------------------------------------------------
# Public entry — run one anomaly check synchronously.
# ----------------------------------------------------------------------


def run_anomaly_check(
    user_id: str,
    db: Session,
    *,
    triggered_by: str,
    source_statement_id: int | None = None,
    agent: AnomalyDetectionAgent | None = None,
) -> AnomalyReport:
    """Run one anomaly-check pass and persist the result.

    Args:
      user_id: tenant scope.
      db: open sync ``Session``. Used to read statements / transactions
          and to write the resulting ``anomaly_reports`` row.
      triggered_by: 'event' | 'daily' | 'manual'. DB CHECK enforces.
      source_statement_id: when ``triggered_by='event'``, the statement
          whose ingest fired this run.
      agent: optional pre-built agent (tests inject a stubbed
          ``_call_model``). When None, a fresh ``AnomalyDetectionAgent``
          is constructed.

    Returns:
      The persisted ``AnomalyReport`` ORM row.

    Failure mode: the agent call is wrapped in a try; on failure the
    runner persists a row with an empty report + severity counts and
    a stamped error in report_json. Callers (the background thread
    scheduler, daily-brief loop) MUST swallow exceptions anyway, but
    we'd rather record the failure than nothing.
    """
    entries_all = load_watchlist_seed()

    # Assemble the statement(s) and the filtered entries.
    statements: list[dict[str, Any]] = []
    entries_applicable: list[dict[str, Any]] = []
    if source_statement_id is not None:
        stmt = db.get(ExpenseStatement, source_statement_id)
        if stmt is not None and stmt.user_id == user_id:
            statements.append(_build_statement_block(db, stmt))
            src = db.get(ExpenseSource, stmt.source_id)
            entries_applicable = _filter_entries_for_statement(entries_all, src)
    else:
        # Daily / manual sweep: for each watchlist entry, grab the most
        # recent matching statement so the agent has SOMETHING to look
        # at. If no statement matches, the entry will resolve to
        # state=UNKNOWN inside the agent (that's by design).
        for e in entries_all:
            issuer = (e.get("issuer_match") or "").lower()
            account = (e.get("account_match") or "").lower()
            q = select(ExpenseSource).where(ExpenseSource.user_id == user_id)
            sources = db.execute(q).scalars().all()
            matched = [
                s
                for s in sources
                if (not issuer or issuer in (s.issuer or "").lower())
                and (
                    not account
                    or account in (s.external_id or "").lower()
                    or account in (s.display_name or "").lower()
                )
            ]
            entries_applicable.append(e)
            for s in matched:
                latest = (
                    db.execute(
                        select(ExpenseStatement)
                        .where(
                            ExpenseStatement.user_id == user_id,
                            ExpenseStatement.source_id == s.id,
                        )
                        .order_by(desc(ExpenseStatement.period_end))
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if latest is not None:
                    # Dedup by statement_id — daily sweep can hit the
                    # same statement via multiple entries.
                    if not any(
                        block["statement_id"] == latest.id for block in statements
                    ):
                        statements.append(_build_statement_block(db, latest))

    # Run the agent. We use the sync wrapper around the async run() so
    # the existing thread-based scheduling pattern works unchanged.
    agent = agent or AnomalyDetectionAgent(user_id=user_id)
    error_text: str | None = None
    agent_report_id: int | None = None
    cited_sources: list[str] = []

    if not entries_applicable:
        # Nothing to evaluate — produce a NORMAL row so the timeline is
        # honest about every fire (the runner ran, found no applicable
        # entries, recorded that).
        report = AnomalyDetectionReport(anomalies=[], watchlist_status=[])
    else:
        try:
            agent_result = agent.run_sync(
                watchlist_entries=entries_applicable,
                statements=statements,
                recent_history=None,
            )
            report = agent_result.output  # type: ignore[assignment]
            cited_sources = list(getattr(report, "cited_sources", []) or [])
        except Exception as exc:  # noqa: BLE001 — record + continue
            log.warning(
                "anomaly_runner.agent_call_failed",
                user_id=user_id,
                triggered_by=triggered_by,
                source_statement_id=source_statement_id,
                error=str(exc),
            )
            error_text = f"{type(exc).__name__}: {exc}"
            report = AnomalyDetectionReport(
                anomalies=[],
                watchlist_status=[],
                cited_sources=[],
            )

    severity = _severity_summary(report)
    report_payload = json.loads(report.model_dump_json())
    if error_text:
        report_payload["_runner_error"] = error_text

    row = AnomalyReport(
        user_id=user_id,
        triggered_by=triggered_by,
        triggered_at=datetime.now(UTC),
        source_statement_id=source_statement_id,
        report_json=json.dumps(report_payload, ensure_ascii=False),
        severity_summary_json=json.dumps(severity),
        agent_report_id=agent_report_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    log.info(
        "anomaly_runner.persisted",
        user_id=user_id,
        triggered_by=triggered_by,
        source_statement_id=source_statement_id,
        report_id=row.id,
        red=severity["RED"],
        amber=severity["AMBER"],
        yellow=severity["YELLOW"],
        anomalies=len(report.anomalies),
        watchlist_count=len(report.watchlist_status),
    )

    # Best-effort WS event when at least one anomaly fired. The home
    # page subscribes to this so the banner refreshes without a manual
    # reload. Always emit on RED; emit on AMBER/YELLOW too so the
    # banner state updates whenever the severity counter changes.
    if any(severity.values()) or report.anomalies:
        try:
            from argosy.api.events import publish_event_threadsafe

            publish_event_threadsafe(
                "anomaly.detected",
                {
                    "user_id": user_id,
                    "report_id": row.id,
                    "triggered_by": triggered_by,
                    "source_statement_id": source_statement_id,
                    "severity_summary": severity,
                    "anomalies_count": len(report.anomalies),
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            log.warning(
                "anomaly_runner.event_publish_failed",
                report_id=row.id, error=str(exc),
            )

    return row


# ----------------------------------------------------------------------
# Canonical fire-and-forget entry — used by event-driven AND daily paths.
# ----------------------------------------------------------------------


def schedule_anomaly_check(
    *,
    user_id: str,
    triggered_by: str,
    triggering_source_file_id: int | None = None,
    source_statement_id: int | None = None,
    session: Session | None = None,
) -> None:
    """Fire-and-forget anomaly check.

    Mirrors the ``fleet_self_review_runner.schedule_post_synthesis_review``
    pattern: gate → daemon thread → fresh sync session → run → swallow.

    Args:
      user_id: tenant scope.
      triggered_by: 'event' | 'daily' | 'manual'. DB CHECK enforces.
      triggering_source_file_id: when ``triggered_by='event'`` and the
          caller knows the ``user_files.id`` that just got ingested (but
          not the statement id), the worker resolves it to the most
          recent ``ExpenseStatement`` whose ``file_id`` matches and
          uses that as ``source_statement_id``. Useful when the
          caller doesn't have the statement row in hand (e.g. lower
          in the catalog stack).
      source_statement_id: when known directly (the expense-upload
          REST route has it post-ingest), wins over
          ``triggering_source_file_id``. Either may be None for
          ``triggered_by='daily' | 'manual'`` which sweep all
          watchlist accounts.
      session: optional caller-owned ``Session`` whose engine we bind
          the new worker session to. When None we construct an engine
          from settings (the daily-backstop path).

    Gating:
      ``is_enabled_for_runtime()`` is checked here so test environments
      never spawn a background thread that touches the LLM.

    Returns immediately; the work happens on a daemon thread.
    """
    if not is_enabled_for_runtime():
        log.debug(
            "anomaly_runner.skipped_disabled",
            user_id=user_id,
            triggered_by=triggered_by,
            source_statement_id=source_statement_id,
            triggering_source_file_id=triggering_source_file_id,
        )
        return

    # Build a sessionmaker. Prefer caller's engine when given so the
    # worker writes to the same DB the caller is using.
    if session is not None:
        engine = session.get_bind()
        engine_owned_locally = False
    else:
        from sqlalchemy import create_engine

        from argosy.config import get_settings

        settings = get_settings()
        sync_url = settings.database_url.replace("+aiosqlite", "")
        engine = create_engine(
            sync_url, connect_args={"check_same_thread": False},
        )
        engine_owned_locally = True

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    thread = threading.Thread(
        target=_anomaly_check_worker,
        kwargs={
            "session_factory": session_factory,
            "user_id": user_id,
            "triggered_by": triggered_by,
            "triggering_source_file_id": triggering_source_file_id,
            "source_statement_id": source_statement_id,
            "engine": engine if engine_owned_locally else None,
        },
        daemon=True,
        name=(
            f"anomaly-check-{triggered_by}-"
            f"{source_statement_id or triggering_source_file_id or 'sweep'}"
        ),
    )
    thread.start()
    log.info(
        "anomaly_runner.scheduled",
        user_id=user_id,
        triggered_by=triggered_by,
        source_statement_id=source_statement_id,
        triggering_source_file_id=triggering_source_file_id,
        thread_name=thread.name,
    )


def _anomaly_check_worker(
    *,
    session_factory: Any,
    user_id: str,
    triggered_by: str,
    triggering_source_file_id: int | None,
    source_statement_id: int | None,
    engine: Any,
) -> None:
    """Background worker for ``schedule_anomaly_check``.

    Opens a fresh session, resolves ``triggering_source_file_id`` to
    the matching ``ExpenseStatement`` if needed, runs the check, and
    cleans up. Failure is logged + swallowed.
    """
    db = session_factory()
    try:
        resolved_stmt_id = source_statement_id
        if resolved_stmt_id is None and triggering_source_file_id is not None:
            # Resolve file_id → most recent statement for that file.
            stmt = (
                db.execute(
                    select(ExpenseStatement)
                    .where(
                        ExpenseStatement.user_id == user_id,
                        ExpenseStatement.file_id == triggering_source_file_id,
                    )
                    .order_by(desc(ExpenseStatement.period_end))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if stmt is not None:
                resolved_stmt_id = stmt.id
        run_anomaly_check(
            user_id,
            db,
            triggered_by=triggered_by,
            source_statement_id=resolved_stmt_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "anomaly_runner.worker_failed",
            user_id=user_id,
            triggered_by=triggered_by,
            source_statement_id=source_statement_id,
            triggering_source_file_id=triggering_source_file_id,
            error=str(exc),
        )
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover — defensive
            pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # pragma: no cover — defensive
                pass


# ----------------------------------------------------------------------
# Background thread scheduler — fired from the expenses ingest path.
# ----------------------------------------------------------------------


def schedule_event_driven_check(
    *,
    session: Session,
    user_id: str,
    source_statement_id: int,
) -> None:
    """Fire-and-forget background anomaly check after a statement ingest.

    Spawns a daemon thread bound to the caller's engine. The session
    passed in is NOT reused (it's owned by the FastAPI request and
    closes when the request returns); we create a fresh
    ``sessionmaker`` from its engine and open our own session inside
    the worker.

    Pattern mirrors ``fleet_self_review_runner.schedule_post_synthesis_review``.
    Failures are logged + swallowed: a crashed anomaly check MUST NOT
    block the statement-upload flow (the user reads parsed transactions
    from the existing /expenses path; anomaly detection is a separate
    surface).

    Gating: ``is_enabled_for_runtime()`` is checked here so test
    environments don't fire a real background thread on every fixture
    ingest.
    """
    if not is_enabled_for_runtime():
        log.debug(
            "anomaly_runner.event_skipped_disabled",
            user_id=user_id, source_statement_id=source_statement_id,
        )
        return

    engine = session.get_bind()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    thread = threading.Thread(
        target=_event_driven_worker,
        kwargs={
            "session_factory": session_factory,
            "user_id": user_id,
            "source_statement_id": source_statement_id,
        },
        daemon=True,
        name=f"anomaly-check-{source_statement_id}",
    )
    thread.start()
    log.info(
        "anomaly_runner.event_scheduled",
        user_id=user_id,
        source_statement_id=source_statement_id,
        thread_name=thread.name,
    )


def _event_driven_worker(
    *,
    session_factory: Any,
    user_id: str,
    source_statement_id: int,
) -> None:
    """Background worker that produces one event-driven anomaly check."""
    db = session_factory()
    try:
        run_anomaly_check(
            user_id,
            db,
            triggered_by="event",
            source_statement_id=source_statement_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "anomaly_runner.event_driven_failed",
            user_id=user_id,
            source_statement_id=source_statement_id,
            error=str(exc),
        )
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover — defensive
            pass


# ----------------------------------------------------------------------
# Daily backstop — fired from the daily-brief loop.
# ----------------------------------------------------------------------


async def run_daily_backstop(user_id: str = "ariel") -> None:
    """Fire the daily anomaly sweep on a worker thread.

    The runner is sync (sqlalchemy.orm.Session). We use ``asyncio.to_thread``
    to keep the daily-brief loop responsive, same pattern as the
    fleet-self-review daily sweep call site.

    Failures are logged + swallowed by the caller (daily-brief
    background_loop wraps the call); this function itself raises only
    if engine creation fails before any work is done.
    """
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from argosy.config import get_settings

    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = _sessionmaker(bind=engine, expire_on_commit=False)

    def _run() -> None:
        db = SessionLocal()
        try:
            run_anomaly_check(
                user_id, db, triggered_by="daily", source_statement_id=None,
            )
        finally:
            db.close()
            engine.dispose()

    await asyncio.to_thread(_run)


# ----------------------------------------------------------------------
# Gate — same shape as the daily-brief runner.
# ----------------------------------------------------------------------


def is_enabled_for_runtime() -> bool:
    """Whether the EX2 anomaly runner should fire in this process.

    Gated by:
      - ARGOSY_ANOMALY_DETECTION_ENABLED. Default ``on`` (per EX2 spec —
        the user genuinely wants this active). Explicit ``0`` disables.
      - pytest detection — if PYTEST_CURRENT_TEST is set, always off so
        importing the API in tests never fires a real LLM call. Tests
        call ``run_anomaly_check`` directly with a stubbed agent.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    raw = os.environ.get("ARGOSY_ANOMALY_DETECTION_ENABLED", "1").strip()
    return raw not in ("", "0", "false", "False", "off", "OFF")


__all__ = [
    "is_enabled_for_runtime",
    "load_watchlist_seed",
    "run_anomaly_check",
    "run_daily_backstop",
    "schedule_anomaly_check",
    "schedule_event_driven_check",
]
