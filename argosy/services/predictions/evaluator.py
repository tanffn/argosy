"""Outcome evaluator service — spec C commit #4.

See ``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``:

* §3.1   — due-selection query (keys off ``evaluation_due_at`` — codex
  BLOCKER 2 fix — NOT raw ``timeframe_days``).
* §3.3   — edge cases (delisted ticker, weekend gaps, gap-through
  open price, same-bar target+stop, missing constituents).
* §3.4   — replay strategy. The ``(prediction_id, evaluation_method)``
  UNIQUE index on ``prediction_outcomes`` is the idempotency contract;
  re-running the same method over the same prediction is a no-op.
* §5.1   — ``target_stop`` scoring: bar-by-bar walk; gap-through detection
  picks ``open`` on a favorable gap (target) or adverse gap (stop).
* §5.2   — ``fixed_lookahead_7d`` / ``fixed_lookahead_30d`` scoring: sign
  + magnitude classification with ±10% hit thresholds and ±1% neutral
  band (tunable per method-version per §5.6).
* §5.3   — same-bar target+stop **always-adverse-first** rule (codex
  IMPORTANT 1 — distance-invariant, deterministic, symmetric across
  long/short).
* §5.5   — 30d cap on ``fixed_lookahead_30d``; long-horizon sources
  (13F @ 90d) are STILL scored at 30d.

Design points worth flagging for codex review:

* **Determinism.** Every code path is pure: same ``(Prediction, bars)``
  → same ``Outcome`` row across replays. The only inputs are columns on
  the prediction row + the price-bar series from the adapter. No clock
  reads inside the scoring functions; no LLM calls; no agent calls.

* **Idempotency.** Re-running ``evaluate_prediction`` returns the
  existing :class:`PredictionOutcome` without inserting a new row. The
  evaluator queries for an existing ``(prediction_id, evaluation_method)``
  outcome FIRST; only if none exists does it compute + INSERT. The
  UNIQUE index is the second line of defence — a concurrent inserter
  would surface as ``IntegrityError`` which we map to "already scored".

* **Hindsight-bias.** The price adapter is consulted ONLY for the bar
  range ``[event_at + 1 trading day, evaluation_due_at]``. We never
  read bars dated after ``evaluation_due_at`` — the spec §5.5 30d cap
  is enforced via the stored ``evaluation_due_at`` column (codex
  BLOCKER 2 fix). The entry_price is read from the prediction row
  (writer-side snapshot at event_at per §2.3) — the evaluator never
  re-snapshots entry.

* **Adapter resilience.** Three terminal cases mapped to
  ``outcome_kind='unparseable'`` per §3.3:

    1. ``ticker IS NULL`` AND not a multi-basket → "no ticker to score".
    2. The injected price-fetcher returned ``None`` (e.g. ticker
       unknown to the provider, no historical coverage).
    3. The injected price-fetcher returned an empty list (delisted /
       no bars in window).

  Transient adapter exceptions (rate-limit, network) propagate as
  ``EvaluatorAdapterError`` from ``evaluate_prediction`` and the batch
  driver SKIPS the prediction (no row inserted, retried tomorrow). This
  matches the §3.3 "transient adapter error → no outcome" row.

* **Sync session contract.** Mirrors ``state_observer_loop`` / the rest
  of the predictions-ledger sub-package: callers pass a sync
  :class:`sqlalchemy.orm.Session`. The evaluator does not open or commit
  sessions; the caller (the loop or a test) owns the transaction
  boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import Prediction, PredictionOutcome

_log = get_logger("argosy.services.predictions.evaluator")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


#: Six outcome_kind values per spec §1.3 + Appendix A.
OutcomeKind = Literal[
    "hit_target",
    "hit_stop",
    "expired_neutral",
    "expired_positive",
    "expired_negative",
    "unparseable",
]


@dataclass(frozen=True)
class Bar:
    """A single daily OHLC bar.

    Bars are the unit the scoring functions consume. The adapter
    layer (``YFinanceAdapter.get_eod_prices``) returns dicts with
    ``Date / Open / High / Low / Close / Volume`` keys — the
    default price fetcher in this module normalises those to
    :class:`Bar` instances (sorted ascending by ``bar_date``).

    ``bar_date`` is a :class:`date`, NOT a :class:`datetime`; daily-
    bar granularity is the contract today. Future intraday work
    would replace this with a (date, intraday_index) shape.
    """

    bar_date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Outcome:
    """In-memory result of a scoring-function call.

    Persisted to ``prediction_outcomes`` by :func:`evaluate_prediction`.
    The ORM row carries the same fields plus the FK ``prediction_id``
    and the audit ``evaluated_at`` timestamp (server-default).
    """

    kind: OutcomeKind
    pnl_pct: float | None = None
    entry_price_used: float | None = None
    exit_price_used: float | None = None
    exit_trigger_date: date | None = None
    notes: str | None = None
    evidence: dict[str, Any] | None = None


@dataclass
class EvaluatorSummary:
    """Per-batch totals returned by :func:`run_evaluator_batch`.

    Used by the loop's ``tick()`` to surface counts in the
    ``job_runs.output_summary`` column (spec A commit #7's
    widened ``dict | None`` return contract on :class:`CadenceLoop`).
    """

    evaluated: int = 0
    skipped_existing: int = 0
    unparseable: int = 0
    adapter_errors: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "skipped_existing": self.skipped_existing,
            "unparseable": self.unparseable,
            "adapter_errors": self.adapter_errors,
            "by_kind": dict(self.by_kind),
        }


class EvaluatorAdapterError(RuntimeError):
    """Raised when the price-fetcher fails with a transient error.

    Distinct from "no data for ticker" (which becomes
    ``outcome_kind='unparseable'``). Caller — :func:`run_evaluator_batch`
    — catches this and SKIPS the prediction (counts toward
    ``adapter_errors`` in the summary; the daily cron retries
    tomorrow). Persistent adapter failures will look like ever-growing
    "due" backlogs in the next-due query.
    """


#: Price-fetcher contract. Returns ``None`` on "ticker not found / no
#: historical coverage" — that becomes ``outcome_kind='unparseable'``.
#: Returns empty list on "ticker known but no bars in window" — also
#: unparseable. Raises :class:`EvaluatorAdapterError` (or any other
#: exception) on transient errors — caller skips the prediction.
PriceFetcher = Callable[[str, date, date], "list[Bar] | None"]


# ---------------------------------------------------------------------------
# Scoring functions — pure; deterministic; no I/O
# ---------------------------------------------------------------------------


# Spec §5.2 — fixed-lookahead thresholds. Tunable per method-version
# (changing them creates a new ``evaluation_method`` row in the registry
# per §5.6, not an in-place edit here).
_HIT_THRESHOLD_PCT: float = 0.10
_NEUTRAL_BAND_PCT: float = 0.01


def _signed_pnl(direction: str, raw_return: float) -> float:
    """Sign-flip for ``short`` predictions.

    A short prediction profits on a negative price move; the rest of
    the scoring math wants positive = "the prediction was right". This
    is the single place the sign flip lives.
    """
    if direction == "short":
        return -raw_return
    return raw_return


def _classify_lookahead(signed_pnl: float) -> OutcomeKind:
    """Spec §5.2 — sign + magnitude classification.

    * ``|signed_pnl| >= 10%`` → hit_target (positive) / hit_stop (negative)
    * ``|signed_pnl| < 1%``  → expired_neutral (a wash)
    * else                     → expired_positive / expired_negative

    The thresholds are locked per method-version per §5.6. A future
    ``fixed_lookahead_30d_v2`` may pick different numbers; this
    function inlines the v1 values.
    """
    if signed_pnl >= _HIT_THRESHOLD_PCT:
        return "hit_target"
    if signed_pnl <= -_HIT_THRESHOLD_PCT:
        return "hit_stop"
    if abs(signed_pnl) < _NEUTRAL_BAND_PCT:
        return "expired_neutral"
    if signed_pnl > 0:
        return "expired_positive"
    return "expired_negative"


def _score_target_stop(
    prediction: Prediction, bars: Sequence[Bar]
) -> Outcome:
    """Spec §5.1 — explicit target+stop bar-walk.

    Empty bars → unparseable.

    For each bar in chronological order:

    1. Detect whether this bar's ``[low, high]`` interval touched the
       target and/or the stop, separately for long vs short.
    2. **Same-bar BOTH touched** → codex IMPORTANT 1's
       always-adverse-first rule fires: outcome = ``hit_stop`` (long
       exits at ``stop`` price; short exits at ``stop`` price). The
       distance from entry to target vs entry to stop is IRRELEVANT;
       cross-source comparability requires this be distance-invariant.
       Worked example in Appendix B Example 3 of the spec.
    3. **Target only** → gap-through detection: if the bar's ``open``
       was already favorable past the target (long: open >= target;
       short: open <= target), exit at the open (favorable gap).
       Otherwise exit AT the target level (touched intra-bar).
    4. **Stop only** → gap-through detection: if the bar's ``open``
       was already adverse past the stop (long: open <= stop; short:
       open >= stop), exit at the open (adverse gap — hurts MORE than
       the stop level assumed). Otherwise exit AT the stop level.

    If neither target nor stop is hit across the entire window:
    classify end-of-window by sign per :func:`_classify_lookahead`'s
    ``expired_*`` branches (the +/-10% bands DON'T fire here — those
    are for ``fixed_lookahead`` only; ``target_stop`` predictions
    have explicit levels and a wash end-of-window is reported as
    ``expired_*`` rather than re-tripping the same magnitude logic).
    """
    if not bars:
        return Outcome(kind="unparseable", notes="no price data")

    entry = float(prediction.entry_price) if prediction.entry_price is not None else None
    target = float(prediction.target_price) if prediction.target_price is not None else None
    stop = float(prediction.stop_price) if prediction.stop_price is not None else None
    direction = prediction.direction

    if entry is None or target is None or stop is None:
        # The writer should never have selected target_stop with NULL
        # levels; defend anyway. Surface as unparseable so the row is
        # excluded from reliability stats.
        return Outcome(
            kind="unparseable",
            notes="target_stop chosen but entry/target/stop missing",
        )

    for bar in bars:
        # Side-specific touched detection.
        if direction == "long":
            target_touched = bar.high >= target
            stop_touched = bar.low <= stop
        elif direction == "short":
            # For shorts: target is BELOW entry (price falls), stop is
            # ABOVE entry (price rises against us).
            target_touched = bar.low <= target
            stop_touched = bar.high >= stop
        else:
            # 'neutral' / 'multi' should never pick target_stop — defend.
            return Outcome(
                kind="unparseable",
                notes=f"target_stop unsupported for direction={direction!r}",
            )

        if target_touched and stop_touched:
            # Codex IMPORTANT 1 — always adverse-first. Exit AT the
            # stop level (NOT at gap-down open, because we don't know
            # which extreme came first; the conservative choice is to
            # assume the stop was hit at the level). This is also
            # distance-invariant — a wide-stop setup that swept both
            # extremes still resolves as hit_stop at the stop level,
            # never punished by an arbitrary distance heuristic.
            pnl = _signed_pnl(direction, (stop - entry) / entry)
            return Outcome(
                kind="hit_stop",
                pnl_pct=pnl,
                entry_price_used=entry,
                exit_price_used=stop,
                exit_trigger_date=bar.bar_date,
                notes="both target and stop touched same bar (adverse-first)",
                evidence={"trigger_bar": _bar_to_evidence(bar)},
            )

        if target_touched:
            # Gap-through detection — favorable open beyond target.
            if (
                (direction == "long" and bar.open >= target)
                or (direction == "short" and bar.open <= target)
            ):
                exit_price = bar.open
            else:
                exit_price = target
            pnl = _signed_pnl(direction, (exit_price - entry) / entry)
            return Outcome(
                kind="hit_target",
                pnl_pct=pnl,
                entry_price_used=entry,
                exit_price_used=exit_price,
                exit_trigger_date=bar.bar_date,
                evidence={"trigger_bar": _bar_to_evidence(bar)},
            )

        if stop_touched:
            # Gap-through detection — adverse open beyond stop (hurts
            # MORE than the stop level assumed; see §3.3 + Appendix B
            # Example 2).
            if (
                (direction == "long" and bar.open <= stop)
                or (direction == "short" and bar.open >= stop)
            ):
                exit_price = bar.open
            else:
                exit_price = stop
            pnl = _signed_pnl(direction, (exit_price - entry) / entry)
            return Outcome(
                kind="hit_stop",
                pnl_pct=pnl,
                entry_price_used=entry,
                exit_price_used=exit_price,
                exit_trigger_date=bar.bar_date,
                evidence={"trigger_bar": _bar_to_evidence(bar)},
            )

    # Neither hit by end-of-window — classify by signed end-of-window
    # close (same kind enum, but the +/-10% bands DON'T fire here:
    # explicit target_stop predictions had their levels checked above).
    last_close = bars[-1].close
    raw_return = (last_close - entry) / entry
    signed = _signed_pnl(direction, raw_return)
    if abs(signed) < _NEUTRAL_BAND_PCT:
        kind: OutcomeKind = "expired_neutral"
    elif signed > 0:
        kind = "expired_positive"
    else:
        kind = "expired_negative"
    return Outcome(
        kind=kind,
        pnl_pct=signed,
        entry_price_used=entry,
        exit_price_used=last_close,
        exit_trigger_date=bars[-1].bar_date,
        evidence={
            "first_bar": _bar_to_evidence(bars[0]),
            "last_bar": _bar_to_evidence(bars[-1]),
        },
    )


def _score_fixed_lookahead(
    prediction: Prediction,
    bars: Sequence[Bar],
    window_days: int,  # noqa: ARG001 - reserved for v2 method registration
) -> Outcome:
    """Spec §5.2 — sign + magnitude at end-of-window classification.

    The ``window_days`` argument is descriptive only; the caller has
    already trimmed ``bars`` to the window. We classify by the
    end-of-window close — ``bars[-1].close`` — relative to
    ``prediction.entry_price``.

    Empty bars → unparseable (delisted / no coverage).
    Missing entry_price → unparseable (writer should not have
    selected this method without an entry; defend).
    """
    if not bars:
        return Outcome(kind="unparseable", notes="no price data")

    if prediction.entry_price is None:
        return Outcome(
            kind="unparseable",
            notes="fixed_lookahead chosen but entry_price missing",
        )

    entry = float(prediction.entry_price)
    last_close = bars[-1].close
    raw_return = (last_close - entry) / entry
    signed = _signed_pnl(prediction.direction, raw_return)
    kind = _classify_lookahead(signed)
    return Outcome(
        kind=kind,
        pnl_pct=signed,
        entry_price_used=entry,
        exit_price_used=last_close,
        exit_trigger_date=bars[-1].bar_date,
        evidence={
            "first_bar": _bar_to_evidence(bars[0]),
            "last_bar": _bar_to_evidence(bars[-1]),
        },
    )


def _bar_to_evidence(bar: Bar) -> dict[str, Any]:
    """Serialise a :class:`Bar` for ``prediction_outcomes.evidence_json``.

    Bounded per §1.3 — we never store the FULL daily-bar series. The
    scoring functions populate either ``trigger_bar`` (target_stop hit)
    or ``first_bar`` + ``last_bar`` (lookahead / expired) so the row
    is ~hundreds of bytes regardless of window length.
    """
    return {
        "date": bar.bar_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
    }


# ---------------------------------------------------------------------------
# Default price fetcher — wraps YFinanceAdapter
# ---------------------------------------------------------------------------


def default_price_fetcher(
    ticker: str, start: date, end: date
) -> list[Bar] | None:
    """Production price-fetcher — wraps
    :class:`argosy.adapters.data.yfinance_adapter.YFinanceAdapter`.

    Returns ``None`` when the adapter raises ``MissingDataSourceError``
    (ticker unknown / no coverage / package not installed in this env);
    returns ``[]`` when the adapter returns an empty bar list (covered
    ticker but no bars in window — usually delisted within the
    window). Both shapes map to ``outcome_kind='unparseable'`` at the
    evaluate_prediction layer.

    Transient errors (network, rate-limit) propagate as
    :class:`EvaluatorAdapterError` so the batch driver can SKIP the
    prediction without burning the next-day retry opportunity.

    Lazy import: this module is imported by the orchestrator loop at
    application startup, but the yfinance package is a heavy
    transitive dep we don't want to pay for in tests that inject a
    fake price-fetcher (the common case for ``test_predictions_evaluator``).
    """
    try:
        import asyncio

        from argosy.adapters import MissingDataSourceError
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
    except ImportError as exc:  # pragma: no cover - heavy deps
        raise EvaluatorAdapterError(
            f"yfinance import failed: {exc}"
        ) from exc

    adapter = YFinanceAdapter()
    try:
        # The adapter's get_eod_prices is async and uses the cached
        # call path; run it on a private loop so we don't depend on
        # the caller's event-loop state.
        payload = asyncio.run(
            adapter.get_eod_prices([ticker], start, end)
        )
    except MissingDataSourceError:
        return None
    except Exception as exc:  # pragma: no cover - transient
        raise EvaluatorAdapterError(
            f"yfinance fetch failed for {ticker} "
            f"[{start.isoformat()}, {end.isoformat()}]: {exc}"
        ) from exc

    rows = payload.get(ticker) or []
    if not rows:
        return []
    return _normalize_rows(rows)


def _normalize_rows(rows: Iterable[dict[str, Any]]) -> list[Bar]:
    """Convert ``YFinanceAdapter.get_eod_prices`` dict-rows to ``Bar``s.

    Defensive against missing/None fields — silently skips rows that
    don't carry an OHLC quadruple; sorts ascending by date so the
    scoring functions can walk in chronological order.
    """
    out: list[Bar] = []
    for row in rows:
        date_str = row.get("Date") or row.get("date")
        if not date_str:
            continue
        try:
            # Truncate ISO-with-time strings to the date part.
            bar_date = date.fromisoformat(str(date_str)[:10])
        except ValueError:
            continue
        try:
            o = float(row.get("Open") or row.get("open") or 0)
            h = float(row.get("High") or row.get("high") or 0)
            lo = float(row.get("Low") or row.get("low") or 0)
            c = float(row.get("Close") or row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if not (o and h and lo and c):
            # Adapter returned a zero-row (no data this day) — skip.
            continue
        out.append(Bar(bar_date=bar_date, open=o, high=h, low=lo, close=c))
    out.sort(key=lambda b: b.bar_date)
    return out


# ---------------------------------------------------------------------------
# Due-selection + per-prediction evaluation
# ---------------------------------------------------------------------------


def find_due_predictions(
    session: Session,
    *,
    now: datetime | None = None,
    batch_size: int = 200,
) -> list[Prediction]:
    """Spec §3.1 step 1 — predictions due for scoring.

    SELECT predictions
    WHERE evaluation_due_at <= now
      AND archived = 0
      AND NOT EXISTS (
        SELECT 1 FROM prediction_outcomes o
        WHERE o.prediction_id = predictions.id
          AND o.evaluation_method = predictions.evaluation_method
      )
    ORDER BY evaluation_due_at ASC
    LIMIT batch_size.

    The "no existing outcome for the active method" filter is what
    makes re-runs idempotent + makes replay-into-new-method
    re-pick-up the row (a new method version has a different
    ``evaluation_method`` so the EXISTS predicate fails for the new
    method-name even though a row exists for the OLD method-name —
    that's the §3.4 replay contract).

    Cursor on ``evaluation_due_at ASC`` so the longest-overdue
    predictions get scored first; this matters if the cron has been
    paused for a few days and a backlog has built up.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Sub-select instead of OUTER JOIN — SQLite handles this fine and
    # the resulting plan uses the (prediction_id, evaluation_method)
    # UNIQUE index on prediction_outcomes for the EXISTS probe.
    outcome_exists = (
        select(PredictionOutcome.id)
        .where(PredictionOutcome.prediction_id == Prediction.id)
        .where(
            PredictionOutcome.evaluation_method
            == Prediction.evaluation_method
        )
        .exists()
    )

    stmt = (
        select(Prediction)
        .where(Prediction.evaluation_due_at <= now)
        .where(Prediction.archived == 0)
        .where(~outcome_exists)
        .order_by(Prediction.evaluation_due_at.asc(), Prediction.id.asc())
        .limit(batch_size)
    )

    return list(session.execute(stmt).scalars().all())


def evaluate_prediction(
    session: Session,
    prediction: Prediction,
    *,
    now: datetime | None = None,  # noqa: ARG001 - reserved for §3.3 split-detection
    price_fetcher: PriceFetcher = default_price_fetcher,
) -> PredictionOutcome:
    """Score a single prediction; INSERT (or return existing) outcome row.

    Idempotency contract (§3.4):

    1. Check for an existing outcome row under the SAME
       ``evaluation_method`` — if found, return it WITHOUT touching
       the database. This is the cheap-path; the DB UNIQUE index is
       only the second line of defence.
    2. Otherwise compute the outcome via the per-method scoring
       function (§5.1 / §5.2 / §5.4) and INSERT a new row.
    3. If a concurrent inserter wins the race (rare today but the
       UNIQUE index makes it impossible to double-insert),
       :class:`IntegrityError` is caught and the existing row is
       re-fetched + returned.

    The function does NOT commit. Callers (the loop / a test) own the
    transaction boundary so per-batch error handling can decide
    whether to commit after each prediction (production) or once at
    end (tests).

    Transient adapter errors propagate as
    :class:`EvaluatorAdapterError`. The batch driver catches; tests
    use an injected fetcher that never raises.
    """
    method = prediction.evaluation_method

    # Cheap-path idempotency — query for an existing outcome BEFORE
    # any adapter work. The unique index would also catch a duplicate
    # INSERT but skipping the price-fetcher call entirely is the win.
    existing = (
        session.execute(
            select(PredictionOutcome)
            .where(PredictionOutcome.prediction_id == prediction.id)
            .where(PredictionOutcome.evaluation_method == method)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    outcome = _compute_outcome(prediction, price_fetcher=price_fetcher)

    row = PredictionOutcome(
        prediction_id=prediction.id,
        evaluation_method=method,
        outcome_kind=outcome.kind,
        pnl_pct=(
            Decimal(str(round(outcome.pnl_pct, 6)))
            if outcome.pnl_pct is not None
            else None
        ),
        entry_price_used=(
            Decimal(str(outcome.entry_price_used))
            if outcome.entry_price_used is not None
            else None
        ),
        exit_price_used=(
            Decimal(str(outcome.exit_price_used))
            if outcome.exit_price_used is not None
            else None
        ),
        exit_trigger_date=outcome.exit_trigger_date,
        evidence_json=(
            json.dumps(outcome.evidence, sort_keys=True)
            if outcome.evidence
            else None
        ),
        notes=outcome.notes,
    )
    # Codex BLOCKER (single-dispatch review 2026-05-29) — wrap the
    # INSERT in a SAVEPOINT so an IntegrityError on the (prediction_id,
    # evaluation_method) UNIQUE race rolls back ONLY the failed insert,
    # NOT the outer batch transaction. Without the nested-tx the batch
    # driver's mid-tick race would silently erase every successful
    # outcome row earlier in the same tick.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # Concurrent inserter beat us; the SAVEPOINT rollback already
        # undid the failed add/flush. The outer transaction is intact.
        winner = (
            session.execute(
                select(PredictionOutcome)
                .where(PredictionOutcome.prediction_id == prediction.id)
                .where(PredictionOutcome.evaluation_method == method)
            )
            .scalars()
            .one()
        )
        return winner
    return row


def _compute_outcome(
    prediction: Prediction,
    *,
    price_fetcher: PriceFetcher,
) -> Outcome:
    """Dispatch on ``evaluation_method`` → bars → scoring function.

    Centralised so :func:`evaluate_prediction` can stay focused on the
    DB-side idempotency contract.

    Window resolution per §3.1 / §2.3:

    * Start = ``event_at + 1 calendar day`` (the scoring functions skip
      weekend/holiday gaps naturally since the bar series only contains
      trading days).
    * End = ``evaluation_due_at`` (the writer pre-computed this as
      ``event_at + chosen_window_days`` per §3.1 — codex BLOCKER 2
      fix — so the §5.5 30d cap is already baked in).
    """
    method = prediction.evaluation_method

    if method == "unparseable":
        # Writer flagged the input as structurally unscoreable; persist
        # the method-of-record row so the source's coverage stat is
        # complete but exclude from hit-rate aggregation.
        return Outcome(
            kind="unparseable",
            notes=prediction.unparseable_reason
            or "writer flagged unparseable",
        )

    if method == "multi_basket_weighted":
        # Multi-basket scoring is implemented in commit #5/#6's
        # reliability layer; v1 of the evaluator surfaces it as
        # unparseable so the row stays in the coverage denominator
        # without inflating reliability with a half-baked scorer.
        return Outcome(
            kind="unparseable",
            notes="multi_basket_weighted scoring not yet implemented",
        )

    # Both target_stop AND fixed_lookahead_* need bars in
    # [event_at + 1 day, evaluation_due_at].
    if prediction.ticker is None:
        return Outcome(
            kind="unparseable",
            notes="ticker is NULL for single-ticker method",
        )

    start = _next_day(prediction.event_at)
    end = _to_date(prediction.evaluation_due_at)

    try:
        raw_bars = price_fetcher(prediction.ticker, start, end)
    except EvaluatorAdapterError:
        # Bubble up so the batch driver counts toward adapter_errors;
        # no outcome row is inserted in this run.
        raise

    if raw_bars is None:
        return Outcome(
            kind="unparseable",
            notes=f"no price coverage for {prediction.ticker}",
        )

    # The fetcher returns Bar objects already normalised; in case a
    # test passes raw dicts, normalise here as a defensive fallback.
    bars: list[Bar]
    if raw_bars and isinstance(raw_bars[0], Bar):
        bars = list(raw_bars)
    else:
        bars = _normalize_rows(raw_bars)  # type: ignore[arg-type]

    # Codex IMPORTANT (single-dispatch review 2026-05-29) — harden
    # determinism. The default fetcher's ``_normalize_rows`` already
    # sorts ascending by ``bar_date``; but a custom fetcher (or a
    # test that injects pre-built ``Bar`` instances) may not. Re-sort
    # here so the scoring functions always see chronological order
    # regardless of fetcher discipline.
    bars.sort(key=lambda b: b.bar_date)

    # Trim to window inclusive — adapter MAY return a slightly wider
    # slice (cache reuse across overlapping requests).
    bars = [b for b in bars if start <= b.bar_date <= end]

    if not bars:
        return Outcome(
            kind="unparseable",
            notes=f"no bars for {prediction.ticker} in [{start}, {end}]",
        )

    if method == "target_stop":
        return _score_target_stop(prediction, bars)
    if method in ("fixed_lookahead_7d", "fixed_lookahead_30d"):
        return _score_fixed_lookahead(
            prediction,
            bars,
            window_days=7 if method == "fixed_lookahead_7d" else 30,
        )

    # Unknown method — registry should have rejected at write time.
    return Outcome(
        kind="unparseable",
        notes=f"unknown evaluation_method={method!r}",
    )


def _next_day(dt: datetime) -> date:
    """``event_at + 1 calendar day`` → date.

    The +1 day is per §3.1's "[event_at + 1 trading day, ...]" window;
    weekend / holiday non-trading-days are skipped naturally by the
    daily-bar series (the adapter only returns trading-day bars), so
    a calendar +1 here is sufficient.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=1)).date()


def _to_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date()


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------


def run_evaluator_batch(
    session: Session,
    *,
    now: datetime | None = None,
    batch_size: int = 200,
    price_fetcher: PriceFetcher = default_price_fetcher,
) -> EvaluatorSummary:
    """Spec §3.1 — full batch: find due → score each → return summary.

    Per-prediction adapter exceptions are isolated: a single ticker
    that the price-fetcher can't reach DOES NOT crash the batch. The
    summary surfaces ``adapter_errors`` as the count of skipped
    predictions so the cron's next-day retry naturally drains the
    backlog once the upstream comes back.

    The caller commits the session. We use one transaction for the
    full batch so a mid-batch crash rolls back partial outcome rows
    (the surviving DUE rows will be re-picked on the next tick — the
    idempotency contract makes this safe).

    Returns :class:`EvaluatorSummary` — converted to the
    ``output_summary`` dict at the loop's tick boundary.
    """
    summary = EvaluatorSummary()
    if now is None:
        now = datetime.now(timezone.utc)

    due = find_due_predictions(
        session, now=now, batch_size=batch_size
    )
    _log.info(
        "predictions.evaluator.batch.start",
        due_count=len(due),
        now=now.isoformat(),
    )

    for prediction in due:
        try:
            outcome = evaluate_prediction(
                session, prediction, price_fetcher=price_fetcher
            )
        except EvaluatorAdapterError as exc:
            summary.adapter_errors += 1
            _log.warning(
                "predictions.evaluator.adapter_error",
                prediction_id=prediction.id,
                ticker=prediction.ticker,
                error=str(exc),
            )
            continue
        except Exception as exc:  # pragma: no cover - defensive
            # Any other exception is a bug we want to learn about, but
            # we keep the batch going so one bad row doesn't kill the
            # daily cron.
            summary.adapter_errors += 1
            _log.exception(
                "predictions.evaluator.unexpected_error",
                prediction_id=prediction.id,
                error_type=type(exc).__name__,
            )
            continue

        # The cheap-path query in evaluate_prediction may have
        # returned an existing row — we still count it as "skipped"
        # for telemetry so re-runs are visible in the summary.
        if outcome.evaluated_at is not None and outcome.id is not None:
            # Heuristic: a freshly-inserted row's evaluated_at is
            # populated by the DB default on flush; an existing row's
            # evaluated_at is older than `now`. Tolerate clock skew.
            # SQLite returns evaluated_at as a NAIVE datetime even
            # when the column is DECLARED DateTime(timezone=True);
            # coerce both sides to UTC-aware for the comparison.
            eval_at = outcome.evaluated_at
            if eval_at.tzinfo is None:
                eval_at = eval_at.replace(tzinfo=timezone.utc)
            now_aware = now
            if now_aware.tzinfo is None:
                now_aware = now_aware.replace(tzinfo=timezone.utc)
            if eval_at < now_aware - timedelta(seconds=1):
                summary.skipped_existing += 1
                continue

        summary.evaluated += 1
        kind = outcome.outcome_kind
        summary.by_kind[kind] = summary.by_kind.get(kind, 0) + 1
        if kind == "unparseable":
            summary.unparseable += 1

    _log.info(
        "predictions.evaluator.batch.done",
        evaluated=summary.evaluated,
        skipped_existing=summary.skipped_existing,
        unparseable=summary.unparseable,
        adapter_errors=summary.adapter_errors,
    )

    # Spec C commit #6 — bust the reliability cache so the NEXT consumer
    # query re-reads the freshly-evaluated outcomes. The cache TTL is
    # 5min anyway (process-local) but a daily batch that inserts dozens
    # of outcomes should not wait the TTL out — the next synth /
    # news-analyst run within those 5 minutes would otherwise read
    # stale weights. Best-effort: any failure logs and is swallowed
    # so the evaluator's primary success path is unaffected.
    try:
        from argosy.services.predictions.reliability import (
            invalidate_reliability_cache,
        )
        invalidate_reliability_cache()
    except Exception:  # noqa: BLE001 — never break evaluator
        _log.warning(
            "predictions.evaluator.cache_invalidate_failed",
            exc_info=True,
        )

    return summary


__all__ = [
    "Bar",
    "EvaluatorAdapterError",
    "EvaluatorSummary",
    "Outcome",
    "OutcomeKind",
    "PriceFetcher",
    "default_price_fetcher",
    "evaluate_prediction",
    "find_due_predictions",
    "run_evaluator_batch",
]
