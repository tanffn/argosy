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
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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

#: Alpha-report fanout default (long-bias structural picks). The runner
#: passes per-signal timeframe values (short=7, medium=30, long=180)
#: for per-ticker signals; structural picks fall back to this
#: long-horizon default. The write_alpha_report_prediction writer caps
#: at LONG_HORIZON_CAP_DAYS via the shared method selector so the
#: evaluator's due-at fires at 30 days even when the source asserts
#: 180 days (§5.5 — the evaluator can re-fire on a fresh prediction
#: when the next analyst run re-confirms the structural pick).
DEFAULT_TIMEFRAME_DAYS_ALPHA_REPORT: int = 180


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


def _alpha_report_message_id(
    *, analysis_id: int | str, ticker: str, kind: str
) -> str:
    """``v1|predictions|discord_alpha_report|<analysis_id>.<ticker>.<kind>``.

    Per-(analysis, ticker, kind) granularity so a single AlphaReportAnalysis
    that emits BOTH a per-ticker signal AND a structural pick for the same
    ticker writes TWO distinct prediction rows (they're separate predictions
    — one is a tactical opinion, the other a structural-portfolio bet).
    ``kind`` is one of ``'signal'`` or ``'pick'`` to disambiguate.
    """
    if analysis_id is None or analysis_id == "":
        raise ValueError(
            "alpha_report prediction needs a non-empty analysis_id"
        )
    if not ticker:
        raise ValueError(
            "alpha_report prediction needs a non-empty ticker"
        )
    if kind not in ("signal", "pick"):
        raise ValueError(
            f"alpha_report prediction kind must be 'signal' or 'pick'; "
            f"got {kind!r}"
        )
    return (
        f"{DEDUP_KEY_VERSION}|predictions|discord_alpha_report|"
        f"{analysis_id}.{ticker.upper()}.{kind}"
    )


# ---------------------------------------------------------------------------
# evaluation_method + evaluation_due_at — spec §3.1 writer-side selection
# ---------------------------------------------------------------------------


def _choose_method_and_window(
    *,
    target_price: Decimal | float | None,
    stop_price: Decimal | float | None,
    direction: str,
    timeframe_days: int | None,
    preserve_long_horizon: bool = False,
) -> tuple[str, int]:
    """Return ``(evaluation_method, window_days)`` per spec §3.1.

    Selection rules (writer-side; evaluator does NOT re-derive):

      * target_price AND stop_price both set → ``target_stop``,
        window = timeframe_days (default 7 if unspecified).
      * direction='multi' → ``multi_basket_weighted``,
        window = min(timeframe_days, 30).
      * preserve_long_horizon with timeframe_days in (180, 365) →
        the matching fixed-lookahead method and exact window.
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
      preserve_long_horizon: retain supported explicit long horizons for
        recommendation clocks and Alpha-report predictions. Other sources
        keep their legacy checkpoint bucketing.

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

    if preserve_long_horizon and timeframe_days in (180, 365):
        return (f"fixed_lookahead_{timeframe_days}d", timeframe_days)

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
    preserve_long_horizon: bool = False,
    evaluation_method_override: str | None = None,
    evaluation_due_at_override: datetime | None = None,
) -> Prediction:
    """INSERT one prediction row with per-source idempotency.

    Idempotency: the partial-unique index
    ``ix_predictions_source_messageid`` on
    ``(user_id, source, message_id)`` enforces tenant-scoped dedup at
    the DB layer. On ``IntegrityError`` we rollback the failed INSERT
    and re-SELECT the existing row, returning it unchanged. The caller
    never sees a duplicate row OR an exception.

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
        preserve_long_horizon=preserve_long_horizon,
    )
    event_at_aware = _ensure_aware(event_at)
    evaluation_due_at = event_at_aware + timedelta(days=window_days)
    if evaluation_method_override is not None:
        method = evaluation_method_override
    if evaluation_due_at_override is not None:
        evaluation_due_at = _ensure_aware(evaluation_due_at_override)

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
        Prediction.user_id == user_id,
        Prediction.source == source,
        Prediction.message_id == message_id,
    )
    existing = session.execute(existing_stmt).scalar_one_or_none()
    if existing is not None:
        logger.debug(
            "predictions.writers: dedup hit for "
            "(user_id=%s, source=%s, message_id=%s) — "
            "returning existing row id=%s",
            user_id,
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
                "(user_id=%s, source=%s, message_id=%s) — "
                "returning existing row id=%s",
                user_id,
                source,
                message_id,
                existing.id,
            )
            return existing
        # Not a dedup race — re-raise the original IntegrityError so
        # the caller's outer try/except captures it.
        raise
    return row


