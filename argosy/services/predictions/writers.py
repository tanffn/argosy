"""Per-source prediction writer adapters — Spec C commit #3.

One writer per signal source (Discord alpha calls, news_signal_analyst
LLM verdicts, per_position_thesis, state_observer flags, plan_monitor
flags). Each writer:

  * Builds a deterministic ``message_id`` (the ``v1|predictions|<source>
    |<entity-id>`` per-source dedup key from spec §2.2) and stores it on
    ``predictions.message_id``.
  * Pre-computes ``evaluation_due_at`` and ``evaluation_method`` per
    spec §3.1 (codex BLOCKER 2 fix — the due query keys off this
    column, NOT raw ``timeframe_days``).
  * INSERTs the row; on ``IntegrityError`` from the
    ``(source, message_id)`` UNIQUE index, returns the existing row.

Idempotency contract (spec §2.2): re-running any writer with the same
``source``+source-stable-entity-id returns the existing prediction row
unchanged. No double-counting. No exception propagated to caller.

Anti-collision / actionable-only gating (spec §3):

  * Discord: caller MUST pre-parse the message (via
    ``parsers.extract_alpha_call_from_text``) and only invoke this
    writer when a ticker + direction were extracted. The writer itself
    does NOT re-parse — caller is the gate.
  * news_signal_analyst: caller MUST gate on materiality in
    {high, medium} per spec §2.4 (low materiality is logged but skipped
    here to avoid coverage explosion).
  * per_position_thesis: ALL verdicts including HOLD are written
    (codex BLOCKER #3 — anti-hide-behind-HOLD). HOLD maps to direction
    ``neutral`` and is scored against subsequent price action.
  * state_observer: caller MUST gate on severity >= warning (info-band
    flags are noise; spec §2.4 lists actionable observer flags as the
    ones reaching the ledger).
  * plan_monitor: every MonitorFlag insertion has a corresponding
    prediction write — the trigger for the underlying MonitorFlag has
    already gated on the actionable case.

evaluation_method selection (spec §3.1):

  * Both target_price AND stop_price set → ``target_stop``,
    window = timeframe_days.
  * Else timeframe_days <= 7 → ``fixed_lookahead_7d``, window = 7.
  * Else timeframe_days <= 30 OR > 30 → ``fixed_lookahead_30d``,
    window = 30 (the §5.5 30-day cap on long-horizon predictions).

``evaluation_due_at = event_at + window_days``. The 30d cap fires at
30 days, NOT at the source's raw timeframe (e.g. 13F at 90d).

Per-source default timeframes (spec §1.2):

  * Discord:                    7 days (unless caller passes one).
  * news_signal_analyst high:  14 days; medium: 30 days; low: SKIP.
  * per_position_thesis:       30 days.
  * state_observer:            30 days (always — fixed_lookahead_30d).
  * monitor_flag:              30 days (always — fixed_lookahead_30d).

The ``user_id`` argument is required on every writer (multi-tenant
ready per SDD §12.5; single user today but FKs are in place).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.state.models import Prediction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — kept module-level so tests can pin contracts via inspection
# ---------------------------------------------------------------------------

#: Spec §2.2 dedup key version. Bump (to ``v2``) if a per-source formula
#: changes so prior writes don't retroactively collide with the new shape.
DEDUP_KEY_VERSION: str = "v1"

#: Spec §3.1 / §5.5 — long-horizon predictions cap at 30 days even when
#: the source's stated timeframe is longer (e.g. 13F at 90d). The
#: evaluator's due-query keys off ``evaluation_due_at``, so this cap is
#: realised at write time, not at evaluation time.
LONG_HORIZON_CAP_DAYS: int = 30

#: Per-source default timeframes when the caller doesn't pass one.
#: Used when the source-specific timeframe is None / unspecified.
DEFAULT_TIMEFRAME_DAYS_DISCORD: int = 7
DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH: int = 14
DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM: int = 30
DEFAULT_TIMEFRAME_DAYS_THESIS: int = 30
DEFAULT_TIMEFRAME_DAYS_OBSERVER: int = 30
DEFAULT_TIMEFRAME_DAYS_MONITOR: int = 30


# ---------------------------------------------------------------------------
# Per-source message_id (== dedup_key) formulas — spec §2.2
# ---------------------------------------------------------------------------


def discord_message_id(
    *, channel_id: int | str | None, message_id: str
) -> str:
    """``v1|predictions|discord|<channel_id>.<message_id>``.

    Codex review BLOCKER 1 fix: the prior shape switched between
    ``<channel_id>.<message_id>`` (when caller passed channel_id) and
    ``<message_id>`` alone (when caller omitted it) for the SAME logical
    event, producing duplicate ledger rows. Fix: ALWAYS emit
    ``<channel_id>.<message_id>``; when channel_id is missing the caller
    gets ``0.<message_id>`` (the canonical "unknown channel" placeholder).
    Discord message ids are Snowflakes (globally unique within a
    workspace), so ``0.<message_id>`` is itself unique — there is no
    collision risk vs a real channel_id which is always a 17-19-digit
    Snowflake.

    Public (no leading underscore) per Spec C commit #7 codex review
    IMPORTANT 2 — the discord_backfill service needs to derive the
    SAME dedup key the writer would produce in order to classify
    ``predictions_written`` vs ``predictions_deduped`` in its summary
    counters. Inlining the formula at the call site was rejected:
    a future per-source-formula change (a v2 dedup key) would
    silently drift the summary classification while DB idempotency
    still held — the writer would dedup correctly but the summary
    would mis-attribute. Single source of truth lives here. The
    ``_discord_message_id`` private alias below is preserved for
    backward compat with any in-tree callers that imported the
    private name.
    """
    if not message_id:
        raise ValueError("discord prediction needs a non-empty message_id")
    # Stable channel_id slot — 0 means "unknown channel" (e.g. backfill
    # paths that have only the message_id). Stringifying int and str
    # channel_ids both collapse to the same partition (999 → "999",
    # "999" → "999").
    ch = str(channel_id) if channel_id is not None and str(channel_id) else "0"
    return f"{DEDUP_KEY_VERSION}|predictions|discord|{ch}.{message_id}"


# Private alias kept for back-compat with any in-tree imports of the
# pre-#7 leading-underscore name. New code MUST use the public
# :func:`discord_message_id` so the dedup-key formula has a single
# source of truth (Spec C commit #7 codex review IMPORTANT 2).
_discord_message_id = discord_message_id


def _news_signal_message_id(*, news_signal_id: int, ticker: str) -> str:
    """``v1|predictions|nsa|<news_signal_id>.<ticker>``.

    Note the per-(signal,ticker) granularity: a news_signal mentioning
    NVDA + AMD writes TWO prediction rows under TWO distinct dedup keys
    — they're separate predictions (one per ticker) but trace back to
    the same NewsSignal row via ``raw_text_ref``.
    """
    if not news_signal_id:
        raise ValueError("news_signal prediction needs a non-empty news_signal_id")
    if not ticker:
        raise ValueError("news_signal prediction needs a non-empty ticker")
    return f"{DEDUP_KEY_VERSION}|predictions|nsa|{news_signal_id}.{ticker.upper()}"


def _thesis_message_id(*, thesis_id: int | str, ticker: str) -> str:
    """``v1|predictions|thesis|<thesis_id>.<ticker>``.

    thesis_id here is the per-position thesis row's identifier. Per
    spec §2.2 the formula is ``thesis|<draft_id>.<ticker>`` where the
    draft_id is the synthesis-run draft this thesis was produced for.
    For commit #3 we accept a generic ``thesis_id`` (the call-site
    composes it from draft_id when one exists).
    """
    if thesis_id is None or thesis_id == "":
        raise ValueError("thesis prediction needs a non-empty thesis_id")
    if not ticker:
        raise ValueError("thesis prediction needs a non-empty ticker")
    return f"{DEDUP_KEY_VERSION}|predictions|thesis|{thesis_id}.{ticker.upper()}"


def _state_observer_message_id(*, observer_flag_id: int | str) -> str:
    """``v1|predictions|so|<observer_flag_id>``.

    observer_flag_id is typically the ``monitor_flags.id`` returned by
    ``write_observer_flags``. One observer flag → one prediction row.
    """
    if observer_flag_id is None or observer_flag_id == "":
        raise ValueError("state_observer prediction needs a non-empty observer_flag_id")
    return f"{DEDUP_KEY_VERSION}|predictions|so|{observer_flag_id}"


def _monitor_flag_message_id(*, monitor_flag_id: int | str) -> str:
    """``v1|predictions|mf|<monitor_flags.id>``."""
    if monitor_flag_id is None or monitor_flag_id == "":
        raise ValueError("monitor_flag prediction needs a non-empty monitor_flag_id")
    return f"{DEDUP_KEY_VERSION}|predictions|mf|{monitor_flag_id}"


# ---------------------------------------------------------------------------
# evaluation_method + evaluation_due_at — spec §3.1 writer-side selection
# ---------------------------------------------------------------------------


def _choose_method_and_window(
    *,
    target_price: Decimal | float | None,
    stop_price: Decimal | float | None,
    direction: str,
    timeframe_days: int | None,
) -> tuple[str, int]:
    """Return ``(evaluation_method, window_days)`` per spec §3.1.

    Selection rules (writer-side; evaluator does NOT re-derive):

      * target_price AND stop_price both set → ``target_stop``,
        window = timeframe_days (default 7 if unspecified).
      * direction='multi' → ``multi_basket_weighted``,
        window = min(timeframe_days, 30).
      * timeframe_days <= 7  → ``fixed_lookahead_7d``,  window = 7.
      * timeframe_days <= 30 → ``fixed_lookahead_30d``, window = 30.
      * timeframe_days > 30  → ``fixed_lookahead_30d``, window = 30
        (spec §5.5 cap — 13F at 90d still scores at 30d).

    Codex BLOCKER 2 fix: the window stored in ``evaluation_due_at`` is
    the CHOSEN window, NOT raw timeframe_days. The evaluator's due-query
    keys off this column directly so the 30d cap fires at 30 days for
    long-horizon sources (13F, state_observer, etc.).

    Args:
      target_price / stop_price: source-asserted levels. Both must be
        non-NULL for the ``target_stop`` method to apply.
      direction: prediction direction enum (long / short / neutral /
        multi). ``multi`` selects ``multi_basket_weighted``.
      timeframe_days: source-asserted timeframe. ``None`` → falls back
        to 7 days (the most conservative per-source default).

    Returns:
      ``(method_name, window_days)`` — both strings used downstream:
      method_name on the row + window_days for the due-at math.
    """
    if direction == "multi":
        # spec §3.1 — multi-basket caps at 30d at write time per §5.5.
        chosen_window = min(timeframe_days or LONG_HORIZON_CAP_DAYS, LONG_HORIZON_CAP_DAYS)
        return ("multi_basket_weighted", chosen_window)

    if target_price is not None and stop_price is not None:
        # target_stop uses the source's stated timeframe verbatim — the
        # source committed to "this will play out in N days." Default to
        # 7 days (Discord-style short-window assumption) when caller
        # leaves it unset.
        chosen_window = timeframe_days or DEFAULT_TIMEFRAME_DAYS_DISCORD
        return ("target_stop", chosen_window)

    # No-target-stop path: bucket by stated timeframe into the two
    # fixed-lookahead methods. The 30d cap (§5.5) is realised here so
    # the evaluator sees a 30d due-at for a 90d-stated 13F prediction.
    tf = timeframe_days or DEFAULT_TIMEFRAME_DAYS_OBSERVER
    if tf <= 7:
        return ("fixed_lookahead_7d", 7)
    return ("fixed_lookahead_30d", LONG_HORIZON_CAP_DAYS)


def _ensure_aware(dt: datetime) -> datetime:
    """Normalise any input datetime to tz-aware UTC.

    Codex IMPORTANT 2 fix: the prior shape only attached UTC tzinfo to
    naive inputs and passed aware inputs through unchanged, which meant
    an aware datetime in a non-UTC zone (e.g. IST = UTC+3) would land on
    the row with its native offset. SQLite then strips the tzinfo on
    roundtrip, leaving the evaluator's "is this row due?" comparison
    drifting by the offset amount (3 hours of false-positive 'due' on
    IST inputs).

    Behavior now:
      * Naive input  → assume UTC, attach UTC tzinfo.
      * Aware input  → convert to UTC via astimezone(UTC), preserving
        the instant in time but normalising the wall-clock to UTC.

    This is the same naive-UTC-baseline policy used by
    ``state_observer_flag_writer._to_naive_utc`` for SQLite-portable
    comparisons.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Core insert helper — INSERT with per-source idempotency
