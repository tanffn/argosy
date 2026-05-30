"""Orchestrator for the long-form Discord alpha-report analyst.

Wires :class:`argosy.agents.alpha_report_analyst.AlphaReportAnalystAgent`
to the ``news_signals`` table:

  1. Find ``news_signals`` rows from Discord with a long-form body that
     do NOT already have an ``alpha_report_analyses`` row.
  2. For each: run the analyst, persist the analysis, fan out per-ticker
     and structural Predictions, promote severe cautions to MonitorFlags.

Idempotency contract
====================

Re-running on the same ``news_signal_id`` returns the existing
:class:`AlphaReportAnalysis` ORM row + does NOT write duplicate
Predictions / MonitorFlags. The two layers of protection:

  * ``UniqueConstraint(news_signal_id)`` on ``alpha_report_analyses``
    (migration 0058) is the floor.
  * The runner SELECTs first and short-circuits when a row exists;
    Predictions are guarded by the per-source ``message_id`` dedup index
    (``ix_predictions_source_messageid``); MonitorFlags by the existing
    ``ix_monitor_flags_observer_dedup`` partial unique index over the
    ``dedup_key`` column.

Caution promotion gate
======================

Cautions are short free-form strings. Only those containing one of the
:data:`_SEVERE_CAUTION_HINTS` substrings (case-insensitive) promote to
a MonitorFlag with ``severity='warning'`` + ``kind='alpha_report_caution'``.
The remaining cautions stay in the analysis row's ``cautions_json``
column for citation / display only — they do NOT fire a flag (we
already have ``feedback_emergent_anomaly_detection``: no hardcoded
"caution mentions X → flag" detectors; the severity-hint gate is the
minimum bound, the LLM's tone is the primary signal).

Cost
====

One Opus call per signal (typical ~5K input tokens for a 6-9 KB report,
~3K output tokens for the structured analysis). Cost lives on the
existing agent-cost ledger via ``BaseAgent.run`` — this runner doesn't
double-track.

Caller (cadence layer)
======================

:class:`argosy.orchestrator.loops.alpha_report_analyst.AlphaReportAnalystLoop`
fires :func:`run_pending_batch` at 18:00 IDT daily (1h after the
news pipeline so all signals have been written before analysis).
On-demand invocation is supported via :func:`run_analyst_for_signal`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.agents.alpha_report_analyst import (
    AlphaReportAnalysis as AlphaReportAnalysisDTO,
)
from argosy.agents.alpha_report_analyst import (
    AlphaReportAnalystAgent,
    StructuralPick,
    TickerSignal,
)
from argosy.services.predictions.writers import (
    write_alpha_report_prediction,
)
from argosy.state.models import (
    AlphaReportAnalysis as AlphaReportAnalysisORM,
)
from argosy.state.models import MonitorFlag, NewsSignal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds — kept module-level so tests can pin contracts via inspection.
# ---------------------------------------------------------------------------

#: Signals shorter than this are tight messages handled by the regex
#: parser (``extract_alpha_call_from_text``). Mirrors the listener +
#: backfill skip threshold so the analyst only ever sees long-form posts.
MIN_LONG_FORM_BODY_CHARS: int = 500

#: Newline-count gate — mirror of the listener's
#: ``LONG_FORM_NEWLINE_THRESHOLD``. Codex review BLOCKER #2 fix: the
#: listener / backfill skip the regex parser when
#: ``len > 500 OR newlines > 5``, but the original runner selection
#: keyed off ``length > 500`` only. A newline-dense short post (e.g.
#: 400 chars with 8 newlines) was skipped by the regex AND skipped by
#: the analyst cron — falling through both gates. We now mirror the
#: full OR-condition in :func:`run_pending_batch`.
MIN_LONG_FORM_NEWLINES: int = 5

#: Timeframe enum -> days mapping for per-ticker signals (codex-locked).
_TIMEFRAME_DAYS_BY_ENUM: dict[str, int] = {
    "short": 7,
    "medium": 30,
    "long": 180,
    "unspecified": 30,  # default to the middle horizon
}

#: Structural picks always assumed long-bias + long-horizon per the
#: sprint brief (Meet Kevin-style; structural picks are portfolio core).
_STRUCTURAL_PICK_TIMEFRAME_DAYS: int = 180
_STRUCTURAL_PICK_DIRECTION: Literal["long"] = "long"

#: Cautions promoted to MonitorFlags require one of these substrings
#: (case-insensitive) in the caution text. The gate is intentionally
#: minimal — we want the LLM's caution authorship to drive flag
#: promotion, not a hand-rolled "if 'crash' in text" detector
#: (``feedback_emergent_anomaly_detection``). The four words below
#: capture the universal "this is a real warning, not a passing
#: observation" markers.
_SEVERE_CAUTION_HINTS: tuple[str, ...] = (
    "warning",
    "danger",
    "crash",
    "panic",
)


# ---------------------------------------------------------------------------
# Return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunBatchResult:
    """Summary returned by :func:`run_pending_batch`."""

    fetched: int
    analyzed: int
    skipped: int
    predictions_written: int
    monitor_flags_written: int


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_analyst_for_signal(
    session: Session,
    news_signal_id: int,
    *,
    agent: AlphaReportAnalystAgent | None = None,
    user_id: str = "ariel",
    now: datetime | None = None,
) -> AlphaReportAnalysisORM | None:
    """Run the analyst on one ``news_signals`` row, idempotently.

    Returns:
      * The existing :class:`AlphaReportAnalysis` ORM row if one already
        exists for ``news_signal_id`` — no LLM call, no downstream writes.
      * The newly-persisted ORM row when the agent succeeds.
      * ``None`` when the signal is missing OR the agent returns an
        unparseable response (the runner declines to persist a row;
        the next cron retries).

    Args:
      session: live SQLAlchemy session. Caller owns commit/rollback;
        this function flushes but does not commit so the caller can
        batch.
      news_signal_id: PK of the ``news_signals`` row to analyse.
      agent: inject an :class:`AlphaReportAnalystAgent` instance — tests
        pass a mock that overrides ``run`` to skip the SDK call.
      user_id: tenant id (defaults ``"ariel"`` for the single-user
        deployment).
      now: override the ``analyzed_at`` timestamp — tests pin time;
        production passes ``None`` and falls back to wall-clock UTC.
    """
    signal = session.get(NewsSignal, int(news_signal_id))
    if signal is None:
        logger.warning(
            "alpha_report_analyst_runner: news_signal %s not found",
            news_signal_id,
        )
        return None

    # Idempotency — short-circuit on existing analysis row.
    existing = _lookup_existing_analysis(session, news_signal_id=news_signal_id)
    if existing is not None:
        logger.debug(
            "alpha_report_analyst_runner: news_signal %s already has "
            "AlphaReportAnalysis id=%s — skipping",
            news_signal_id, existing.id,
        )
        return existing

    raw_text = signal.raw_text or ""
    if not raw_text.strip():
        logger.info(
            "alpha_report_analyst_runner: news_signal %s has empty "
            "raw_text — skipping",
            news_signal_id,
        )
        return None

    analyst = agent or AlphaReportAnalystAgent(user_id=user_id)

    # Run the LLM. ``analyst.run`` is async; the runner is sync (caller
    # contract mirrors the news_signal_analyst runner) so we use
    # asyncio.run. Will raise RuntimeError if called from an event loop;
    # async callers should call ``analyst.run`` + ``_post_validate_output``
    # directly.
    parsed_tickers = _decode_json_list(signal.parsed_tickers)
    try:
        report = asyncio.run(
            analyst.run(
                raw_text=raw_text,
                parsed_tickers=parsed_tickers,
                sentiment=signal.sentiment,
            )
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "run_analyst_for_signal is sync — call from a non-async "
            "context, or await AlphaReportAnalystAgent.run + "
            "_post_validate_output directly."
        ) from exc
    except Exception:  # noqa: BLE001
        logger.exception(
            "alpha_report_analyst_runner: agent.run failed for "
            "news_signal_id=%s",
            news_signal_id,
        )
        return None

    # Post-validate (drops hallucinated tickers, coerces enums, returns
    # the dataclass form). The agent's ``run`` already returned an
    # ``AgentReport`` whose ``.output`` is the pydantic model; we hand
    # the model to ``_post_validate_output`` which converts to the
    # dataclass with the source-text ticker check applied.
    validated = analyst._post_validate_output(report.output, raw_text)
    if validated is None:
        logger.warning(
            "alpha_report_analyst_runner: post-validation returned "
            "None for news_signal_id=%s — skipping",
            news_signal_id,
        )
        return None

    # Persist + fan out.
    analyzed_at = now or datetime.now(UTC)
    analysis_row = _persist_analysis(
        session,
        user_id=user_id,
        news_signal_id=int(news_signal_id),
        analyzed_at=analyzed_at,
        analysis=validated,
    )
    if analysis_row is None:
        # Race with a parallel writer (UNIQUE violation). Look up the
        # winner and treat as the existing row.
        existing = _lookup_existing_analysis(
            session, news_signal_id=news_signal_id,
        )
        if existing is not None:
            logger.info(
                "alpha_report_analyst_runner: race-loser on "
                "news_signal_id=%s — returning existing analysis id=%s",
                news_signal_id, existing.id,
            )
            return existing
        # We lost a race but the winner vanished — log and bail.
        logger.warning(
            "alpha_report_analyst_runner: failed to persist analysis "
            "for news_signal_id=%s and no winner found",
            news_signal_id,
        )
        return None

    # Predictions fanout — per-ticker signals + structural picks.
    _fan_out_predictions(
        session,
        user_id=user_id,
        analysis_row=analysis_row,
        analysis=validated,
        event_at=signal.received_at,
    )

    # MonitorFlag promotion for severity-hinting cautions.
    _maybe_promote_cautions(
        session,
        user_id=user_id,
        news_signal_id=int(news_signal_id),
        analysis_id=int(analysis_row.id),
        cautions=validated.cautions,
        surfaced_at=analyzed_at,
    )

    return analysis_row


def run_pending_batch(
    session: Session,
    *,
    agent: AlphaReportAnalystAgent | None = None,
    user_id: str = "ariel",
    limit: int = 20,
    now: datetime | None = None,
) -> RunBatchResult:
    """Find unanalysed long-form Discord signals + run the analyst on each.

    Selection: ``source='discord'`` AND ``length(raw_text) >
    MIN_LONG_FORM_BODY_CHARS`` AND no ``alpha_report_analyses`` row
    exists. Ordered oldest-first.

    Args:
      session: live SQLAlchemy session. Caller owns commit/rollback;
        this function flushes between signals so partial progress
        survives a per-signal failure (the loop traps + logs).
      agent: inject the analyst (tests pass a mock; production lets
        the function lazy-construct an :class:`AlphaReportAnalystAgent`).
      user_id: tenant id.
      limit: cap on signals processed per call — keeps wall-clock
        bounded under the cadence loop. Defaults 20.
      now: ``analyzed_at`` override for determinism.

    Returns:
      :class:`RunBatchResult` with counts.
    """
    # Find candidate signals — left-anti-join with alpha_report_analyses.
    #
    # Codex review BLOCKER #2 fix — mirror the listener/backfill's full
    # OR condition (len > 500 OR newlines > 5) so the analyst picks up
    # newline-dense short posts the regex skipped. SQLite has no
    # built-in "count newlines" function; we approximate via
    # ``length(raw_text) - length(replace(raw_text, X'0A', ''))`` which
    # counts the number of LF bytes in the text (the listener's
    # ``text.count("\n")`` uses the same definition). The
    # ``length - length(replace(...))`` idiom is portable across SQLite
    # / Postgres without a UDF.
    newline_count_expr = (
        func.length(NewsSignal.raw_text)
        - func.length(func.replace(NewsSignal.raw_text, "\n", ""))
    )
    stmt = (
        select(NewsSignal)
        .where(NewsSignal.source == "discord")
        .where(
            (func.length(NewsSignal.raw_text) > MIN_LONG_FORM_BODY_CHARS)
            | (newline_count_expr > MIN_LONG_FORM_NEWLINES)
        )
        .where(
            ~select(AlphaReportAnalysisORM.id)
            .where(AlphaReportAnalysisORM.news_signal_id == NewsSignal.id)
            .exists()
        )
        .order_by(NewsSignal.received_at.asc())
        .limit(limit)
    )
    rows: list[NewsSignal] = list(session.execute(stmt).scalars())
    fetched = len(rows)
    if fetched == 0:
        return RunBatchResult(
            fetched=0, analyzed=0, skipped=0,
            predictions_written=0, monitor_flags_written=0,
        )

    analyst = agent or AlphaReportAnalystAgent(user_id=user_id)

    analyzed = 0
    skipped = 0
    predictions_total = 0
    monitor_flags_total = 0

    for signal in rows:
        try:
            row = run_analyst_for_signal(
                session,
                signal.id,
                agent=analyst,
                user_id=user_id,
                now=now,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "alpha_report_analyst_runner: per-signal failure "
                "news_signal_id=%s",
                signal.id,
            )
            skipped += 1
            continue
        if row is None:
            skipped += 1
            continue
        analyzed += 1
        session.flush()

        # Per-signal counts — query the DB for the rows whose source_ref
        # references this signal. Used by the cadence layer for
        # operator visibility; the analysis row itself is the source of
        # truth, this is just a roll-up.
        Prediction = _imp_prediction_table()
        post_pred_count = session.scalar(
            select(func.count()).select_from(Prediction).where(
                Prediction.c.source == "discord_alpha_report",
                Prediction.c.source_ref.like(
                    f'%"news_signal_id": {signal.id}%'
                ),
            )
        ) or 0
        post_flag_count = session.scalar(
            select(func.count(MonitorFlag.id)).where(
                MonitorFlag.user_id == user_id,
                MonitorFlag.kind == "alpha_report_caution",
                MonitorFlag.dedup_key.like(
                    f"v1|alpha_report_caution|{signal.id}.%"
                ),
            )
        ) or 0
        predictions_total += int(post_pred_count)
        monitor_flags_total += int(post_flag_count)

    return RunBatchResult(
        fetched=fetched,
        analyzed=analyzed,
        skipped=skipped,
        predictions_written=predictions_total,
        monitor_flags_written=monitor_flags_total,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _imp_prediction_table():
    """Lazy import of the Prediction table for the per-signal counters
    in :func:`run_pending_batch`. Avoids a circular import at module top."""
    from argosy.state.models import Prediction
    return Prediction.__table__


def _lookup_existing_analysis(
    session: Session, *, news_signal_id: int,
) -> AlphaReportAnalysisORM | None:
    stmt = select(AlphaReportAnalysisORM).where(
        AlphaReportAnalysisORM.news_signal_id == news_signal_id
    )
    return session.execute(stmt).scalar_one_or_none()


def _persist_analysis(
    session: Session,
    *,
    user_id: str,
    news_signal_id: int,
    analyzed_at: datetime,
    analysis: AlphaReportAnalysisDTO,
) -> AlphaReportAnalysisORM | None:
    """INSERT one ``alpha_report_analyses`` row. Returns ``None`` on
    UNIQUE-constraint race (caller looks up the existing row)."""
    row = AlphaReportAnalysisORM(
        news_signal_id=news_signal_id,
        user_id=user_id,
        analyzed_at=analyzed_at,
        macro_tone=analysis.macro_tone,
        macro_tone_confidence=analysis.macro_tone_confidence,
        key_themes=json.dumps(list(analysis.key_themes), ensure_ascii=False),
        summary_rationale=analysis.summary_rationale,
        ticker_signals_json=json.dumps(
            [_ticker_signal_to_dict(s) for s in analysis.ticker_signals],
            ensure_ascii=False,
        ),
        structural_picks_json=json.dumps(
            [_structural_pick_to_dict(p) for p in analysis.structural_picks],
            ensure_ascii=False,
        ),
        cautions_json=json.dumps(list(analysis.cautions), ensure_ascii=False),
        index_targets_json=json.dumps(
            dict(analysis.index_targets), ensure_ascii=False,
        ),
        confidence_overall=analysis.confidence_overall,
        agent_version="v1",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def _fan_out_predictions(
    session: Session,
    *,
    user_id: str,
    analysis_row: AlphaReportAnalysisORM,
    analysis: AlphaReportAnalysisDTO,
    event_at: datetime,
) -> None:
    """Per-ticker signals + structural picks → :func:`write_alpha_report_prediction`.

    Best-effort: each write wraps in a SAVEPOINT so a single
    writer failure (FK / CHECK violation) rolls back only that one
    prediction — the analysis row + sibling predictions survive.
    """
    raw_text_ref = f"news_signals.id:{analysis_row.news_signal_id}"

    for sig in analysis.ticker_signals:
        direction = _sentiment_to_direction(sig.sentiment)
        timeframe_days = _TIMEFRAME_DAYS_BY_ENUM.get(sig.timeframe, 30)
        try:
            with session.begin_nested():
                write_alpha_report_prediction(
                    session,
                    user_id,
                    analysis_id=int(analysis_row.id),
                    news_signal_id=int(analysis_row.news_signal_id),
                    ticker=sig.ticker,
                    direction=direction,
                    kind="signal",
                    event_at=event_at,
                    timeframe_days=timeframe_days,
                    raw_text_ref=raw_text_ref,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "alpha_report_analyst_runner: failed to write "
                "ticker_signal prediction for analysis_id=%s ticker=%s",
                analysis_row.id, sig.ticker,
            )

    for pick in analysis.structural_picks:
        try:
            with session.begin_nested():
                write_alpha_report_prediction(
                    session,
                    user_id,
                    analysis_id=int(analysis_row.id),
                    news_signal_id=int(analysis_row.news_signal_id),
                    ticker=pick.ticker,
                    direction=_STRUCTURAL_PICK_DIRECTION,
                    kind="pick",
                    event_at=event_at,
                    timeframe_days=_STRUCTURAL_PICK_TIMEFRAME_DAYS,
                    raw_text_ref=raw_text_ref,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "alpha_report_analyst_runner: failed to write "
                "structural_pick prediction for analysis_id=%s ticker=%s",
                analysis_row.id, pick.ticker,
            )


def _maybe_promote_cautions(
    session: Session,
    *,
    user_id: str,
    news_signal_id: int,
    analysis_id: int,
    cautions: list[str],
    surfaced_at: datetime,
) -> None:
    """Promote severity-hinting cautions to ``alpha_report_caution`` flags.

    Idempotency: ``dedup_key`` is
    ``v1|alpha_report_caution|<news_signal_id>.<caution_hash>``. The
    runner's idempotency contract already prevents re-running on the
    same news_signal_id, but the dedup_key gives a safety net against
    parallel writers.

    Best-effort: each insert wraps in a SAVEPOINT.
    """
    for caution in cautions:
        if not _is_severe_caution(caution):
            continue
        # Stable per-caution hash so re-runs of the analyst on the same
        # signal that re-emit the same caution string dedup at the
        # partial-unique-index layer.
        import hashlib
        digest = hashlib.sha1(caution.encode("utf-8")).hexdigest()[:12]
        dedup_key = f"v1|alpha_report_caution|{news_signal_id}.{digest}"
        payload = {
            "news_signal_id": news_signal_id,
            "alpha_report_analysis_id": analysis_id,
            "caution": caution,
            "severity_hint_matched": _matched_severity_hint(caution),
        }
        try:
            with session.begin_nested():
                row = MonitorFlag(
                    user_id=user_id,
                    kind="alpha_report_caution",
                    severity="warning",
                    payload=json.dumps(payload, default=str),
                    surfaced_at=surfaced_at,
                    dedup_key=dedup_key,
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            logger.debug(
                "alpha_report_analyst_runner: dedup hit for caution "
                "flag news_signal_id=%s dedup_key=%s",
                news_signal_id, dedup_key,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "alpha_report_analyst_runner: failed to write "
                "alpha_report_caution monitor_flag for "
                "news_signal_id=%s caution=%r",
                news_signal_id, caution[:80],
            )


def _is_severe_caution(caution: str) -> bool:
    """True iff the caution contains a severity-warning hint substring."""
    if not caution:
        return False
    lowered = caution.lower()
    return any(hint in lowered for hint in _SEVERE_CAUTION_HINTS)


def _matched_severity_hint(caution: str) -> str | None:
    """Return the first severity hint that matched, for the flag payload."""
    lowered = (caution or "").lower()
    for hint in _SEVERE_CAUTION_HINTS:
        if hint in lowered:
            return hint
    return None


def _sentiment_to_direction(
    sentiment: str,
) -> Literal["long", "short", "neutral"]:
    if sentiment == "positive":
        return "long"
    if sentiment == "negative":
        return "short"
    return "neutral"


def _ticker_signal_to_dict(sig: TickerSignal) -> dict:
    return {
        "ticker": sig.ticker,
        "sentiment": sig.sentiment,
        "conviction": sig.conviction,
        "timeframe": sig.timeframe,
        "action_hint": sig.action_hint,
        "context_excerpt": sig.context_excerpt,
    }


def _structural_pick_to_dict(pick: StructuralPick) -> dict:
    return {
        "ticker": pick.ticker,
        "kind": pick.kind,
        "conviction": pick.conviction,
        "rationale": pick.rationale,
    }


def _decode_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(x) for x in decoded]


__all__ = [
    "MIN_LONG_FORM_BODY_CHARS",
    "RunBatchResult",
    "run_analyst_for_signal",
    "run_pending_batch",
]