def write_order_sheet_prediction(
    session: Session,
    user_id: str,
    *,
    fingerprint: str,
    proposal_id: int,
    ticker: str,
    action: str,
    event_at: datetime,
    due_at: datetime,
    entry_price: Decimal | float,
    expectation: str,
    success_measure: str,
    stance_source: str,
) -> Prediction:
    """Put an executable order's authored expectation on the calibration clock."""

    direction = "long" if action.upper() in ("BUY", "ADD") else "short"
    timeframe_days = max(1, (_ensure_aware(due_at) - _ensure_aware(event_at)).days)
    return _insert_prediction(
        session,
        user_id,
        source="signal_stream:order_sheet",
        source_ref={
            "order_sheet_fingerprint": fingerprint,
            "proposal_id": proposal_id,
            "action": action.upper(),
            "expectation": expectation,
            "success_measure": success_measure,
            "stance_source": stance_source,
        },
        message_id=f"v1|predictions|order_sheet|{fingerprint}.{ticker.upper()}",
        ticker=ticker.upper(),
        direction=direction,
        event_at=event_at,
        entry_price=entry_price,
        timeframe_days=timeframe_days,
        evaluation_method_override="order_sheet_due_date_v1",
        evaluation_due_at_override=due_at,
    )


def write_order_sheet_predictions(
    session: Session,
    user_id: str,
    **kwargs: Any,
) -> tuple[Prediction, Prediction, Prediction]:
    """Track a surfaced order recommendation whether or not it is accepted.

    The authored expectation keeps its exact due date. Independent six-month
    and one-year clocks make accepted, declined, expired, and ignored advice
    comparable in the same long-horizon calibration cohorts.
    """

    authored = write_order_sheet_prediction(session, user_id, **kwargs)
    event_at = kwargs["event_at"]
    fingerprint = kwargs["fingerprint"]
    ticker = str(kwargs["ticker"]).upper()
    action = str(kwargs["action"]).upper()
    direction = "long" if action in ("BUY", "ADD") else "short"
    thesis = _insert_prediction(
        session,
        user_id,
        source="signal_stream:order_sheet",
        source_ref={
            "order_sheet_fingerprint": fingerprint,
            "proposal_id": kwargs["proposal_id"],
            "action": action,
            "expectation": kwargs["expectation"],
            "success_measure": kwargs["success_measure"],
            "stance_source": kwargs["stance_source"],
            "horizon_days": 180,
        },
        message_id=(
            f"v1|predictions|order_sheet|{fingerprint}.{ticker}|180d"
        ),
        ticker=ticker,
        direction=direction,
        event_at=event_at,
        entry_price=kwargs["entry_price"],
        timeframe_days=180,
        preserve_long_horizon=True,
    )
    annual = _insert_prediction(
        session,
        user_id,
        source="signal_stream:order_sheet",
        source_ref={
            "order_sheet_fingerprint": fingerprint,
            "proposal_id": kwargs["proposal_id"],
            "action": action,
            "expectation": kwargs["expectation"],
            "success_measure": kwargs["success_measure"],
            "stance_source": kwargs["stance_source"],
            "horizon_days": 365,
        },
        message_id=(
            f"v1|predictions|order_sheet|{fingerprint}.{ticker}|365d"
        ),
        ticker=ticker,
        direction=direction,
        event_at=event_at,
        entry_price=kwargs["entry_price"],
        timeframe_days=365,
        preserve_long_horizon=True,
    )
    return authored, thesis, annual


def ensure_surfaced_order_sheet_predictions(
    session: Session,
    *,
    user_id: str | None = None,
) -> dict[str, int]:
    """Repair clocks for every persisted trade plan, including ignored plans."""

    from argosy.services.order_sheet import OrderSheet
    from argosy.services.order_sheet_materializer import order_sheet_fingerprint
    from argosy.state.models import ActionProposal

    stmt = select(ActionProposal).where(ActionProposal.kind == "allocate")
    if user_id is not None:
        stmt = stmt.where(ActionProposal.user_id == user_id)
    proposals = session.execute(stmt.order_by(ActionProposal.id)).scalars().all()
    before = len(session.execute(
        select(Prediction.id).where(
            Prediction.source == "signal_stream:order_sheet",
            *((Prediction.user_id == user_id,) if user_id is not None else ()),
        )
    ).scalars().all())
    sheets = 0
    lines = 0
    for proposal in proposals:
        try:
            payload = json.loads(proposal.suggested_payload or "{}")
            raw_sheet = payload.get("order_sheet")
            if not isinstance(raw_sheet, dict):
                continue
            sheet = OrderSheet.model_validate(raw_sheet)
            fingerprint = str(
                payload.get("order_sheet_fingerprint")
                or order_sheet_fingerprint(sheet)
            )
            sheets += 1
            for line in sheet.lines:
                write_order_sheet_predictions(
                    session,
                    proposal.user_id,
                    fingerprint=fingerprint,
                    proposal_id=int(proposal.id),
                    ticker=line.symbol,
                    action=line.action.value,
                    event_at=sheet.generated_at,
                    due_at=datetime.combine(
                        line.expectation.due_date,
                        time(23, 59, 59),
                        tzinfo=timezone.utc,
                    ),
                    entry_price=line.evidence.price_usd,
                    expectation=line.expectation.statement,
                    success_measure=line.expectation.success_measure,
                    stance_source=line.stance_source,
                )
                lines += 1
        except Exception as exc:  # noqa: BLE001 - continue repairing other plans
            logger.warning(
                "predictions.order_sheet_repair.skipped proposal=%s: %s",
                proposal.id,
                str(exc)[:160],
            )
    after = len(session.execute(
        select(Prediction.id).where(
            Prediction.source == "signal_stream:order_sheet",
            *((Prediction.user_id == user_id,) if user_id is not None else ()),
        )
    ).scalars().all())
    return {
        "sheets": sheets,
        "lines": lines,
        "predictions_created": max(0, after - before),
    }


