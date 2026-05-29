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

from sqlalchemy import text
from sqlalchemy.orm import Session

from argosy.logging import get_logger

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
) -> float:
    """Return the multiplicative weight for signals from this source.

    Formula (spec §4.3 + §6.6):

        weight_raw = hit_rate * participation_penalty * sample_size_factor
        weight     = clip(weight_raw, WEIGHT_FLOOR, WEIGHT_CEIL)

    Defaults:

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

    Returns:
      Float in ``[WEIGHT_FLOOR, WEIGHT_CEIL]`` plus the special prior
      value 1.0 for the "no data / unknown" cases.
    """
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


__all__ = [
    "CACHE_TTL_SECONDS",
    "FULL_SAMPLE_SIZE",
    "MIN_SAMPLE_SIZE",
    "SourceReliability",
    "WEIGHT_CEIL",
    "WEIGHT_FLOOR",
    "get_source_reliability",
    "get_weight_for_source",
    "invalidate_reliability_cache",
]
