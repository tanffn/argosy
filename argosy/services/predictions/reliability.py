"""Source reliability accessor — Spec C commit #5.

Read-side companion to the ``source_reliability`` SQL VIEW shipped in
migration 0052. Consumers (``synthesizer``, ``news_signal_analyst``,
``per_position_thesis``, Spec-B ``state_observer``, ``plan_monitor``)
call into here to weight future signals from a given (user, source,
method_family) tuple.

See ``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``:

* §4   — view + service design overview.
* §4.2 — Python accessor surface.
* §4.3 — the "small sample" floor (parameterised here, in code, NOT in
  the view — the view exposes ``sample_size_warning=1`` when scored
  < 10 but does NOT bake the consumer policy of "what weight do we
  use under the floor?" into SQL).
* §6.6 — anti-feedback-loop contract. Mitigation #2 (min-sample floor)
  + mitigation #6 (Codex IMPORTANT 3 — ``cumulative_attenuation``
  floor of 0.10 — implemented here as the hard min of
  ``get_weight_for_source``).

Public surface
==============

* :class:`SourceReliability` — frozen dataclass mirroring one row of the
  ``source_reliability`` view, with the median computed in-Python.
* :func:`get_source_reliability` — return view rows, optionally filtered
  by ``source`` and ``method_family``. Cached with a 5-minute TTL keyed
  by ``(user_id, source, method_family)``.
* :func:`get_weight_for_source` — return the multiplicative weight a
  consumer should apply to a signal from a given source/family.
  Default = 1.0 for unknown / insufficient-sample. Clipped to
  ``[0.10, 1.50]`` per spec §6.6 (floor prevents the feedback-loop
  death spiral; cap prevents runaway up-weighting of small lucky
  samples).
* :func:`invalidate_reliability_cache` — bust the cache. Called by the
  evaluator at the end of every batch (a fresh outcome row may have
  shifted the metrics).

Cache design
============

Process-local in-memory cache with a 5-minute TTL per key. Rationale
(spec §4.2): consumers call this on every weight decision (potentially
hundreds of calls per planning run); the view itself is cheap but the
network round-trip + the ROW_NUMBER window + the per-prediction dedup
isn't free. A 5-minute window is well below the daily evaluator
cadence so we never serve "stale-by-a-day" weights.

Cache is intentionally NOT per-session: a single argosy process owns
the cache; restarts naturally invalidate. Multi-process deployments
(future) would want a shared cache or a shorter TTL — out of scope
today (single-user, single-process).

The cache key is ``(user_id, source or "<all>", method_family or
"<all>")`` so an "all sources / all families" call is cached separately
from a "discord / fixed_lookahead" call. The view itself doesn't
denormalise so we'd have to re-aggregate anyway; caching the
post-filter Python list is simpler than caching the raw rows + filtering
in Python on every hit.

Determinism / idempotency
=========================

``get_source_reliability`` returns a list of frozen dataclasses; same
view contents → same list (modulo cache freshness). The view itself
picks ONE outcome per (prediction_id, family) via the migration's
ROW_NUMBER + tie-break ladder (method_version DESC, evaluated_at
DESC, outcome_id DESC) so re-querying with the same data gives the
same aggregation. Codex BLOCKER 1 (spec §3.4) is honoured by the
migration; this module trusts the view.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import EvaluationMethod, Prediction, PredictionOutcome

_log = get_logger("argosy.services.predictions.reliability")


# ---------------------------------------------------------------------------
# Tunables — kept module-level so tests can pin contracts via inspection.
# ---------------------------------------------------------------------------

#: Minimum scored sample size below which the consumer dims the source
#: to ``0.5 * sample_size_factor`` (the floor of the ramp). This is
#: DIVERGENT from spec §4.3's prose ("min_samples=20 → return prior
#: 1.0") — the implementation contract per Spec C commit #5's task
#: brief is a CONTINUOUS ramp from 0.5 (small) to 1.0 (full sample),
#: NOT a step-function "prior or hit_rate" choice. Rationale: the spec
#: text was Bayesian-prior-style; the task brief picked a smoother
#: dim-then-trust profile so a 9/9 source isn't treated identically to
#: a 0/9 source. Codex single-dispatch review 2026-05-29 flagged the
#: divergence as a BLOCKER; the resolution per "task brief wins" is to
#: keep the ramp + document the divergence here. Future work: a spec
#: amendment ratifying the smoother profile.
MIN_SAMPLE_SIZE: int = 10

#: Sample size at which the consumer trusts the hit_rate fully (1.0×
#: confidence multiplier). Between MIN_SAMPLE_SIZE and FULL_SAMPLE_SIZE
#: the confidence multiplier ramps linearly from 0.5 to 1.0. Above, it
#: stays at 1.0.
FULL_SAMPLE_SIZE: int = 50

#: Spec §6.6 anti-feedback-loop floor. Codex IMPORTANT 3 — the
#: ``cumulative_attenuation`` end-to-end across all consumers must not
#: dip below 0.10× regardless of how many consumers have already dimmed
#: the signal. The single-hop floor implemented here is the same number
#: so a one-hop discount can't single-handedly drive below the
#: end-to-end floor.
WEIGHT_FLOOR: float = 0.10

#: Cap on the up-weight side. NOTE (codex review 2026-05-29 IMPORTANT
#: #3): with the v1 formula ``hit_rate * participation_penalty *
#: sample_size_factor`` all three factors are bounded in ``[0, 1]``,
#: so ``raw`` cannot exceed 1.0; the ceiling is defensive — it
#: protects against a future formula variant that includes a
#: ``hit_rate / 0.5`` baseline-expansion term (spec §4.3's
#: ``effective_weight`` formulation, which can exceed 1.0 for a
#: > 50% hit-rate source). v1 is attenuation-only by design; up-
#: weighting is reserved for a follow-on commit that introduces the
#: baseline-expansion term + a re-review of the cap.
WEIGHT_CEIL: float = 1.50

#: TTL for the in-memory reliability cache. 5 minutes is well below the
#: daily evaluator cadence — a fresh outcome batch resets the cache via
#: ``invalidate_reliability_cache`` anyway; this TTL just caps the
#: worst-case staleness when the invalidation hook isn't wired.
CACHE_TTL_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceReliability:
    """One row of the ``source_reliability`` view, post-Python-median.

    Fields mirror the view's columns 1:1 plus ``median_pnl_pct`` which is
    computed here (SQLite has no MEDIAN aggregate). All counts are
    integers; rates are floats in ``[0, 1]`` (or ``None`` when the
    denominator was zero); ``mean_pnl_pct`` / ``median_pnl_pct`` /
    ``rolling_30d_mean_pnl`` are signed floats (positive = the
    prediction's direction was right).

    ``last_evaluated_at`` is a UTC-naive :class:`datetime` (SQLite drops
    the tz on round-trip) — consumers needing tz-aware should localise.
    """

    user_id: str
    source: str
    method_family: str
    total_predictions: int
    scored_predictions: int
    unparseable_count: int
    hit_target_count: int
    hit_stop_count: int
    expired_positive_count: int
    expired_negative_count: int
    expired_neutral_count: int
    mean_pnl_pct: Optional[float]
    median_pnl_pct: Optional[float]
    hit_rate: Optional[float]
    abstain_rate: Optional[float]
    participation_penalty: Optional[float]
    last_evaluated_at: Optional[datetime]
    rolling_30d_hit_rate: Optional[float]
    rolling_30d_mean_pnl: Optional[float]
    sample_size_warning: int  # 0 or 1


@dataclass(frozen=True)
class SignalFunnelContextPolicy:
    """Pure result of the pre-agreed 180d context-privilege policy."""

    funnel_context_enabled: bool
    calibrated: bool
    kill_reason: str | None


def signal_funnel_context_policy(
    *,
    scored_180d: int,
    win_rate_180d: float | None,
    always_long_same_tickers_win_rate: float | None,
) -> SignalFunnelContextPolicy:
    """Pause only a verified 180d stream that fails its same-ticker baseline."""
    if (
        scored_180d < 50
        or win_rate_180d is None
        or always_long_same_tickers_win_rate is None
    ):
        return SignalFunnelContextPolicy(
            funnel_context_enabled=True,
            calibrated=False,
            kill_reason=None,
        )
    if win_rate_180d <= always_long_same_tickers_win_rate:
        return SignalFunnelContextPolicy(
            funnel_context_enabled=False,
            calibrated=True,
            kill_reason=(
                f"180d stream win rate {win_rate_180d:.1%} does not beat "
                "always-long same-tickers benchmark "
                f"{always_long_same_tickers_win_rate:.1%} "
                f"(n={scored_180d})"
            ),
        )
    return SignalFunnelContextPolicy(
        funnel_context_enabled=True,
        calibrated=True,
        kill_reason=None,
    )


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------


# Cache value: (expires_at_monotonic, payload).
# Payload is a list[SourceReliability] (already filtered to the requested
# scope) so a cache hit is one dict lookup + freshness check.
_CACHE: dict[tuple[str, str, str], tuple[float, list[SourceReliability]]] = {}
_CACHE_LOCK = RLock()


def invalidate_reliability_cache() -> None:
    """Bust the entire reliability cache.

    Called by the evaluator at end-of-batch (a fresh outcome row may
    have shifted the aggregation) and by tests / manual-override flows.

    Cheap (just clears a dict under a lock); safe to call from any
    thread.
    """
    with _CACHE_LOCK:
        if _CACHE:
            _log.debug(
                "predictions.reliability.cache.invalidate",
                size=len(_CACHE),
            )
        _CACHE.clear()


def _cache_key(
    user_id: str,
    source: Optional[str],
    method_family: Optional[str],
) -> tuple[str, str, str]:
    """Canonical cache key — ``None`` → ``"<all>"`` sentinel.

    Tests use this to inspect the cache after a hit/miss without
    duplicating the sentinel-string convention.
    """
    return (
        user_id,
        source if source is not None else "<all>",
        method_family if method_family is not None else "<all>",
    )


# ---------------------------------------------------------------------------
# View accessor
# ---------------------------------------------------------------------------


# All-columns SELECT against the view; filtered via bound params in the
# accessor. Kept as a module-level string so the column order is
# greppable + the migration's column order is the source of truth.
_VIEW_SELECT_SQL = """
SELECT
    user_id,
    source,
    method_family,
    total_predictions,
    scored_predictions,
    unparseable_count,
    hit_target_count,
    hit_stop_count,
    expired_positive_count,
    expired_negative_count,
    expired_neutral_count,
    mean_pnl_pct,
    median_pnl_pct,
    hit_rate,
    abstain_rate,
    participation_penalty,
    last_evaluated_at,
    rolling_30d_hit_rate,
    rolling_30d_mean_pnl,
    sample_size_warning
FROM source_reliability
WHERE user_id = :user_id
"""


# Companion query for the in-Python median computation. The view doesn't
# emit raw pnl_pct lists (SQLite lacks ARRAY_AGG); we re-query the
# (deduped) outcomes for each tuple and compute the median client-side.
#
# Mirrors the view's dedup logic (ROW_NUMBER over method_version DESC,
# evaluated_at DESC, id DESC; pick rn=1; archived=0). Keeping the dedup
# in sync between view and helper is the codex-probe-worthy bit — if
# the view's dedup changes, this query MUST change in lockstep.
_PNL_FOR_MEDIAN_SQL = """
WITH dedup_outcomes AS (
    SELECT
        o.id            AS outcome_id,
        o.prediction_id AS prediction_id,
        o.outcome_kind  AS outcome_kind,
        o.pnl_pct       AS pnl_pct,
        o.evaluated_at  AS evaluated_at,
        r.family        AS method_family,
        ROW_NUMBER() OVER (
            PARTITION BY o.prediction_id, r.family
            ORDER BY r.method_version DESC,
                     o.evaluated_at DESC,
                     o.id DESC
        ) AS rn
    FROM prediction_outcomes o
    JOIN evaluation_method_registry r
      ON r.method_name = o.evaluation_method
     AND r.is_active = 1
)
SELECT
    p.source        AS source,
    d.method_family AS method_family,
    d.pnl_pct       AS pnl_pct
FROM dedup_outcomes d
JOIN predictions p ON p.id = d.prediction_id
WHERE d.rn = 1
  AND p.archived = 0
  AND p.user_id = :user_id
  AND d.pnl_pct IS NOT NULL
"""


#: Explicit exclusions documented on every standing scorecard row.
SCORECARD_EXCLUSIONS_DOC: tuple[str, ...] = (
    "outcome_kind='unparseable' (generic)",
    "direction='neutral' scored holds (reported under hold_* only)",
    "unscored (no outcome row yet)",
    "pending_entry / missing_entry_quote (verdict recorded, not yet scoreable)",
    "superseded_by_correction (prior immutable version; head is scored)",
    "hold_self_benchmark (CSPX vs CSPX — degenerate)",
    "hold_non_equity (CSPX not a valid benchmark for asset class)",
    "hold_incomplete_benchmark (CSPX/name marks missing or truncated)",
)


@dataclass(frozen=True)
class SourceScorecardRow:
    """Auditable per-source hit rate + avg P&L.

    Archived-but-scored outcomes stay in the sample. Pending-entry,
    superseded, and HOLD-ineligible rows are explicit exclusion buckets.
    ``hold_*`` is strictly separate from directional hit-rate.

    Invariant (output-trust):
      falsifiable_denominator
      + excluded_unparseable
      + excluded_neutral
      + excluded_unscored
      + excluded_pending_entry
      + excluded_superseded
      + excluded_hold_self_benchmark
      + excluded_hold_non_equity
      + excluded_hold_incomplete_benchmark
      == total_predictions
    """

    source: str
    hit_rate: float | None
    win_rate: float | None
    avg_pnl_pct: float | None
    falsifiable_denominator: int
    hit_target_count: int
    expired_positive_count: int
    hold_scored: int
    hold_correct_count: int
    hold_correct_rate: float | None
    excluded_unparseable: int
    excluded_neutral: int
    excluded_archived: int  # always 0 — archived scored stay in sample
    excluded_unscored: int
    excluded_pending_entry: int
    excluded_superseded: int
    excluded_hold_self_benchmark: int
    excluded_hold_non_equity: int
    excluded_hold_incomplete_benchmark: int
    total_predictions: int
    exclusions: tuple[str, ...] = SCORECARD_EXCLUSIONS_DOC

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "hit_rate": self.hit_rate,
            "win_rate": self.win_rate,
            "avg_pnl_pct": self.avg_pnl_pct,
            "falsifiable_denominator": self.falsifiable_denominator,
            "hit_target_count": self.hit_target_count,
            "expired_positive_count": self.expired_positive_count,
            "hold_scored": self.hold_scored,
            "hold_correct_count": self.hold_correct_count,
            "hold_correct_rate": self.hold_correct_rate,
            "excluded_unparseable": self.excluded_unparseable,
            "excluded_neutral": self.excluded_neutral,
            "excluded_archived": self.excluded_archived,
            "excluded_unscored": self.excluded_unscored,
            "excluded_pending_entry": self.excluded_pending_entry,
            "excluded_superseded": self.excluded_superseded,
            "excluded_hold_self_benchmark": self.excluded_hold_self_benchmark,
            "excluded_hold_non_equity": self.excluded_hold_non_equity,
            "excluded_hold_incomplete_benchmark": (
                self.excluded_hold_incomplete_benchmark
            ),
            "total_predictions": self.total_predictions,
            "exclusions": list(self.exclusions),
            "hit_rate_definition": (
                "hit_target / falsifiable_denominator "
                "(directional long/short only; never includes HOLD)"
            ),
            "win_rate_definition": (
                "(hit_target + expired_positive) / falsifiable_denominator"
            ),
            "hold_correct_rate_definition": (
                "(expired_neutral + expired_positive) / hold_scored — "
                "CSPX-relative: excess = name_ret − CSPX_ret; "
                "correct when excess >= −HOLD_BENCHMARK_BAND_PCT (3%)"
            ),
            "hold_benchmark": "CSPX",
            "hold_benchmark_band_pct": 0.03,
        }


def ledger_scorecard_by_source(
    session: Session,
    user_id: str,
) -> list[SourceScorecardRow]:
    """Standing scorecard: hit rate + avg P&L broken out by ``source``.

    Archived-but-scored outcomes remain in the denominator (retention
    must not improve hit rate by aging failures out). Pending-entry and
    superseded versions are published exclusion buckets.
    """
    from argosy.services.predictions.writers import (
        MISSING_ENTRY_REASON,
        SUPERSEDED_REASON,
    )

    rows = session.execute(
        select(Prediction, PredictionOutcome, EvaluationMethod)
        .join(
            PredictionOutcome,
            PredictionOutcome.prediction_id == Prediction.id,
        )
        .join(
            EvaluationMethod,
            EvaluationMethod.method_name
            == PredictionOutcome.evaluation_method,
        )
        .where(
            Prediction.user_id == user_id,
            EvaluationMethod.is_active == 1,
        )
    ).all()

    selected: dict[
        int, tuple[Prediction, PredictionOutcome, EvaluationMethod]
    ] = {}
    for prediction, outcome, method in rows:
        current = selected.get(prediction.id)
        rank = (
            int(method.method_version or 0),
            outcome.evaluated_at or datetime.min,
            int(outcome.id or 0),
        )
        if current is None:
            selected[prediction.id] = (prediction, outcome, method)
            continue
        current_rank = (
            int(current[2].method_version or 0),
            current[1].evaluated_at or datetime.min,
            int(current[1].id or 0),
        )
        if rank > current_rank:
            selected[prediction.id] = (prediction, outcome, method)

    all_preds = session.execute(
        select(Prediction).where(Prediction.user_id == user_id)
    ).scalars().all()
    by_source_preds: dict[str, list[Prediction]] = {}
    for p in all_preds:
        by_source_preds.setdefault(p.source, []).append(p)

    out: list[SourceScorecardRow] = []
    for source, preds in sorted(by_source_preds.items()):
        from argosy.services.predictions.hold_benchmark import (
            hold_ineligibility_bucket,
        )

        excluded_unparseable = 0
        excluded_neutral = 0
        excluded_unscored = 0
        excluded_pending_entry = 0
        excluded_superseded = 0
        excluded_hold_self_benchmark = 0
        excluded_hold_non_equity = 0
        excluded_hold_incomplete_benchmark = 0
        falsifiable: list[PredictionOutcome] = []
        hit_target = 0
        expired_positive = 0
        hold_scored = 0
        hold_correct = 0
        pnl_values: list[float] = []
        for p in preds:
            if (
                p.superseded_by_prediction_id is not None
                or (p.unparseable_reason or "") == SUPERSEDED_REASON
            ):
                excluded_superseded += 1
                continue
            if (p.unparseable_reason or "") == MISSING_ENTRY_REASON or (
                p.entry_price is None
                and (p.unparseable_reason or "").startswith("missing_entry")
            ):
                excluded_pending_entry += 1
                continue
            triple = selected.get(p.id)
            if triple is None:
                excluded_unscored += 1
                continue
            _p, outcome, _m = triple
            if outcome.outcome_kind == "unparseable":
                bucket = hold_ineligibility_bucket(outcome.notes)
                if bucket == "excluded_hold_self_benchmark":
                    excluded_hold_self_benchmark += 1
                elif bucket == "excluded_hold_non_equity":
                    excluded_hold_non_equity += 1
                elif bucket == "excluded_hold_incomplete_benchmark":
                    excluded_hold_incomplete_benchmark += 1
                else:
                    excluded_unparseable += 1
                continue
            if p.direction == "neutral":
                excluded_neutral += 1
                hold_scored += 1
                if outcome.outcome_kind in (
                    "expired_neutral",
                    "expired_positive",
                ):
                    hold_correct += 1
                continue
            falsifiable.append(outcome)
            if outcome.outcome_kind == "hit_target":
                hit_target += 1
            if outcome.outcome_kind == "expired_positive":
                expired_positive += 1
            if outcome.pnl_pct is not None:
                pnl_values.append(float(outcome.pnl_pct))

        denom = len(falsifiable)
        hit_rate = (hit_target / denom) if denom else None
        win_rate = (
            (hit_target + expired_positive) / denom if denom else None
        )
        avg_pnl = (
            sum(pnl_values) / len(pnl_values) if pnl_values else None
        )
        hold_rate = (
            hold_correct / hold_scored if hold_scored else None
        )
        row = SourceScorecardRow(
            source=source,
            hit_rate=hit_rate,
            win_rate=win_rate,
            avg_pnl_pct=avg_pnl,
            falsifiable_denominator=denom,
            hit_target_count=hit_target,
            expired_positive_count=expired_positive,
            hold_scored=hold_scored,
            hold_correct_count=hold_correct,
            hold_correct_rate=hold_rate,
            excluded_unparseable=excluded_unparseable,
            excluded_neutral=excluded_neutral,
            excluded_archived=0,
            excluded_unscored=excluded_unscored,
            excluded_pending_entry=excluded_pending_entry,
            excluded_superseded=excluded_superseded,
            excluded_hold_self_benchmark=excluded_hold_self_benchmark,
            excluded_hold_non_equity=excluded_hold_non_equity,
            excluded_hold_incomplete_benchmark=(
                excluded_hold_incomplete_benchmark
            ),
            total_predictions=len(preds),
        )
        reconciled = (
            row.falsifiable_denominator
            + row.excluded_unparseable
            + row.excluded_neutral
            + row.excluded_unscored
            + row.excluded_pending_entry
            + row.excluded_superseded
            + row.excluded_hold_self_benchmark
            + row.excluded_hold_non_equity
            + row.excluded_hold_incomplete_benchmark
        )
        if reconciled != row.total_predictions:
            raise RuntimeError(
                f"scorecard reconcile failed for source={source!r}: "
                f"{reconciled} != {row.total_predictions}"
            )
        out.append(row)
    return out


def _compute_medians(
    session: Session, user_id: str
) -> dict[tuple[str, str], float]:
    """Compute median pnl_pct per (source, method_family) for one user.

    Returns a dict keyed by ``(source, method_family)``. Missing keys
    mean "no non-NULL pnl rows for this tuple"; the caller treats those
    as ``None``.
    """
    rows = session.execute(
        text(_PNL_FOR_MEDIAN_SQL), {"user_id": user_id}
    ).all()

    buckets: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        key = (row.source, row.method_family)
        buckets.setdefault(key, []).append(float(row.pnl_pct))

    return {
        key: statistics.median(values)
        for key, values in buckets.items()
        if values
    }


def get_source_reliability(
    session: Session,
    user_id: str,
    *,
    source: Optional[str] = None,
    method_family: Optional[str] = None,
) -> list[SourceReliability]:
    """Return reliability rows for ``user_id``, optionally filtered.

    Hits the 5-minute in-memory cache keyed by
    ``(user_id, source, method_family)``; a cache miss runs the view +
    the median helper and stores the post-filter list.

    Args:
      session: sync SQLAlchemy session bound to the predictions DB.
      user_id: tenant id (always ``'ariel'`` today; required for
        multi-tenant readiness per SDD §12.5).
      source: optional filter — one of the 11 spec §1.2 source enums
        (``'discord'``, ``'internal_per_position_thesis'``, etc.).
        ``None`` returns rows across ALL sources for the user.
      method_family: optional filter — one of the four spec §3.4
        families (``'target_stop'``, ``'fixed_lookahead'``,
        ``'multi_basket'``, ``'unparseable'``). ``None`` returns rows
        across ALL families.

    Returns:
      Sorted list of :class:`SourceReliability` (stable sort by
      ``(source, method_family)`` so test assertions are deterministic).
      Empty list if the user has no scored predictions yet.
    """
    key = _cache_key(user_id, source, method_family)
    now_mono = time.monotonic()

    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry[0] > now_mono:
            return list(entry[1])

    # Cache miss — query the view + compute medians client-side.
    rows = session.execute(
        text(_VIEW_SELECT_SQL), {"user_id": user_id}
    ).all()
    medians = _compute_medians(session, user_id)

    out: list[SourceReliability] = []
    for r in rows:
        if source is not None and r.source != source:
            continue
        if method_family is not None and r.method_family != method_family:
            continue
        median = medians.get((r.source, r.method_family))
        out.append(
            SourceReliability(
                user_id=r.user_id,
                source=r.source,
                method_family=r.method_family,
                total_predictions=int(r.total_predictions or 0),
                scored_predictions=int(r.scored_predictions or 0),
                unparseable_count=int(r.unparseable_count or 0),
                hit_target_count=int(r.hit_target_count or 0),
                hit_stop_count=int(r.hit_stop_count or 0),
                expired_positive_count=int(r.expired_positive_count or 0),
                expired_negative_count=int(r.expired_negative_count or 0),
                expired_neutral_count=int(r.expired_neutral_count or 0),
                mean_pnl_pct=(
                    float(r.mean_pnl_pct)
                    if r.mean_pnl_pct is not None
                    else None
                ),
                median_pnl_pct=median,
                hit_rate=(
                    float(r.hit_rate) if r.hit_rate is not None else None
                ),
                abstain_rate=(
                    float(r.abstain_rate)
                    if r.abstain_rate is not None
                    else None
                ),
                participation_penalty=(
                    float(r.participation_penalty)
                    if r.participation_penalty is not None
                    else None
                ),
                last_evaluated_at=r.last_evaluated_at,
                rolling_30d_hit_rate=(
                    float(r.rolling_30d_hit_rate)
                    if r.rolling_30d_hit_rate is not None
                    else None
                ),
                rolling_30d_mean_pnl=(
                    float(r.rolling_30d_mean_pnl)
                    if r.rolling_30d_mean_pnl is not None
                    else None
                ),
                sample_size_warning=int(r.sample_size_warning or 0),
            )
        )

    out.sort(key=lambda x: (x.source, x.method_family))

    with _CACHE_LOCK:
        _CACHE[key] = (now_mono + CACHE_TTL_SECONDS, list(out))

    return out


# ---------------------------------------------------------------------------
# Weight calculation — consumer-facing
# ---------------------------------------------------------------------------


def _sample_size_factor(scored_predictions: int) -> float:
    """Ramp from 0.5 (small sample) → 1.0 (full sample).

    * ``< MIN_SAMPLE_SIZE`` → 0.5 (consumer dimming under the floor —
      the source's hit_rate is too noisy to enter weighting at full
      conviction, but we don't zero it out either; some signal still
      flows).
    * ``MIN_SAMPLE_SIZE..FULL_SAMPLE_SIZE`` → linear ramp from 0.5 to 1.0.
    * ``>= FULL_SAMPLE_SIZE`` → 1.0 (full confidence).

    Returns a float in ``[0.5, 1.0]``.
    """
    if scored_predictions < MIN_SAMPLE_SIZE:
        return 0.5
    if scored_predictions >= FULL_SAMPLE_SIZE:
        return 1.0
    span = FULL_SAMPLE_SIZE - MIN_SAMPLE_SIZE
    return 0.5 + 0.5 * (scored_predictions - MIN_SAMPLE_SIZE) / span


def get_weight_for_source(
    session: Session,
    user_id: str,
    source: str,
    method_family: str,
    *,
    provenance_weights_applied: bool = False,
) -> float:
    """Return the multiplicative weight for signals from this source.

    Formula (spec §4.3 + §6.6):

        weight_raw = hit_rate * participation_penalty * sample_size_factor
        weight     = clip(weight_raw, WEIGHT_FLOOR, WEIGHT_CEIL)

    Defaults:

    * ``provenance_weights_applied=True`` (spec §6.6 / codex IMPORTANT
      3) → return ``1.0`` immediately. The upstream consumer already
      applied a reliability weight to this signal; re-applying it would
      compound attenuation across the consumer chain. This is the
      primary anti-feedback-loop discipline; the ``WEIGHT_FLOOR`` is
      the safety net. **Every consumer that reads this function MUST
      thread ``provenance_weights_applied`` from the upstream
      derivative** — synth from its input theses, news_signal_analyst
      from the news_signal row, etc.
    * Unknown (source, family) → 1.0 (no signal yet; consumer should
      treat as baseline).
    * scored_predictions == 0 → 1.0 (only unparseable rows; can't
      assess reliability).
    * hit_rate is None (denominator 0 inside the view) → 1.0.
    * weight after raw-formula is between 0 and WEIGHT_FLOOR → clamped
      UP to WEIGHT_FLOOR (spec §6.6 — never let a feedback loop
      cascade into zero).
    * weight after raw-formula > WEIGHT_CEIL → clamped DOWN.

    Args:
      session: sync SQLAlchemy session.
      user_id: tenant id.
      source: one of the 11 v1 source enums.
      method_family: one of the four v1 method families.
      provenance_weights_applied: if True, return 1.0 unconditionally
        — the caller is signalling that an upstream consumer already
        applied a reliability weight to this signal and a second hop
        would double-attenuate. Defaults to False (apply weight as
        usual).

    Returns:
      Float in ``[WEIGHT_FLOOR, WEIGHT_CEIL]`` plus the special prior
      value 1.0 for the "no data / unknown / already-weighted" cases.
    """
    # Codex IMPORTANT 3 / spec §6.6 — short-circuit at the very top.
    # An upstream consumer's weight is in-flight on this signal; the
    # contract is "apply at most once per derivative path."
    if provenance_weights_applied:
        return 1.0

    rows = get_source_reliability(
        session, user_id, source=source, method_family=method_family
    )
    if not rows:
        return 1.0

    # The (source, family) filter should yield AT MOST one row by view
    # design (GROUP BY user_id, source, method_family). Defensive: pick
    # the first.
    rel = rows[0]

    if rel.scored_predictions == 0 or rel.hit_rate is None:
        return 1.0

    # participation_penalty can be None when total_predictions == 0;
    # default to 1.0 (no penalty) in that case.
    penalty = (
        rel.participation_penalty
        if rel.participation_penalty is not None
        else 1.0
    )

    factor = _sample_size_factor(rel.scored_predictions)
    raw = rel.hit_rate * penalty * factor

    if raw < WEIGHT_FLOOR:
        return WEIGHT_FLOOR
    if raw > WEIGHT_CEIL:
        return WEIGHT_CEIL
    return raw


def reliability_annotation(
    session: Session,
    user_id: str,
    source: str,
    *,
    method_family: str = "fixed_lookahead",
) -> dict[str, object]:
    """Return a small dict suitable for surfacing as audit/UI metadata.

    Used by ``per_position_thesis`` (and any other consumer) that wants
    to ATTACH a reliability hint to a derivative rather than
    multiplying the signal by a weight directly. The shape is:

        {
            "source": "<source>",
            "method_family": "<family>",
            "hit_rate": float | None,
            "scored_predictions": int,
            "sample_size_warning": bool,
            "effective_weight": float,  # what get_weight_for_source returns
        }

    Soft data — consumers (LLM prompts, operator dashboards) may use
    it to colour decisions. NOT a hard veto: per spec §6.3 the
    per-position thesis derivation "should NOT let a low-reliability
    sentiment FLIP a HOLD to BUY/SELL alone."

    The ``effective_weight`` field is computed via
    :func:`get_weight_for_source` **without** the
    ``provenance_weights_applied`` short-circuit — annotations are
    descriptive, not load-bearing for the signal math, and the caller
    can decide whether to honour the weight or just surface it.
    """
    rows = get_source_reliability(
        session, user_id, source=source, method_family=method_family
    )
    if not rows:
        return {
            "source": source,
            "method_family": method_family,
            "hit_rate": None,
            "scored_predictions": 0,
            "sample_size_warning": True,
            "effective_weight": 1.0,
        }
    rel = rows[0]
    return {
        "source": source,
        "method_family": method_family,
        "hit_rate": rel.hit_rate,
        "scored_predictions": rel.scored_predictions,
        "sample_size_warning": bool(rel.sample_size_warning),
        "effective_weight": get_weight_for_source(
            session,
            user_id,
            source,
            method_family,
            provenance_weights_applied=False,
        ),
    }


def signal_scorecard_label(*, scored: int, observation_days: int) -> str:
    if scored < 50:
        return (
            f"uncalibrated (beta — {scored} scored over "
            f"{observation_days} days)"
        )
    return "calibrated"


def _authoritative_signal_outcomes(
    session: Session,
    *,
    user_id: str,
    source: str,
) -> list[tuple[Prediction, PredictionOutcome, EvaluationMethod]]:
    """Select one highest-version outcome per prediction/method family."""
    rows = session.execute(
        select(Prediction, PredictionOutcome, EvaluationMethod)
        .join(
            PredictionOutcome,
            PredictionOutcome.prediction_id == Prediction.id,
        )
        .join(
            EvaluationMethod,
            EvaluationMethod.method_name
            == PredictionOutcome.evaluation_method,
        )
        .where(
            Prediction.user_id == user_id,
            Prediction.source == source,
            Prediction.archived == 0,
            EvaluationMethod.is_active == 1,
        )
    ).all()
    selected: dict[
        tuple[int, str],
        tuple[Prediction, PredictionOutcome, EvaluationMethod],
    ] = {}
    for prediction, outcome, method in rows:
        key = (prediction.id, method.family)
        current = selected.get(key)
        rank = (
            int(method.method_version or 0),
            outcome.evaluated_at or datetime.min,
            int(outcome.id or 0),
        )
        if current is None:
            selected[key] = (prediction, outcome, method)
            continue
        current_rank = (
            int(current[2].method_version or 0),
            current[1].evaluated_at or datetime.min,
            int(current[1].id or 0),
        )
        if rank > current_rank:
            selected[key] = (prediction, outcome, method)
    return list(selected.values())


def _signal_horizon_slice(
    rows: list[tuple[Prediction, PredictionOutcome, EvaluationMethod]],
    *,
    include_always_long: bool,
) -> dict[str, object]:
    scored_rows = [
        row for row in rows if row[1].outcome_kind != "unparseable"
    ]
    scored = len(scored_rows)
    wins = sum(
        row[1].outcome_kind in {"hit_target", "expired_positive"}
        for row in scored_rows
    )
    pnl_values = [
        float(row[1].pnl_pct)
        for row in scored_rows
        if row[1].pnl_pct is not None
    ]
    result: dict[str, object] = {
        "scored_outcomes": scored,
        "win_rate": (wins / scored) if scored else None,
        "avg_pnl_pct": (
            sum(pnl_values) / len(pnl_values) if pnl_values else None
        ),
    }
    if include_always_long:
        raw_long_wins = 0
        benchmark_verifiable = scored > 0
        for _prediction, outcome, _method in scored_rows:
            try:
                entry = float(outcome.entry_price_used)
                exit_price = float(outcome.exit_price_used)
                if entry <= 0:
                    benchmark_verifiable = False
                    break
                raw_long_wins += ((exit_price - entry) / entry) >= 0.01
            except (TypeError, ValueError, ArithmeticError):
                benchmark_verifiable = False
                break
        result["always_long_same_tickers_win_rate"] = (
            raw_long_wins / scored if benchmark_verifiable else None
        )
    return result


def signal_source_scorecard(
    session: Session,
    user_id: str,
    stream: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Raw-row signal scorecard and context policy; never an investment verdict."""
    source = f"signal_stream:{stream}"
    authoritative = _authoritative_signal_outcomes(
        session, user_id=user_id, source=source
    )
    horizon_30d = _signal_horizon_slice(
        [row for row in authoritative if row[0].timeframe_days == 30],
        include_always_long=False,
    )
    horizon_180d = _signal_horizon_slice(
        [row for row in authoritative if row[0].timeframe_days == 180],
        include_always_long=True,
    )
    aggregate = _signal_horizon_slice(
        [
            row
            for row in authoritative
            if row[0].timeframe_days in {30, 180}
        ],
        include_always_long=False,
    )
    first_event = session.execute(
        select(func.min(Prediction.event_at)).where(
            Prediction.user_id == user_id,
            Prediction.source == source,
        )
    ).scalar_one_or_none()
    now_dt = now or datetime.now()
    if first_event is None:
        observation_days = 0
    else:
        observation_days = max(0, (now_dt.date() - first_event.date()).days)
    policy = signal_funnel_context_policy(
        scored_180d=int(horizon_180d["scored_outcomes"]),
        win_rate_180d=horizon_180d["win_rate"],  # type: ignore[arg-type]
        always_long_same_tickers_win_rate=horizon_180d[
            "always_long_same_tickers_win_rate"
        ],  # type: ignore[arg-type]
    )
    scored = int(aggregate["scored_outcomes"])
    return {
        "source": source,
        "win_rate": aggregate["win_rate"],
        "scored_outcomes": scored,
        "avg_pnl_pct": aggregate["avg_pnl_pct"],
        "observation_days": observation_days,
        "calibration": (
            "calibrated"
            if policy.calibrated
            else (
                f"uncalibrated (beta — {scored} scored over "
                f"{observation_days} days)"
            )
        ),
        "horizons": {"30d": horizon_30d, "180d": horizon_180d},
        "funnel_context_enabled": policy.funnel_context_enabled,
        "kill_reason": policy.kill_reason,
    }


__all__ = [
    "CACHE_TTL_SECONDS",
    "FULL_SAMPLE_SIZE",
    "MIN_SAMPLE_SIZE",
    "SCORECARD_EXCLUSIONS_DOC",
    "SourceReliability",
    "SourceScorecardRow",
    "SignalFunnelContextPolicy",
    "WEIGHT_CEIL",
    "WEIGHT_FLOOR",
    "get_source_reliability",
    "get_weight_for_source",
    "invalidate_reliability_cache",
    "ledger_scorecard_by_source",
    "reliability_annotation",
    "signal_scorecard_label",
    "signal_funnel_context_policy",
    "signal_source_scorecard",
]