# ---------------------------------------------------------------------------


def _insert_prediction(
    session: Session,
    user_id: str,
    *,
    source: str,
    source_ref: dict[str, Any],
    message_id: str,
    direction: str,
    event_at: datetime,
    ticker: str | None = None,
    entry_price: Decimal | float | None = None,
    target_price: Decimal | float | None = None,
    stop_price: Decimal | float | None = None,
    timeframe_days: int | None = None,
    raw_text_ref: str | None = None,
    unparseable_reason: str | None = None,
    multi_ticker_json: str | None = None,
    entry_prices_json: str | None = None,
    provenance_weights_applied: bool = False,
) -> Prediction:
    """INSERT one prediction row with per-source idempotency.

    Idempotency: the partial-unique index
    ``ix_predictions_source_messageid`` on ``(source, message_id)``
    enforces dedup at the DB layer. On ``IntegrityError`` we rollback
    the failed INSERT and re-SELECT the existing row, returning it
    unchanged. The caller never sees a duplicate row OR an exception.

    Method selection: ``_choose_method_and_window`` is consulted to
    pre-compute ``evaluation_method`` + ``evaluation_due_at`` so the
    evaluator (commit #4) can scan due rows without re-deriving the
    window from raw ``timeframe_days``.

    Returns:
      The persisted (or already-existing) ``Prediction`` ORM instance.
    """
    method, window_days = _choose_method_and_window(
        target_price=target_price,
        stop_price=stop_price,
        direction=direction,
        timeframe_days=timeframe_days,
    )
    event_at_aware = _ensure_aware(event_at)
    evaluation_due_at = event_at_aware + timedelta(days=window_days)

    row = Prediction(
        user_id=user_id,
        source=source,
        source_ref=json.dumps(source_ref, sort_keys=True, default=str),
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        timeframe_days=timeframe_days,
        multi_ticker_json=multi_ticker_json,
        entry_prices_json=entry_prices_json,
        message_id=message_id,
        raw_text_ref=raw_text_ref,
        unparseable_reason=unparseable_reason,
        event_at=event_at_aware,
        evaluation_due_at=evaluation_due_at,
        evaluation_method=method,
        # Spec C commit #6 — anti-feedback-loop stamp (spec §6.6 / codex
        # IMPORTANT 3). Defaults to 0; consumers that have ALREADY
        # applied a reliability weight upstream pass True so downstream
        # readers know to skip re-applying the weight.
        provenance_weights_applied=1 if provenance_weights_applied else 0,
    )
    # PRE-CHECK existing row before INSERT. This avoids needing to
    # rollback the session on dedup hits — which would also unwind any
    # SAVEPOINT the caller wrapped us in. The PRE-CHECK is a SELECT
    # against the same partial-unique index the INSERT would collide
    # on; in single-writer Argosy there's no race window worth
    # mitigating between SELECT and INSERT.
    existing_stmt = select(Prediction).where(
        Prediction.source == source,
        Prediction.message_id == message_id,
    )
    existing = session.execute(existing_stmt).scalar_one_or_none()
    if existing is not None:
        logger.debug(
            "predictions.writers: dedup hit for (source=%s, message_id=%s) — "
            "returning existing row id=%s",
            source,
            message_id,
            existing.id,
        )
        return existing

    # Wrap the INSERT in our OWN SAVEPOINT so we can roll back JUST the
    # failed flush + re-SELECT in the race-loser case without disturbing
    # the caller's outer transaction. ``session.begin_nested()`` is
    # idempotent-nestable: even if the caller also wrapped us in a
    # savepoint, this just nests one level deeper.
    #
    # Codex IMPORTANT 1 fix: the writer's documented "re-running returns
    # the existing row, no exception" contract must hold even when the
    # caller didn't wrap us in their own savepoint. The inner savepoint
    # here makes the contract self-contained.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # Could be a race-loser on the dedup index (rare in single-writer
        # Argosy after the PRE-CHECK above) OR an unrelated FK / CHECK
        # violation (e.g. unseeded evaluation_method_registry in a
        # legacy test env). The inner SAVEPOINT just rolled back, so
        # the session is in a clean state for a re-SELECT.
        existing = session.execute(existing_stmt).scalar_one_or_none()
        if existing is not None:
            logger.debug(
                "predictions.writers: race-loser dedup hit for "
                "(source=%s, message_id=%s) — returning existing row id=%s",
                source, message_id, existing.id,
            )
            return existing
        # Not a dedup race — re-raise the original IntegrityError so
        # the caller's outer try/except captures it.
        raise
    return row