def write_signal_stream_predictions(
    session: Session,
    user_id: str,
    *,
    stream: str,
    dedup_key: str,
    ticker: str,
    direction: Literal["long", "short"],
    event_at: datetime,
    entry_price: Decimal | float,
    evidence: dict[str, Any],
) -> tuple[Prediction, Prediction]:
    """Write the tactical and thesis-horizon rows for one nomination."""
    if entry_price is None:
        raise ValueError("signal-stream predictions require an entry price")
    source = f"signal_stream:{stream}"
    common = {
        "session": session,
        "user_id": user_id,
        "source": source,
        "source_ref": {
            "stream": stream,
            "dedup_key": dedup_key,
            "evidence": evidence,
        },
        "ticker": ticker.upper(),
        "direction": direction,
        "event_at": event_at,
        "entry_price": entry_price,
        "raw_text_ref": evidence.get("award_url"),
    }
    tactical = _insert_prediction(
        message_id=f"v1|predictions|{source}|{dedup_key}|30d",
        timeframe_days=30,
        **common,
    )
    thesis = _insert_prediction(
        message_id=f"v1|predictions|{source}|{dedup_key}|180d",
        timeframe_days=180,
        preserve_long_horizon=True,
        **common,
    )
    return tactical, thesis


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

    Gating contract: the caller (``check_mc_regression``) has ALREADY
    decided the flag should fire before reaching this writer. No additional
    gate here.

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


def write_alpha_report_prediction(
    session: Session,
    user_id: str,
    *,
    analysis_id: int | str,
    news_signal_id: int,
    ticker: str,
    direction: Literal["long", "short", "neutral"],
    kind: Literal["signal", "pick"],
    event_at: datetime,
    timeframe_days: int | None = None,
    raw_text_ref: str | None = None,
) -> Prediction:
    """Write a prediction row sourced from an AlphaReportAnalysis.

    The alpha_report_analyst fans out each TickerSignal + StructuralPick
    into its own prediction row so the ledger can score the analyst's
    per-ticker calls independently. Caller is
    :mod:`argosy.services.alpha_report_analyst_runner` which decides
    direction + timeframe per-row based on the structured output's
    sentiment / kind / timeframe enum.

    Anti-collision: ``(source='discord_alpha_report', message_id=
    v1|predictions|discord_alpha_report|<analysis_id>.<ticker>.<kind>)``.
    Re-running the runner on the same news_signal returns the existing
    AlphaReportAnalysis row → caller passes the same ``analysis_id``
    → this writer dedup-hits.

    Args:
      session: live SQLAlchemy session. Caller owns the outer transaction.
      user_id: tenant id.
      analysis_id: the AlphaReportAnalysis row's PK — the per-analysis
        dedup partition.
      news_signal_id: the underlying NewsSignal row's PK — stored in
        ``source_ref`` for traceability.
      ticker: the ticker the signal/pick is about.
      direction: ``"long"`` / ``"short"`` / ``"neutral"``. Caller's
        mapping: positive sentiment → long, negative → short, neutral
        → neutral; structural picks always long (Meet Kevin's bias).
      kind: ``"signal"`` (TickerSignal) or ``"pick"`` (StructuralPick).
        Stored on the dedup key so a ticker that appears in BOTH
        produces two rows (different prediction questions: tactical
        vs structural).
      event_at: the NewsSignal's ``received_at`` — real-world prediction
        time per spec §2.3 (analyzed_at would be 1+ hour later and
        introduce hindsight bias).
      timeframe_days: per-signal timeframe (caller maps from the
        TickerSignal.timeframe enum: short=7, medium=30, long=180,
        unspecified=30). Explicit 180/365-day horizons are preserved rather
        than replaced with the legacy 30-day checkpoint.
      raw_text_ref: pointer to ``news_signals.id:<id>`` for citation
        display. NEVER injected into LLM prompts.

    Returns:
      Persisted ``Prediction`` row (or existing row on idempotent re-run).
    """
    dedup_id = _alpha_report_message_id(
        analysis_id=analysis_id, ticker=ticker, kind=kind,
    )
    return _insert_prediction(
        session,
        user_id,
        source="discord_alpha_report",
        source_ref={
            "analysis_id": analysis_id,
            "news_signal_id": int(news_signal_id),
            "ticker": ticker.upper(),
            "kind": kind,
        },
        message_id=dedup_id,
        ticker=ticker.upper(),
        direction=direction,
        event_at=event_at,
        timeframe_days=(
            timeframe_days
            if timeframe_days is not None
            else DEFAULT_TIMEFRAME_DAYS_ALPHA_REPORT
        ),
        raw_text_ref=raw_text_ref,
        preserve_long_horizon=True,
    )


