"""Notification dispatcher — Spec E commit #3.

Central fan-out of system-generated notifications across the three v1
channels: in-app WebSocket events, web-push (VAPID), and email digest
(commit #8 placeholder — this commit ships the in-app + web-push paths;
email is added by commit #8).

Why this layer exists
=====================

Producers (action proposer, observer→replan, anomaly detector, etc.)
should NOT know:

  * which channels a given user has enabled;
  * the severity floor per channel;
  * the dedup story across days (same proposal re-firing on observer
    runs);
  * the 410-Gone subscription cleanup story;
  * the VAPID signing or JWT lifecycle;
  * the publish_event() pub-sub pumping.

Producers SHOULD know:

  * what kind of thing they're emitting (the ``kind`` discriminator);
  * its severity;
  * its identity (``ref_id`` + ``ref_kind`` so the user-facing
    deep-link works);
  * a title + body string (LLM already wrote these as
    ``ActionProposal.summary``).

This module is the bridge.  One call per producer:

    dispatch_notification(
        session,
        user_id="ariel",
        kind="action_proposal_repatriate_currency",
        severity="warning",
        ref_id=42,
        ref_kind="action_proposal",
        title="USD → NIS repatriation suggested",
        body="…",
        payload={"action_proposal_id": 42},
    )

…and the dispatcher takes care of preference matrix evaluation, per-
channel idempotency via the ``notification_dispatch_ledger``, the
web-push 410-Gone subscription cleanup, the in-app publish_event call,
and the telemetry tags for ops.

Preference matrix evaluation (spec §3.1, §3.3, §3.5)
====================================================

The matrix is a ``(channel, severity, kind)`` cube; each row in
``notification_preferences`` is one cell with ``enabled ∈ {0,1}``.
The dispatcher uses **opt-out** semantics: if NO row exists for a
cell, the channel is considered enabled (default-on).  An explicit
``enabled=0`` row mutes the cell.  Per spec §3.3 the default-on
choice is the user-friendly path; muting requires explicit user
action.  When commit #7's settings UI ships, it materialises rows for
every cell so the user can see + toggle every cube position.

Dedup ledger (spec §3.6)
========================

``notification_id`` is the cross-channel dedup key, computed
deterministically as
``f"{kind}|{ref_kind}:{ref_id}|{severity}|{today_utc}"``.  The
``notification_dispatch_ledger`` has a UNIQUE on
``(user_id, notification_id, channel)`` (see migration 0055
``uq_notification_dispatch_ledger_user_notification_channel``); this
is the LAST line of defence — even if two concurrent ``dispatch_*``
calls race past the application-level "already sent?" check, the DB
constraint refuses the second write.  The dispatcher catches that
IntegrityError as a benign idempotency hit and reports
``channels_skipped=['<channel>']``.

For web-push the ledger row is written PER subscription — a user with
two browsers gets two ledger rows on a single dispatch.  The channel
column is ``'web_push'`` for all of them; the ``subscription_id`` FK
attributes which subscription received the send.  Multi-subscription
dedup is therefore subscription-granular: re-running ``dispatch_*``
for the same notification will detect the existing ledger rows and
skip the per-subscription sends.

VAPID + endpoint shape (spec §3.2, §3.4)
========================================

All web-push work is delegated to
``argosy.services.web_push.send_web_push``.  This module only handles:

  * loading active subscriptions;
  * calling send_web_push per subscription;
  * mapping the returned ``WebPushResult.status`` to a ledger row
    (``sent`` / ``failed``) AND to the subscription-side ``status=
    'gone'`` flip when WebPushResult returns ``gone``.

Concurrency
===========

The dispatcher is ``async def`` because both ``publish_event`` and
``send_web_push`` are async.  DB session ops use the SQLAlchemy sync
API (the caller passes a sync ``Session``); this matches the
project-wide convention (see ``argosy/services/state_observer_flag_writer.py``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from argosy.state.models import (
    NotificationDispatchLedger,
    NotificationPreference,
    NotificationSubscription,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

    from argosy.services.web_push import WebPushResult


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Channels we fan out to in v1.  Email is intentionally absent — it
#: lands in commit #8 (digest renderer).  When commit #8 ships, the
#: tuple gains ``'email'`` and the dispatcher gains a writer for the
#: ``pending_digest_entries`` table.
_DEFAULT_CHANNELS: tuple[str, ...] = ("in_app", "web_push")

#: Severity-rank ladder.  Used both for preference matrix comparisons
#: AND for ``severity_floor``-style gating in commit #7's settings UI.
_SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}

#: Default opt-out value when a preference row is missing.  Spec §3.3
#: documents the matrix as default-on; this is the literal value the
#: writer falls back to when the cell has no row.
_DEFAULT_ENABLED: int = 1


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchSummary:
    """Per-call result of ``dispatch_notification``.

    Mutually-exclusive per-channel counters (each channel ends up in
    exactly ONE of attempted/sent/skipped; web_push counts per
    subscription).

    Fields:
      notification_id: the deterministic dedup key the dispatcher
        computed.  Returned so callers can log "I asked the
        dispatcher to fire notification X".
      channels_attempted: every channel the dispatcher TRIED to send
        on (preference gate passed AND not already in the ledger).
      channels_sent: every channel that returned a 2xx.  For web_push,
        the count is per-subscription (a user with two browsers
        contributes 2 to ``channels_sent`` on a single dispatch).
      channels_skipped: every channel the dispatcher chose to skip,
        WITH a reason — values like ``'in_app:preference_disabled'``
        / ``'web_push:already_dispatched'`` / ``'web_push:no_subscriptions'``.
      errors: list of (channel, error_tag) pairs.  Failed sends are
        ALSO ledger-written (status='failed'); this list is the
        in-process echo so the caller can log.
    """

    notification_id: str
    channels_attempted: list[str] = field(default_factory=list)
    channels_sent: list[str] = field(default_factory=list)
    channels_skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_today_iso(now: datetime | None = None) -> str:
    """ISO date string for the dedup key's day partition."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def _compute_notification_id(
    *,
    kind: str,
    ref_kind: str,
    ref_id: Any,
    severity: str,
    now: datetime | None = None,
) -> str:
    """Build the deterministic per-day cross-channel dedup key.

    Convention (spec §3.6):
        f"{kind}|{ref_kind}:{ref_id}|{severity}|{utc_day}"

    The day partition guarantees that re-firing the same notification
    a day later is NOT a dedup hit — the user gets a fresh ping (the
    underlying situation hasn't been resolved).  Same-day re-fires
    (e.g. observer ran twice in 12h) ARE dedup-hits and skip cleanly.
    """
    day = _utc_today_iso(now)
    return f"{kind}|{ref_kind}:{ref_id}|{severity}|{day}"