# ---------------------------------------------------------------------------
# Public writers — one per signal source family
# ---------------------------------------------------------------------------


def write_discord_prediction(
    session: Session,
    user_id: str,
    *,
    message_id: str,
    ticker: str,
    direction: Literal["long", "short"],
    event_at: datetime,
    channel_id: int | str | None = None,
    entry_price: Decimal | float | None = None,
    target_price: Decimal | float | None = None,
    stop_price: Decimal | float | None = None,
    timeframe_days: int | None = None,
    raw_text_ref: str | None = None,
) -> Prediction:
    """Write a prediction row sourced from a Discord alpha call.

    Caller pre-conditions (spec §3 anti-collision contract):

      * The message body has ALREADY been parsed via
        ``parsers.extract_alpha_call_from_text`` and a ticker + direction
        were extracted. This writer assumes actionability.
      * ``event_at`` is the Discord message timestamp (NOT the
        backfill-run timestamp). Per spec §2.3 the entry-price snapshot
        and the evaluation window both anchor at this moment.

    Idempotency: ``(source='discord', message_id=v1|predictions|discord|
    <channel_id>.<message_id>)`` — re-running with the same Discord
    message_id returns the existing row.

    Args:
      session: live SQLAlchemy session. Caller owns the outer transaction.
      user_id: tenant whose predictions row this is. Multi-tenant ready.
      message_id: the Discord MESSAGE_CREATE event's id (Snowflake).
      ticker: extracted ticker, e.g. ``"NVDA"``. Case-normalised to upper.
      direction: ``"long"`` (BUY/LONG/ADD) or ``"short"`` (SELL/SHORT/TRIM).
      event_at: the message timestamp — real-world prediction time per §2.3.
      channel_id: optional Discord channel id for cross-channel dedup
        partition. Falls back to message_id-alone when missing.
      entry_price / target_price / stop_price: source-asserted price levels.
        entry_price typically filled by the writer's caller using a
        price-adapter snapshot at ``event_at`` (hindsight-bias killer
        per spec §2.3). ``None`` is acceptable — evaluator fills in
        later from adapter snapshot at evaluation time.
      timeframe_days: e.g. 7 for "by Friday" calls. None → 7 (Discord
        default per spec §1.2).
      raw_text_ref: pointer to the NewsSignal row this Discord call was
        ingested into (``"news_signals.id:<id>"``). NEVER injected into
        LLM prompts — citation-display only.

    Returns:
      Persisted ``Prediction`` row (or existing row on idempotent re-run).
    """
    dedup_id = _discord_message_id(channel_id=channel_id, message_id=message_id)
    return _insert_prediction(
        session,
        user_id,
        source="discord",
        source_ref={
            "channel_id": channel_id,
            "message_id": message_id,
        },
        message_id=dedup_id,
        ticker=ticker.upper(),
        direction=direction,
        event_at=event_at,
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        timeframe_days=timeframe_days if timeframe_days is not None else DEFAULT_TIMEFRAME_DAYS_DISCORD,
        raw_text_ref=raw_text_ref,
    )