# ---------------------------------------------------------------------------
# Deep-decision verdict bridge (SEAM 2) — grade what the FLEET decided
# ---------------------------------------------------------------------------
#
# The per-ticker decision fleet settles a verdict (BUY/SELL/TRIM/HOLD) into the
# verdict registry. This bridge emits ONE prediction per settled verdict so the
# LIVE outcome evaluator scores the fleet's actual deep-decision calls — not just
# the plan-thesis cards ``emit_thesis_predictions`` already covers.
#
# Source label: we reuse the whitelisted ``signal_stream:%`` CHECK escape-hatch
# (migration 0082) so a distinct, isolated source string works on the migrated
# prod DB with NO schema change. It gets its own row in the source_reliability
# view and never collides with the real early-signal streams A-E (their consumers
# match exact stream names, not a ``signal_stream:%`` wildcard — verified). A
# first-class ``deep_decision_verdict`` source would need a one-line CHECK
# relaxation migration (mirroring 0058/0082); flagged as the clean alternative.
DEEP_DECISION_VERDICT_SOURCE: str = "signal_stream:deep_decision_verdict"
DISCOVERY_EVALUATION_SOURCE: str = "signal_stream:discovery_evaluation"


def write_discovery_evaluation_predictions(
    session: Session,
    user_id: str,
    *,
    event_key: str,
    ticker: str,
    verdict: str,
    conviction: str,
    event_at: datetime,
    entry_price: Decimal | float,
    estimator_go: bool | None,
    estimator_conviction: str | None,
    estimation: str,
    thesis: str,
    radar_rank: int | None,
    radar_score: float | None,
) -> tuple[Prediction, Prediction, Prediction]:
    """Put the actual discovery-fleet call on 30d/180d/365d clocks.

    Every fleet-graded name keeps a long price observation. ``BUY`` means the
    fleet recommended the upside; ``WATCH``/``PASS`` use the same return to
    identify a correct skip or a missed winner. A non-recommendation is never
    silently rewritten as a short thesis.
    """

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("discovery evaluation requires a ticker")
    if entry_price is None or float(entry_price) <= 0:
        raise ValueError("discovery evaluation requires a positive entry price")
    recommendation = (verdict or "").strip().upper()
    if recommendation not in {"BUY", "WATCH", "PASS"}:
        raise ValueError(f"unsupported discovery verdict: {verdict!r}")
    common_ref = {
        "kind": "discovery_evaluation",
        "verdict": recommendation,
        "conviction": (conviction or "").strip().upper() or None,
        "estimator_go": estimator_go,
        "estimator_conviction": estimator_conviction,
        "estimation": estimation,
        "expectation": thesis,
        "radar_rank": radar_rank,
        "radar_score": radar_score,
    }
    rows: list[Prediction] = []
    for horizon in (30, 180, 365):
        if recommendation == "BUY":
            success_measure = (
                f"At {horizon} days, report total price return from the dated "
                "entry. Positive is supporting evidence; +10% or more is a "
                "strong win. Negative challenges the call; -10% or worse is a "
                "strong miss. This grades price outcome, not thesis milestones."
            )
        else:
            success_measure = (
                f"At {horizon} days, report total price return from the dated "
                "entry. +10% or more is a missed opportunity; -10% or worse "
                "supports the skip. WATCH/PASS is never scored as a short. "
                "This grades price outcome, not thesis milestones."
            )
        rows.append(
            _insert_prediction(
                session,
                user_id,
                source=DISCOVERY_EVALUATION_SOURCE,
                source_ref={
                    **common_ref,
                    "horizon_days": horizon,
                    "success_measure": success_measure,
                },
                message_id=(
                    f"v1|predictions|discovery_evaluation|{event_key}|"
                    f"{symbol}|{horizon}d"
                ),
                ticker=symbol,
                direction="long",
                event_at=event_at,
                entry_price=entry_price,
                timeframe_days=horizon,
                preserve_long_horizon=horizon in (180, 365),
            )
        )
    return rows[0], rows[1], rows[2]

