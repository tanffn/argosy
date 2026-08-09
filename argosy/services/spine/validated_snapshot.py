"""Spine GATE ACCESSOR — read a snapshot's positions ONLY through a pass verdict.

Operating-model spec §2A "validated_snapshot" + §3. This is the read-side gate:
:func:`read_validated_snapshot` returns a snapshot's positions **only if** the
current ``integrity_verdict_head`` for that snapshot points at a ``pass`` verdict
**and** that verdict's ``snapshot_content_hash`` still matches the current bytes
(a snapshot mutated after assessment no longer hash-matches and is REFUSED).

The returned :class:`ValidatedSnapshot` is honestly labelled ``proof_grade`` /
``diagnostic`` (spec §3): conservation passing is necessary but NOT sufficient
for proof-grade — that additionally needs independent-source reconciliation
(broker-signed manifest / account totals / expected-set completeness), which is
currently unavailable for every account, so every snapshot today is diagnostic.

This slice provides the accessor + ONE demonstrating call (a feature-flagged
consult inside ``current_book.load_current_book``). The ~19 raw readers are NOT
rewired here — that is the later migration backlog (spec §2A point 5).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from argosy.logging import get_logger
from argosy.services.holding_books import parse_positions_json
from argosy.services.spine.integrity import (
    RESULT_PASS,
    compute_snapshot_content_hash,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class ValidatedSnapshot:
    """The gated, provenance-carrying view of a snapshot's positions.

    Only ever constructed when the current-head verdict passed AND the content
    hash matches. ``proof_grade`` is ``True`` ONLY when the verdict's
    ``unavailable_checks`` (independent-source binding / broker-account-total
    reconciliation / expected-set completeness) are ALL satisfied — which today
    is NEVER, so every current snapshot is honestly ``diagnostic`` (spec §3). A
    small-account halving that only a broker-total cross-foot would catch is thus
    an honestly-flagged diagnostic limitation, not a silent proof-grade pass.
    """

    snapshot_id: int
    positions: list[Any]
    verdict_id: int
    content_hash: str
    proof_grade: bool
    unavailable_checks: tuple[str, ...] = field(default_factory=tuple)


def _fresh(session: Any, model: Any, stmt_where: Any):
    """Read a single row FRESH from the DB, overwriting any identity-map cache.

    ``session.get`` returns the (possibly stale) identity-map object under the
    repo's ``expire_on_commit=False`` sessions — a cached pass head could be
    served after the DB head advanced to a fail (defect 5). ``populate_existing``
    forces attributes to reflect the committed DB state at read time.
    """
    from sqlalchemy import select

    return session.execute(
        select(model).where(stmt_where).execution_options(populate_existing=True)
    ).scalar_one_or_none()


def read_validated_snapshot(
    session: Any, user_id: str, snapshot_row: Any
) -> ValidatedSnapshot | None:
    """Return the snapshot's positions ONLY through a pass-head + hash match.

    Returns ``None`` (caller treats as unavailable / degraded) when the snapshot
    is not owned by ``user_id``, there is no verdict head, the current-head
    verdict is cross-snapshot / cross-user / seq-mismatched / not ``pass``, or the
    committed ``snapshot_content_hash`` no longer equals the current bytes' hash.
    """
    from argosy.state.models import IntegrityVerdict, IntegrityVerdictHead

    snapshot_id = getattr(snapshot_row, "id", None)
    if snapshot_id is None:
        return None
    # Defect 4b: the caller may only read THEIR OWN snapshot.
    if getattr(snapshot_row, "user_id", None) != user_id:
        log.warning(
            "spine.gate.snapshot_owner_mismatch",
            snapshot_id=snapshot_id,
            snapshot_user_id=getattr(snapshot_row, "user_id", None),
            asked_user_id=user_id,
        )
        return None

    # Defect 5: read head + verdict FRESH from the committed DB state, never a
    # possibly-stale identity-map get().
    head = _fresh(session, IntegrityVerdictHead, IntegrityVerdictHead.snapshot_id == snapshot_id)
    if head is None:
        log.debug("spine.gate.no_head", snapshot_id=snapshot_id)
        return None

    verdict = _fresh(session, IntegrityVerdict, IntegrityVerdict.id == head.current_verdict_id)
    if verdict is None:
        log.warning(
            "spine.gate.head_dangling",
            snapshot_id=snapshot_id,
            current_verdict_id=head.current_verdict_id,
        )
        return None
    # Defect 4: never serve on a CROSS-SNAPSHOT or cross-user verdict, nor on a
    # head/verdict seq mismatch. The DB composite FK also refuses a cross-snapshot
    # head; these accessor checks are the belt to that suspenders.
    if verdict.snapshot_id != snapshot_id:
        log.warning(
            "spine.gate.cross_snapshot_verdict",
            snapshot_id=snapshot_id,
            verdict_id=verdict.id,
            verdict_snapshot_id=verdict.snapshot_id,
        )
        return None
    if verdict.user_id != user_id:
        log.warning(
            "spine.gate.user_mismatch",
            snapshot_id=snapshot_id,
            verdict_id=verdict.id,
            verdict_user_id=verdict.user_id,
            asked_user_id=user_id,
        )
        return None
    if head.seq != verdict.verdict_seq:
        log.warning(
            "spine.gate.head_seq_mismatch",
            snapshot_id=snapshot_id,
            verdict_id=verdict.id,
            head_seq=head.seq,
            verdict_seq=verdict.verdict_seq,
        )
        return None
    if verdict.result != RESULT_PASS:
        log.debug(
            "spine.gate.head_not_pass",
            snapshot_id=snapshot_id,
            result=verdict.result,
        )
        return None

    # Hash the WHOLE snapshot ROW (positions + snapshot_date + totals), matching
    # exactly what the producer committed — a mutated value_local / snapshot_date
    # / totals / review_status no longer matches and is refused (defects 2, 3).
    current_hash = compute_snapshot_content_hash(snapshot_row)
    positions = parse_positions_json(getattr(snapshot_row, "positions_json", None))
    if current_hash != verdict.snapshot_content_hash:
        log.warning(
            "spine.gate.content_mismatch",
            snapshot_id=snapshot_id,
            verdict_id=verdict.id,
            committed=verdict.snapshot_content_hash[:12],
            current=current_hash[:12],
        )
        return None

    # Defect 6: honest proof-grade labelling. Conservation passed, but proof-grade
    # additionally requires the independent-source checks — currently NEVER
    # satisfied, so this is diagnostic-grade (still served, flagged).
    unavailable = _verdict_unavailable_checks(verdict)
    proof_grade = not unavailable
    if not proof_grade:
        log.debug(
            "spine.gate.diagnostic_grade",
            snapshot_id=snapshot_id,
            verdict_id=verdict.id,
            unavailable_checks=len(unavailable),
        )

    return ValidatedSnapshot(
        snapshot_id=snapshot_id,
        positions=positions,
        verdict_id=verdict.id,
        content_hash=current_hash,
        proof_grade=proof_grade,
        unavailable_checks=unavailable,
    )


def _verdict_unavailable_checks(verdict: Any) -> tuple[str, ...]:
    try:
        detail = json.loads(verdict.detail_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        # Provenance is NOT NULL and producer-authored JSON; an unreadable detail
        # is itself a reason to withhold proof-grade (conservative).
        return ("detail_json:unreadable",)
    checks = detail.get("unavailable_checks")
    if isinstance(checks, list) and checks:
        return tuple(str(c) for c in checks)
    return ()


__all__ = ["ValidatedSnapshot", "read_validated_snapshot"]