def write_news_signal_prediction(
    session: Session,
    user_id: str,
    *,
    news_signal_id: int,
    ticker: str,
    direction: Literal["long", "short", "neutral"],
    materiality_tier: Literal["high", "medium", "low"],
    event_at: datetime,
    raw_text_ref: str | None = None,
) -> Prediction | None:
    """Write a prediction row sourced from a NewsSignalAnalyst Stage-2 verdict.

    materiality_tier → timeframe_days mapping (spec §2.4 + binding
    decision: HIGH news → 14d hypothesis, MEDIUM → 30d, LOW → SKIP):

      * ``high``:   timeframe = 14d → method ``fixed_lookahead_30d``
                     with window = 30d after the §5.5 cap normalisation.
                     (Codex review note: HIGH news predicts SHORT-term
                     impact; we cap at 30d for cross-source comparability
                     with the rest of the ledger.)
      * ``medium``: timeframe = 30d → ``fixed_lookahead_30d``.
      * ``low``:    SKIP — return ``None``. Low-materiality classifications
                     would inflate ``unparseable``-equivalent coverage
                     noise; the caller's gate is materiality >= medium.

    Idempotency: ``v1|predictions|nsa|<news_signal_id>.<ticker>``. A
    multi-ticker news signal writes ONE row per ticker, all sharing the
    same NewsSignal raw_text_ref.

    Args:
      session: live SQLAlchemy session.
      user_id: tenant id.
      news_signal_id: the NewsSignal row's primary key — the source
        of the materiality classification.
      ticker: the ticker extracted from the signal's ``parsed_tickers``.
      direction: ``"long"`` (positive sentiment + macro_shift),
        ``"short"`` (negative sentiment + macro_shift), ``"neutral"``
        (neutral or no recommended flag).
      materiality_tier: ``high`` / ``medium`` / ``low`` — caller MUST
        gate on >= medium; ``low`` returns ``None`` here as a defensive
        second gate.
      event_at: NewsSignal received_at — real-world prediction time.
      raw_text_ref: e.g. ``"news_signals.id:423"``. Citation-display only.

    Returns:
      Persisted ``Prediction`` row, OR ``None`` when materiality_tier
      is ``low``.
    """
    if materiality_tier == "low":
        # Defensive gate — caller should not invoke this writer for low
        # materiality, but if they do we no-op (avoids polluting the
        # ledger with noise that would dominate coverage stats).
        return None
    if materiality_tier == "high":
        timeframe_days = DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH
    else:  # medium
        timeframe_days = DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM

    dedup_id = _news_signal_message_id(
        news_signal_id=news_signal_id, ticker=ticker
    )
    return _insert_prediction(
        session,
        user_id,
        source="internal_news_signal_analyst",
        source_ref={"news_signal_id": int(news_signal_id), "ticker": ticker.upper()},
        message_id=dedup_id,
        ticker=ticker.upper(),
        direction=direction,
        event_at=event_at,
        timeframe_days=timeframe_days,
        raw_text_ref=raw_text_ref,
    )


