"""State-observer flag writer — Spec B commit #6 (§4).

Consumes ``FlagCandidate`` objects emitted by ``StateObserverAgent``
and writes ``monitor_flags`` rows with deterministic dedup_keys,
tombstone-then-insert idempotency, and a per-candidate error envelope
so one bad row never breaks the batch.

Architectural surface
=====================

This module is the ONLY place in Argosy that translates LLM-emitted
observer judgments into persisted rows. Three responsibilities:

  1. **Map ``primary_field`` → ``inferred_kind``** via a deterministic
     prefix-table (§4.2). This is the lone bit of pre-coded domain
     semantics in the whole pipeline — it is NOT detection (the
     observer already decided to flag), it's normalization for dedup.
     Any prefix the table doesn't cover falls through to
     ``state_observer_other_observation``; new state-snapshot fields
     therefore don't crash the writer, they just dedupe coarser
     until the table is extended.

  2. **Bridge ``deviation_bucket`` label families.** The
     ``state_diff.compute_deviation_bucket`` helper emits brief labels
     (``"<5pct"`` / ``"5to15pct"`` / ``"15to30pct"`` / ``">30pct"``)
     per the commit #3 brief. The spec §4.2 + the LLM prompt
     (commit #4 Appendix B.1) use ``"small"`` / ``"moderate"`` /
     ``"large"`` / ``"extreme"``. ``FlagCandidate.deviation_bucket``
     is typed as the latter via a Literal, but defensive code accepts
     either family — see ``_normalize_bucket`` — because a real LLM
     might also emit a brief label if its training data leaked one.

  3. **Tombstone-then-insert for expired peers** (§4.3, writer-
     orchestrated). The migration 0049 partial-unique index is STRICT —
     ``(user_id, dedup_key)`` is unique whenever ``dedup_key IS NOT
     NULL AND acknowledged_at IS NULL``. SQLite forbids
     ``CURRENT_TIMESTAMP`` in partial-index WHERE predicates, so the
     migration cannot express "unique unless the peer is already
     expired". The writer enforces that contract here: BEFORE
     INSERT, ``UPDATE monitor_flags SET acknowledged_at = NOW()``
     against any unacknowledged row with the same dedup_key whose
     ``expires_at`` has passed. The tombstone moves the stale row
     out of the partial-unique scope, freeing the slot for the
     fresh INSERT.

     The writer ALSO defensively skips inserting when an
     unacknowledged-and-unexpired peer already exists (a duplicate
     fire within the same TTL). The DB constraint would reject the
     INSERT regardless; pre-checking lets us count it cleanly as
     ``deduplicated`` and avoid the noise of an IntegrityError
     traceback at every dedup hit.

Idempotency contract (§4.3) — three branches:

  +------------------+------------------------+---------------------+
  | Peer state       | Writer decision        | DB enforcement      |
  +------------------+------------------------+---------------------+
  | active           | SKIP (deduplicated++)  | Partial-unique idx  |
  | (unack +         |                        | rejects duplicate   |
  | unexpired)       |                        | insert (safety net) |
  +------------------+------------------------+---------------------+
  | expired          | TOMBSTONE + INSERT     | After tombstone the |
  | (unack +         | (tombstoned++ /        | old row is out of   |
  | past expires_at) | written++)             | partial-unique      |
  |                  |                        | scope; INSERT OK    |
  +------------------+------------------------+---------------------+
  | acknowledged     | SKIP (deduplicated++)  | DB allows (idx      |
  | (ack'd at any    | — re-firing is noise   | excludes ack'd      |
  | time)            | until the dedup_key    | rows); writer       |
  |                  | changes (typically     | enforces the skip   |
  |                  | via a bucket move)     |                     |
  +------------------+------------------------+---------------------+

Error envelope (per-candidate, never break the batch)
=====================================================

Every candidate goes through its own try/except. A failure on
candidate N (e.g. the LLM hallucinated an inferred_kind that fails
the CHECK constraint, or a downstream INSERT raises IntegrityError
for any reason) is captured in ``WriteSummary.errors`` and the loop
moves on to candidate N+1. The session is rolled back to the
last successful commit before the next attempt so one bad row never
poisons the rest.

Cross-references
================

  * Migration: ``alembic/versions/0049_state_snapshots_and_monitor_flags.py``
  * Spec: ``docs/superpowers/specs/2026-05-29-state-observer-agent-design.md``
    §4 (this module) + §4.2 (inferred_kind table) + §4.3 (idempotency)
  * Caller (commit #7 — sibling): the daily ``StateObserverLoop`` invokes
    ``StateObserverAgent.run`` → ``write_observer_flags``. The backfill
    script (commit #5 — sibling) uses the same entry point with
    ``trigger_reason='backfill'``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from argosy.state.models import MonitorFlag

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

    from argosy.agents.state_observer import FlagCandidate


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dedup-key formula version (§4.2). Bump if the formula changes so a
#: prompt iteration in the agent doesn't retroactively collide with
#: previously-written observer flags.
DEDUP_KEY_VERSION: str = "v1"

#: Spec §4.3 default TTL — 7 days. After expiry, the same dedup_key can
#: re-fire (via the writer's tombstone-then-insert path).
DEFAULT_OBSERVER_FLAG_TTL_DAYS: int = 7

#: §4.2 ``primary_field`` prefix → ``inferred_kind`` (un-prefixed). The
#: writer concatenates ``state_observer_`` in front when persisting to
#: ``monitor_flags.kind``. Order matters: longer / more-specific
#: prefixes MUST appear before shorter ones so e.g.
#: ``portfolio.top_concentration_pct`` matches ``concentration_observation``
#: before falling through to ``portfolio.*`` shapes.
#:
#: The 12 kinds enumerated here MUST stay a subset of the CHECK
#: constraint enum in migration 0049 (``_OBSERVER_FLAG_KINDS``). Adding a
#: new kind requires a follow-on migration to extend the CHECK; until
#: then, the writer falls back to ``other_observation`` for unknown
#: prefixes — preventing silent CHECK-violation crashes.
_INFERRED_KIND_MAP: tuple[tuple[str, str], ...] = (
    # Concentration BEFORE generic portfolio.allocations / portfolio.positions
    # because the field name shares the ``portfolio.`` prefix and the more-
    # specific match should win.
    ("portfolio.top_concentration_",    "concentration_observation"),
    ("portfolio.unallocated_cash_",     "cash_observation"),
    ("portfolio.cash_balances_",        "cash_observation"),
    ("portfolio.allocations",           "allocation_observation"),
    ("portfolio.positions",             "position_observation"),
    # macro.* — fx first (more specific), then rates, then equity.
    ("macro.fx_",                       "fx_observation"),
    ("macro.fed_funds_",                "rates_observation"),
    ("macro.treasury_",                 "rates_observation"),
    ("macro.sp500_",                    "equity_observation"),
    ("macro.nasdaq_",                   "equity_observation"),
    ("macro.vix",                       "volatility_observation"),
    # cashflow / tax / plan_inputs.
    ("cashflow_recent.",                "cashflow_observation"),
    ("tax_assumptions.",                "tax_observation"),
    ("plan_inputs.",                    "plan_assumption_observation"),
)

#: Fallback when no prefix matches. The CHECK constraint in migration
#: 0049 explicitly admits this so new snapshot fields never crash the
#: writer — they just dedupe coarser until ``_INFERRED_KIND_MAP`` is
#: extended.
_FALLBACK_INFERRED_KIND: str = "other_observation"

#: Brief-label → spec-label translation for ``deviation_bucket`` (the
#: §-3 commit bridge). Accepts both families so the LLM and the
#: deterministic helper round-trip cleanly. Anything not in this table
#: AND not already a spec label is normalised to ``"small"`` (the
#: lowest band — safe default; will not over-dedup).
_BUCKET_BRIDGE: dict[str, str] = {
    # Brief labels from state_diff.compute_deviation_bucket.
    "<5pct":      "small",
    "5to15pct":   "moderate",
    "15to30pct":  "large",
    ">30pct":     "extreme",
    # Spec labels — pass through unchanged.
    "small":      "small",
    "moderate":   "moderate",
    "large":      "large",
    "extreme":    "extreme",
    # Categorical partition from state_diff for non-numeric deviations.
    "categorical": "categorical",
}

_SPEC_BUCKET_FALLBACK: str = "small"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteSummary:
    """Per-batch result of ``write_observer_flags``.

    Counters are mutually exclusive — each candidate increments
    exactly one of ``written_count`` / ``deduplicated_count`` /
    (``tombstoned_count`` increments alongside ``written_count`` when
    the write was preceded by a tombstone) / ``errors`` (length).

    Fields:
      written_count: number of fresh ``monitor_flags`` rows inserted.
      deduplicated_count: number of candidates skipped because an
        active or acknowledged peer with the same dedup_key already
        existed. Spec §4.3 branches (a) and (c).
      tombstoned_count: number of expired peers we acknowledged
        (set ``acknowledged_at = now``) to free the partial-unique
        slot before a fresh insert. Each tombstone PRECEDES a paired
        write — so this count is bounded above by ``written_count``.
      errors: list of (primary_field, error_message) pairs for
        candidates that failed. The batch always completes; failures
        don't propagate.
      written_flag_ids: the new ``monitor_flags.id`` values, in the
        order they were inserted. Useful for tests + observability
        without requiring a follow-up query.
    """

    written_count: int = 0
    deduplicated_count: int = 0
    tombstoned_count: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    written_flag_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable shape for logging / observability."""
        return {
            "written_count": self.written_count,
            "deduplicated_count": self.deduplicated_count,
            "tombstoned_count": self.tombstoned_count,
            "errors": [
                {"primary_field": pf, "error": err} for pf, err in self.errors
            ],
            "written_flag_ids": list(self.written_flag_ids),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_observer_flags(
    session: "Session",
    user_id: str,
    candidates: "Iterable[FlagCandidate]",
    *,
    snapshot_id: int,
    now: datetime | None = None,
    ttl_days: int = DEFAULT_OBSERVER_FLAG_TTL_DAYS,
) -> WriteSummary:
    """Persist ``FlagCandidate`` rows into ``monitor_flags``.

    Per-candidate flow:

      1. Map ``primary_field`` → ``inferred_kind`` via
         ``_INFERRED_KIND_MAP`` (falls back to ``other_observation``).
      2. Bridge ``deviation_bucket`` via ``_BUCKET_BRIDGE`` so a brief
         label like ``"5to15pct"`` and the spec label ``"moderate"``
         hash to the same dedup_key partition.
      3. Compose the dedup_key:
         ``v1|state_observer|<user>|<inferred_kind>|<primary_field>|<bucket>``
      4. Tombstone any unacknowledged peer with the same dedup_key
         whose ``expires_at`` has passed. The tombstone is committed
         immediately so the subsequent INSERT sees the cleared slot.
      5. Skip the INSERT if an unacknowledged-AND-unexpired peer still
         exists (active dedup) — counts as ``deduplicated``.
      6. Skip the INSERT if an acknowledged peer with the same
         dedup_key exists (re-firing is noise until the bucket moves)
         — counts as ``deduplicated``.
      7. INSERT a fresh row with ``kind = state_observer_<inferred_kind>``.
         The payload carries rationale_md / primary_field /
         related_fields / mitigation_hint / snapshot_id /
         deviation_bucket — both the LLM's bucket label and the
         normalised spec label — plus the LLM's audit ``validator_actions``.

    Args:
      session: live SQLAlchemy Session. Caller owns the outer
        transaction; this function commits after each successful
        candidate so a later failure doesn't roll back already-
        landed rows.
      user_id: the tenant whose monitor_flags row we're writing.
      candidates: iterable of ``FlagCandidate`` (typically the
        post-validated output of ``StateObserverAgent.run``).
      snapshot_id: the ``state_snapshots.id`` whose diff produced
        these candidates. Stored in the payload for traceability;
        the UI / future re-renders can pull diff_evidence from
        that snapshot.
      now: override for tests + deterministic backfills. Defaults
        to ``datetime.now(timezone.utc)``.
      ttl_days: TTL for the inserted rows. Defaults to 7 (§4.3).

    Returns:
      ``WriteSummary`` capturing all four counters. The summary is
      authoritative even if individual candidates raised — failures
      go in ``errors``, not propagating.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl_days) if ttl_days > 0 else None

    written_count = 0
    deduplicated_count = 0
    tombstoned_count = 0
    errors: list[tuple[str, str]] = []
    written_flag_ids: list[int] = []

    for cand in candidates:
        primary_field = getattr(cand, "primary_field", None) or "<unknown>"
        try:
            inferred_kind = infer_kind_from_field(primary_field)
            kind = f"state_observer_{inferred_kind}"

            raw_bucket = getattr(cand, "deviation_bucket", None) or "small"
            spec_bucket = _normalize_bucket(raw_bucket)

            dedup_key = build_dedup_key(
                user_id=user_id,
                inferred_kind=inferred_kind,
                primary_field=primary_field,
                deviation_bucket=spec_bucket,
            )

            # --- Branch order is load-bearing (§4.3) -----------------
            # 1. ACTIVE (unack + unexpired) peer    → skip (branch a)
            # 2. EXPIRED (unack + past expires_at) → tombstone + write
            #    (branch b)
            # 3. USER-ACKNOWLEDGED peer            → skip (branch c)
            #
            # We must check the active branch BEFORE tombstoning —
            # otherwise an unrelated active row with this dedup_key
            # would be missed (it has acknowledged_at IS NULL but
            # expires_at IS NULL OR > now, so the tombstone UPDATE
            # would NOT touch it). And we must check the
            # user-acknowledged branch AFTER deciding tombstone-vs-
            # nothing — otherwise our own tombstones look like
            # user-acknowledgments and suppress the re-fire we just
            # made room for.
            #
            # Implementation: snapshot the "user-acknowledged"
            # decision BEFORE the tombstone UPDATE runs, then process
            # the tombstone+insert flow. The "ack-peer existed before
            # we started" predicate captures branch (c) without
            # mistaking our own tombstones for user dismissals.

            if _active_peer_exists(
                session,
                user_id=user_id,
                dedup_key=dedup_key,
                now=now,
            ):
                deduplicated_count += 1
                continue

            user_ack_peer_exists_before = _acknowledged_peer_exists(
                session,
                user_id=user_id,
                dedup_key=dedup_key,
            )

            tombstones_now = _tombstone_expired_peers(
                session,
                user_id=user_id,
                dedup_key=dedup_key,
                now=now,
            )

            # §4.3 branch (b) trumps branch (c): if we just tombstoned
            # an expired peer, write the fresh row even if older
            # user-acknowledged peers exist (the user acknowledged a
            # different, now-expired fire — that ack doesn't carry
            # forward across a TTL boundary). Without this priority,
            # an old user-ack would suppress every future re-fire for
            # the same dedup_key, which the spec explicitly does NOT
            # want (the bucket-moves-only re-fire rule applies to
            # NEVER-EXPIRED peers).
            if tombstones_now == 0 and user_ack_peer_exists_before:
                # §4.3 branch (c): user dismissed; skip.
                deduplicated_count += 1
                continue

            # --- INSERT the fresh row -------------------------------
            payload_dict = _build_payload(
                cand=cand,
                primary_field=primary_field,
                snapshot_id=snapshot_id,
                normalised_bucket=spec_bucket,
                raw_bucket=raw_bucket,
            )
            row = MonitorFlag(
                user_id=user_id,
                kind=kind,
                severity=str(cand.severity),
                payload=json.dumps(payload_dict, default=str),
                surfaced_at=now,
                expires_at=expires_at,
                dedup_key=dedup_key,
            )
            session.add(row)
            try:
                session.flush()  # surfaces IntegrityError / CHECK violation NOW
            except IntegrityError as exc:
                # Either the dedup_key collided despite our preflight
                # (race: another writer landed between our SELECT and
                # this INSERT — should not happen in single-writer
                # Argosy but the DB index is the floor) OR the CHECK
                # rejected an out-of-enum kind. Treat both as a recover-
                # able per-candidate failure.
                session.rollback()
                msg = _short_exc(exc)
                if _looks_like_dedup_violation(msg):
                    # Race-loser: count as deduplicated, NOT an error.
                    deduplicated_count += 1
                    tombstoned_count += tombstones_now
                else:
                    errors.append((primary_field, msg))
                    _log.warning(
                        "state_observer_flag_writer.integrity_error",
                        extra={
                            "primary_field": primary_field,
                            "kind": kind,
                            "error": msg,
                        },
                    )
                continue

            session.commit()
            written_count += 1
            tombstoned_count += tombstones_now
            if row.id is not None:
                written_flag_ids.append(int(row.id))

            # Spec C commit #3 — predictions ledger writer wiring. GATE
            # on actionable severity (>= warning) per spec §2.4: info-
            # band observer fires are noise and skipped at the ledger.
            _maybe_write_observer_prediction(
                session,
                user_id=user_id,
                observer_flag_row=row,
                primary_field=primary_field,
                severity=str(cand.severity),
                deviation_bucket=spec_bucket,
                now=now,
            )

        except SQLAlchemyError as exc:  # noqa: PERF203 — per-cand guard
            # Catch-all for non-Integrity DB errors (e.g. operational
            # errors, programming errors). Roll back the bad partial
            # state so the next candidate starts clean.
            session.rollback()
            msg = _short_exc(exc)
            errors.append((primary_field, msg))
            _log.warning(
                "state_observer_flag_writer.sqlalchemy_error",
                extra={
                    "primary_field": primary_field,
                    "error": msg,
                },
            )
        except Exception as exc:  # noqa: BLE001 — never break the batch
            session.rollback()
            msg = _short_exc(exc)
            errors.append((primary_field, msg))
            _log.warning(
                "state_observer_flag_writer.unexpected_error",
                extra={
                    "primary_field": primary_field,
                    "error": msg,
                },
            )

    return WriteSummary(
        written_count=written_count,
        deduplicated_count=deduplicated_count,
        tombstoned_count=tombstoned_count,
        errors=errors,
        written_flag_ids=written_flag_ids,
    )


# ---------------------------------------------------------------------------
# Helpers (public so tests can pin contracts directly)
# ---------------------------------------------------------------------------


def infer_kind_from_field(primary_field: str) -> str:
    """Map ``primary_field`` to its ``inferred_kind`` (un-prefixed).

    Implements the §4.2 mapping table. Unknown prefixes fall through
    to ``other_observation`` — by design, so new snapshot fields don't
    crash the writer.

    Args:
      primary_field: e.g. ``"macro.fx_usd_nis_spot"``.

    Returns:
      The un-prefixed kind: e.g. ``"fx_observation"``.
      ``"other_observation"`` for unmatched prefixes (including
      ``None`` / empty inputs).
    """
    if not primary_field:
        return _FALLBACK_INFERRED_KIND
    pf = str(primary_field)
    for prefix, kind in _INFERRED_KIND_MAP:
        if pf.startswith(prefix):
            return kind
    return _FALLBACK_INFERRED_KIND


def build_dedup_key(
    *,
    user_id: str,
    inferred_kind: str,
    primary_field: str,
    deviation_bucket: str,
) -> str:
    """Compose the §4.2 dedup_key.

    Formula:
      ``v1|state_observer|<user_id>|<inferred_kind>|<primary_field>|<bucket>``

    The version prefix lets a future formula change opt in to fresh
    re-fires without retroactively breaking existing keys.

    All five components are joined by ``|``; we deliberately do NOT
    URL-encode or escape — the inputs are operator-controlled
    (``user_id`` from the auth surface, ``inferred_kind`` from the
    closed-set map, ``deviation_bucket`` from the closed-set bridge,
    ``primary_field`` from the LLM but validated by the post-validator
    against known field_paths). In normal operation a ``|`` cannot
    reach any component:

      * ``user_id`` is a slug from the auth surface.
      * ``inferred_kind`` is from ``_INFERRED_KIND_MAP``'s closed set.
      * ``deviation_bucket`` is from ``_BUCKET_BRIDGE``'s closed set.
      * ``primary_field`` is post-validated against the diff's
        known field paths (which are Python-identifier-shaped).

    But the post-validator failure modes (a future regression, a
    bypass) could conceivably leak a ``|`` into ``primary_field``,
    and silently producing an ambiguous dedup_key would let two
    different (kind, field, bucket) tuples collide. Codex round-2
    IMPORTANT #2: enforce the invariant explicitly. Any ``|`` in
    any component raises ``ValueError`` — the writer's per-candidate
    try/except converts that into a captured error in WriteSummary,
    so one bad candidate doesn't poison the batch.
    """
    components = (
        DEDUP_KEY_VERSION,
        "state_observer",
        str(user_id),
        str(inferred_kind),
        str(primary_field),
        str(deviation_bucket),
    )
    for c in components:
        if "|" in c:
            raise ValueError(
                f"dedup_key component contains illegal '|' character: {c!r}"
            )
    return "|".join(components)


def _normalize_bucket(bucket: str) -> str:
    """Bridge brief-label (commit #3) and spec-label (§4.2) families.

    Returns the spec label. Defaults to ``"small"`` for unrecognised
    inputs — chosen so a stray bucket value doesn't accidentally
    upgrade severity-of-dedup; a "small" partition is the lowest
    band and least likely to mis-suppress a future legitimate fire.

    **Canonicalisation (codex round-2 BLOCKER #1):** the raw input is
    lower-cased + stripped before the table lookup so ``"Moderate"`` /
    ``" moderate "`` / ``"MODERATE"`` all collapse to ``"moderate"``.
    Without this, an LLM emitting a capitalised label would fall
    through to the ``"small"`` fallback, producing an UNSTABLE
    dedup_key across runs (run A → "moderate" → bucket=moderate;
    run B → "Moderate" → bucket=small) — exactly the jitter the
    formula is meant to prevent. The brief-label keys (``"<5pct"`` /
    ``"5to15pct"`` / etc.) are already lowercase by convention; their
    lookup is unaffected.
    """
    norm = str(bucket).strip().lower() if bucket is not None else ""
    return _BUCKET_BRIDGE.get(norm, _SPEC_BUCKET_FALLBACK)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _maybe_write_observer_prediction(
    session: "Session",
    *,
    user_id: str,
    observer_flag_row: MonitorFlag,
    primary_field: str,
    severity: str,
    deviation_bucket: str,
    now: datetime,
) -> None:
    """Spec C commit #3 — emit a meta-prediction for actionable observer fires.

    Gates on ``severity in {warning, critical}`` per spec §2.4 — info
    band fires are noise and excluded from the ledger. The writer's
    own gate is a defensive second line.

    Idempotent on the observer flag's row id. Best-effort: any failure
    logs + swallows so a writer issue never breaks the flag-write batch.
    """
    if severity not in ("warning", "critical"):
        return
    if observer_flag_row.id is None:
        return
    try:
        from argosy.services.predictions.writers import (
            write_state_observer_prediction,
        )
        # Coerce deviation_bucket to the closed-set the writer accepts.
        bucket_in: str = str(deviation_bucket) if deviation_bucket else "small"
        if bucket_in not in ("small", "moderate", "large", "extreme", "categorical"):
            bucket_in = "small"
        # SAVEPOINT so a writer FK / CHECK failure (e.g. unseeded
        # evaluation_method_registry in legacy tests) doesn't undo the
        # observer-flag-writer's just-committed monitor_flags row.
        with session.begin_nested():
            write_state_observer_prediction(
                session,
                user_id,
                observer_flag_id=int(observer_flag_row.id),
                primary_field=primary_field,
                severity=severity,  # type: ignore[arg-type]
                deviation_bucket=bucket_in,  # type: ignore[arg-type]
                event_at=now,
            )
        session.commit()
    except Exception:  # noqa: BLE001 — never break observer batch
        _log.warning(
            "state_observer_flag_writer.predictions_write_failed",
            extra={
                "observer_flag_id": observer_flag_row.id,
                "primary_field": primary_field,
            },
            exc_info=True,
        )


def _tombstone_expired_peers(
    session: "Session",
    *,
    user_id: str,
    dedup_key: str,
    now: datetime,
) -> int:
    """Acknowledge any UNACKNOWLEDGED, EXPIRED peer with this dedup_key.

    The partial-unique index ix_monitor_flags_observer_dedup is STRICT
    (one unacknowledged row per dedup_key, regardless of expires_at).
    An expired peer that nobody acknowledged would still occupy that
    slot and reject a fresh insert. We tombstone it by stamping
    ``acknowledged_at = now`` — which moves it out of the partial-index
    scope, freeing the slot.

    Returns the number of rows tombstoned. Typically 0 or 1 in
    practice; the SQL is unbounded so a hypothetical broken state with
    multiple stale rows still resolves in one statement.

    Implementation note: SQLite stores DATETIME columns as naive text
    even when the inbound value carried tzinfo, so a tz-aware ``now``
    must be normalised to naive UTC before pushing it into the
    comparison and the UPDATE value. Otherwise SQLAlchemy raises
    ``can't compare offset-naive and offset-aware datetimes`` on the
    Python-side ``expires_at <= now`` evaluation against the rehydrated
    naive value. We strip tzinfo here (after re-anchoring to UTC if
    needed) so the query plan stays driver-portable; Postgres' aware
    TIMESTAMPTZ accepts the naive UTC value as if it were UTC, which
    is correct.
    """
    n = _to_naive_utc(now)
    stmt = (
        update(MonitorFlag)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.dedup_key == dedup_key)
        .where(MonitorFlag.acknowledged_at.is_(None))
        .where(MonitorFlag.expires_at.is_not(None))
        .where(MonitorFlag.expires_at <= n)
        .values(acknowledged_at=n)
    )
    # Use the synchronize_session='fetch' default for clarity; for
    # large batches we'd switch to 'evaluate' but observer batches
    # are tiny (≤ a handful of candidates per run).
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def _active_peer_exists(
    session: "Session",
    *,
    user_id: str,
    dedup_key: str,
    now: datetime,
) -> bool:
    """True iff an unacknowledged + (unexpired or no expiry) peer exists.

    "Active" = exactly the predicate the partial-unique index would
    enforce IF SQLite admitted ``CURRENT_TIMESTAMP`` in partial-index
    predicates. We evaluate it here in application code instead.

    See ``_tombstone_expired_peers`` for the tz-normalisation
    rationale — same naive-UTC normalisation applies.
    """
    n = _to_naive_utc(now)
    stmt = (
        select(MonitorFlag.id)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.dedup_key == dedup_key)
        .where(MonitorFlag.acknowledged_at.is_(None))
        .where(
            (MonitorFlag.expires_at.is_(None))
            | (MonitorFlag.expires_at > n)
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def _to_naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo after re-anchoring to UTC.

    SQLite's DATETIME column type roundtrips through a naive string;
    rehydrated values lose their tzinfo. Mixing tz-aware ``now`` from
    Python with naive stored values in a comparison raises. Normalising
    the Python side to naive-UTC is the path of least surprise — and
    matches the semantics SQLite already assumed.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _acknowledged_peer_exists(
    session: "Session",
    *,
    user_id: str,
    dedup_key: str,
) -> bool:
    """True iff an acknowledged peer with the same dedup_key exists.

    §4.3 branch (c): "If an ACKNOWLEDGED flag with the same dedup_key
    exists, skip (the user already saw and dismissed; re-firing is
    noise — until the dedup_key changes via deviation_bucket)."
    """
    stmt = (
        select(MonitorFlag.id)
        .where(MonitorFlag.user_id == user_id)
        .where(MonitorFlag.dedup_key == dedup_key)
        .where(MonitorFlag.acknowledged_at.is_not(None))
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def _build_payload(
    *,
    cand: "FlagCandidate",
    primary_field: str,
    snapshot_id: int,
    normalised_bucket: str,
    raw_bucket: str,
) -> dict[str, Any]:
    """Build the JSON payload stored in ``monitor_flags.payload``.

    Mirrors the shape called out in spec §4.3 ("MonitorFlag.payload
    JSON for observer flags carries: ...") with two practical
    extensions:

      * ``deviation_bucket_llm`` — the raw label the LLM emitted, for
        audit. The dedup_key is built from the NORMALISED value, but
        the LLM's choice is preserved so we can diff "what the LLM
        thought" vs "what the bridge translated to."
      * ``validator_actions`` — passthrough of the post-validator's
        audit list (e.g. ``"pruned_related_field: foo"`` entries from
        ``StateObserverAgent._post_validate_output``).

    The full snapshot_id is included; ``diff_evidence`` lookup is
    deferred to the renderer (the renderer can re-fetch from
    state_snapshots / state_diff cheaply, and embedding the diff
    rows here would inflate the payload by the field-count multiplier).
    """
    return {
        "snapshot_id": int(snapshot_id),
        "primary_field": primary_field,
        "related_fields": list(getattr(cand, "related_fields", None) or []),
        "rationale_md": getattr(cand, "rationale_md", ""),
        "mitigation_hint": getattr(cand, "mitigation_hint", None),
        "deviation_bucket": normalised_bucket,
        "deviation_bucket_llm": raw_bucket,
        "observer_confidence": (
            getattr(cand.confidence, "value", None)
            if getattr(cand, "confidence", None) is not None
            else None
        ),
        "validator_actions": list(
            getattr(cand, "validator_actions", None) or []
        ),
    }


def _looks_like_dedup_violation(msg: str) -> bool:
    """Heuristic: did this IntegrityError come from the partial-unique idx?

    SQLAlchemy's IntegrityError stringifies as
    ``"<driver>: <sqlite/pg error phrase>\\n[SQL: <INSERT statement>]\\n[parameters: ...]"``.
    The INSERT statement BODY contains the column-name list — which on
    monitor_flags includes ``dedup_key`` — so a naive ``"dedup_key" in
    msg`` substring match returns true for ANY IntegrityError on the
    table, including CHECK violations on unrelated columns. We must
    look at the driver's ERROR PHRASE only, not the SQL body.

    Strategy: extract the part before ``[SQL:`` (or before ``[parameters:``
    if no SQL block is present) and pattern-match the canonical
    UNIQUE-violation phrases against that prefix only.

    Recognised phrases:
      * SQLite: ``"UNIQUE constraint failed: monitor_flags.dedup_key"``
        or ``"... monitor_flags.user_id, monitor_flags.dedup_key"``.
      * Postgres: ``"duplicate key value violates unique constraint
        \"ix_monitor_flags_observer_dedup\""``.
    """
    lo = msg.lower()
    # Trim to the driver's error PHRASE only. SQLAlchemy appends one or
    # more bracketed blocks after the driver error — ``[sql: ...]`` /
    # ``[parameters: ...]`` / ``(background on this error: ...)`` /
    # ``(orig: ...)``. The driver's actual error text always precedes
    # the first such marker. Codex round-2 BLOCKER #2: the prior
    # implementation only trimmed on ``[sql:``, so a driver shape that
    # omits the SQL block (some async wrappers do this) would expose
    # the parameter list — which contains the column name ``dedup_key``
    # — to the phrase matcher, producing a false-positive dedup match
    # on unrelated INSERT failures.
    for marker in ("[sql:", "[parameters:", "(background on this error", "(orig:"):
        idx = lo.find(marker)
        if idx != -1:
            lo = lo[:idx]

    if "ix_monitor_flags_observer_dedup" in lo:
        return True
    if "unique constraint failed" in lo and "dedup_key" in lo:
        return True
    if "duplicate key" in lo and "ix_monitor_flags_observer_dedup" in lo:
        return True  # covered by the first arm; defensive
    return False


def _short_exc(exc: BaseException) -> str:
    """Trim an exception's message for the WriteSummary.errors list.

    Long SQLAlchemy stringifications (with full statement + parameters)
    bloat the summary log. We cap at 400 chars — enough to identify
    the failure mode, short enough to fit in a log line.
    """
    s = str(exc) or exc.__class__.__name__
    return s[:400]


__all__ = [
    "DEDUP_KEY_VERSION",
    "DEFAULT_OBSERVER_FLAG_TTL_DAYS",
    "WriteSummary",
    "build_dedup_key",
    "infer_kind_from_field",
    "write_observer_flags",
]
