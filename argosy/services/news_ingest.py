"""Stage 1 pipeline orchestrator — pulls + extracts + persists news_signals.

Sprint commit #13 of the plan/execute/monitor reorg. Reads from every
configured Stage 1 source (RSS + macro feed today; Discord is dormant
until commit #16), runs each item through the deterministic extractor,
and idempotently persists to ``news_signals`` using the unique
``(source, source_ref)`` index for dedup.

NO LLM in this module. Stage 2 (commit #14) is a separate pass that
reads back the rows this pipeline writes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.macro_feed import get_upcoming_macro_events
from argosy.services.news_extractor import ExtractedSignal
from argosy.services.rss_feed import poll_rss_for_tickers
from argosy.state.models import NewsSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsIngestResult:
    """Counts returned to the caller for observability / cadence_state.

    ``fetched`` is the gross count of items the source adapters
    returned; ``persisted`` is the net count of new rows written
    (``fetched - duplicates``). The two diverge whenever the same item
    is re-ingested across polls.
    """

    fetched: int
    persisted: int
    duplicates: int
    by_source: dict[str, int]


def run_news_ingest(
    session: Session,
    *,
    tickers: list[str] | None = None,
    rss_fetcher: object = None,
    now: datetime | None = None,
    macro_within_days: int = 30,
    known_tickers: frozenset[str] | None = None,
) -> NewsIngestResult:
    """Pull from all configured Stage 1 sources, extract, persist.

    Args:
        session: SQLAlchemy session bound to the Argosy DB. The caller
            owns commit/rollback; this function flushes but does not
            commit so the caller can compose multiple writes in one
            transaction if needed.
        tickers: Ticker symbols to poll the RSS feed for. ``None`` →
            skip the RSS source for this run (used by tests / cadences
            that only want the macro feed).
        rss_fetcher: Optional HTTP fetcher passed through to
            ``poll_rss_for_tickers`` for test mocking.
        now: "Now" override for the macro horizon + RSS fallback
            pubDate. Defaults to wallclock UTC.
        macro_within_days: Forward window for the macro feed. Default 30.
        known_tickers: Override for the Stage 1 ticker whitelist. None
            falls back to ``KNOWN_TICKERS_DEFAULT`` inside the
            extractor.

    Returns:
        ``NewsIngestResult`` with counts.
    """
    signals: list[ExtractedSignal] = []
    by_source: dict[str, int] = {"rss": 0, "macro_feed": 0, "discord": 0}

    # --- RSS source (per-ticker polling) -----------------------------
    if tickers:
        rss_signals = poll_rss_for_tickers(
            tickers, fetch=rss_fetcher, now=now,
        )
        # The RSS adapter calls the extractor itself; but if the caller
        # passed a custom whitelist, we need to re-extract so the
        # whitelist applies. Otherwise, accept the RSS adapter's output
        # verbatim — the common path.
        if known_tickers is not None:
            from argosy.services.news_extractor import extract as _extract
            rss_signals = [
                _extract(
                    source=s.source,
                    source_ref=s.source_ref,
                    raw_text=s.raw_text,
                    received_at=s.received_at,
                    known_tickers=known_tickers,
                )
                for s in rss_signals
            ]
        signals.extend(rss_signals)
        by_source["rss"] = len(rss_signals)

    # --- Macro feed --------------------------------------------------
    macro_signals = get_upcoming_macro_events(
        within_days=macro_within_days, now=now,
    )
    signals.extend(macro_signals)
    by_source["macro_feed"] = len(macro_signals)

    # --- Discord (dormant — commit #16 lights it up) -----------------
    # Intentionally no source pull here. The path exists for
    # observability counts.
    by_source["discord"] = 0

    # --- Persist with idempotency on (source, source_ref) ------------
    # Codex IMPORTANT (commit #13 review): use SAVEPOINTs per-row so a
    # failed insert only rolls back THAT row, not the whole batch.
    # Prior implementation called session.rollback() inside the loop,
    # which would undo earlier successful flushes from the same run
    # while counters still treated them as persisted — a real
    # durability+counter inconsistency under race or DB-error paths.
    persisted = 0
    duplicates = 0
    for sig in signals:
        if _already_ingested(session, sig.source, sig.source_ref):
            duplicates += 1
            continue
        row = NewsSignal(
            source=sig.source,
            source_ref=sig.source_ref,
            received_at=sig.received_at,
            parsed_tickers=json.dumps(sig.parsed_tickers),
            event_keywords=json.dumps(sig.event_keywords),
            sentiment=sig.sentiment,
            source_trust=sig.source_trust,
            evidence_excerpt=sig.evidence_excerpt,
            raw_text=sig.raw_text,
        )
        try:
            with session.begin_nested():  # SAVEPOINT
                session.add(row)
                session.flush()
            persisted += 1
        except Exception as exc:  # pragma: no cover — defensive
            # SAVEPOINT auto-rolled back by the `with` block; earlier
            # rows in this run are unaffected. Treat as duplicate.
            logger.warning(
                "news_ingest insert failed for %s/%s: %s",
                sig.source, sig.source_ref, exc,
            )
            duplicates += 1
            continue

    return NewsIngestResult(
        fetched=len(signals),
        persisted=persisted,
        duplicates=duplicates,
        by_source=by_source,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _already_ingested(session: Session, source: str, source_ref: str) -> bool:
    """Idempotency check against the unique ``(source, source_ref)`` index.

    A dedicated SELECT is cheaper than relying on IntegrityError +
    rollback for the common re-ingest case — and SQLite's transaction
    state after a constraint violation is fiddly enough that a clean
    pre-check is the maintainable path.
    """
    stmt = select(NewsSignal.id).where(
        NewsSignal.source == source,
        NewsSignal.source_ref == source_ref,
    )
    return session.execute(stmt).first() is not None