# Per-position thesis action → prediction direction mapping (codex BLOCKER
# #3 anti-hide-behind-HOLD). HOLD is logged with direction='neutral' and
# scored against subsequent price action so an agent that hides behind
# HOLD verdicts gets its abstention surfaced in reliability stats.
_THESIS_ACTION_TO_DIRECTION: dict[str, Literal["long", "short", "neutral"]] = {
    "BUY": "long",
    "ADD": "long",
    "TRIM": "short",
    "SELL": "short",
    "HOLD": "neutral",
}


def write_per_position_thesis_prediction(
    session: Session,
    user_id: str,
    *,
    thesis_id: int | str,
    ticker: str,
    action: Literal["BUY", "ADD", "TRIM", "SELL", "HOLD"],
    conviction: Literal["HIGH", "MEDIUM", "LOW"],
    event_at: datetime,
    target_price: Decimal | float | None = None,
    stop_price: Decimal | float | None = None,
    provenance_weights_applied: bool = False,
) -> Prediction:
    """Write a prediction row sourced from a per-position thesis card.

    Codex BLOCKER #3 — anti-hide-behind-HOLD: HOLD verdicts ARE written
    as predictions with ``direction='neutral'``. Per spec §2.4 this
    closes the selection-bias hole — an agent that hides behind HOLD
    now gets its HOLDs scored against actual subsequent price action
    (a HOLD where the price moved >5% in either direction is recorded
    as ``expired_positive`` or ``expired_negative`` against the neutral
    call). Conviction is NOT stored on the prediction row itself —
    it's a derivative the consumer can re-derive; the prediction row
    is purely the "what's the source's directional bet?" data.

    Action → direction mapping:

      * BUY / ADD  → ``long``
      * TRIM / SELL → ``short``
      * HOLD       → ``neutral`` (logged + scored, NOT excluded)

    Idempotency: ``v1|predictions|thesis|<thesis_id>.<ticker>``.

    Args:
      session: live SQLAlchemy session.
      user_id: tenant id.
      thesis_id: the per-position thesis row's id (typically the
        synthesis draft id; commit #3 accepts any stable identifier).
      ticker: the ticker the thesis is about.
      action: ``BUY`` / ``ADD`` / ``TRIM`` / ``SELL`` / ``HOLD``.
        Unrecognised actions raise ``ValueError`` (we don't silently
        downgrade to neutral — that would hide an upstream bug).
      conviction: HIGH / MEDIUM / LOW. Currently unused on the row
        (intentionally — see docstring) but kept in the signature so
        the call-site doesn't change when commit #6 reads conviction
        from the consumer side.
      event_at: when the thesis card was emitted (typically the
        synthesis run's completion time).
      target_price / stop_price: optional; default-None per-position
        theses rarely carry explicit levels.

    Returns:
      Persisted ``Prediction`` row.
    """
    direction = _THESIS_ACTION_TO_DIRECTION.get(action)
    if direction is None:
        raise ValueError(
            f"per_position_thesis prediction got unrecognised action {action!r}; "
            f"expected one of {tuple(_THESIS_ACTION_TO_DIRECTION)}"
        )

    dedup_id = _thesis_message_id(thesis_id=thesis_id, ticker=ticker)
    # Per-position theses use the standard 30-day default. Conviction
    # informs the consumer but doesn't change the timeframe.
    _ = conviction  # noqa: F841 — kept in signature for spec contract
    return _insert_prediction(
        session,
        user_id,
        source="internal_per_position_thesis",
        source_ref={
            "thesis_id": thesis_id,
            "ticker": ticker.upper(),
            "action": action,
            "conviction": conviction,
        },
        message_id=dedup_id,
        ticker=ticker.upper(),
        direction=direction,
        event_at=event_at,
        target_price=target_price,
        stop_price=stop_price,
        timeframe_days=DEFAULT_TIMEFRAME_DAYS_THESIS,
        provenance_weights_applied=provenance_weights_applied,
    )


