"""REST routes for /api/notifications/* — Spec E commit #7.

Surfaces the subscription + preference-matrix + VAPID-public-key + test-push
endpoints consumed by ``ui/src/components/notifications/PushSubscriptionCard``
and ``ui/src/app/settings/notifications/page.tsx``.

Endpoints
=========

* ``GET    /api/notifications/vapid-key``               -> ``{public_key: str}``
* ``GET    /api/notifications/subscriptions``           -> list of subscription rows
* ``POST   /api/notifications/subscribe``               -> create/upsert subscription
* ``DELETE /api/notifications/subscribe/{id}``          -> hard delete
* ``GET    /api/notifications/preferences``             -> matrix (channels x severities x kinds)
* ``PUT    /api/notifications/preferences``             -> bulk upsert cells
* ``POST   /api/notifications/test-push``               -> fire a one-off test notification

Service plumbing
================

* VAPID public-key lookup uses ``argosy.services.web_push._load_vapid_creds``;
  if creds are absent we return a 503 telling the operator how to seed them,
  so the UI can render a "VAPID is not configured on this machine" banner
  rather than silently failing the subscribe flow.
* Subscription CRUD goes through ``notification_dispatcher.add_subscription`` /
  ``.remove_subscription`` so the application-level endpoint-shape validation
  used at dispatch time is also enforced at subscribe time.
* Preference upserts route through ``notification_dispatcher.update_preference``
  for the same reason — single writer == consistent validation + telemetry.
* Test push goes through ``dispatch_notification`` with ``ref_kind='test_push'``;
  this exercises the entire dispatch path (preference gate -> ledger -> VAPID
  send) so the user gets the same observability they'd get for a real
  notification.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.notification_dispatcher import (
    add_subscription,
    dispatch_notification,
    remove_subscription,
    update_preference,
)
from argosy.services.web_push import _load_vapid_creds
from argosy.state.models import (
    NotificationPreference,
    NotificationSubscription,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Shared enums (mirror migration 0055).
# ---------------------------------------------------------------------------

Channel = Literal["in_app", "web_push", "email"]
Severity = Literal["info", "warning", "critical"]

#: The eight action-proposal kinds from migration 0055.  Surfaced in the
#: preference matrix as one column per kind so the user can mute (e.g.)
#: only ``note_only`` proposals without losing the others.  Synced with
#: ``alembic/versions/0055_action_proposals_and_notifications.py``.
ACTION_PROPOSAL_KINDS: tuple[str, ...] = (
    "allocate",
    "repatriate_currency",
    "rebalance",
    "replan_full",
    "add_life_event_phase",
    "update_plan_assumption",
    "set_watchlist",
    "note_only",
)

#: Synthetic ``kind`` strings used by the matrix UI.  The dispatcher's
#: ``kind`` is permissive TEXT (no DB enum) so adding entries here doesn't
#: require a migration.  Producer-side kinds beyond this list still
#: dispatch correctly via the default-on fallback in
#: ``notification_dispatcher._preference_enabled``.
PREFERENCE_MATRIX_KINDS: tuple[str, ...] = ACTION_PROPOSAL_KINDS

CHANNELS: tuple[Channel, ...] = ("in_app", "web_push", "email")
SEVERITIES: tuple[Severity, ...] = ("info", "warning", "critical")


# ---------------------------------------------------------------------------
# DTOs.
# ---------------------------------------------------------------------------


class VapidKeyResponse(BaseModel):
    """``GET /vapid-key`` response.  ``public_key`` is the base64url-encoded
    P-256 public key from ``~/.argosy/vapid_creds.json``."""

    public_key: str


class SubscriptionDTO(BaseModel):
    """Wire shape for one ``NotificationSubscription`` row."""

    id: int
    user_id: str
    channel: str
    endpoint: str
    status: str
    subscribed_at: str
    last_seen_at: str | None = None


class SubscribeRequest(BaseModel):
    """``POST /subscribe`` body.  ``channel`` defaults to ``web_push`` since
    that's the only channel the browser opt-in flow creates."""

    channel: Channel = "web_push"
    endpoint: str = Field(..., min_length=1, max_length=2048)
    p256dh: str | None = Field(default=None, max_length=512)
    auth: str | None = Field(default=None, max_length=256)