def _preference_enabled(
    session: "Session",
    user_id: str,
    channel: str,
    severity: str,
    kind: str,
) -> bool:
    """Look up the (user, channel, severity, kind) preference cell.

    Opt-out semantics: missing row = enabled.  Returns ``False`` ONLY
    when an explicit ``enabled=0`` row exists for the exact cell.
    """
    row = session.execute(
        select(NotificationPreference.enabled).where(
            NotificationPreference.user_id == user_id,
            NotificationPreference.channel == channel,
            NotificationPreference.severity == severity,
            NotificationPreference.kind == kind,
        )
    ).scalar_one_or_none()
    if row is None:
        return bool(_DEFAULT_ENABLED)
    return bool(row)


def _ledger_already_dispatched(
    session: "Session",
    *,
    user_id: str,
    notification_id: str,
    channel: str,
    subscription_id: int | None = None,
) -> bool:
    """Pre-flight: has this (user, notification, channel[, sub]) been logged?

    For web_push we also key on ``subscription_id`` so a user with two
    browsers correctly sends to both on the first dispatch, and a
    re-dispatch correctly skips both.  For in_app the
    ``subscription_id`` is always NULL so the DB-level UNIQUE on
    ``(user_id, notification_id, channel)`` catches a re-fire on its
    own.

    SECURITY NOTE: the DB UNIQUE constraint is the ground-truth.  This
    pre-check is the cheap path that lets us count the skip cleanly
    without an IntegrityError traceback at every dedup hit.
    """
    stmt = select(NotificationDispatchLedger.id).where(
        NotificationDispatchLedger.user_id == user_id,
        NotificationDispatchLedger.notification_id == notification_id,
        NotificationDispatchLedger.channel == channel,
    )
    if subscription_id is not None:
        stmt = stmt.where(
            NotificationDispatchLedger.subscription_id == subscription_id
        )
    row = session.execute(stmt).scalar_one_or_none()
    return row is not None