def write_state_observer_prediction(
    session: Session,
    user_id: str,
    *,
    observer_flag_id: int | str,
    primary_field: str,
    severity: Literal["info", "warning", "critical"],
    deviation_bucket: Literal["small", "moderate", "large", "extreme", "categorical"],
    event_at: datetime,
    provenance_weights_applied: bool = False,
) -> Prediction | None:
    """Write a prediction row sourced from a state_observer flag.

    Meta-prediction: ``ticker=None`` (state_observer flags are about
    user-state fields like ``macro.fx_usd_nis_spot`` or
    ``portfolio.top_concentration_pct`` — not about a single ticker).
    Direction = ``neutral`` (a coarse confirmation question: "did the
    subsequent state evolve in a way that confirmed the concern?").
    Evaluation method = ``fixed_lookahead_30d``.

    Gating contract: caller MUST pre-filter for actionable severity
    (>= warning). info-band flags are noise and skipped here as a
    defensive second gate (returning ``None``). Per spec §2.4 only
    actionable observer flags are scored.

    Idempotency: ``v1|predictions|so|<observer_flag_id>``.

    Args:
      session: live SQLAlchemy session.
      user_id: tenant id.
      observer_flag_id: the monitor_flags.id returned by
        ``write_observer_flags`` for this candidate.
      primary_field: the diff field path the observer flagged (stored
        in ``source_ref`` for traceability; the score itself is field-
        agnostic since direction is always ``neutral``).
      severity: ``info`` is skipped (returns None); ``warning`` and
        ``critical`` proceed to write.
      deviation_bucket: small / moderate / large / extreme / categorical.
        Stored in source_ref for the consumer-side re-render.
      event_at: when the observer fired the flag.

    Returns:
      Persisted ``Prediction`` row, OR ``None`` when severity == info.
    """
    if severity == "info":
        # Defensive gate — caller should not invoke for info-band flags,
        # but no-op here so a misrouted call doesn't pollute the ledger.
        return None

    dedup_id = _state_observer_message_id(observer_flag_id=observer_flag_id)
    return _insert_prediction(
        session,
        user_id,
        source="internal_state_observer",
        source_ref={
            "observer_flag_id": observer_flag_id,
            "primary_field": primary_field,
            "severity": severity,
            "deviation_bucket": deviation_bucket,
        },
        message_id=dedup_id,
        ticker=None,  # meta-prediction — no single ticker
        direction="neutral",
        event_at=event_at,
        timeframe_days=DEFAULT_TIMEFRAME_DAYS_OBSERVER,
        provenance_weights_applied=provenance_weights_applied,
    )


