"""Tenant onboarding endpoint (Phase 6).

  POST /api/onboarding/redeem
    body: { email: str, token: str }
    response: { user_id: str, plan: str }

The NextAuth Credentials provider POSTs the setup token + email here.
We consume the token (one-shot) and bind the email to the existing
tenant row. On success we return the tenant's user_id, which NextAuth
stores in the JWT subject / a custom claim.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import User
from argosy.tenancy.onboarding import consume_setup_token

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class RedeemRequest(BaseModel):
    email: str
    token: str


class RedeemResponse(BaseModel):
    user_id: str
    plan: str


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest) -> RedeemResponse:
    user_id = await consume_setup_token(body.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="invalid or expired setup token")
    async with db_mod.get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="tenant not provisioned")
        # Bind the email if absent (or if the operator-supplied email is
        # being asserted here for the first time). We trust the setup-
        # token flow to authenticate the binding.
        if not user.email:
            user.email = body.email
            await session.commit()
        elif user.email != body.email:
            # Don't reveal mismatch; a malicious client probing tokens
            # shouldn't be able to differentiate a wrong-email vs a
            # wrong-token failure mode.
            raise HTTPException(status_code=400, detail="invalid or expired setup token")
        return RedeemResponse(user_id=user.id, plan=user.plan)


__all__ = ["router"]