#: Settled-verdict → prediction direction. SELL/TRIM → ``short``: the fleet
#: expected the price it AVOIDED to fall (a down-or-flat move vindicates the
#: exit; the evaluator's ``short`` sign-flip scores it that way). HOLD/WAIT →
#: ``neutral`` (anti-hide-behind-HOLD, mirroring the thesis writer): a HOLD is a
#: no-change call scored against subsequent price drift (expired_neutral when the
#: price barely moves, expired_positive/negative otherwise).
_VERDICT_TO_DIRECTION: dict[str, Literal["long", "short", "neutral"]] = {
    "BUY": "long",
    "ADD": "long",
    "SELL": "short",
    "TRIM": "short",
    "HOLD": "neutral",
    "WAIT": "neutral",
}


def deep_decision_verdict_message_id(*, verdict_id: int | str) -> str:
    """``v1|predictions|deep_decision_verdict|<verdict_id>`` — the dedup key.

    Keyed on the registry ``verdicts.id`` so a re-emit for the same settled
    verdict dedup-hits (no duplicate row) and outcomes join back to the verdict
    via ``source_ref.verdict_id``.
    """
    if verdict_id is None or verdict_id == "":
        raise ValueError("deep_decision_verdict prediction needs a verdict_id")
    return f"{DEDUP_KEY_VERSION}|predictions|deep_decision_verdict|{verdict_id}"


def _trigger_prices(
    revisit_triggers: list[dict[str, Any]] | None, kind: str
) -> list[float]:
    """Numeric prices from typed ``price_below`` / ``price_above`` triggers."""
    out: list[float] = []
    for t in revisit_triggers or []:
        if not isinstance(t, dict) or str(t.get("kind")) != kind:
            continue
        px = t.get("price")
        if px is None:
            continue
        try:
            out.append(float(px))
        except (TypeError, ValueError):
            continue
    return out


def _derive_target_stop(
    *,
    direction: str,
    entry: float,
    revisit_triggers: list[dict[str, Any]] | None,
    verdict_stop: float | None,
) -> tuple[float | None, float | None]:
    """Map the verdict's typed price triggers to (target_price, stop_price).

    Geometry matches the evaluator's ``target_stop`` scorer:
      * long  → target ABOVE entry (nearest ``price_above``), stop BELOW entry
        (nearest ``price_below`` or the authored stop).
      * short → target BELOW entry (nearest ``price_below``), stop ABOVE entry
        (nearest ``price_above`` or the authored stop).
    Returns ``(None, None)`` unless BOTH a target and a stop resolve with the
    correct side — a partial set falls back to a direction-only (fixed_lookahead)
    prediction the evaluator can still grade by expiry sign.
    """
    aboves = _trigger_prices(revisit_triggers, "price_above")
    belows = _trigger_prices(revisit_triggers, "price_below")
    vstop = None
    if verdict_stop is not None:
        try:
            vstop = float(verdict_stop)
        except (TypeError, ValueError):
            vstop = None

    if direction == "long":
        up = [p for p in aboves if p > entry]
        down = [p for p in belows if p < entry]
        if vstop is not None and vstop < entry:
            down.append(vstop)
        target = min(up) if up else None            # nearest resistance
        stop = max(down) if down else None          # nearest support / tightest stop
    elif direction == "short":
        down = [p for p in belows if p < entry]
        up = [p for p in aboves if p > entry]
        if vstop is not None and vstop > entry:
            up.append(vstop)
        target = max(down) if down else None        # nearest downside target
        stop = min(up) if up else None              # nearest upside stop
    else:
        return (None, None)

    if target is None or stop is None:
        return (None, None)
    return (target, stop)


