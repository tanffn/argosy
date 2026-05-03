"""Security API routes (TOTP setup + verify, Phase 5).

Endpoints:
  POST /api/security/totp/setup   {user_id} → {secret, provisioning_uri}
  POST /api/security/totp/verify  {user_id, code} → {ok, last_verified_at}
  GET  /api/security/totp/status?user_id=
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from argosy import secrets as keychain
from argosy.execution.audit import record_audit_event
from argosy.security import totp as totp_mod
from argosy.state import db as db_mod
from argosy.state.models import TOTPSecret


router = APIRouter(prefix="/security", tags=["security"])


class TOTPSetupRequest(BaseModel):
    user_id: str = "ariel"
    account_label: str | None = None


class TOTPSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TOTPVerifyRequest(BaseModel):
    user_id: str = "ariel"
    code: str


class TOTPVerifyResponse(BaseModel):
    ok: bool
    last_verified_at: str | None = None
    detail: str = ""


class TOTPStatusResponse(BaseModel):
    enrolled: bool
    last_verified_at: str | None = None


@router.post("/totp/setup", response_model=TOTPSetupResponse)
async def totp_setup(body: TOTPSetupRequest) -> TOTPSetupResponse:
    """Generate a fresh secret + provisioning URI for the user.

    Subsequent calls REPLACE the secret — the user must re-enroll their
    authenticator. Audit-logged.
    """
    secret = totp_mod.generate_secret()
    label = body.account_label or body.user_id
    uri = totp_mod.provisioning_uri(secret=secret, account_name=label)

    await totp_mod.set_user_totp_secret(body.user_id, secret)
    keychain.set_totp_secret(body.user_id, secret)

    await record_audit_event(
        user_id=body.user_id,
        event_type="totp.enrolled",
        entity_type="user",
        entity_id=body.user_id,
        payload={"account_label": label},
    )

    return TOTPSetupResponse(secret=secret, provisioning_uri=uri)


@router.post("/totp/verify", response_model=TOTPVerifyResponse)
async def totp_verify(body: TOTPVerifyRequest) -> TOTPVerifyResponse:
    secret = await totp_mod.get_user_totp_secret(body.user_id)
    if not secret:
        raise HTTPException(
            status_code=404,
            detail="user has no TOTP secret enrolled; call /totp/setup first",
        )

    # Replay protection: read the prior `last_verified_at` and translate
    # to a counter floor.
    async with db_mod.get_session() as session:
        row = await session.get(TOTPSecret, body.user_id)
    last_used_counter: int | None = None
    if row is not None and row.last_verified_at is not None:
        last_used_counter = int(
            row.last_verified_at.timestamp() // totp_mod.DEFAULT_STEP_SECONDS
        )

    try:
        result = totp_mod.verify_code(
            secret, body.code, last_used_counter=last_used_counter
        )
    except totp_mod.TOTPVerificationError as exc:
        await record_audit_event(
            user_id=body.user_id,
            event_type="totp.verify_failed",
            entity_type="user",
            entity_id=body.user_id,
            payload={"reason": str(exc)},
        )
        return TOTPVerifyResponse(ok=False, detail=str(exc))

    # Persist `last_verified_at` to (counter * step) so future verifies
    # in the same step are rejected as replays.
    counter_at_epoch = result.counter * totp_mod.DEFAULT_STEP_SECONDS
    await totp_mod.mark_verified(body.user_id, at=counter_at_epoch)

    await record_audit_event(
        user_id=body.user_id,
        event_type="totp.verified",
        entity_type="user",
        entity_id=body.user_id,
        payload={"counter": result.counter},
    )

    return TOTPVerifyResponse(
        ok=True,
        last_verified_at=datetime.fromtimestamp(
            counter_at_epoch, tz=timezone.utc
        ).isoformat(),
    )


@router.get("/totp/status", response_model=TOTPStatusResponse)
async def totp_status(user_id: str = Query("ariel")) -> TOTPStatusResponse:
    async with db_mod.get_session() as session:
        row = await session.get(TOTPSecret, user_id)
    if row is None or not row.secret_encrypted:
        return TOTPStatusResponse(enrolled=False)
    return TOTPStatusResponse(
        enrolled=True,
        last_verified_at=(
            row.last_verified_at.isoformat() if row.last_verified_at else None
        ),
    )


__all__ = ["router"]
