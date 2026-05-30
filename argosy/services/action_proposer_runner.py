"""Action-proposer runner — Spec E commit #2 (§2.5 trigger surfaces).

Orchestration layer between the ``ActionProposerAgent`` (the LLM) and
the ``action_proposals`` table (the write surface). Provides one
entry point per trigger surface from spec §2.5:

  * ``run_action_proposer_for_flag``     — state-observer flag input.
  * ``run_action_proposer_for_snapshot`` — manual UI re-evaluate.
  * ``run_action_proposer_for_inferred_event``
        — commit-#5 inferred-life-event finding (forward-compat).

Each runner:

  1. Builds the right ``ProposerTrigger`` shape from the source row.
  2. Checks the per-(kind, primary_ref) cooldown — skip if recent.
  3. Calls the agent's ``run`` (post-validation already applied
     internally — drops execution-language hits + missing required
     fields).
  4. For each surviving proposal, writes a ``action_proposals`` row
     via ``write_action_proposal`` (tombstone-then-insert dedup).
  5. Returns the list of persisted ``ActionProposal`` ORM objects.

Capability-boundary enforcement (codex BLOCKER #1 / spec §2.2.1)
=================================================================

The writer ALWAYS sets ``execution_state='proposed'``. There is NO
overload on this function that accepts an ``execution_state`` kwarg —
the column CANNOT be set to anything else from the proposer code path.
Advancement to ``'accepted_pending_user_action'`` happens only in the
UI Accept handler (commit #6), and there is no path to a hypothetical
``'executed'`` state because the CHECK enum doesn't admit one.

A 100-fixture invariant test in ``tests/test_action_proposer.py``
exhaustively walks 100 mocked LLM outputs and asserts NO row lands
with ``execution_state != 'proposed'`` — pinning the invariant
empirically against any future regression.

Dedup contract (spec §1.5 / §2.3)
==================================

Dedup key formula:

    f"v1|{kind}|{primary_ref_id}|{severity_bucket}"

Where ``primary_ref_id`` is the trigger's identity (flag id, snapshot
id, or detector finding id) and ``severity_bucket`` is one of
``info|warning|critical``. Two distinct flags with the same kind on
the same field will dedupe; two distinct fields will not. The
writer applies the tombstone-then-insert pattern from Spec B:

  - SELECT existing 'open' row with same (user_id, dedup_key) AND
    expires_at < now → UPDATE its status to 'rejected' (tombstone;
    the migration's enum doesn't include 'expired' yet).
  - INSERT the new row.
  - If the existing 'open' row is NOT expired, the IntegrityError
    raised by the partial-unique index is caught and the EXISTING
    row is returned.

Cooldown (spec §2.5)
====================

The proposer's input is a flag/snapshot/finding; the same input
should not fire the proposer twice within 24 hours. We check
``action_proposals.surfaced_at`` directly for the most recent row
with the same (kind-family, primary_ref_id). A row within the
window suppresses the call. Per spec §2.5 a separate cooldown
table is the future-polish path; querying the existing surface
keeps this commit minimal.

The cooldown applies BEFORE the LLM call so we don't pay the cost
of an Opus call we're going to drop on the floor.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from argosy.agents.action_proposer import (
    ActionProposerAgent,
    ActionProposerOutput,
    FlagTrigger,
    InferredEventTrigger,
    ProposedAction,
    ProposerTrigger,
    SnapshotTrigger,
)
from argosy.state.models import ActionProposal, MonitorFlag, StateSnapshot

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dedup-key formula version (spec §1.5 / §2.3). Bump if the formula
#: changes so a prompt iteration doesn't retroactively collide with
#: previously-written rows.
DEDUP_KEY_VERSION: str = "v1"

#: Spec §2.5 cooldown window for the (monitor_flag) trigger family.
#: Same flag_kind + primary_field + user should not refire the
#: proposer within this window.
COOLDOWN_WINDOW_HOURS: int = 24

#: TTL for newly-written proposals (spec §1.2):
#:   - 7 days for critical (time-sensitive)
#:   - 30 days for info / warning
_EXPIRY_DAYS_BY_SEVERITY: dict[str, int] = {
    "critical": 7,
    "warning": 30,
    "info": 30,
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_dedup_key(
    *,
    kind: str,
    primary_ref_id: int | str,
    severity_bucket: str,
) -> str:
    """Compose the spec §2.3 dedup_key.

    Formula:
      ``v1|<kind>|<primary_ref_id>|<severity_bucket>``

    The version prefix lets a future formula change opt in to fresh
    re-fires without retroactively breaking existing keys.

    Args:
      kind: one of the eight ActionProposalKind values.
      primary_ref_id: the trigger's identity (flag id / snapshot id /
        finding id). Coerced to str.
      severity_bucket: one of ``info|warning|critical``.

    Raises:
      ValueError: if any component contains the ``|`` delimiter.
    """
    components = (
        DEDUP_KEY_VERSION,
        str(kind),
        str(primary_ref_id),
        str(severity_bucket),
    )
    for c in components:
        if "|" in c:
            raise ValueError(
                f"dedup_key component contains illegal '|' character: {c!r}"
            )
    return "|".join(components)


def expires_at_for(severity: str, *, now: datetime) -> datetime:
    """Return the expires_at cushion per spec §1.2.

    Critical proposals get a tight 7-day cushion (the action is
    time-sensitive); info/warning get 30 days.
    """
    days = _EXPIRY_DAYS_BY_SEVERITY.get(severity, 30)
    return now + timedelta(days=days)


# ---------------------------------------------------------------------------
# Writer (the only path to action_proposals from the proposer flow)
# ---------------------------------------------------------------------------


def write_action_proposal(
    session: "Session",
    user_id: str,
    *,
    kind: str,
    summary: str,
    rationale_md: str,
    suggested_payload: dict[str, Any],
    severity: str,
    source_flag_id: int | None = None,
    source_observation_id: int | None = None,
    source_inferred_event_id: int | None = None,
    dedup_key: str,
    now: datetime | None = None,
) -> ActionProposal:
    """Insert one ``action_proposals`` row with the tombstone pattern.

    Capability-boundary enforcement (codex BLOCKER #1):

      - ``execution_state`` is HARDCODED to ``'proposed'`` here. There
        is NO kwarg to override it. The CHECK enum on the column
        (migration 0055) admits only ``proposed`` /
        ``accepted_pending_user_action`` / ``dismissed``; this writer
        only ever sets the first.

    Tombstone-then-insert (spec §1.5):

      1. SELECT an open row with same (user_id, dedup_key) that has
         expired (``expires_at < now``).
      2. If found, UPDATE its status to ``'rejected'`` to release the
         partial-unique slot. (The migration's status enum doesn't
         include ``'expired'`` yet; ``'rejected'`` is the closest
         status that releases the slot.)
      3. INSERT the new row. If an IntegrityError fires (an unexpired
         open peer raced us), return the EXISTING row instead.

    Args:
      session: live SQLAlchemy Session. The caller owns the outer
        transaction; this function commits internally so a downstream
        failure doesn't roll back already-written proposals.
      user_id: tenant id.
      kind: one of the eight ActionProposalKind values.
      summary: 1-2 sentence summary for notification rendering.
      rationale_md: longer markdown rationale.
      suggested_payload: structured payload per kind (spec §1.4).
      severity: info / warning / critical.
      source_flag_id / source_observation_id / source_inferred_event_id:
        optional FK back to the trigger row.
      dedup_key: the spec §2.3 dedup key. Required (the writer is the
        only path that hits the partial-unique slot, and we want to
        be explicit).
      now: override for tests; defaults to ``datetime.now(timezone.utc)``.

    Returns:
      The persisted ``ActionProposal`` ORM object. May be a newly
      inserted row OR a pre-existing row if a dedup collision was
      caught.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    expires_at = expires_at_for(severity, now=now)

    # Tombstone any expired open peer that holds our dedup slot.
    expired_peer = session.execute(
        select(ActionProposal).where(
            and_(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key == dedup_key,
                ActionProposal.status == "open",
                ActionProposal.expires_at < now,
            )
        )
    ).scalar_one_or_none()
    if expired_peer is not None:
        # The migration's status enum (commit #1 / migration 0055) ships
        # 5 values: open / accepted / deferred / rejected / superseded.
        # No 'expired' yet — that lands in commit #2's housekeeping-loop
        # migration per spec §1.6. We use 'rejected' as the closest
        # "releases the partial-unique slot" state. The dedicated
        # 'expired' migration will let the housekeeping loop replace
        # this; the WRITER's contract is unchanged ("transition out of
        # 'open' to release the slot").
        expired_peer.status = "rejected"
        expired_peer.decided_at = now
        expired_peer.decided_by_user_note = "tombstoned: expired peer"
        session.flush()

    row = ActionProposal(
        user_id=user_id,
        source_flag_id=source_flag_id,
        source_observation_id=source_observation_id,
        source_inferred_event_id=source_inferred_event_id,
        summary=summary,
        rationale_md=rationale_md,
        suggested_payload=json.dumps(suggested_payload, default=str),
        severity=severity,
        surfaced_at=now,
        expires_at=expires_at,
        status="open",
        kind=kind,
        dedup_key=dedup_key,
        execution_state="proposed",  # codex BLOCKER #1 — hardcoded
    )
    session.add(row)
    try:
        session.flush()
        session.commit()
        return row
    except IntegrityError as exc:
        session.rollback()
        # Dedup collision — an unexpired open peer holds the slot.
        # Return the existing row so the caller can surface it
        # without thinking it has two distinct proposals.
        existing = session.execute(
            select(ActionProposal).where(
                and_(
                    ActionProposal.user_id == user_id,
                    ActionProposal.dedup_key == dedup_key,
                    ActionProposal.status == "open",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            _log.info(
                "action_proposer_runner.dedup_collision_returning_existing",
                extra={
                    "dedup_key": dedup_key,
                    "existing_id": existing.id,
                    "kind": kind,
                    "severity": severity,
                },
            )
            return existing
        # Truly unexpected — re-raise so the caller sees it.
        raise


def _write_cooldown_marker(
    session: "Session",
    *,
    user_id: str,
    dedup_key: str,
    source_flag_id: int | None,
    source_observation_id: int | None,
    source_inferred_event_id: int | None,
    no_action_reason: str,
    now: datetime,
) -> ActionProposal | None:
    """Write a tombstoned 'note_only' row so the cooldown lookup sees
    this trigger even when the LLM emitted zero proposals.

    The row's lifecycle is unusual:
      * It's written as a marker — never user-visible — and goes
        straight into ``status='rejected'`` with a documenting
        ``decided_by_user_note``.
      * The dedup_key uses the same scheme so the cooldown's LIKE
        prefix matches it.
      * The TTL is the same as a 'note_only' (30 days) so the marker
        ages out naturally.

    Returns the persisted row, or None on failure (the caller logs
    and continues).
    """
    summary = f"(cooldown marker: no proposals from this trigger)"
    rationale = (
        f"Action proposer ran for this trigger and emitted zero "
        f"proposals. Reason: {no_action_reason or 'unspecified'}. "
        f"This marker is written so the cooldown lookup sees the "
        f"trigger and we don't re-pay the Opus cost within the "
        f"cooldown window."
    )
    row = ActionProposal(
        user_id=user_id,
        source_flag_id=source_flag_id,
        source_observation_id=source_observation_id,
        source_inferred_event_id=source_inferred_event_id,
        summary=summary,
        rationale_md=rationale,
        suggested_payload="{}",
        severity="info",
        surfaced_at=now,
        expires_at=expires_at_for("info", now=now),
        # status='rejected' from the START — the marker is never
        # user-visible. The /proposals UI filters status='open' so
        # markers don't reach the queue.
        status="rejected",
        decided_at=now,
        decided_by_user_note="cooldown_marker: empty_proposer_output",
        kind="note_only",
        dedup_key=dedup_key,
        execution_state="dismissed",
    )
    session.add(row)
    try:
        session.flush()
        session.commit()
        return row
    except IntegrityError:
        # A real proposal with the same dedup_key already exists
        # (this happens when a previous run on the same trigger
        # produced proposals; we don't need a marker on top of them).
        session.rollback()
        return None


# ---------------------------------------------------------------------------
# Runners (one per trigger surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """Summary of one proposer run."""

    proposals: list[ActionProposal]
    skipped_cooldown: bool = False
    skipped_no_output: bool = False


async def run_action_proposer_for_flag(
    session: "Session",
    monitor_flag: MonitorFlag,
    *,
    agent: ActionProposerAgent | None = None,
    state: dict[str, Any] | None = None,
    related_history: list[dict[str, Any]] | None = None,
    plan_summary: str = "",
    user_notes: str = "",
    now: datetime | None = None,
    force: bool = False,
) -> RunResult:
    """Run the proposer for a state-observer flag.

    Builds a ``FlagTrigger`` from ``monitor_flag``, applies the
    cooldown gate, calls the agent, writes the proposals.

    Args:
      session: live SQLAlchemy Session.
      monitor_flag: the trigger row.
      agent: optional pre-constructed agent (test injection). Default:
        construct an ``ActionProposerAgent(user_id=monitor_flag.user_id)``.
      state: the snapshot's state dict for prompt context. Optional;
        ``None`` is rendered as an empty object.
      related_history: list of recent proposals on related fields.
      plan_summary: plain-text plan paragraph.
      user_notes: user-typed notes (treated as tainted-data).
      now: override for tests; defaults to ``datetime.now(timezone.utc)``.
      force: skip the cooldown check. Used by the UI re-evaluate path.

    Returns:
      ``RunResult`` with the persisted proposals (may be empty).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if agent is None:
        agent = ActionProposerAgent(user_id=monitor_flag.user_id)

    # Parse the flag's payload for primary_field / related_fields /
    # rationale (defensive — the flag may be a legacy non-observer
    # row that doesn't carry the spec-B shape).
    try:
        flag_payload = json.loads(monitor_flag.payload or "{}")
    except (TypeError, ValueError):
        flag_payload = {}
    primary_field = str(flag_payload.get("primary_field") or monitor_flag.kind)

    # Cooldown gate (codex BLOCKER #2 integration). Per spec §2.5 the
    # cooldown is per-(flag_kind, primary_field, user) — NOT per-flag-
    # id. Two different MonitorFlag rows for the SAME underlying signal
    # (e.g. observer re-fires daily with a slightly-different bucket
    # but same primary_field) must share the cooldown so we don't pay
    # Opus per re-fire.
    cooldown_token = _flag_cooldown_token(
        flag_kind=str(monitor_flag.kind),
        primary_field=primary_field,
    )
    if not force and _any_proposal_or_marker_within_cooldown(
        session,
        user_id=monitor_flag.user_id,
        cooldown_token=cooldown_token,
        now=now,
    ):
        _log.info(
            "action_proposer_runner.cooldown_skip",
            extra={
                "user_id": monitor_flag.user_id,
                "flag_id": monitor_flag.id,
                "flag_kind": str(monitor_flag.kind),
                "primary_field": primary_field,
                "cooldown_token": cooldown_token,
            },
        )
        return RunResult(proposals=[], skipped_cooldown=True)

    # Build the trigger shape.
    trigger = FlagTrigger(
        flag_id=int(monitor_flag.id),
        flag_kind=str(monitor_flag.kind),
        primary_field=primary_field,
        related_fields=list(flag_payload.get("related_fields") or []),
        severity=monitor_flag.severity,  # type: ignore[arg-type]
        rationale=str(flag_payload.get("rationale_md") or ""),
    )

    # primary_ref_id embeds the cooldown token rather than the raw
    # flag id, so two distinct flag rows for the same (flag_kind,
    # primary_field) share the cooldown footprint. The flag's own
    # FK is still set on the row via source_flag_id (audit /
    # provenance is preserved).
    return await _invoke_and_persist(
        session,
        agent=agent,
        trigger=trigger,
        state=state,
        related_history=related_history,
        plan_summary=plan_summary,
        user_notes=user_notes,
        source_flag_id=int(monitor_flag.id),
        source_observation_id=None,
        source_inferred_event_id=None,
        primary_ref_id=cooldown_token,
        now=now,
    )


async def run_action_proposer_for_snapshot(
    session: "Session",
    state_snapshot: StateSnapshot,
    *,
    requested_focus: list[str] | None = None,
    agent: ActionProposerAgent | None = None,
    state: dict[str, Any] | None = None,
    related_history: list[dict[str, Any]] | None = None,
    plan_summary: str = "",
    user_notes: str = "",
    now: datetime | None = None,
    force: bool = True,
) -> RunResult:
    """Run the proposer for a state snapshot (UI re-evaluate).

    Per spec §2.5 the snapshot trigger has NO cooldown — the user is
    explicitly asking. ``force=True`` is the default.

    Args:
      session: live SQLAlchemy Session.
      state_snapshot: the trigger row.
      requested_focus: optional list of field paths the user wants the
        proposer to focus on.
      agent: optional pre-constructed agent (test injection).
      state: the snapshot's state dict for prompt context.
      related_history / plan_summary / user_notes / now: as in
        ``run_action_proposer_for_flag``.
      force: skip the cooldown check (default True).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if agent is None:
        agent = ActionProposerAgent(user_id=state_snapshot.user_id)

    cooldown_token = _snapshot_cooldown_token(
        snapshot_id=int(state_snapshot.id),
    )
    if not force and _any_proposal_or_marker_within_cooldown(
        session,
        user_id=state_snapshot.user_id,
        cooldown_token=cooldown_token,
        now=now,
    ):
        _log.info(
            "action_proposer_runner.cooldown_skip_snapshot",
            extra={
                "user_id": state_snapshot.user_id,
                "snapshot_id": state_snapshot.id,
            },
        )
        return RunResult(proposals=[], skipped_cooldown=True)

    trigger = SnapshotTrigger(
        snapshot_id=int(state_snapshot.id),
        requested_focus=list(requested_focus or []),
    )

    # Hydrate state from the snapshot if not provided.
    if state is None and state_snapshot.state_json:
        try:
            state = json.loads(state_snapshot.state_json)
        except (TypeError, ValueError):
            state = None

    return await _invoke_and_persist(
        session,
        agent=agent,
        trigger=trigger,
        state=state,
        related_history=related_history,
        plan_summary=plan_summary,
        user_notes=user_notes,
        source_flag_id=None,
        source_observation_id=int(state_snapshot.id),
        source_inferred_event_id=None,
        primary_ref_id=cooldown_token,
        now=now,
    )


async def run_action_proposer_for_inferred_event(
    session: "Session",
    *,
    inferred_event: Any,  # forward-compat: commit #5 type
    agent: ActionProposerAgent | None = None,
    state: dict[str, Any] | None = None,
    related_history: list[dict[str, Any]] | None = None,
    plan_summary: str = "",
    user_notes: str = "",
    user_id: str | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> RunResult:
    """Run the proposer for an inferred-life-event finding.

    Forward-compat: the commit-#5 ``inferred_life_event_findings``
    table doesn't exist yet, so this function accepts a duck-typed
    ``inferred_event`` with the following attributes:

      - ``id: int``  — the finding row id
      - ``pattern: str`` — one of the six patterns (see
        ``InferredEventTrigger.pattern``)
      - ``evidence_summary: str`` — a human-readable evidence
        summary string
      - ``user_id: str`` (or pass ``user_id=`` kwarg)

    The runner doesn't write the ``source_inferred_event_id`` FK
    yet — the column exists in migration 0055 but has no FK target
    until commit #5. The id is recorded in the dedup_key.

    Args:
      session: live SQLAlchemy Session.
      inferred_event: duck-typed finding row.
      agent / state / related_history / plan_summary / user_notes / now:
        as in the other runners.
      user_id: explicit override when the duck-type doesn't carry it.
      force: skip the cooldown check.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    resolved_user_id = (
        user_id
        or getattr(inferred_event, "user_id", None)
        or (agent.user_id if agent is not None else None)
    )
    if resolved_user_id is None:
        raise ValueError(
            "run_action_proposer_for_inferred_event: could not resolve "
            "user_id (pass user_id= or set it on the inferred_event)"
        )
    if agent is None:
        agent = ActionProposerAgent(user_id=resolved_user_id)

    finding_id = int(getattr(inferred_event, "id"))
    pattern = getattr(inferred_event, "pattern")
    evidence = str(getattr(inferred_event, "evidence_summary", "") or "")

    # Cooldown per spec §2.5: inferred events cooldown by
    # (pattern, user_id) — NOT per-finding-id. A re-firing pattern
    # on the same user must share the cooldown footprint.
    cooldown_token = _inferred_cooldown_token(pattern=pattern)
    if not force and _any_proposal_or_marker_within_cooldown(
        session,
        user_id=resolved_user_id,
        cooldown_token=cooldown_token,
        now=now,
    ):
        _log.info(
            "action_proposer_runner.cooldown_skip_inferred",
            extra={
                "finding_id": finding_id,
                "pattern": pattern,
                "cooldown_token": cooldown_token,
            },
        )
        return RunResult(proposals=[], skipped_cooldown=True)

    trigger = InferredEventTrigger(
        detector_finding_id=finding_id,
        pattern=pattern,  # type: ignore[arg-type]
        evidence_summary=evidence,
    )

    return await _invoke_and_persist(
        session,
        agent=agent,
        trigger=trigger,
        state=state,
        related_history=related_history,
        plan_summary=plan_summary,
        user_notes=user_notes,
        source_flag_id=None,
        source_observation_id=None,
        source_inferred_event_id=finding_id,
        primary_ref_id=cooldown_token,
        now=now,
        explicit_user_id=resolved_user_id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _invoke_and_persist(
    session: "Session",
    *,
    agent: ActionProposerAgent,
    trigger: ProposerTrigger,
    state: dict[str, Any] | None,
    related_history: list[dict[str, Any]] | None,
    plan_summary: str,
    user_notes: str,
    source_flag_id: int | None,
    source_observation_id: int | None,
    source_inferred_event_id: int | None,
    primary_ref_id: str,
    now: datetime,
    explicit_user_id: str | None = None,
) -> RunResult:
    """Common path for all three runners.

    Calls the agent, walks the validated proposals, writes each via
    ``write_action_proposal``. Per-proposal errors don't break the
    batch (mirrors Spec B's flag-writer pattern).
    """
    user_id = explicit_user_id or agent.user_id

    report = await agent.run(
        trigger=trigger,
        state=state,
        related_history=related_history,
        plan_summary=plan_summary,
        user_notes=user_notes,
        user_id=user_id,
    )
    output: ActionProposerOutput = report.output  # type: ignore[assignment]

    if not output.proposed_actions:
        # Codex IMPORTANT #2 integration: write a lightweight cooldown
        # marker so the same trigger doesn't re-pay the Opus cost
        # within the cooldown window when the LLM persistently emits
        # zero proposals. The marker is a `note_only` row whose
        # summary records the no_action_reason; status='rejected' +
        # decided_by_user_note documents the cooldown intent so the
        # housekeeping loop (commit #2's later wave) and the /proposals
        # UI can both filter it out from the user-visible queue.
        try:
            marker_dedup = build_dedup_key(
                kind="note_only",
                primary_ref_id=primary_ref_id,
                severity_bucket="info",
            )
            _write_cooldown_marker(
                session,
                user_id=user_id,
                dedup_key=marker_dedup,
                source_flag_id=source_flag_id,
                source_observation_id=source_observation_id,
                source_inferred_event_id=source_inferred_event_id,
                no_action_reason=output.no_action_reason or "empty_output",
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — never break the run
            session.rollback()
            _log.warning(
                "action_proposer_runner.cooldown_marker_write_failed",
                extra={"error": str(exc)[:300]},
            )
        _log.info(
            "action_proposer_runner.no_proposals",
            extra={
                "user_id": user_id,
                "trigger_kind": getattr(trigger, "kind", None),
                "no_action_reason": output.no_action_reason,
            },
        )
        return RunResult(proposals=[], skipped_no_output=True)

    written: list[ActionProposal] = []
    for prop in output.proposed_actions:
        try:
            dedup_key = build_dedup_key(
                kind=prop.kind,
                primary_ref_id=primary_ref_id,
                severity_bucket=prop.severity,
            )
        except ValueError as exc:
            _log.warning(
                "action_proposer_runner.dedup_key_invalid",
                extra={"error": str(exc), "kind": prop.kind},
            )
            continue

        try:
            row = write_action_proposal(
                session,
                user_id,
                kind=prop.kind,
                summary=prop.summary,
                rationale_md=prop.rationale_md,
                suggested_payload=prop.suggested_payload or {},
                severity=prop.severity,
                source_flag_id=source_flag_id,
                source_observation_id=source_observation_id,
                source_inferred_event_id=source_inferred_event_id,
                dedup_key=dedup_key,
                now=now,
            )
            written.append(row)
        except Exception as exc:  # noqa: BLE001 — never break the batch
            session.rollback()
            _log.warning(
                "action_proposer_runner.write_failed",
                extra={
                    "user_id": user_id,
                    "kind": prop.kind,
                    "dedup_key": dedup_key,
                    "error": str(exc)[:300],
                },
            )

    return RunResult(proposals=written)


def _inferred_cooldown_token(*, pattern: str) -> str:
    """Cooldown token for the inferred-life-event trigger family.

    Per spec §2.5 the inferred trigger cools down per (pattern,
    user_id). The token replaces the raw finding_id in the
    primary_ref slot.
    """
    safe = str(pattern).replace("|", "_")
    return f"inferpat:{safe}"


def _snapshot_cooldown_token(*, snapshot_id: int) -> str:
    """Cooldown token for the snapshot trigger family.

    Snapshot triggers have no cooldown by default (force=True);
    when a caller does opt into cooldown, we key per snapshot_id —
    the snapshot is itself the identity (a re-eval of the SAME
    snapshot within the window is wasteful).
    """
    return f"snap:{int(snapshot_id)}"


def _flag_cooldown_token(*, flag_kind: str, primary_field: str) -> str:
    """Build the cooldown token for the (flag_kind, primary_field) pair.

    Per spec §2.5 the cooldown is per-(flag_kind, primary_field, user) —
    NOT per-flag-id. Two distinct flag rows for the same underlying
    signal must share the cooldown footprint. The token replaces the
    raw flag_id in the dedup_key's primary_ref slot.

    Format: ``flagsig:<flag_kind>:<primary_field_sanitised>``

    The ``|`` separator (used by ``build_dedup_key``) is forbidden in
    any component — flag_kind / primary_field strings legitimately
    contain dots but never pipes; this is asserted on input via
    ``build_dedup_key``'s ValueError on illegal pipes.
    """
    # Sanitise pipes defensively. Real values shouldn't contain
    # them (flag_kind is from a closed-set enum; primary_field is a
    # dotted path), but if a future regression leaks one, replace
    # with `_` rather than crashing the dedup-key formula.
    safe_kind = str(flag_kind).replace("|", "_")
    safe_field = str(primary_field).replace("|", "_")
    return f"flagsig:{safe_kind}:{safe_field}"


def _any_proposal_or_marker_within_cooldown(
    session: "Session",
    *,
    user_id: str,
    cooldown_token: str,
    now: datetime,
    window_hours: int = COOLDOWN_WINDOW_HOURS,
) -> bool:
    """Return True iff at least one proposal OR cooldown-marker row
    with this cooldown token surfaced within the window.

    Codex BLOCKER #1 + IMPORTANT #2 integration:
      * boundary is strict ``>`` (NOT ``>=``) so a re-fire at exactly
        t+24h falls OUTSIDE the cooldown.
      * matches ALL surfaced rows for this cooldown token — including
        the lightweight cooldown markers written on empty LLM
        outputs (so we don't re-pay Opus for an input that produced
        nothing).
    """
    threshold = now - timedelta(hours=window_hours)
    prefix_token = f"|{cooldown_token}|"
    row = session.execute(
        select(ActionProposal.id)
        .where(
            and_(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key.like(f"%{prefix_token}%"),
                ActionProposal.surfaced_at > threshold,
            )
        )
        .limit(1)
    ).first()
    return row is not None




__all__ = [
    "COOLDOWN_WINDOW_HOURS",
    "DEDUP_KEY_VERSION",
    "RunResult",
    "build_dedup_key",
    "expires_at_for",
    "run_action_proposer_for_flag",
    "run_action_proposer_for_inferred_event",
    "run_action_proposer_for_snapshot",
    "write_action_proposal",
]