class PreferenceCellDTO(BaseModel):
    """One (channel, severity, kind, enabled) cell of the preference matrix."""

    channel: Channel
    severity: Severity
    kind: str
    enabled: bool


class PreferencesResponse(BaseModel):
    """``GET /preferences`` response — full matrix the UI renders.

    The ``cells`` list always covers the full cross-product of
    (channels x severities x kinds); missing-from-DB cells materialise as
    ``enabled=True`` to honour the dispatcher's default-on semantics.
    """

    channels: list[str]
    severities: list[str]
    kinds: list[str]
    cells: list[PreferenceCellDTO]


class PreferencesUpdateRequest(BaseModel):
    """``PUT /preferences`` body — bulk upsert payload."""

    cells: list[PreferenceCellDTO] = Field(default_factory=list)


class TestPushRequest(BaseModel):
    """``POST /test-push`` body — caller may override the title/body to
    sanity-check rendering, but the defaults are fine for the UI button."""

    title: str = Field(default="Argosy test notification", max_length=120)
    body: str = Field(
        default="If you can see this, push delivery is working end-to-end.",
        max_length=480,
    )
    severity: Severity = "info"


class TestPushResponse(BaseModel):
    """``POST /test-push`` response.  ``notification_id`` + per-channel
    counters mirror the dispatcher's ``DispatchSummary`` shape so the UI
    can surface "sent: in_app, web_push:sub=3" inline."""

    notification_id: str
    channels_attempted: list[str]
    channels_sent: list[str]
    channels_skipped: list[str]
    errors: list[list[str]]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _serialize_subscription(row: NotificationSubscription) -> SubscriptionDTO:
    """Project a SQLAlchemy row into the wire DTO."""
    return SubscriptionDTO(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        endpoint=row.endpoint,
        status=row.status,
        subscribed_at=row.subscribed_at.isoformat() if row.subscribed_at else "",
        last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else None,
    )


def _load_preference_rows(
    session: Session, user_id: str
) -> dict[tuple[str, str, str], bool]:
    """Read every stored cell into a (channel, severity, kind) -> enabled dict."""
    rows = session.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user_id
        )
    ).scalars()
    return {
        (r.channel, r.severity, r.kind): bool(r.enabled) for r in rows
    }


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


@router.get("/vapid-key", response_model=VapidKeyResponse)
async def get_vapid_key() -> VapidKeyResponse:
    """Return the server's VAPID public key for ``pushManager.subscribe``.

    Returns 503 when ``~/.argosy/vapid_creds.json`` is missing/malformed so
    the UI can render an actionable "VAPID not configured" banner.
    """
    creds = _load_vapid_creds()
    if creds is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "vapid_not_configured",
                "hint": (
                    "Run scripts/generate_vapid_keys.py to seed "
                    "~/.argosy/vapid_creds.json, then restart the API."
                ),
            },
        )
    return VapidKeyResponse(public_key=creds.public_key_b64url)


