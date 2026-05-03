"""Argosy tenancy primitives (Phase 6).

Multi-tenant routing, per-tenant database engine factory, and the
request-scoped contextvar that downstream code reads via
`current_user_id()`. All previous phases threaded `user_id` through
explicit parameters; Phase 6 keeps that contract intact and layers a
contextvar on top so request-scoped helpers don't have to refactor.
"""

from __future__ import annotations

from argosy.tenancy.context import (
    CrossTenantAccessError,
    TenantContext,
    bind_user_id,
    current_user_id,
    require_user_id,
    set_current_user_id,
)
from argosy.tenancy.database import (
    get_tenant_engine,
    get_tenant_session,
    reset_tenant_engines,
    tenant_db_path,
)

__all__ = [
    "CrossTenantAccessError",
    "TenantContext",
    "bind_user_id",
    "current_user_id",
    "require_user_id",
    "set_current_user_id",
    "get_tenant_engine",
    "get_tenant_session",
    "reset_tenant_engines",
    "tenant_db_path",
]