def write_monitor_flag_prediction(
    session: Session,
    user_id: str,
    *,
    monitor_flag_id: int | str,
    kind: str,
    severity: Literal["info", "warning", "critical"],
    event_at: datetime,
) -> Prediction:
    """Write a prediction row sourced from a plan_monitor MonitorFlag.

    Meta-prediction: same shape as state_observer — ``ticker=None``,
    ``direction='neutral'``, ``evaluation_method='fixed_lookahead_30d'``.

    Per spec §2.4 ``mc_regression`` flags are predictions about plan
    health (not ticker prices), scored via the ``expired_*`` path
    against a portfolio-level proxy. ``allocation_drift`` flags carry
    an implicit allocation recommendation but the writer keeps
    direction=neutral to match the ``mc_regression`` shape and let the
    consumer (commit #6) interpret direction from the underlying
    flag's payload.

    Gating contract: the caller (``check_allocation_drift`` /
    ``check_mc_regression``) has ALREADY decided the flag should fire
    before reaching this writer. No additional gate here.

    Idempotency: ``v1|predictions|mf|<monitor_flag_id>``.

    Args:
      session: live SQLAlchemy session.
      user_id: tenant id.
      monitor_flag_id: the MonitorFlag row's id (or a deterministic
        string id when the row hasn't been INSERTed yet — rare).
      kind: ``allocation_drift`` / ``mc_regression`` / ``macro_shift``.
        Stored in source_ref for the consumer-side re-render.
      severity: stored in source_ref.
      event_at: when the monitor fired the flag.

    Returns:
      Persisted ``Prediction`` row.
    """
    dedup_id = _monitor_flag_message_id(monitor_flag_id=monitor_flag_id)
    return _insert_prediction(
        session,
        user_id,
        source="internal_monitor_flags",
        source_ref={
            "monitor_flag_id": monitor_flag_id,
            "kind": kind,
            "severity": severity,
        },
        message_id=dedup_id,
        ticker=None,  # meta-prediction
        direction="neutral",
        event_at=event_at,
        timeframe_days=DEFAULT_TIMEFRAME_DAYS_MONITOR,
    )


__all__ = [
    "DEDUP_KEY_VERSION",
    "DEFAULT_TIMEFRAME_DAYS_DISCORD",
    "DEFAULT_TIMEFRAME_DAYS_MONITOR",
    "DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH",
    "DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM",
    "DEFAULT_TIMEFRAME_DAYS_OBSERVER",
    "DEFAULT_TIMEFRAME_DAYS_THESIS",
    "LONG_HORIZON_CAP_DAYS",
    "discord_message_id",
    "write_discord_prediction",
    "write_monitor_flag_prediction",
    "write_news_signal_prediction",
    "write_per_position_thesis_prediction",
    "write_state_observer_prediction",
]