def _write_ledger(
    session: "Session",
    *,
    user_id: str,
    notification_id: str,
    channel: str,
    status: str,
    subscription_id: int | None = None,
    error_message: str | None = None,
) -> bool:
    """Insert a ledger row.  Returns ``True`` on insert, ``False`` on
    UNIQUE-violation (treated as benign idempotency hit — another
    dispatcher beat us to it).
    """
    row = NotificationDispatchLedger(
        user_id=user_id,
        notification_id=notification_id,
        channel=channel,
        subscription_id=subscription_id,
        status=status,
        error_message=error_message,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True


# ---------------------------------------------------------------------------
# Public API — main entry
# ---------------------------------------------------------------------------


async def dispatch_notification(
    session: "Session",
    user_id: str,
    *,
    kind: str,
    severity: Literal["info", "warning", "critical"],
    ref_id: Any,
    ref_kind: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    channels: tuple[str, ...] = _DEFAULT_CHANNELS,
    now: datetime | None = None,
    web_push_sender=None,  # injectable for tests
) -> DispatchSummary:
    """Fan out a notification across enabled channels for ``user_id``.

    Args:
      session: live SQLAlchemy Session.  This function flushes ledger
        rows + subscription status updates but does NOT commit — the
        caller owns the outer transaction.  All writes are visible to
        subsequent queries on the same session immediately.
      user_id: tenant.
      kind: notification family discriminator (used in preference
        matrix lookup + ledger writeback).  Conventionally one of:
          * ``"action_proposal_<proposal_kind>"`` — produced by the
            action proposer (commit #2 sibling);
          * ``"observer_flag_<flag_kind>"`` — produced by the
            state-observer flag writer (Spec B);
          * ``"replan_dispatched"`` — produced by the observer→replan
            wiring (commit #4 sibling).
      severity: matches MonitorFlag.severity.  Drives both preference
        gating AND ledger column.
      ref_id: identifier of the underlying entity (proposal id, flag
        id, job_run id).  Stored verbatim in the dedup_key.
      ref_kind: namespace for ``ref_id`` (e.g. ``"action_proposal"``).
      title: short notification headline.  Surfaced verbatim to in_app
        + (when v2 lands) web-push body.
      body: longer notification text.
      payload: extra structured fields surfaced into the in_app event
        payload + web-push body.  Defaults to ``{}``.
      channels: override the default channel set (mostly for tests).
      now: clock injection for tests.  Defaults to ``datetime.now(utc)``.
      web_push_sender: injectable async callable
        ``(NotificationSubscription, dict) -> WebPushResult``.  When
        None, the real ``argosy.services.web_push.send_web_push`` is
        used.  Tests pass a stub to avoid hitting the network.

    Returns:
      DispatchSummary — counters across channels + per-channel error
      list.

    Side effects:
      * Calls ``argosy.api.events.publish_event`` for in_app.
      * Calls ``send_web_push`` per active subscription for web_push.
      * Flips ``NotificationSubscription.status='gone'`` on a 410-
        equivalent return.
      * Inserts ``NotificationDispatchLedger`` rows for every
        attempted channel (status='sent' / 'failed' / 'skipped').
    """
    payload = payload or {}
    notification_id = _compute_notification_id(
        kind=kind,
        ref_kind=ref_kind,
        ref_id=ref_id,
        severity=severity,
        now=now,
    )
    summary = DispatchSummary(notification_id=notification_id)

    in_app_payload = {
        "kind": kind,
        "severity": severity,
        "title": title,
        "body": body,
        "ref_id": ref_id,
        "ref_kind": ref_kind,
        "payload": payload,
    }

    for channel in channels:
        if channel == "in_app":
            await _dispatch_in_app(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                kind=kind,
                severity=severity,
                payload=in_app_payload,
                summary=summary,
            )
        elif channel == "web_push":
            await _dispatch_web_push(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                kind=kind,
                severity=severity,
                payload=in_app_payload,
                summary=summary,
                web_push_sender=web_push_sender,
            )
        else:  # pragma: no cover — defensive; commit #8 adds 'email'
            _log.warning(
                "dispatch_notification: unsupported channel=%s "
                "(skipping; supported=%s)",
                channel,
                _DEFAULT_CHANNELS,
            )
            summary.channels_skipped.append(f"{channel}:unsupported")

    return summary


# ---------------------------------------------------------------------------
# Per-channel dispatch helpers
# ---------------------------------------------------------------------------


async def _dispatch_in_app(
    session: "Session",
    *,
    user_id: str,
    notification_id: str,
    channel: str,
    kind: str,
    severity: str,
    payload: dict[str, Any],
    summary: DispatchSummary,
) -> None:
    """Send via ``publish_event`` + write ledger row.

    Preference gate: skip if (user, in_app, severity, kind) is
    explicitly disabled.  Default-on per spec §3.3.

    Dedup gate: skip if a same-day ledger row already exists.

    On the broadcast itself: ``publish_event`` swallows all errors
    internally (no subscriber may raise back into us), so we always
    treat the publish as 'sent' once we made the call.  An exception
    here means we couldn't WRITE the ledger row, which IS a real
    failure.
    """
    if not _preference_enabled(session, user_id, channel, severity, kind):
        summary.channels_skipped.append(f"{channel}:preference_disabled")
        # Skip rows go into the ledger too, so the admin UI can show
        # "we suppressed N notifications today" — but the writer
        # tolerates UNIQUE collision (a previous dispatch may have
        # already written 'skipped' for the same key).
        _write_ledger(
            session,
            user_id=user_id,
            notification_id=notification_id,
            channel=channel,
            status="skipped",
            error_message="preference_disabled",
        )
        return

    if _ledger_already_dispatched(
        session,
        user_id=user_id,
        notification_id=notification_id,
        channel=channel,
    ):
        summary.channels_skipped.append(f"{channel}:already_dispatched")
        return

    summary.channels_attempted.append(channel)

    # publish_event is best-effort; we deliberately do NOT propagate
    # its exceptions (per the threadsafe variant docstring in
    # argosy/api/events.py: "Any failure is swallowed; event
    # publishing must never break primary work.")
    try:
        # Local import to avoid a hard dependency cycle between
        # services.* and api.* at module import time.
        from argosy.api.events import publish_event

        await publish_event(f"notification.{kind}", payload)
    except Exception as exc:  # noqa: BLE001 — explicit best-effort
        _log.warning(
            "in_app_publish_failed kind=%s user=%s exc=%s",
            kind,
            user_id,
            exc,
        )
        summary.errors.append((channel, f"publish_failed:{type(exc).__name__}"))
        _write_ledger(
            session,
            user_id=user_id,
            notification_id=notification_id,
            channel=channel,
            status="failed",
            error_message=f"publish_failed:{type(exc).__name__}",
        )
        return

    inserted = _write_ledger(
        session,
        user_id=user_id,
        notification_id=notification_id,
        channel=channel,
        status="sent",
    )
    if inserted:
        summary.channels_sent.append(channel)
    else:
        # The UNIQUE caught a concurrent dispatcher; the publish DID
        # fire (no rollback on publish_event) but the ledger row is
        # the other dispatcher's.  Treat as "already_dispatched" so
        # the count reads cleanly.
        summary.channels_skipped.append(f"{channel}:concurrent_dispatch")


async def _dispatch_web_push(
    session: "Session",
    *,
    user_id: str,
    notification_id: str,
    channel: str,
    kind: str,
    severity: str,
    payload: dict[str, Any],
    summary: DispatchSummary,
    web_push_sender,
) -> None:
    """Fan out to every active web_push subscription for ``user_id``.

    Per-subscription flow:

      1. Skip if the (user, notification, web_push, sub) tuple already
         exists in the ledger (idempotent re-dispatch).
      2. Call the sender (injectable; defaults to
         ``argosy.services.web_push.send_web_push``).
      3. On ``WebPushResult.status='gone'``: flip the subscription
         row's ``status`` to ``'gone'`` AND write a ledger row with
         status='failed' (the send DID fail; the subscription is now
         dead).
      4. On 'sent' / 'failed': write the matching ledger row.
    """
    if not _preference_enabled(session, user_id, channel, severity, kind):
        summary.channels_skipped.append(f"{channel}:preference_disabled")
        _write_ledger(
            session,
            user_id=user_id,
            notification_id=notification_id,
            channel=channel,
            status="skipped",
            error_message="preference_disabled",
        )
        return

    subs = list(
        session.execute(
            select(NotificationSubscription).where(
                NotificationSubscription.user_id == user_id,
                NotificationSubscription.channel == "web_push",
                NotificationSubscription.status == "active",
            )
        ).scalars()
    )
    if not subs:
        summary.channels_skipped.append(f"{channel}:no_subscriptions")
        return

    # Resolve sender lazily so the import only fires when web_push is
    # actually used (keeps import-time light for in_app-only callers
    # and avoids a hard import dep when web_push.py is unreachable in
    # very stripped-down test envs).
    if web_push_sender is None:
        from argosy.services.web_push import send_web_push as web_push_sender

    for sub in subs:
        if _ledger_already_dispatched(
            session,
            user_id=user_id,
            notification_id=notification_id,
            channel=channel,
            subscription_id=sub.id,
        ):
            summary.channels_skipped.append(
                f"{channel}:already_dispatched:sub={sub.id}"
            )
            continue

        summary.channels_attempted.append(f"{channel}:sub={sub.id}")

        try:
            result: "WebPushResult" = await web_push_sender(sub, payload)
        except Exception as exc:  # noqa: BLE001 — send is best-effort
            _log.warning(
                "web_push_send_unhandled sub_id=%s user=%s exc=%s",
                sub.id,
                user_id,
                exc,
            )
            summary.errors.append(
                (channel, f"sender_exception:{type(exc).__name__}")
            )
            _write_ledger(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                subscription_id=sub.id,
                status="failed",
                error_message=f"sender_exception:{type(exc).__name__}",
            )
            continue

        if result.status == "sent":
            inserted = _write_ledger(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                subscription_id=sub.id,
                status="sent",
            )
            if inserted:
                summary.channels_sent.append(f"{channel}:sub={sub.id}")
            else:
                summary.channels_skipped.append(
                    f"{channel}:concurrent_dispatch:sub={sub.id}"
                )
        elif result.status == "gone":
            # Flip subscription status + write ledger row.
            session.execute(
                update(NotificationSubscription)
                .where(NotificationSubscription.id == sub.id)
                .values(status="gone")
            )
            summary.errors.append(
                (channel, f"sub_gone:sub={sub.id}:{result.error or 'http_410'}")
            )
            _write_ledger(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                subscription_id=sub.id,
                status="failed",
                error_message=result.error or "http_410",
            )
        else:  # 'failed'
            summary.errors.append(
                (channel, f"send_failed:sub={sub.id}:{result.error}")
            )
            _write_ledger(
                session,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
                subscription_id=sub.id,
                status="failed",
                error_message=result.error,
            )


# ---------------------------------------------------------------------------
# Public API — preference + subscription CRUD
# ---------------------------------------------------------------------------


def update_preference(
    session: "Session",
    user_id: str,
    *,
    channel: str,
    severity: str,
    kind: str,
    enabled: bool,
) -> NotificationPreference:
    """Upsert one preference cell.

    The natural key is ``(user_id, channel, severity, kind)``; the
    DB has a UNIQUE on those four columns (migration 0055
    ``uq_notification_preferences_user_cell``).  This function
    either:

      * inserts a new row when the cell is missing;
      * updates the ``enabled`` value (+ ``updated_at``) when the
        cell exists.

    Validates inputs:
      * ``channel`` must be one of ``in_app|web_push|email``;
      * ``severity`` must be one of ``info|warning|critical``.

    Raises ``ValueError`` on bad inputs (DB CHECK would catch them too,
    but the loud-error pattern surfaces problems at write time rather
    than at flush).
    """
    if channel not in ("in_app", "web_push", "email"):
        raise ValueError(f"invalid channel: {channel!r}")
    if severity not in _SEVERITY_RANK:
        raise ValueError(f"invalid severity: {severity!r}")

    row = session.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user_id,
            NotificationPreference.channel == channel,
            NotificationPreference.severity == severity,
            NotificationPreference.kind == kind,
        )
    ).scalar_one_or_none()
    if row is None:
        row = NotificationPreference(
            user_id=user_id,
            channel=channel,
            severity=severity,
            kind=kind,
            enabled=1 if enabled else 0,
        )
        session.add(row)
        session.flush()
    else:
        row.enabled = 1 if enabled else 0
        row.updated_at = datetime.now(timezone.utc)
        session.flush()
    return row


