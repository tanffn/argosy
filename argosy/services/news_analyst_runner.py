"""Stage 2 orchestrator — reads unanalyzed news_signals, runs analyst, persists.

Sprint commit #14 of the plan/execute/monitor reorg. Pairs the
``NewsSignalAnalystAgent`` (see ``argosy/agents/news_signal_analyst.py``)
with the ``news_signals`` table:

  1. Select rows where ``analyzed_at IS NULL`` (oldest first).
  2. Batch them (max ``MAX_BATCH_SIZE`` = 20 per LLM call to keep prompt
     size bounded; the analyst is a single-call-per-batch agent).
  3. Hydrate each row into the agent's ``AnalyzedSignalIn`` schema —
     normalized fields ONLY. ``raw_text`` IS NEVER READ HERE.
  4. Call the agent.
  5. Write ``materiality`` / ``recommended_flag`` / ``rationale`` /
     ``analyzed_at`` back to the row.

Codex BLOCKER #2 contract: this module is the bridge between the DB
``raw_text`` column (persisted, citation-only) and the analyst LLM
prompt (normalized fields only). The hydration step in
``_row_to_analyst_input`` deliberately does NOT touch the ``raw_text``
attribute — making the isolation visible in code review. The companion
test ``test_news_signal_analyst.py::test_raw_text_canary_not_in_prompt``
pins the contract against drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.agents.news_signal_analyst import (
    AnalyzedSignalIn,
    AnalyzedSignalOut,
    NewsSignalAnalystAgent,
)
from argosy.state.models import NewsSignal

logger = logging.getLogger(__name__)


# Max signals per single LLM call. The analyst's max_tokens is 16K (the
# BaseAgent fallback for roles without an explicit per-role cap); each
# signal's prompt slice is ~7 lines + ≤280 char excerpt, so 20 signals
# fits comfortably under 16K input context with room for the schema +
# system prompt. The orchestrator iterates batches until the unanalyzed
# queue drains.
MAX_BATCH_SIZE = 20


@dataclass(frozen=True)
class AnalysisRunResult:
    """Summary returned to the caller (cadence layer / CLI).

    ``analyzed`` is the count of rows for which the agent returned a
    classification AND the row was updated; ``skipped`` covers rows
    that were in the input batch but missing from the agent's output
    (defensive against the model emitting fewer outputs than inputs).
    """

    fetched: int
    analyzed: int
    skipped: int
    batches: int


def run_news_signal_analysis(
    session: Session,
    *,
    agent: NewsSignalAnalystAgent,
    user_holdings: list[str],
    max_rows: int | None = None,
    batch_size: int = MAX_BATCH_SIZE,
    now: datetime | None = None,
) -> AnalysisRunResult:
    """Run Stage 2 analysis over all (or up to ``max_rows``) unanalyzed rows.

    Args:
        session: SQLAlchemy session. Caller owns commit/rollback; the
            runner flushes after each batch so a partial run still
            persists prior batches. Mirrors ``run_news_ingest``'s
            session contract.
        agent: The analyst instance. Injected so tests can pass a
            ``_MockNewsSignalAnalystAgent`` without exercising the SDK.
        user_holdings: Ticker symbols the user holds. Threaded into the
            agent's prompt as materiality context (a signal touching a
            non-held ticker is usually low materiality unless macro).
        max_rows: Optional cap on the total rows analyzed in this call;
            useful for cadences that want a bounded wall-clock per run.
        batch_size: Override the per-LLM-call batch size. Defaults to
            ``MAX_BATCH_SIZE`` (20).
        now: "Now" override for the ``analyzed_at`` timestamp; defaults
            to wallclock UTC. Tests use this for determinism.

    Returns:
        ``AnalysisRunResult`` with counts. Empty (0/0/0/0) when no
        unanalyzed rows are queued — the call is cheap in that case.
    """
    now = now or datetime.now(UTC)

    # Pull all unanalyzed rows in one query, oldest first. SQLite's
    # default isolation is fine here: even if a parallel writer adds new
    # signals during analysis, they'll be picked up on the next run.
    stmt = (
        select(NewsSignal)
        .where(NewsSignal.analyzed_at.is_(None))
        .order_by(NewsSignal.received_at.asc())
    )
    if max_rows is not None:
        stmt = stmt.limit(max_rows)
    rows: list[NewsSignal] = list(session.execute(stmt).scalars())
    fetched = len(rows)

    if not rows:
        return AnalysisRunResult(fetched=0, analyzed=0, skipped=0, batches=0)

    analyzed_total = 0
    skipped_total = 0
    batches = 0

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        # Hydrate each row into the AnalyzedSignalIn schema. CRITICAL:
        # ``raw_text`` is NOT referenced here — the function's contract
        # is that only normalized fields cross into the prompt.
        analyst_inputs = [_row_to_analyst_input(r) for r in batch]

        try:
            analyses = asyncio.run(
                agent.analyze(
                    analyst_inputs, user_holdings=user_holdings,
                )
            )
        except RuntimeError as exc:
            # ``asyncio.run`` refuses to nest. The runner is the
            # canonical sync entry; callers in async contexts should
            # await ``agent.analyze`` directly. Surface a clearer error.
            raise RuntimeError(
                "run_news_signal_analysis is sync — call from a non-async "
                "context, or await NewsSignalAnalystAgent.analyze directly."
            ) from exc

        by_id: dict[int, AnalyzedSignalOut] = {a.signal_id: a for a in analyses}

        for row in batch:
            out = by_id.get(row.id)
            if out is None:
                # The model didn't emit a result for this signal id.
                # Don't touch the row — it'll be retried next run.
                skipped_total += 1
                logger.warning(
                    "news_analyst_runner: no analysis returned for "
                    "signal_id=%s; will retry on next run.",
                    row.id,
                )
                continue
            row.materiality = out.materiality
            row.recommended_flag = out.recommended_flag
            row.rationale = out.rationale
            row.analyzed_at = now
            analyzed_total += 1

        session.flush()
        batches += 1

    return AnalysisRunResult(
        fetched=fetched,
        analyzed=analyzed_total,
        skipped=skipped_total,
        batches=batches,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _row_to_analyst_input(row: NewsSignal) -> AnalyzedSignalIn:
    """Hydrate one ORM row into the agent's input schema.

    Reads ONLY the normalized Stage 1 fields + ``evidence_excerpt``. The
    ``raw_text`` column on the row is intentionally not referenced — the
    test ``test_raw_text_canary_not_in_prompt`` pins this contract.
    """
    parsed_tickers = _decode_json_list(row.parsed_tickers)
    event_keywords = _decode_json_list(row.event_keywords)
    source = _coerce_source_literal(row.source)
    sentiment = _coerce_sentiment_literal(row.sentiment)
    source_trust = _coerce_trust_literal(row.source_trust)
    return AnalyzedSignalIn(
        signal_id=row.id,
        source=source,
        source_trust=source_trust,
        received_at=row.received_at,
        parsed_tickers=parsed_tickers,
        event_keywords=event_keywords,
        sentiment=sentiment,
        evidence_excerpt=row.evidence_excerpt,
    )


def _decode_json_list(raw: str | None) -> list[str]:
    """Best-effort decoder for the ``parsed_tickers`` / ``event_keywords``
    JSON columns. Returns [] on malformed input — Stage 1 wrote these
    so it shouldn't happen, but the runner stays robust if a hand-edit
    or migration leaves a row partially populated."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(x) for x in decoded]


def _coerce_source_literal(value: Any) -> Literal["discord", "rss", "macro_feed"]:
    """Narrow a string to the source Literal; defaults to ``rss`` on a
    bad value (defensive — the DB CHECK constraint should prevent it)."""
    if value in ("discord", "rss", "macro_feed"):
        return value  # type: ignore[return-value]
    return "rss"


def _coerce_sentiment_literal(
    value: Any,
) -> Literal["positive", "neutral", "negative"]:
    if value in ("positive", "neutral", "negative"):
        return value  # type: ignore[return-value]
    return "neutral"


def _coerce_trust_literal(value: Any) -> Literal["high", "medium", "low"]:
    if value in ("high", "medium", "low"):
        return value  # type: ignore[return-value]
    return "medium"


__all__ = [
    "AnalysisRunResult",
    "MAX_BATCH_SIZE",
    "run_news_signal_analysis",
]
