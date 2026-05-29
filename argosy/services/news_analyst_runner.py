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
from argosy.services.predictions.reliability import get_weight_for_source
from argosy.services.predictions.writers import write_news_signal_prediction
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
    user_id: str = "ariel",
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

    # Spec C commit #6 / spec §6.2 — per-source reliability multiplier.
    # Computed ONCE per source per batch (the predictions-ledger view
    # already caches with 5-min TTL but we further memoise here to
    # avoid N calls for N news_signals from the same source). Defaults
    # to 1.0 (no adjustment) when the ledger has no scored data for
    # the source yet.
    #
    # Codex review BLOCKER 2 fix (2026-05-29) — the runtime ``user_id``
    # is THREADED into the lookup so multi-tenant deployments don't
    # leak one user's reliability ledger into another's. The default
    # ``"ariel"`` is the single-user fallback; future multi-tenant
    # callers MUST pass the actual tenant id from the cron registry
    # / job dispatcher.
    #
    # Best-effort: the lookup itself catches and absorbs upstream
    # failures so a missing reliability surface NEVER blocks the
    # news analyst's primary classification path.
    weight_cache: dict[str, float] = {}

    def _weight_for(source: str) -> float:
        if source in weight_cache:
            return weight_cache[source]
        try:
            w = get_weight_for_source(
                session,
                user_id,
                source,
                "fixed_lookahead",
                provenance_weights_applied=False,
            )
        except Exception:  # noqa: BLE001 — never break analyst
            logger.exception(
                "news_analyst_runner: get_weight_for_source(%s) failed; "
                "defaulting to 1.0",
                source,
            )
            w = 1.0
        weight_cache[source] = w
        return w

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        # Hydrate each row into the AnalyzedSignalIn schema. CRITICAL:
        # ``raw_text`` is NOT referenced here — the function's contract
        # is that only normalized fields cross into the prompt.
        analyst_inputs = [
            _row_to_analyst_input(
                r, source_reliability_factor=_weight_for(r.source)
            )
            for r in batch
        ]

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

            # Spec C commit #3 — predictions ledger writer wiring. Only
            # write predictions for actionable signals (materiality in
            # {high, medium} per spec §2.4) with at least one extracted
            # ticker + a non-neutral sentiment direction. low-materiality
            # rows are gated out so the ledger isn't dominated by noise.
            _maybe_write_news_signal_predictions(
                session, row, out, user_id=user_id,
            )

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


def _maybe_write_news_signal_predictions(
    session: Session,
    row: NewsSignal,
    out: AnalyzedSignalOut,
    *,
    user_id: str = "ariel",
) -> None:
    """Spec C commit #3 — fan-out one prediction per ticker for actionable signals.

    Gates:
      * materiality in {high, medium} — low-materiality output is noise.
      * at least one ticker in ``row.parsed_tickers`` — multi-ticker
        signals fan out (one prediction row per ticker, sharing
        raw_text_ref).
      * direction inferred from sentiment: positive → long, negative →
        short, neutral → neutral. Predictions with direction=neutral
        are still logged (codex anti-hide pattern); the writer does
        not exclude them.

    Best-effort: any failure logs + swallows so a writer issue never
    blocks the analyst's primary materiality persistence.
    """
    if out.materiality not in ("high", "medium"):
        return
    tickers = _decode_json_list(row.parsed_tickers)
    if not tickers:
        return
    # Sentiment → prediction direction. The analyst's own sentiment
    # field is the most reliable directional cue we have at this
    # stage; consumers in commit #6 may refine via reliability
    # weighting.
    sentiment = (row.sentiment or "neutral").lower()
    if sentiment == "positive":
        direction: Literal["long", "short", "neutral"] = "long"
    elif sentiment == "negative":
        direction = "short"
    else:
        direction = "neutral"

    # Multi-tenant: ``user_id`` is threaded in by the caller; the
    # single-user default falls back to ``ariel`` for legacy /
    # discord_listener-style invocations. Codex review BLOCKER 2 fix
    # (2026-05-29) — removed the hardcoded ``ariel`` assignment that
    # masked an upstream multi-tenant leak.
    for ticker in tickers:
        # Wrap each write in a SAVEPOINT (nested transaction) so a
        # writer failure (e.g. FK violation against an unseeded
        # evaluation_method_registry in legacy test envs) ROLLS BACK
        # only that savepoint — the outer transaction's row mutations
        # (materiality/flag/rationale/analyzed_at) survive intact.
        try:
            with session.begin_nested():
                write_news_signal_prediction(
                    session,
                    user_id,
                    news_signal_id=int(row.id),
                    ticker=str(ticker),
                    direction=direction,
                    materiality_tier=out.materiality,
                    event_at=row.received_at,
                    raw_text_ref=f"news_signals.id:{row.id}",
                )
        except Exception:  # noqa: BLE001 — never break analyst on writer failure
            logger.exception(
                "news_analyst_runner: write_news_signal_prediction failed for "
                "signal_id=%s ticker=%s",
                row.id, ticker,
            )
    # NOTE: write_news_signal_prediction does NOT accept
    # provenance_weights_applied today — the analyst's output is an
    # internal_news_signal_analyst row whose own reliability is
    # measured separately from the upstream Discord/news source it
    # consumed. The stamp would only matter if the analyst's output
    # were ITSELF being fed back to another consumer that would re-
    # multiply by discord's weight. The synth integration above (the
    # per-source reliability banner) DOES surface
    # internal_news_signal_analyst as a separately-weighted source,
    # and the per_position_thesis emit_thesis_predictions(...
    # provenance_weights_applied=True) call site is what guards the
    # downstream chain. If a future consumer chains discord →
    # news_signal_analyst → THIS_NEW_CONSUMER, this writer call should
    # add provenance_weights_applied=True to that path.


def _row_to_analyst_input(
    row: NewsSignal,
    *,
    source_reliability_factor: float = 1.0,
) -> AnalyzedSignalIn:
    """Hydrate one ORM row into the agent's input schema.

    Reads ONLY the normalized Stage 1 fields + ``evidence_excerpt``. The
    ``raw_text`` column on the row is intentionally not referenced — the
    test ``test_raw_text_canary_not_in_prompt`` pins this contract.

    Args:
      row: the NewsSignal ORM row.
      source_reliability_factor: spec C commit #6 / §6.2 — the
        predictions-ledger-derived weight for this row's source.
        Caller (the runner) threads in the cached
        ``get_weight_for_source`` value; tests / standalone callers
        can pass 1.0 to default to baseline.
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
        source_reliability_factor=source_reliability_factor,
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