def write_deep_decision_verdict_prediction(
    session: Session,
    user_id: str,
    *,
    verdict_id: int | str,
    subject: str,
    verdict: str,
    event_at: datetime,
    entry_price: Decimal | float | None = None,
    stop_price: Decimal | float | None = None,
    revisit_triggers: list[dict[str, Any]] | None = None,
    timeframe_days: int | None = None,
) -> Prediction | None:
    """Emit one graded prediction for a settled deep-decision verdict (SEAM 2).

    Returns the persisted (or already-existing) row, or ``None`` when the
    verdict is not gradeable-as-a-prediction (unrecognised verdict string, or
    an empty subject).

    Shape:
      * direction from :data:`_VERDICT_TO_DIRECTION`.
      * ``target_price`` / ``stop_price`` derived from the verdict's typed
        price triggers via :func:`_derive_target_stop` — ONLY when a real
        ``entry_price`` anchors the geometry and the direction is long/short.
        Both present → the writer selects ``target_stop`` (numeric grade).
      * No numeric target (HOLD, or a verdict with only fundamental
        falsifiers) → direction-only ``fixed_lookahead_*`` — graded by the
        evaluator's ±10%/±1% expiry classification (expired_positive/negative/
        neutral), NOT a null-target unparseable row.
      * ``entry_price=None`` (common for market/long-hold BUYs + HOLDs, which
        carry no limit price) → still emitted as ``fixed_lookahead_*`` with a
        NULL entry; the daily entry-backfill re-evaluation (already wired into
        ``PredictionsEvaluatorLoop``) backfills entry from the close at
        ``event_at`` and grades it under the ``*_entry_backfilled`` method.
      * ``source_ref.verdict_id`` is the verdict↔outcome join key; dedup on
        ``verdict_id`` makes re-emit idempotent (no duplicate row).
    """
    v = (verdict or "").strip().upper()
    direction = _VERDICT_TO_DIRECTION.get(v)
    if direction is None:
        return None
    ticker = (subject or "").strip().upper()
    if not ticker:
        return None

    target_price: float | None = None
    stop_out: float | None = None
    if entry_price is not None and direction in ("long", "short"):
        try:
            entry_f = float(entry_price)
        except (TypeError, ValueError):
            entry_f = None  # type: ignore[assignment]
        if entry_f is not None and entry_f > 0:
            target_price, stop_out = _derive_target_stop(
                direction=direction,
                entry=entry_f,
                revisit_triggers=revisit_triggers,
                verdict_stop=(
                    float(stop_price) if stop_price is not None else None
                ),
            )

    return _insert_prediction(
        session,
        user_id,
        source=DEEP_DECISION_VERDICT_SOURCE,
        source_ref={
            "verdict_id": verdict_id,
            "subject": ticker,
            "verdict": v,
            "kind": "deep_decision_verdict",
        },
        message_id=deep_decision_verdict_message_id(verdict_id=verdict_id),
        ticker=ticker,
        direction=direction,
        event_at=event_at,
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_out,
        timeframe_days=(
            timeframe_days
            if timeframe_days is not None
            else DEFAULT_TIMEFRAME_DAYS_THESIS
        ),
    )


def write_deep_decision_verdict_predictions(
    session: Session,
    user_id: str,
    **kwargs: Any,
) -> tuple[Prediction, Prediction, Prediction] | tuple[()]:
    """Put a settled verdict on tactical, thesis, and annual clocks.

    The first row preserves the original 30-day contract and dedup key.  The
    next rows are independent 180-day and 365-day counterfactuals. None depends
    on whether the user accepts or executes the related proposal.
    """

    tactical = write_deep_decision_verdict_prediction(session, user_id, **kwargs)
    if tactical is None:
        return ()
    verdict_id = kwargs.get("verdict_id")
    ref = _verdict_source_ref_for_horizon(
        verdict_id=verdict_id,
        ticker=str(tactical.ticker or ""),
        verdict=str(kwargs.get("verdict") or "").strip().upper(),
        horizon_days=180,
    )
    thesis = _insert_prediction(
        session,
        user_id,
        source=DEEP_DECISION_VERDICT_SOURCE,
        source_ref=ref,
        message_id=(
            f"{deep_decision_verdict_message_id(verdict_id=verdict_id)}|180d"
        ),
        ticker=tactical.ticker,
        direction=tactical.direction,
        event_at=kwargs["event_at"],
        entry_price=kwargs.get("entry_price"),
        target_price=tactical.target_price,
        stop_price=tactical.stop_price,
        timeframe_days=180,
        preserve_long_horizon=True,
    )
    annual = _insert_prediction(
        session,
        user_id,
        source=DEEP_DECISION_VERDICT_SOURCE,
        source_ref=_verdict_source_ref_for_horizon(
            verdict_id=verdict_id,
            ticker=str(tactical.ticker or ""),
            verdict=str(kwargs.get("verdict") or "").strip().upper(),
            horizon_days=365,
        ),
        message_id=(
            f"{deep_decision_verdict_message_id(verdict_id=verdict_id)}|365d"
        ),
        ticker=tactical.ticker,
        direction=tactical.direction,
        event_at=kwargs["event_at"],
        entry_price=kwargs.get("entry_price"),
        target_price=tactical.target_price,
        stop_price=tactical.stop_price,
        timeframe_days=365,
        preserve_long_horizon=True,
    )
    return tactical, thesis, annual


def _verdict_source_ref_for_horizon(
    *, verdict_id: int | str, ticker: str, verdict: str, horizon_days: int
) -> dict[str, Any]:
    return {
        "verdict_id": verdict_id,
        "subject": ticker,
        "verdict": verdict,
        "kind": "deep_decision_verdict",
        "horizon_days": horizon_days,
    }