@router.get("/subscriptions", response_model=list[SubscriptionDTO])
async def list_subscriptions(
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> list[SubscriptionDTO]:
    """Return every subscription row for ``user_id`` (active + gone).

    Lists ``gone`` subscriptions too so the UI can surface a "this browser
    revoked its subscription; opt in again" hint.
    """
    rows = (
        db.execute(
            select(NotificationSubscription)
            .where(NotificationSubscription.user_id == user_id)
            .order_by(NotificationSubscription.subscribed_at.desc())
        )
        .scalars()
        .all()
    )
    return [_serialize_subscription(r) for r in rows]


@router.post(
    "/subscribe",
    response_model=SubscriptionDTO,
    status_code=201,
)
async def subscribe(
    body: SubscribeRequest,
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> SubscriptionDTO:
    """Upsert one push subscription.  Idempotent on (user, channel, endpoint).

    The dispatcher's ``add_subscription`` enforces web_push endpoint shape
    validation (rejects non-https / missing host) by raising ``ValueError``;
    we translate to HTTP 422 so the UI can surface a "invalid push endpoint"
    banner without a 500.
    """
    try:
        row = add_subscription(
            db,
            user_id=user_id,
            channel=body.channel,
            endpoint=body.endpoint,
            p256dh=body.p256dh,
            auth=body.auth,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_subscription",
                "message": str(exc),
            },
        ) from exc
    db.commit()
    db.refresh(row)
    return _serialize_subscription(row)


@router.delete("/subscribe/{subscription_id}", status_code=204)
async def unsubscribe(
    subscription_id: int,
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> None:
    """Hard-delete the subscription row.  404 if not owned by ``user_id``.

    The dispatch_ledger FK is ``ON DELETE SET NULL`` so historical
    delivery rows survive the delete.
    """
    row = db.get(NotificationSubscription, subscription_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="subscription_not_found")
    remove_subscription(db, subscription_id)
    db.commit()


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> PreferencesResponse:
    """Return the full preference matrix for ``user_id``.

    Missing cells materialise as ``enabled=True`` per spec §3.3 (default-on).
    The UI never has to special-case "this cell doesn't exist yet" — the
    response always covers the full cross-product.
    """
    stored = _load_preference_rows(db, user_id)
    cells: list[PreferenceCellDTO] = []
    for channel in CHANNELS:
        for severity in SEVERITIES:
            for kind in PREFERENCE_MATRIX_KINDS:
                enabled = stored.get((channel, severity, kind), True)
                cells.append(
                    PreferenceCellDTO(
                        channel=channel,
                        severity=severity,
                        kind=kind,
                        enabled=enabled,
                    )
                )
    return PreferencesResponse(
        channels=list(CHANNELS),
        severities=list(SEVERITIES),
        kinds=list(PREFERENCE_MATRIX_KINDS),
        cells=cells,
    )


@router.put("/preferences", response_model=PreferencesResponse)
async def update_preferences(
    body: PreferencesUpdateRequest,
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> PreferencesResponse:
    """Bulk upsert preference cells.  Returns the full refreshed matrix.

    Cells not present in the request body are LEFT ALONE (partial update);
    the UI sends only the cells that changed.
    """
    for cell in body.cells:
        try:
            update_preference(
                db,
                user_id=user_id,
                channel=cell.channel,
                severity=cell.severity,
                kind=cell.kind,
                enabled=cell.enabled,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_preference_cell",
                    "message": str(exc),
                    "cell": cell.model_dump(),
                },
            ) from exc
    db.commit()
    return await get_preferences(user_id=user_id, db=db)


@router.post("/test-push", response_model=TestPushResponse)
async def test_push(
    body: TestPushRequest,
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> TestPushResponse:
    """Fire a one-off notification through the dispatcher.

    The notification flows through the same code path as real
    notifications (preference gate -> ledger -> web push sender) so the
    user sees a realistic "skipped because web_push/info preference is
    off" message if they've muted the cell.  ``ref_kind='test_push'`` +
    a monotonic ``ref_id`` from the dispatch_ledger row count keep the
    dedup-key fresh across repeated clicks.
    """
    # A monotonic counter from the existing dispatch ledger lets repeat
    # clicks fire fresh notifications instead of dedup-hitting on the
    # same-day same-id key.
    from sqlalchemy import func

    from argosy.state.models import NotificationDispatchLedger

    ref_id = (
        db.execute(
            select(func.coalesce(func.max(NotificationDispatchLedger.id), 0))
        ).scalar_one()
        + 1
    )
    summary = await dispatch_notification(
        db,
        user_id=user_id,
        kind="test_push",
        severity=body.severity,
        ref_id=ref_id,
        ref_kind="test_push",
        title=body.title,
        body=body.body,
        payload={"source": "settings_test_button"},
    )
    db.commit()
    return TestPushResponse(
        notification_id=summary.notification_id,
        channels_attempted=summary.channels_attempted,
        channels_sent=summary.channels_sent,
        channels_skipped=summary.channels_skipped,
        # Pydantic doesn't like raw tuples through Literal lists; coerce to
        # nested lists so the response model serializes cleanly.
        errors=[[c, e] for (c, e) in summary.errors],
    )


# Re-exports for test discovery.
__all__ = [
    "ACTION_PROPOSAL_KINDS",
    "CHANNELS",
    "PREFERENCE_MATRIX_KINDS",
    "SEVERITIES",
    "router",
]


# Silence unused-import lint on Any when this module is imported in a
# typing-only context.
_ = Any
