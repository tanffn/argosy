"""Spine PRODUCER (Phase 2) — the decision ledger's three producers.

Operating-model spec §2A "the decision records — OBSERVED → VALIDATED → OUTCOME".
The ONLY sanctioned writers of ``observed_decision`` / ``validated_decision`` /
``validated_decision_outcome`` (+ its head). Three state-machine steps:

  * :func:`observe_decision` — ALWAYS writes an immutable observation. Allocates the
    next monotonic per-user ``ingress_seq`` RACE-SAFELY (a bounded retry loop that,
    on the ``UNIQUE(user_id, ingress_seq)`` conflict, rolls back the SAVEPOINT and
    re-reads MAX+1 — a concurrent observe is never LOST, Sol defect 2), freezes
    ``predictive_terms_at_birth`` with EXACTLY the 6 keys (a caller-omitted term is
    an EXPLICIT null), and derives ``validation_status_at_birth``.
  * :func:`promote_to_validated` — RELOADS the observation from the DB and validates
    against the STORED birth state (never the caller-supplied object, Sol defect 1):
    refuses unless stored ``validation_status_at_birth == 'gradable'`` AND stored
    ``birth_input_fingerprint == input_fingerprint``; requires a non-null
    ``input_validated_snapshot_id``.
  * :func:`append_outcome` — append-only grade; the idempotency UNIQUE key makes an
    identical retry a no-op (returns the existing row, catching the concurrent-append
    IntegrityError too, Sol defect 4b); a distinct grade supersedes the current head
    via an in-transaction CAS (rollback + raise on race). ``vs_benchmark_delta`` is
    DEFERRED (left NULL; a later attribution phase derives it from the
    contribution_ledger).

The producers write inside a SAVEPOINT (``session.begin_nested``) and do NOT commit
the caller's session (Sol defect 3) — the caller owns the outer transaction.

Integration with the fleet verdict/proposal authoring is a LATER phase — this module
provides the clean producer API + tests only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from argosy.logging import get_logger

log = get_logger(__name__)

# The 6 keys EXACTLY, in a fixed order (spec §2A(a) predictive_terms_at_birth).
PREDICTIVE_TERM_KEYS: tuple[str, ...] = (
    "target_band",
    "alternative_at_birth",
    "stop",
    "falsifiers",
    "revisit_triggers",
    "evaluation_due_at",
)

# The PROSPECTIVE (hindsight-vulnerable) terms whose absence at birth makes a
# decision PERMANENTLY unscorable (spec §2A(b) "missing PREDICTIONS, forbidden").
PROSPECTIVE_TERM_KEYS: tuple[str, ...] = (
    "target_band",
    "alternative_at_birth",
    "stop",
)

STATUS_GRADABLE = "gradable"
STATUS_MISSING_TERM = "unvalidated:missing-predictive-term"
STATUS_DIRTY_BOOK = "unvalidated:dirty-book"

# Bounded retry on the ingress_seq UNIQUE race (defect 2).
_MAX_INGRESS_RETRIES = 8


class DecisionNotGradable(Exception):
    """Promotion refused: the observation was not gradable at birth (permanent)."""


class FingerprintMismatch(Exception):
    """Promotion refused: a different/later book cannot be back-attached."""


class ObservationNotFound(Exception):
    """Promotion refused: no stored observation for the supplied id."""


class OutcomeHeadRaced(Exception):
    """The outcome head moved under us (CAS lost); the transaction is rolled back."""


# ---------------------------------------------------------------------------
# Producer 1 — observe (ALWAYS writes, race-safe seq)
# ---------------------------------------------------------------------------
def _freeze_predictive_terms(predictive_terms: dict | None) -> dict:
    """Return an object with EXACTLY the 6 keys; a missing term is an explicit null.

    Extra caller keys are dropped (the frozen object is the canonical 6-key shape),
    so a term absent at authoring is stored as ``None`` and can never be back-filled.
    """
    src = predictive_terms or {}
    return {k: src.get(k, None) for k in PREDICTIVE_TERM_KEYS}


def _derive_status(frozen_terms: dict, source_input_id: str | None) -> str:
    """Derive ``validation_status_at_birth`` from the frozen terms + input.

    Order matters: a missing PROSPECTIVE term is permanent unscorability and takes
    precedence over dirty-book (which is merely *currently* unvalidated). A non-null
    ``source_input_id`` means the decision was authored against a raw/diagnostic
    (unvalidated) snapshot — there is no validated_snapshot table yet, so its
    presence is the dirty-book signal (spec §2A(a) PRE-VALIDATION path).
    """
    if any(frozen_terms.get(k) is None for k in PROSPECTIVE_TERM_KEYS):
        return STATUS_MISSING_TERM
    if source_input_id is not None:
        return STATUS_DIRTY_BOOK
    return STATUS_GRADABLE


def _next_ingress_seq(session, user_id: str) -> int:
    """MAX(ingress_seq)+1 for ``user_id`` — the seq allocator (patchable for tests)."""
    from sqlalchemy import func, select

    from argosy.state.models import ObservedDecision

    prior_max = int(
        session.execute(
            select(func.max(ObservedDecision.ingress_seq)).where(
                ObservedDecision.user_id == user_id
            )
        ).scalar_one_or_none()
        or 0
    )
    return prior_max + 1


def observe_decision(
    session,
    user_id: str,
    *,
    subject: str,
    action: str,
    decision_kind: str,
    predictive_terms: dict | None,
    source_input_id: str | None = None,
    input_fingerprint: str,
    conviction: str | None = None,
    source_decision_run_id: int | None = None,
):
    """Write an immutable :class:`ObservedDecision`. ALWAYS writes.

    Race-safe ``ingress_seq``: allocate MAX+1 and INSERT inside a SAVEPOINT; on the
    ``UNIQUE(user_id, ingress_seq)`` conflict (a concurrent observe grabbed the same
    seq), roll back the savepoint, re-read MAX+1, and retry — so a concurrent observe
    is never LOST (defect 2). Does NOT commit the caller's session (defect 3).

    ``source_decision_run_id`` (migration 0100) is the durable IDENTITY of a
    per-ticker fleet decision — the fleet run, not the book. A partial UNIQUE index
    on ``(user_id, subject, decision_kind, source_decision_run_id)`` makes a caller's
    re-record of the same run idempotent; the caller catches that conflict and
    re-selects (the run identity, unlike ``ingress_seq``, is caller-supplied so it is
    NOT retried here — a genuine run-collision must surface, not silently re-seq).
    """
    from argosy.state.models import ObservedDecision

    frozen = _freeze_predictive_terms(predictive_terms)
    status = _derive_status(frozen, source_input_id)

    last_err: Exception | None = None
    for _attempt in range(_MAX_INGRESS_RETRIES):
        next_seq = _next_ingress_seq(session, user_id)
        observed = ObservedDecision(
            user_id=user_id,
            subject=subject,
            action=action,
            decision_kind=decision_kind,
            conviction=conviction,
            ingress_seq=next_seq,
            predictive_terms_at_birth=frozen,
            validation_status_at_birth=status,
            observed_source_input_id=source_input_id,
            birth_input_fingerprint=input_fingerprint,
            source_decision_run_id=source_decision_run_id,
            authored_at=datetime.now(timezone.utc),
        )
        try:
            with session.begin_nested():  # SAVEPOINT — flushes on release
                session.add(observed)
        except IntegrityError as exc:
            # Only the ``ingress_seq`` UNIQUE race is retried (re-seq). Any OTHER
            # UNIQUE (the caller-supplied ``source_decision_run_id`` run-identity
            # index, migration 0100) is NOT a seq race — re-seqing would loop
            # forever, so surface it so the caller can re-select the winner.
            if observed in session:
                session.expunge(observed)
            # Inspect the RAW DB message (``exc.orig``) — the SQLAlchemy str echoes
            # the whole INSERT column list (which always names ``ingress_seq``), so
            # only the driver message distinguishes WHICH unique constraint failed.
            raw = str(getattr(exc, "orig", exc))
            if "ingress_seq" not in raw:
                raise
            last_err = exc  # UNIQUE(user_id, ingress_seq) lost the race — retry
            continue
        log.info(
            "spine.decision.observed",
            user_id=user_id,
            observed_decision_id=observed.id,
            subject=subject,
            decision_kind=decision_kind,
            ingress_seq=next_seq,
            validation_status_at_birth=status,
        )
        return observed

    raise RuntimeError(
        f"observe_decision: could not allocate ingress_seq for {user_id!r} after "
        f"{_MAX_INGRESS_RETRIES} attempts"
    ) from last_err


# ---------------------------------------------------------------------------
# Producer 2 — promote to validated (reloads DB state; gradable + fingerprint)
# ---------------------------------------------------------------------------
def promote_to_validated(
    session,
    observed_decision,
    *,
    input_validated_snapshot_id,
    input_fingerprint: str,
    verdict: str | None = None,
    conviction: str | None = None,
    instrument_stable_id: str | None = None,
    cost_basis_completeness: str | None = None,
    metadata_freshness: str | None = None,
    equivalence_evidence: dict | None = None,
    extra_validated_terms: dict | None = None,
):
    """Construct a :class:`ValidatedDecision` from the STORED birth state — or REFUSE.

    RELOADS the observation from the DB by its id (never trusts the caller-supplied
    object's mutable attributes, Sol defect 1) and validates against the stored row:
    refuses when the stored ``validation_status_at_birth`` is not ``gradable`` (a
    missing-predictive-term decision is PERMANENTLY unscorable) or when
    ``input_fingerprint`` differs from the stored ``birth_input_fingerprint`` (a
    different/later book cannot be back-attached). ``input_validated_snapshot_id`` is
    REQUIRED (NOT NULL). Grades against the birth-frozen terms only.
    """
    from sqlalchemy import select

    from argosy.state.models import ObservedDecision, ValidatedDecision

    if input_validated_snapshot_id is None:
        raise ValueError(
            "promote_to_validated: input_validated_snapshot_id is required — a "
            "promotion must name the validated book it is graded against"
        )

    observed_id = getattr(observed_decision, "id", None)
    if observed_id is None:
        raise ObservationNotFound(
            "promote_to_validated: observed_decision has no id to reload"
        )

    # Reload the AUTHORITATIVE stored row — ignore the passed object entirely
    # (populate_existing overwrites any stale/forged identity-map attributes).
    stored = session.execute(
        select(ObservedDecision)
        .where(ObservedDecision.id == observed_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if stored is None:
        raise ObservationNotFound(
            f"promote_to_validated: no observed_decision with id {observed_id!r}"
        )

    if stored.validation_status_at_birth != STATUS_GRADABLE:
        raise DecisionNotGradable(
            f"observed_decision {stored.id} is "
            f"{stored.validation_status_at_birth!r} at birth (stored) — permanently "
            "unscorable; no validated_decision can be constructed"
        )
    if input_fingerprint != stored.birth_input_fingerprint:
        raise FingerprintMismatch(
            f"input_fingerprint does not match the STORED birth fingerprint of "
            f"observed_decision {stored.id}; a different/later book cannot be "
            "back-attached"
        )

    birth_terms = dict(stored.predictive_terms_at_birth or {})
    validated_terms = dict(birth_terms)
    validated_terms.update(
        {
            "verdict": verdict,
            "conviction": conviction if conviction is not None else stored.conviction,
            "instrument_stable_id": instrument_stable_id,
        }
    )
    if extra_validated_terms:
        validated_terms.update(extra_validated_terms)

    validated = ValidatedDecision(
        observed_decision_id=stored.id,
        user_id=stored.user_id,
        input_validated_snapshot_id=str(input_validated_snapshot_id),
        instrument_stable_id=instrument_stable_id,
        decision_kind=stored.decision_kind,
        verdict=verdict,
        conviction=conviction if conviction is not None else stored.conviction,
        cost_basis_completeness=cost_basis_completeness,
        metadata_freshness=metadata_freshness,
        equivalence_evidence=equivalence_evidence,
        validated_terms=validated_terms,
        authored_at=datetime.now(timezone.utc),
    )
    with session.begin_nested():
        session.add(validated)

    log.info(
        "spine.decision.validated",
        observed_decision_id=stored.id,
        validated_decision_id=validated.id,
        input_validated_snapshot_id=validated.input_validated_snapshot_id,
    )
    return validated


# ---------------------------------------------------------------------------
# Producer 3 — append outcome (append-only + idempotent + head CAS)
# ---------------------------------------------------------------------------
def _select_outcome_by_key(
    session,
    decision_id: int,
    evaluation_window_id: str,
    benchmark_version: str,
    exposure_mapping_version: str,
    calculator_version: str,
):
    from sqlalchemy import select

    from argosy.state.models import ValidatedDecisionOutcome

    return session.execute(
        select(ValidatedDecisionOutcome).where(
            ValidatedDecisionOutcome.validated_decision_id == decision_id,
            ValidatedDecisionOutcome.evaluation_window_id == evaluation_window_id,
            ValidatedDecisionOutcome.benchmark_version == benchmark_version,
            ValidatedDecisionOutcome.exposure_mapping_version
            == exposure_mapping_version,
            ValidatedDecisionOutcome.calculator_version == calculator_version,
        )
    ).scalar_one_or_none()


def append_outcome(
    session,
    validated_decision,
    *,
    evaluation_window_id: str,
    benchmark_version: str,
    exposure_mapping_version: str,
    calculator_version: str,
    linking_algorithm_version: str | None = None,
    outcome_kind: str | None = None,
    post_mortem_category: str | None = None,
    regime: str | None = None,
    shadow: bool = False,
):
    """Append a :class:`ValidatedDecisionOutcome` and CAS-advance the head.

    Idempotent: an identical retry (same 5 calc-provenance keys) returns the
    EXISTING outcome without appending — including the concurrent case, where the
    insert loses on the idempotency UNIQUE and we re-select and return the winner
    (defect 4b), never raising to the caller. A distinct grade appends a new
    immutable row and CAS-advances the head — the first outcome creates the head
    (root, ``supersedes_outcome_id`` NULL); a later distinct grade carries
    ``supersedes_outcome_id = <prior head>`` and swaps the head from the prior id in
    the same SAVEPOINT (rollback + :class:`OutcomeHeadRaced` on a lost CAS). Old rows
    remain. ``vs_benchmark_delta`` stays NULL (DEFERRED). Does NOT commit the
    caller's session (defect 3).
    """
    from sqlalchemy import select, update

    from argosy.state.models import (
        ValidatedDecisionOutcome,
        ValidatedDecisionOutcomeHead,
    )

    decision_id = validated_decision.id

    # Fast path: an identical 5-key retry is a no-op — return the existing row.
    existing = _select_outcome_by_key(
        session, decision_id, evaluation_window_id, benchmark_version,
        exposure_mapping_version, calculator_version,
    )
    if existing is not None:
        return existing

    head = session.execute(
        select(ValidatedDecisionOutcomeHead).where(
            ValidatedDecisionOutcomeHead.validated_decision_id == decision_id
        )
    ).scalar_one_or_none()

    supersedes_id = None if head is None else head.current_outcome_id
    next_seq = 1 if head is None else head.seq + 1

    outcome = ValidatedDecisionOutcome(
        validated_decision_id=decision_id,
        evaluation_window_id=evaluation_window_id,
        benchmark_version=benchmark_version,
        exposure_mapping_version=exposure_mapping_version,
        calculator_version=calculator_version,
        linking_algorithm_version=linking_algorithm_version,
        outcome_kind=outcome_kind,
        post_mortem_category=post_mortem_category,
        regime=regime,
        shadow=shadow,
        vs_benchmark_delta=None,  # DEFERRED
        supersedes_outcome_id=supersedes_id,
        authored_at=datetime.now(timezone.utc),
    )
    try:
        with session.begin_nested():  # SAVEPOINT
            session.add(outcome)
            session.flush()  # assign outcome.outcome_id

            if head is None:
                # One root per decision — create the head in the same transaction.
                session.add(
                    ValidatedDecisionOutcomeHead(
                        validated_decision_id=decision_id,
                        current_outcome_id=outcome.outcome_id,
                        seq=next_seq,
                    )
                )
                session.flush()
            else:
                res = session.execute(
                    update(ValidatedDecisionOutcomeHead)
                    .where(
                        ValidatedDecisionOutcomeHead.validated_decision_id
                        == decision_id,
                        # CAS: expected-old head id.
                        ValidatedDecisionOutcomeHead.current_outcome_id
                        == supersedes_id,
                    )
                    .values(current_outcome_id=outcome.outcome_id, seq=next_seq)
                )
                if res.rowcount != 1:
                    raise OutcomeHeadRaced(
                        f"validated_decision_outcome_head for decision {decision_id} "
                        f"moved (expected current {supersedes_id}); rolled back"
                    )
    except IntegrityError:
        # Concurrent idempotent append: the winner already committed the 5-key row.
        # Drop the rolled-back pending row so the re-select's autoflush can't
        # re-raise, then return the winner — the loser gets the existing row.
        if outcome in session:
            session.expunge(outcome)
        existing = _select_outcome_by_key(
            session, decision_id, evaluation_window_id, benchmark_version,
            exposure_mapping_version, calculator_version,
        )
        if existing is not None:
            return existing
        raise

    log.info(
        "spine.decision.outcome_appended",
        validated_decision_id=decision_id,
        outcome_id=outcome.outcome_id,
        seq=next_seq,
        supersedes_outcome_id=supersedes_id,
    )
    return outcome


__all__ = [
    "PREDICTIVE_TERM_KEYS",
    "PROSPECTIVE_TERM_KEYS",
    "STATUS_GRADABLE",
    "STATUS_MISSING_TERM",
    "STATUS_DIRTY_BOOK",
    "DecisionNotGradable",
    "FingerprintMismatch",
    "ObservationNotFound",
    "OutcomeHeadRaced",
    "observe_decision",
    "promote_to_validated",
    "append_outcome",
]