def add_subscription(
    session: "Session",
    user_id: str,
    *,
    channel: str,
    endpoint: str,
    p256dh: str | None = None,
    auth: str | None = None,
) -> NotificationSubscription:
    """Insert a new ``notification_subscriptions`` row.

    Endpoint shape validation: for ``channel='web_push'`` we delegate
    to ``argosy.services.web_push._validate_endpoint_shape`` so the
    same loose-but-correct rules used at delivery time are enforced
    at subscription time.  For other channels the shape check is
    permissive (email format is the caller's concern; in_app id is
    structural).

    Idempotency: the DB has UNIQUE(user_id, channel, endpoint) (see
    migration 0055 ``uq_notification_subscriptions_user_channel_endpoint``).
    When the browser re-POSTs an existing subscription, this function
    returns the existing row instead of raising — matches the
    real-world pattern of a browser re-subscribing on each reload.

    Raises:
      ValueError: for web_push endpoints that fail shape validation
        (non-https / missing host).  The route handler should map
        this to a 400.
    """
    if channel == "web_push":
        from argosy.services.web_push import _validate_endpoint_shape

        is_valid, err_tag, _host = _validate_endpoint_shape(endpoint)
        if not is_valid:
            raise ValueError(f"web_push endpoint failed shape validation: {err_tag}")

    existing = session.execute(
        select(NotificationSubscription).where(
            NotificationSubscription.user_id == user_id,
            NotificationSubscription.channel == channel,
            NotificationSubscription.endpoint == endpoint,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-subscribe: refresh crypto material if provided, flip status
        # back to 'active' if previously 'gone'.
        if p256dh is not None:
            existing.p256dh = p256dh
        if auth is not None:
            existing.auth = auth
        if existing.status != "active":
            existing.status = "active"
        session.flush()
        return existing

    row = NotificationSubscription(
        user_id=user_id,
        channel=channel,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
    )
    session.add(row)
    session.flush()
    return row


def remove_subscription(session: "Session", subscription_id: int) -> None:
    """Delete a subscription row by id.

    The dispatch_ledger FK is ON DELETE SET NULL (migration 0055), so
    historical audit rows survive the delete with
    ``subscription_id=NULL``.  Callers that want to *deactivate*
    without losing FK linkage should flip ``status='gone'`` instead;
    this function is for full user-driven opt-out.
    """
    row = session.get(NotificationSubscription, subscription_id)
    if row is None:
        return
    session.delete(row)
    session.flush()


__all__ = [
    "DispatchSummary",
    "add_subscription",
    "dispatch_notification",
    "remove_subscription",
    "update_preference",
]


# json is imported for downstream callers that want to introspect
# payload JSON; keep the reference visible.
_ = json