def ensure_deep_verdict_prediction_horizons(
    session: Session,
    *,
    user_id: str | None = None,
) -> dict[str, int]:
    """Idempotently repair the counterfactual clocks for settled verdicts.

    This is intentionally a scheduler seam, not a one-off migration: if the
    fire-on-settle bridge ever misses, the next evaluator pass repairs it.  It
    also backfills pre-bridge verdicts.  Execution/acceptance status is never a
    condition for writing any horizon.
    """

    from argosy.state.models import Verdict

    # Superseding clears `settled`; it must not erase the original call's
    # accountability. Unfinished drafts have neither marker and stay excluded.
    stmt = select(Verdict).where(
        or_(Verdict.settled.is_(True), Verdict.superseded_by.is_not(None))
    )
    if user_id is not None:
        stmt = stmt.where(Verdict.user_id == user_id)
    verdicts = session.execute(stmt.order_by(Verdict.id)).scalars().all()
    before = session.execute(
        select(Prediction.id).where(
            Prediction.source == DEEP_DECISION_VERDICT_SOURCE,
            *(
                (Prediction.user_id == user_id,)
                if user_id is not None
                else ()
            ),
        )
    ).scalars().all()
    attempted = 0
    for row in verdicts:
        verdict = (row.verdict or "").strip().upper()
        if verdict not in _VERDICT_TO_DIRECTION:
            continue
        try:
            triggers = json.loads(row.revisit_triggers_json or "[]")
        except (TypeError, ValueError):
            triggers = []
        existing = session.execute(
            select(Prediction)
            .where(
                Prediction.user_id == row.user_id,
                Prediction.source == DEEP_DECISION_VERDICT_SOURCE,
                func.json_extract(Prediction.source_ref, "$.verdict_id") == int(row.id),
            )
            .order_by(Prediction.id)
            .limit(1)
        ).scalar_one_or_none()
        entry_price = existing.entry_price if existing is not None else None
        stop_price = existing.stop_price if existing is not None else None
        write_deep_decision_verdict_predictions(
            session,
            row.user_id,
            verdict_id=row.id,
            subject=row.subject,
            verdict=verdict,
            event_at=row.created_at,
            entry_price=entry_price,
            stop_price=stop_price,
            revisit_triggers=triggers if isinstance(triggers, list) else [],
        )
        attempted += 1
    after = session.execute(
        select(Prediction.id).where(
            Prediction.source == DEEP_DECISION_VERDICT_SOURCE,
            *(
                (Prediction.user_id == user_id,)
                if user_id is not None
                else ()
            ),
        )
    ).scalars().all()
    return {
        "settled_verdicts": sum(bool(row.settled) for row in verdicts),
        "superseded_verdicts": sum(not row.settled for row in verdicts),
        "eligible_verdicts": attempted,
        "predictions_created": max(0, len(after) - len(before)),
    }


def ensure_discovery_evaluation_predictions(
    session: Session,
    *,
    user_id: str | None = None,
) -> dict[str, int]:
    """Backfill the fleet verdict, not merely its radar input, into telemetry.

    ``ScanState`` is the durable latest discovery judgment. Its quote is
    recovered from the nearest dated radar observation, so a reused grade is
    anchored near its authored time rather than silently using today's price.
    """

    from argosy.state.models import ScanState

    stmt = select(ScanState).where(
        ScanState.fleet_json.is_not(None),
        ScanState.last_fleet_at.is_not(None),
    )
    if user_id is not None:
        stmt = stmt.where(ScanState.user_id == user_id)
    scan_rows = session.execute(stmt.order_by(ScanState.updated_at)).scalars().all()

    radar_stmt = select(Prediction).where(
        Prediction.source == "signal_stream:radar_observation",
        Prediction.timeframe_days == 30,
    )
    if user_id is not None:
        radar_stmt = radar_stmt.where(Prediction.user_id == user_id)
    radar_rows = session.execute(radar_stmt).scalars().all()
    radar_by_key: dict[tuple[str, str], list[Prediction]] = {}
    for prediction in radar_rows:
        if prediction.ticker:
            radar_by_key.setdefault(
                (prediction.user_id, prediction.ticker.upper()), []
            ).append(prediction)

    before = session.execute(
        select(Prediction.id).where(
            Prediction.source == DISCOVERY_EVALUATION_SOURCE,
            *((Prediction.user_id == user_id,) if user_id is not None else ()),
        )
    ).scalars().all()
    eligible = 0
    skipped_no_price = 0
    for scan in scan_rows:
        try:
            fleet = json.loads(scan.fleet_json or "{}")
            estimator = json.loads(scan.estimator_json or "{}")
        except (TypeError, ValueError):
            continue
        verdict = str(fleet.get("verdict") or "").upper()
        if verdict not in {"BUY", "WATCH", "PASS"}:
            continue
        fleet_at = _ensure_aware(scan.last_fleet_at)
        candidates = radar_by_key.get((scan.user_id, scan.ticker.upper()), [])
        priced = [row for row in candidates if row.entry_price is not None]
        if not priced:
            skipped_no_price += 1
            continue
        nearest = min(
            priced,
            key=lambda row: abs(
                (_ensure_aware(row.event_at) - fleet_at).total_seconds()
            ),
        )
        quote_distance = abs(_ensure_aware(nearest.event_at) - fleet_at)
        if quote_distance > timedelta(days=3):
            skipped_no_price += 1
            continue
        expectation = str(fleet.get("thesis_md") or "").strip()
        if len(expectation) > 2_000:
            expectation = expectation[:1_997].rstrip() + "..."
        write_discovery_evaluation_predictions(
            session,
            scan.user_id,
            event_key=(
                f"{fleet_at.isoformat()}|{scan.radar_fingerprint}|{verdict}"
            ),
            ticker=scan.ticker,
            verdict=verdict,
            conviction=str(fleet.get("conviction") or ""),
            event_at=fleet_at,
            entry_price=nearest.entry_price,
            estimator_go=estimator.get("go"),
            estimator_conviction=estimator.get("conviction"),
            estimation=str(estimator.get("one_line") or ""),
            thesis=expectation,
            radar_rank=scan.rank,
            radar_score=scan.last_score,
        )
        eligible += 1

    after = session.execute(
        select(Prediction.id).where(
            Prediction.source == DISCOVERY_EVALUATION_SOURCE,
            *((Prediction.user_id == user_id,) if user_id is not None else ()),
        )
    ).scalars().all()
    return {
        "fleet_evaluations": len(scan_rows),
        "eligible_evaluations": eligible,
        "skipped_no_price": skipped_no_price,
        "predictions_created": max(0, len(after) - len(before)),
    }


