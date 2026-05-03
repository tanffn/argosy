"""Tenant onboarding helpers (Phase 6).

Two surfaces:
  * `provision_tenant(user_id, email)` — create a tenant row, ensure
    config dir exists, write skeleton `entitlements.yaml` if absent,
    create the user row with the email mapping. Idempotent.
  * `issue_setup_token(user_id)` — mint a single-use first-login
    token (uses `argosy.api.auth` signing if available; otherwise a
    random url-safe string).

Onboarding does NOT run alembic migrations against the tenant DB
directly — that requires alembic config plumbing that's overkill for
v1. Instead, the tenant DB is initialized from `Base.metadata.create_all`
which produces an identical schema for SQLite (we control the schema
via SQLAlchemy models). When we move to Postgres, `alembic upgrade
head` will run per-tenant, but for now `create_all` is enough.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml
from sqlalchemy import select

from argosy.config import get_settings
from argosy.state import db as db_mod
from argosy.state.models import Base, SetupToken, Tenant, User
from argosy.tenancy.database import get_tenant_engine, tenant_db_path


SETUP_TOKEN_TTL_HOURS = 72


# ----------------------------------------------------------------------
# Tenant provisioning
# ----------------------------------------------------------------------


async def provision_tenant(
    user_id: str,
    email: str | None = None,
    *,
    plan: str = "free",
    seed_files: bool = True,
) -> Tenant:
    """Create a new tenant row + per-tenant DB + config skeleton.

    Idempotent: if the tenant already exists, the row is updated
    in-place (email rewritten, last_active_at refreshed) and returned.
    """
    settings = get_settings()
    db_path = tenant_db_path(user_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Per-tenant DB schema bootstrap (SQLite create_all).
    eng = get_tenant_engine(user_id)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Configs scaffolding.
    if seed_files:
        cfg_dir = settings.configs_dir / user_id
        cfg_dir.mkdir(parents=True, exist_ok=True)
        ent_path = cfg_dir / "entitlements.yaml"
        if not ent_path.is_file():
            ent_path.write_text(_render_entitlements_yaml(plan), encoding="utf-8")
        brand_path = cfg_dir / "branding.yaml"
        if not brand_path.is_file():
            brand_path.write_text(_DEFAULT_BRANDING_YAML, encoding="utf-8")

    # Control-plane: register the tenant + the user row.
    async with db_mod.get_session() as session:
        tenant = await session.get(Tenant, user_id)
        if tenant is None:
            tenant = Tenant(
                user_id=user_id,
                db_path=str(db_path),
                plan=plan,
                status="active",
            )
            session.add(tenant)
        else:
            tenant.db_path = str(db_path)
            tenant.last_active_at = datetime.now(timezone.utc)
            tenant.plan = plan

        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, email=email, plan=plan)
            session.add(user)
        else:
            if email is not None:
                user.email = email
            user.plan = plan

        await session.commit()
        await session.refresh(tenant)
        return tenant


# Also seed the per-tenant DB with a `users` row so per-tenant FK
# references on user_id resolve. SQLite enforces FKs lazily but this
# keeps us honest.
async def ensure_tenant_user_row(user_id: str, email: str | None = None) -> None:
    from argosy.tenancy.database import get_tenant_session

    async with get_tenant_session(user_id) as session:
        existing = await session.get(User, user_id)
        if existing is None:
            session.add(User(id=user_id, email=email))
            await session.commit()
        elif email is not None and existing.email != email:
            existing.email = email
            await session.commit()


# ----------------------------------------------------------------------
# Setup tokens
# ----------------------------------------------------------------------


async def issue_setup_token(
    user_id: str,
    *,
    ttl_hours: int = SETUP_TOKEN_TTL_HOURS,
    now: datetime | None = None,
) -> str:
    """Create + persist a one-time first-login token. Returns the token."""
    moment = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    async with db_mod.get_session() as session:
        session.add(
            SetupToken(
                token=token,
                user_id=user_id,
                created_at=moment,
                expires_at=moment + timedelta(hours=ttl_hours),
            )
        )
        await session.commit()
    return token


async def consume_setup_token(token: str, *, now: datetime | None = None) -> str | None:
    """Mark a setup token used and return its user_id, or None on miss."""
    moment = now or datetime.now(timezone.utc)
    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(SetupToken).where(SetupToken.token == token)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        # SQLite returns naive datetimes; coerce to UTC for comparison.
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if row.used_at is not None or (expires is not None and expires < moment):
            return None
        row.used_at = moment
        await session.commit()
        return row.user_id


async def list_tenants() -> Iterable[Tenant]:
    async with db_mod.get_session() as session:
        rows = (await session.execute(select(Tenant))).scalars().all()
        return list(rows)


# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------

def _render_entitlements_yaml(plan: str) -> str:
    """Render an entitlements.yaml that just records the plan tier.

    Per-tier feature defaults live in `argosy.billing.entitlements`
    (`_default_features`); we keep the on-disk file minimal so it
    doesn't drift from the defaults table when a tier moves up.
    """
    return (
        "# Argosy entitlements (Phase 6 scaffold).\n"
        "# Plan defaults are resolved from the billing module.\n"
        "# To override a feature flag or numeric limit, add a `features:`\n"
        "# or `limits:` block here.\n"
        f"plan: {plan}\n"
    )


_DEFAULT_BRANDING_YAML = """\
# Argosy branding (Phase 6 default).
app_name: Argosy
theme:
  primary: "#0ea5e9"
  accent: "#f59e0b"
logo_url: /logo.svg
favicon_url: /favicon.ico
support_email: support@argosy.app
"""


__all__ = [
    "consume_setup_token",
    "ensure_tenant_user_row",
    "issue_setup_token",
    "list_tenants",
    "provision_tenant",
    "SETUP_TOKEN_TTL_HOURS",
]


def _ensure_path_exists(p: Path) -> None:  # pragma: no cover - convenience
    p.parent.mkdir(parents=True, exist_ok=True)
