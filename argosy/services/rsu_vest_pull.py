"""Scan-and-ingest helper that wires `ingest_schwab_vest_events`.

This module is the production caller for the parser at
`argosy.services.rsu_vest_ingest.ingest_schwab_vest_events`. It:

  * scans ``$ARGOSY_EXPENSE_SAMPLES_ROOT`` recursively for Schwab
    Equity Awards Center CSVs (filename pattern
    ``EquityAwardsCenter_Transactions_*.csv``);
  * opens a sync SQLAlchemy session for each file and ingests it,
    catching per-file errors so a single bad CSV doesn't abort the
    loop;
  * returns a list of result dicts (one per file processed) that the
    caller can log / surface to the UI.

Callers today:
  * ``argosy.orchestrator.loops.monthly_cycle._real_rsu_pull`` (the
    1st-of-month cycle) — replaces the prior ``_noop_rsu_pull``
    placeholder.
  * ``argosy.api.routes.portfolio.refresh_rsu_vests`` — explicit
    "ingest now" surface for the user when they don't want to wait
    for the monthly cycle.

Idempotency contract: ``ingest_schwab_vest_events`` itself is
idempotent on ``(user_id, grant_id, vest_date)`` per the table's
UNIQUE constraint — re-running this helper on the same samples
root is a no-op for already-ingested events.

Environment fallback: if ``ARGOSY_EXPENSE_SAMPLES_ROOT`` is unset
or doesn't exist on disk, this helper logs a warning and returns an
empty list (no exception). That matches how the rest of the
expense-samples-aware routes degrade gracefully when the env is
not configured (e.g. CI, fresh checkouts without a Drive mount).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.services.rsu_vest_ingest import (
    VestIngestResult,
    ingest_schwab_vest_events,
)

_log = get_logger(__name__)


# Filename patterns for Schwab Equity Awards Center transaction exports.
# Schwab's "Download" produces either a date-stamped variant
# (``EquityAwardsCenter_Transactions_20260529.csv`` — the fixture used
# in tests) or the base name (``EquityAwardsCenter_Transactions.csv``)
# when the user keeps overwriting the same file. Codex BLOCKER fix
# 2026-05-30: glob both so the second case isn't silently missed.
_SCHWAB_FILENAME_GLOBS: tuple[str, ...] = (
    "EquityAwardsCenter_Transactions.csv",
    "EquityAwardsCenter_Transactions_*.csv",
)


def _resolve_samples_root() -> Path | None:
    """Return the configured samples root, or ``None`` if unconfigured.

    Mirrors the convention used by
    ``argosy/api/routes/expenses.py::get_rsu_reconciliation`` and
    ``argosy/api/routes/portfolio.py::_resolve_snapshot_root``: read
    ``ARGOSY_EXPENSE_SAMPLES_ROOT`` directly so the wiring matches
    the user-guide's documented contract.
    """
    env_root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not env_root:
        return None
    root = Path(env_root)
    if not root.exists():
        return None
    return root


def discover_schwab_csvs(root: Path) -> list[Path]:
    """Recursively find Schwab Equity Awards CSVs under ``root``.

    Matches both ``EquityAwardsCenter_Transactions.csv`` (no date
    suffix) and ``EquityAwardsCenter_Transactions_*.csv`` (date-stamped
    variant). Dedupes if a path matches both patterns.

    Filters out scratch / build directories so a stray copy under
    ``.venv`` or ``node_modules`` doesn't shadow real exports.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in _SCHWAB_FILENAME_GLOBS:
        for csv in root.rglob(pattern):
            if csv in seen:
                continue
            s = str(csv).lower()
            if any(seg in s for seg in (".venv", "node_modules", "__pycache__")):
                continue
            if not csv.is_file():
                continue
            seen.add(csv)
            out.append(csv)
    # Sort for deterministic ordering (oldest filename → newest).
    out.sort()
    return out


def _build_sync_session_factory() -> tuple[sa.Engine, sessionmaker]:
    """Build a sync ``(engine, sessionmaker)`` pair bound to the production DB.

    Same shape as ``argosy/orchestrator/loops/monthly_cycle.py``'s
    ``_run_sync_tick`` and ``argosy/api/routes/plan.py::get_db``:
    strip the ``+aiosqlite`` driver and open a sync engine. The
    caller is responsible for ``engine.dispose()`` after use; this
    helper returns both so the caller can clean up.
    """
    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal


def ingest_samples_root(
    user_id: str,
    *,
    session_factory: sessionmaker | None = None,
    samples_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Scan + ingest every Schwab CSV under the samples root.

    Args:
      user_id: tenant id used by ``ingest_schwab_vest_events``.
      session_factory: optional override (tests inject one bound to a
        scratch SQLite engine). When ``None``, a sync sessionmaker is
        built from settings.database_url.
      samples_root: optional override of ``$ARGOSY_EXPENSE_SAMPLES_ROOT``.
        When ``None``, the env var is read (and missing → skip + log).

    Returns:
      A list of per-file result dicts. Each dict has the shape:
        ``{"source_file": str, "parsed": int, "inserted": int,
           "duplicates": int}`` on success, or
        ``{"source_file": str, "error": str}`` when a single CSV
        couldn't be parsed. An empty list means no CSVs were found
        (or the samples root was unconfigured).

    The dict shape matches the monthly_cycle audit-log expectation
    that ``rsu_events`` is JSON-serialisable (list of dicts).
    """
    if samples_root is None:
        root = _resolve_samples_root()
    else:
        root = samples_root if samples_root.exists() else None

    if root is None:
        _log.info(
            "rsu_vest_pull.samples_root_unset_or_missing",
            env=os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT"),
        )
        return []

    csvs = discover_schwab_csvs(root)
    if not csvs:
        _log.info(
            "rsu_vest_pull.no_schwab_csvs_found",
            root=str(root),
            patterns=list(_SCHWAB_FILENAME_GLOBS),
        )
        return []

    # Build a sync session factory if the caller didn't supply one.
    own_engine = None
    if session_factory is None:
        own_engine, session_factory = _build_sync_session_factory()

    results: list[dict[str, Any]] = []
    try:
        for csv_path in csvs:
            session = session_factory()
            try:
                outcome: VestIngestResult = ingest_schwab_vest_events(
                    session=session,
                    user_id=user_id,
                    csv_path=csv_path,
                )
                results.append({
                    "source_file": str(csv_path),
                    "parsed": outcome.parsed_event_count,
                    "inserted": outcome.inserted_count,
                    "duplicates": outcome.duplicate_count,
                })
                _log.info(
                    "rsu_vest_pull.csv_ingested",
                    user_id=user_id,
                    source_file=str(csv_path),
                    parsed=outcome.parsed_event_count,
                    inserted=outcome.inserted_count,
                    duplicates=outcome.duplicate_count,
                )
            except Exception as exc:  # noqa: BLE001 — per-file isolation
                _log.exception(
                    "rsu_vest_pull.csv_failed",
                    user_id=user_id,
                    source_file=str(csv_path),
                )
                results.append({
                    "source_file": str(csv_path),
                    "error": str(exc),
                })
            finally:
                session.close()
    finally:
        if own_engine is not None:
            own_engine.dispose()

    return results


__all__ = [
    "discover_schwab_csvs",
    "ingest_samples_root",
]
