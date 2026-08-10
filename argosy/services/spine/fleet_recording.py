"""Spine BRIDGE — record a settled per-ticker fleet verdict into the decision ledger.

The per-ticker decision fleet (``argosy/decisions/flow.py``) settles a verdict
(hold / buy / sell) and durably records it in the verdict registry
(``verdict_registry.write_verdict``). This module records that SAME settled verdict,
**alongside**, as an immutable Phase-2 spine ``observed_decision`` (operating-model
spec §2A "the decision records — OBSERVED → VALIDATED → OUTCOME").

Contract — the recording is strictly ADDITIVE and BEST-EFFORT:

  * It never changes what the fleet decides or how a verdict is computed.
  * Every failure is caught + logged, never propagated — a spine write must not
    break or alter the decision flow (:func:`record_fleet_decision_best_effort`).
  * It is idempotent per settled verdict, DB-enforced by identity = the fleet
    RUN: ``observed_decision.source_decision_run_id`` + the partial UNIQUE index
    ``(user_id, subject, decision_kind, source_decision_run_id)``. Re-recording
    the same run resolves to the prior observation (never a duplicate), and this
    is independent of snapshot-hash churn. ``birth_input_fingerprint`` is the
    INPUT COMMITMENT only (the authored-on book's content_hash, for promotion
    matching) — NOT identity.

Predictive-term honesty (spec §2A(b)): the fleet emits ``falsifiers`` and typed
``revisit_triggers`` — those map through. ``target_band`` / ``alternative_at_birth``
are NOT produced by the trader today and are passed as EXPLICIT null; ``stop`` maps
from the trader's ``stop_price`` when present. A verdict missing any PROSPECTIVE term
(target_band / alternative / stop) is recorded ``unvalidated:missing-predictive-term``
and is permanently unscorable — that is correct and honest, not a bug: a verdict
authored without a target / alternative / stop cannot be graded later.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from argosy.logging import get_logger
from argosy.services.spine.decisions import (
    STATUS_GRADABLE,
    observe_decision,
    promote_to_validated,
)

log = get_logger(__name__)

# decision_kind stamped on every fleet per-ticker verdict observation (spec §2A(a)).
FLEET_DECISION_KIND = "per_ticker_verdict"

# Sentinel input reference for a decision authored against an UNKNOWN/unvalidated
# book — a non-null observed_source_input_id so the producer classifies it
# "unvalidated:dirty-book" (gradable REQUIRES a current-PASS validated_snapshot).
NO_INPUT_SENTINEL = "no-input"


# ---------------------------------------------------------------------------
# fingerprint (INPUT commitment only — NOT identity) / predictive-term mapping
# ---------------------------------------------------------------------------
def _input_fingerprint(content_hash: str | None) -> str:
    """The birth INPUT commitment — the content-hash of the book authored against.

    This is what ``promote_to_validated`` matches on (a later/different book cannot
    back-attach), NOT the decision's identity: identity is the fleet run
    (``source_decision_run_id``), so a retry after a new snapshot arrived does NOT
    change identity even though this fingerprint changes. When no book is known the
    decision is dirty-book (never promotable) so a stable sentinel suffices.
    """
    return content_hash if content_hash else NO_INPUT_SENTINEL


def _predictive_terms(
    *,
    stop: Any,
    target_band: Any,
    alternative_at_birth: Any,
    falsifiers: list[str] | None,
    revisit_triggers: list[dict] | None,
    evaluation_due_at: Any,
) -> dict:
    """Map fleet output → the 6 spine predictive terms.

    A term the fleet does not author is an EXPLICIT null (the spine freezes it
    permanently). ``target_band`` / ``alternative_at_birth`` are commonly null
    today (the trader emits neither) → the observation is permanently unscorable,
    honestly. Empty falsifier / trigger lists are normalised to null so an
    author-less list is not mistaken for a present-but-empty prediction.
    """
    return {
        "target_band": target_band,
        "alternative_at_birth": alternative_at_birth,
        "stop": stop,
        "falsifiers": list(falsifiers) if falsifiers else None,
        "revisit_triggers": list(revisit_triggers) if revisit_triggers else None,
        "evaluation_due_at": evaluation_due_at,
    }


# ---------------------------------------------------------------------------
# idempotency selectors (module-level so they are patchable, like the spine tests)
# ---------------------------------------------------------------------------
def _select_by_run(session, user_id: str, subject: str, run_id: int):
    """The existing observation for this fleet run (run identity), or None."""
    from sqlalchemy import select

    from argosy.state.models import ObservedDecision

    return session.execute(
        select(ObservedDecision)
        .where(
            ObservedDecision.user_id == user_id,
            ObservedDecision.subject == subject,
            ObservedDecision.decision_kind == FLEET_DECISION_KIND,
            ObservedDecision.source_decision_run_id == run_id,
        )
        .limit(1)
    ).scalar_one_or_none()


def _select_by_fingerprint(session, user_id: str, subject: str, fingerprint: str):
    """Run-less fallback pre-check (NOT DB-enforced) — the degenerate path."""
    from sqlalchemy import select

    from argosy.state.models import ObservedDecision

    return session.execute(
        select(ObservedDecision)
        .where(
            ObservedDecision.user_id == user_id,
            ObservedDecision.subject == subject,
            ObservedDecision.decision_kind == FLEET_DECISION_KIND,
            ObservedDecision.source_decision_run_id.is_(None),
            ObservedDecision.birth_input_fingerprint == fingerprint,
        )
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# core recorder (caller owns the transaction — mirrors the spine producers)
# ---------------------------------------------------------------------------
def record_settled_fleet_decision(
    session,
    *,
    user_id: str,
    subject: str,
    action: str,
    conviction: str | None = None,
    decision_run_id: int | None = None,
    snapshot_id: int | None = None,
    content_hash: str | None = None,
    validated_snapshot_id: int | None = None,
    stop: float | None = None,
    target_band: Any = None,
    alternative_at_birth: Any = None,
    falsifiers: list[str] | None = None,
    revisit_triggers: list[dict] | None = None,
    evaluation_due_at: str | None = None,
):
    """Observe a settled per-ticker verdict in the decision ledger (idempotent).

    Writes an immutable ``observed_decision`` and, when the observation is gradable
    at birth AND ``validated_snapshot_id`` names a current-PASS validated book,
    promotes it to a ``validated_decision``. Does NOT commit — the caller owns the
    transaction (mirrors ``spine.decisions``). Returns the ObservedDecision
    (existing on a duplicate, new otherwise). May raise; the best-effort wrapper
    (:func:`record_fleet_decision_best_effort`) is what guarantees the fleet flow is
    never broken.
    """
    subj = (subject or "").strip().upper()
    act = (action or "").strip().lower()
    fp = _input_fingerprint(content_hash)

    # Idempotency (spec: exactly one observation per settled verdict/run). IDENTITY
    # is the fleet RUN, not the book — so a retry after a new snapshot arrived (a
    # different content-hash / fingerprint) still resolves to the SAME decision.
    # The pre-check is the fast path; the DB partial-unique index
    # (uq_observed_decision_run) is the race-safe backstop (below).
    if decision_run_id is not None:
        existing = _select_by_run(session, user_id, subj, decision_run_id)
        if existing is not None:
            log.info(
                "spine.fleet_decision.dup_skipped",
                user_id=user_id, subject=subj,
                observed_decision_id=existing.id, decision_run_id=decision_run_id,
            )
            return existing
    else:
        # No run id → no DB-enforced identity; fall back to a fingerprint pre-check
        # (best-effort, NOT race-safe — a run-less decision is the degenerate path).
        existing = _select_by_fingerprint(session, user_id, subj, fp)
        if existing is not None:
            return existing

    # ``observed_source_input_id`` doubles as the dirty-book signal in the producer:
    # a non-null value marks the decision authored against an UNVALIDATED book (→
    # non-gradable). ONLY a current-PASS validated_snapshot (validated_snapshot_id
    # set) yields a gradable/promotable observation; a raw book uses its snapshot id
    # and an UNKNOWN/absent/errored input uses the sentinel — both dirty-book, so a
    # verdict authored against an unvalidated input can NEVER be gradable.
    if validated_snapshot_id is not None:
        source_input_id = None
    elif snapshot_id is not None:
        source_input_id = str(snapshot_id)
    else:
        source_input_id = NO_INPUT_SENTINEL

    terms = _predictive_terms(
        stop=stop,
        target_band=target_band,
        alternative_at_birth=alternative_at_birth,
        falsifiers=falsifiers,
        revisit_triggers=revisit_triggers,
        evaluation_due_at=evaluation_due_at,
    )

    try:
        observed = observe_decision(
            session,
            user_id,
            subject=subj,
            action=act,
            decision_kind=FLEET_DECISION_KIND,
            predictive_terms=terms,
            source_input_id=source_input_id,
            input_fingerprint=fp,
            conviction=conviction,
            source_decision_run_id=decision_run_id,
        )
    except IntegrityError:
        # Race-safe backstop: a concurrent record of the SAME run lost the
        # uq_observed_decision_run unique index — re-select and return the winner
        # (mirrors the outcome-idempotency pattern), exactly-once per run.
        if decision_run_id is not None:
            existing = _select_by_run(session, user_id, subj, decision_run_id)
            if existing is not None:
                log.info(
                    "spine.fleet_decision.dup_raced",
                    user_id=user_id, subject=subj,
                    observed_decision_id=existing.id,
                    decision_run_id=decision_run_id,
                )
                return existing
        raise

    # Step 3 — optional promotion: only when the observation is GRADABLE at birth
    # (every prospective term present) AND the book is a current-PASS
    # validated_snapshot. Most fleet verdicts carry no target_band / alternative →
    # missing-predictive-term → permanently unscorable → left as an observation
    # (never force-promoted). Promotion failure is inert — the observation stands.
    if (
        observed.validation_status_at_birth == STATUS_GRADABLE
        and validated_snapshot_id is not None
    ):
        try:
            promote_to_validated(
                session,
                observed,
                input_validated_snapshot_id=validated_snapshot_id,
                input_fingerprint=fp,
                verdict=act.upper(),
                conviction=conviction,
            )
        except Exception as exc:  # noqa: BLE001 — promotion is optional; observe stands
            log.info(
                "spine.fleet_decision.promotion_skipped",
                subject=subj,
                error=str(exc)[:200],
            )

    return observed


# ---------------------------------------------------------------------------
# input resolution (best-effort) + the best-effort flow wrapper
# ---------------------------------------------------------------------------
def resolve_input_snapshot(
    session, user_id: str
) -> tuple[int | None, str | None, int | None]:
    """Best-effort: the latest raw portfolio snapshot the fleet reasoned against.

    Returns ``(snapshot_id, content_hash, validated_snapshot_id)``. ``content_hash``
    reuses ``compute_snapshot_content_hash``; ``validated_snapshot_id`` is set only
    when that snapshot is a current-PASS ``validated_snapshot`` (spec §2A read gate),
    which is what makes a gradable decision promotable. Any element is None on
    absence/failure. Never raises.
    """
    try:
        from sqlalchemy import select

        from argosy.services.spine.integrity import compute_snapshot_content_hash
        from argosy.services.spine.validated_snapshot import read_validated_snapshot
        from argosy.state.models import PortfolioSnapshotRow

        row = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.imported_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return (None, None, None)

        content_hash: str | None = None
        try:
            content_hash = compute_snapshot_content_hash(row)
        except Exception:  # noqa: BLE001 — hash is best-effort
            content_hash = None

        validated_id: int | None = None
        try:
            vs = read_validated_snapshot(session, user_id, row)
            if vs is not None:
                validated_id = vs.snapshot_id
        except Exception:  # noqa: BLE001 — validation lookup is best-effort
            validated_id = None

        return (row.id, content_hash, validated_id)
    except Exception:  # noqa: BLE001 — resolution must never crash the recorder
        return (None, None, None)


def record_fleet_decision_best_effort(
    *,
    user_id: str,
    subject: str,
    action: str,
    conviction: str | None = None,
    decision_run_id: int | None = None,
    stop: float | None = None,
    target_band: Any = None,
    alternative_at_birth: Any = None,
    falsifiers: list[str] | None = None,
    revisit_triggers: list[dict] | None = None,
    evaluation_due_at: str | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> Any | None:
    """Open a session, resolve the input book, record the verdict, commit — swallowing
    ALL failures. This is the exact seam the decision flow calls; it MUST never raise.

    ``session_factory`` is injectable for tests; production opens its own SQLite
    session (isolated from the caller's transaction, so a spine error can never
    poison the verdict-registry write). Returns the ObservedDecision, or None on any
    failure.
    """
    session = None
    try:
        if session_factory is None:
            import sqlalchemy as sa
            from sqlalchemy.orm import sessionmaker

            from argosy.state import db as db_mod

            url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
            session_factory = sessionmaker(
                bind=sa.create_engine(url, connect_args={"check_same_thread": False}),
                expire_on_commit=False,
            )
        session = session_factory()
        snapshot_id, content_hash, validated_snapshot_id = resolve_input_snapshot(
            session, user_id
        )
        observed = record_settled_fleet_decision(
            session,
            user_id=user_id,
            subject=subject,
            action=action,
            conviction=conviction,
            decision_run_id=decision_run_id,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            validated_snapshot_id=validated_snapshot_id,
            stop=stop,
            target_band=target_band,
            alternative_at_birth=alternative_at_birth,
            falsifiers=falsifiers,
            revisit_triggers=revisit_triggers,
            evaluation_due_at=evaluation_due_at,
        )
        session.commit()
        return observed
    except Exception as exc:  # noqa: BLE001 — recording must NEVER break the flow
        log.warning(
            "spine.fleet_decision.record_failed",
            user_id=user_id,
            subject=subject,
            error=str(exc)[:200],
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


__all__ = [
    "FLEET_DECISION_KIND",
    "record_settled_fleet_decision",
    "record_fleet_decision_best_effort",
    "resolve_input_snapshot",
]
