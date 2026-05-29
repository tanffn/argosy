"""source_reliability view — Spec C commit #5.

Revision ID: 0052_source_reliability_view
Revises: 0051_prediction_outcomes
Create Date: 2026-05-30

Spec C (predictions ledger) commit #5 — ships the ``source_reliability``
SQL VIEW that consumers (``synthesizer``, ``news_signal_analyst``,
``per_position_thesis``, Spec-B ``state_observer``, ``plan_monitor``)
read to weight future signals. See
``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``:

* §4   — view + service design.
* §4.1 — view SQL (PostgreSQL flavour with FILTER; SQLite analog ships
  here using ``CASE WHEN``).
* §3.4 — Codex BLOCKER 1: pick ONE outcome per
  ``(prediction_id, method_family)`` even when more than one
  ``is_active = 1`` method version exists during a transition window.
  Strategy: subquery picks the row with the largest
  ``method_version`` per ``(prediction_id, family)`` from the active
  registry rows; ties broken by ``evaluated_at DESC`` then ``id DESC``
  for full determinism.
* §2.4 — Codex BLOCKER 3: HOLD verdicts are scored as ``direction='neutral'``
  predictions. The view exposes ``abstain_rate`` and
  ``participation_penalty = 1 - abstain_rate`` so consumers can dim
  agents that hide behind HOLD.
* §5.5 — rolling 30d window uses ``evaluated_at`` (not ``event_at``):
  the codex re-review noted that ``event_at`` would freeze the rolling
  window for backfilled-but-stale predictions; ``evaluated_at`` is the
  decision-relevant timestamp ("how recently did we score this?").

View columns (per the task contract):

* ``user_id``, ``source``, ``method_family`` — group keys.
* ``total_predictions`` — every prediction with an outcome row under an
  active method (the family-dedup'd set).
* ``scored_predictions`` — ``total_predictions`` minus ``unparseable``
  (the denominator for hit_rate).
* ``unparseable_count`` — outcome_kind = 'unparseable'.
* ``hit_target_count`` / ``hit_stop_count`` /
  ``expired_positive_count`` / ``expired_negative_count`` /
  ``expired_neutral_count`` — per-outcome tallies.
* ``mean_pnl_pct`` — AVG over non-NULL ``pnl_pct``.
* ``median_pnl_pct`` — SQLite has no built-in MEDIAN; computed in the
  Python service layer (`reliability.py`) instead. The view exposes
  NULL here as a stable placeholder so consumers can still SELECT
  the column without conditionals.
* ``hit_rate`` — ``hit_target_count / NULLIF(scored_predictions, 0)``.
* ``abstain_rate`` — ``neutral_predictions / NULLIF(total_predictions, 0)``
  where neutral_predictions is the count of underlying predictions with
  ``direction='neutral'`` (HOLD verdicts, qualitative flags, etc.). NOT
  the same as ``expired_neutral_count`` (which counts outcomes; an
  expired_neutral can come from a long/short call that landed flat).
* ``participation_penalty`` — ``1 - abstain_rate``. Multiplicative
  factor; an agent abstaining on 30% of its predictions sees a 0.7×
  participation penalty.
* ``last_evaluated_at`` — MAX(evaluated_at).
* ``rolling_30d_hit_rate`` — same hit_rate calc but filtered to
  outcomes with ``evaluated_at >= NOW() - 30d``.
* ``rolling_30d_mean_pnl`` — same mean_pnl calc but filtered to 30d.
* ``sample_size_warning`` — 1 if scored_predictions < 10, else 0.

Downgrade drops the view; the underlying tables (``predictions``,
``prediction_outcomes``, ``evaluation_method_registry``) remain.

SQLite limitations exercised
============================

* No PostgreSQL ``FILTER (WHERE …)`` — replaced with the ``SUM(CASE
  WHEN <pred> THEN 1 ELSE 0 END)`` idiom. Same semantics.
* No ``NOW()`` keyword — SQLite uses ``CURRENT_TIMESTAMP`` and
  ``datetime('now', '-30 days')``. The 30d window predicates wrap the
  column in ``datetime(evaluated_at)`` to normalise the stored value
  (SQLAlchemy writes ``YYYY-MM-DD HH:MM:SS.ffffff+00:00`` for
  ``DateTime(timezone=True)``; SQLite's ``datetime()`` strips the
  fractional seconds + TZ suffix to a clean ``YYYY-MM-DD HH:MM:SS``
  which lex-compares correctly against ``datetime('now', …)``).
  Without the ``datetime()`` wrapper the ``<=`` upper-bound check
  fails because ``"2026-05-29 12:00:00.123456+00:00"`` is
  lex-greater than ``"2026-05-29 12:00:00"`` — the trailing
  fractional + offset are non-empty.

  **UTC-canonical encoding contract (codex review IMPORTANT #4).**
  ``datetime()`` SQLite-side normalisation interprets the input as
  UTC when no offset is present; with a ``+00:00`` suffix it
  preserves the wall-clock minute (good — matches the implicit UTC
  semantics). With a NON-UTC offset (e.g. ``+05:30``) ``datetime()``
  would NOT shift to UTC — it would still strip the offset and lex-
  compare the local wall-clock time. The current evaluator writes
  UTC exclusively (see ``evaluator.py``'s
  ``now=datetime.now(timezone.utc)`` and the
  ``DateTime(timezone=True)`` column type). If a future writer
  ingests externally-evaluated outcomes with non-UTC timestamps,
  normalise to UTC at the write boundary, or this view's rolling
  window will silently mis-classify. There is no DB CHECK for this
  — it's a writer-side invariant.
* No window functions in ``CREATE VIEW`` context-of-trouble — SQLite
  3.25+ supports ``ROW_NUMBER() OVER`` inside a view; Argosy's
  baseline (>= 3.38, per ``config.py``) covers it. We use ROW_NUMBER
  for the per-family dedup so a single prediction enters the
  aggregation at most once regardless of method versions.

Rolling window note
===================

The 30d window uses ``evaluated_at`` (not ``event_at``) so a backfill
that scores 200 old Discord predictions in one batch immediately
appears in the rolling-30d hit_rate. Using ``event_at`` would have
backfilled predictions show up as "stale" forever (event_at is months
old) which defeats the consumer-feedback loop. This matches §5.5's
emphasis on decision latency.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0052_source_reliability_view"
down_revision: str | None = "0051_prediction_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The view definition. Kept as a module-level string so the downgrade /
# upgrade pair stays in sync and the SQL is greppable for codex review.
#
# Two CTEs:
#   1. ``dedup_outcomes`` — per (prediction_id, family) picks ONE outcome
#      row via ROW_NUMBER ordered by method_version DESC, evaluated_at
#      DESC, outcome id DESC. The first slot (rn=1) wins.
#   2. ``joined`` — joins the dedup'd outcomes back to predictions for
#      the source / user_id / direction columns needed by the GROUP BY +
#      abstain_rate calc.
#
# The final SELECT does all aggregation in one pass.
_VIEW_SQL = """
CREATE VIEW source_reliability AS
WITH dedup_outcomes AS (
    SELECT
        o.id            AS outcome_id,
        o.prediction_id AS prediction_id,
        o.outcome_kind  AS outcome_kind,
        o.pnl_pct       AS pnl_pct,
        o.evaluated_at  AS evaluated_at,
        o.evaluation_method AS evaluation_method,
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
),
joined AS (
    SELECT
        p.user_id        AS user_id,
        p.source         AS source,
        p.direction      AS direction,
        d.method_family  AS method_family,
        d.outcome_kind   AS outcome_kind,
        d.pnl_pct        AS pnl_pct,
        d.evaluated_at   AS evaluated_at,
        p.archived       AS archived
    FROM dedup_outcomes d
    JOIN predictions p ON p.id = d.prediction_id
    WHERE d.rn = 1
      AND p.archived = 0
)
SELECT
    user_id,
    source,
    method_family,

    COUNT(*) AS total_predictions,

    SUM(CASE WHEN outcome_kind != 'unparseable' THEN 1 ELSE 0 END)
        AS scored_predictions,

    SUM(CASE WHEN outcome_kind = 'unparseable' THEN 1 ELSE 0 END)
        AS unparseable_count,

    SUM(CASE WHEN outcome_kind = 'hit_target' THEN 1 ELSE 0 END)
        AS hit_target_count,

    SUM(CASE WHEN outcome_kind = 'hit_stop' THEN 1 ELSE 0 END)
        AS hit_stop_count,

    SUM(CASE WHEN outcome_kind = 'expired_positive' THEN 1 ELSE 0 END)
        AS expired_positive_count,

    SUM(CASE WHEN outcome_kind = 'expired_negative' THEN 1 ELSE 0 END)
        AS expired_negative_count,

    SUM(CASE WHEN outcome_kind = 'expired_neutral' THEN 1 ELSE 0 END)
        AS expired_neutral_count,

    -- Mean P/L over scored (non-unparseable, non-NULL pnl) outcomes.
    -- SQLite's AVG already ignores NULL so we just need to AVG the
    -- pnl_pct directly; unparseable rows have NULL pnl_pct by contract.
    AVG(pnl_pct) AS mean_pnl_pct,

    -- Median is computed in Python (SQLite has no MEDIAN). Stable
    -- placeholder NULL so consumers can SELECT the column without
    -- conditionals.
    CAST(NULL AS REAL) AS median_pnl_pct,

    -- Hit rate = hit_target / scored. NULLIF guards div-by-zero when a
    -- (user_id, source, family) tuple has only unparseable outcomes
    -- (coverage but no signal).
    CAST(SUM(CASE WHEN outcome_kind = 'hit_target' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(SUM(CASE WHEN outcome_kind != 'unparseable' THEN 1 ELSE 0 END), 0)
        AS hit_rate,

    -- Abstain rate over the underlying predictions' direction. HOLD
    -- verdicts (direction='neutral') are the spec §2.4 anti-hide-behind
    -- signal; high abstain_rate means an agent is over-using HOLD.
    CAST(SUM(CASE WHEN direction = 'neutral' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0)
        AS abstain_rate,

    -- Participation penalty = 1 - abstain_rate. A multiplicative dim
    -- factor consumers apply alongside hit_rate. An agent abstaining
    -- on 30% of its predictions sees a 0.7× participation penalty.
    1.0 - (
        CAST(SUM(CASE WHEN direction = 'neutral' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0)
    ) AS participation_penalty,

    MAX(evaluated_at) AS last_evaluated_at,

    -- 30d rolling hit_rate. Uses evaluated_at (NOT event_at) so a
    -- backfill of 200 old predictions appears immediately in the
    -- rolling window. See module docstring for rationale.
    --
    -- Codex review 2026-05-29 IMPORTANT #2: bounded BOTH sides
    -- (>= now-30d AND <= now). A future-dated evaluated_at (clock skew
    -- on a writer, a bug in a backfill that sets evaluated_at past
    -- now) would otherwise inflate "recent" metrics. The upper bound
    -- silently excludes such rows from the rolling window; they still
    -- count in the all-time aggregates.
    CAST(SUM(
        CASE
            WHEN outcome_kind = 'hit_target'
             AND datetime(evaluated_at) >= datetime('now', '-30 days')
             AND datetime(evaluated_at) <= datetime('now')
            THEN 1 ELSE 0
        END
    ) AS REAL)
    / NULLIF(SUM(
        CASE
            WHEN outcome_kind != 'unparseable'
             AND datetime(evaluated_at) >= datetime('now', '-30 days')
             AND datetime(evaluated_at) <= datetime('now')
            THEN 1 ELSE 0
        END
    ), 0) AS rolling_30d_hit_rate,

    -- 30d rolling mean P/L. SQLite AVG over a CASE column emits NULL
    -- when no rows match (because every CASE branch yielded NULL); we
    -- accept that — consumers treat NULL as "no recent data, use the
    -- all-time mean_pnl_pct as fallback". Upper-bound on
    -- evaluated_at per IMPORTANT #2 above.
    AVG(
        CASE
            WHEN datetime(evaluated_at) >= datetime('now', '-30 days')
             AND datetime(evaluated_at) <= datetime('now')
            THEN pnl_pct
            ELSE NULL
        END
    ) AS rolling_30d_mean_pnl,

    -- Sample size warning: hit_rate over fewer than 10 scored
    -- predictions is statistical noise. Consumers SHOULD treat the
    -- (user_id, source, family) row as "no signal" and fall back to
    -- a prior of 1.0× weight when this flag is 1.
    CASE
        WHEN SUM(CASE WHEN outcome_kind != 'unparseable' THEN 1 ELSE 0 END) < 10
        THEN 1 ELSE 0
    END AS sample_size_warning

FROM joined
GROUP BY user_id, source, method_family
"""


def upgrade() -> None:
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS source_reliability")