def emit_verdict_prediction_best_effort(
    *,
    user_id: str,
    verdict_id: int | str,
    subject: str,
    verdict: str,
    event_at: datetime,
    entry_price: Decimal | float | None = None,
    stop_price: Decimal | float | None = None,
    revisit_triggers: list[dict[str, Any]] | None = None,
    timeframe_days: int | None = None,
    session_factory: Any = None,
) -> Prediction | None:
    """Fire-on-settle bridge wrapper (SEAM 2). Opens its OWN isolated session,
    emits one prediction deduped on ``verdict_id``, commits, swallows ALL
    failures. Never raises — a bridge error must not break the settle path.

    ``session_factory`` is injectable for tests; production derives a sync
    sessionmaker from the live engine (mirrors
    ``spine.fleet_recording.record_fleet_decision_best_effort``).
    """
    session = None
    engine = None
    try:
        if session_factory is None:
            import sqlalchemy as sa

            from argosy.state import db as db_mod

            url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
            engine = sa.create_engine(
                url, connect_args={"check_same_thread": False}
            )
            session_factory = sessionmaker(
                bind=engine, expire_on_commit=False
            )
        session = session_factory()
        rows = write_deep_decision_verdict_predictions(
            session,
            user_id,
            verdict_id=verdict_id,
            subject=subject,
            verdict=verdict,
            event_at=event_at,
            entry_price=entry_price,
            stop_price=stop_price,
            revisit_triggers=revisit_triggers,
            timeframe_days=timeframe_days,
        )
        session.commit()
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 — bridge must NEVER break the flow
        logger.warning(
            "predictions.verdict_bridge.emit_failed: %s", str(exc)[:200]
        )
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        return None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        if engine is not None:
            try:
                engine.dispose()  # per-call engine — dispose to avoid a leak
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "DEDUP_KEY_VERSION",
    "DEEP_DECISION_VERDICT_SOURCE",
    "DISCOVERY_EVALUATION_SOURCE",
    "deep_decision_verdict_message_id",
    "emit_verdict_prediction_best_effort",
    "ensure_discovery_evaluation_predictions",
    "ensure_deep_verdict_prediction_horizons",
    "ensure_surfaced_order_sheet_predictions",
    "write_deep_decision_verdict_prediction",
    "write_deep_decision_verdict_predictions",
    "write_discovery_evaluation_predictions",
    "DEFAULT_TIMEFRAME_DAYS_ALPHA_REPORT",
    "DEFAULT_TIMEFRAME_DAYS_DISCORD",
    "DEFAULT_TIMEFRAME_DAYS_MONITOR",
    "DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH",
    "DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM",
    "DEFAULT_TIMEFRAME_DAYS_OBSERVER",
    "DEFAULT_TIMEFRAME_DAYS_THESIS",
    "LONG_HORIZON_CAP_DAYS",
    "discord_message_id",
    "write_alpha_report_prediction",
    "write_discord_prediction",
    "write_monitor_flag_prediction",
    "write_news_signal_prediction",
    "write_per_position_thesis_prediction",
    "write_order_sheet_prediction",
    "write_order_sheet_predictions",
    "write_state_observer_prediction",
    "write_signal_stream_predictions",
]
