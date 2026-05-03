"""Phase 6: tenant onboarding integration test (SDD §13 exit gate).

The exit gate for Phase 6 reads:

> Second user onboarded end-to-end without engine changes; their plan
> critique passes; they can run a paper-mode month.

This test exercises:
  - `provision_tenant("bob", ...)` against a fresh control DB.
  - The new tenant gets per-tenant DB + per-tenant config dir.
  - The new tenant can intake + record agent_reports.
  - No cross-tenant data leak in the control DB users table.

We don't actually call Claude; the agent_reports row is written
directly to mimic what a real critique run would do.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport,
    Base,
    SetupToken,
    Tenant,
    User,
)
from argosy.tenancy.database import (
    get_tenant_engine,
    get_tenant_session,
    reset_tenant_engines,
    tenant_db_path,
)
from argosy.tenancy.onboarding import (
    consume_setup_token,
    ensure_tenant_user_row,
    issue_setup_token,
    list_tenants,
    provision_tenant,
)


@pytest.fixture(autouse=True)
def _isolate_argosy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets a fresh ARGOSY_HOME so per-tenant DBs don't leak."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()
    yield
    # cleanup engines created during the test
    import asyncio

    asyncio.run(reset_tenant_engines())


@pytest.mark.asyncio
async def test_provision_tenant_creates_per_tenant_db(engine: None, tmp_path: Path) -> None:
    tenant = await provision_tenant("bob", "bob@example.com", plan="pro")
    assert tenant.user_id == "bob"
    assert tenant.plan == "pro"

    # Per-tenant DB file exists.
    db_path = tenant_db_path("bob")
    assert db_path.is_file()

    # Per-tenant config dir + entitlements scaffold exists.
    cfg_dir = Path(os.environ["ARGOSY_HOME"]) / "configs" / "bob"
    assert (cfg_dir / "entitlements.yaml").is_file()
    assert (cfg_dir / "branding.yaml").is_file()

    # users row is in the control DB.
    async with db_mod.get_session() as session:
        u = await session.get(User, "bob")
        assert u is not None
        assert u.email == "bob@example.com"


@pytest.mark.asyncio
async def test_provision_is_idempotent(engine: None) -> None:
    t1 = await provision_tenant("carol", "carol@example.com", plan="free")
    t2 = await provision_tenant("carol", "carol@example.com", plan="pro")
    assert t1.user_id == t2.user_id == "carol"
    # Plan was updated in-place to pro.
    async with db_mod.get_session() as session:
        u = await session.get(User, "carol")
        assert u is not None and u.plan == "pro"


@pytest.mark.asyncio
async def test_setup_token_lifecycle(engine: None) -> None:
    await provision_tenant("dave", "dave@example.com")
    tok = await issue_setup_token("dave")
    assert isinstance(tok, str) and len(tok) > 16

    # Consume once -> success
    user_id = await consume_setup_token(tok)
    assert user_id == "dave"

    # Consume again -> fail (used)
    user_id2 = await consume_setup_token(tok)
    assert user_id2 is None


@pytest.mark.asyncio
async def test_full_onboarding_flow(engine: None) -> None:
    """End-to-end: provision tenant, write an agent_report, verify isolation."""
    # Two tenants, two per-tenant DBs.
    await provision_tenant("alice", "alice@example.com", plan="free")
    await provision_tenant("bob", "bob@example.com", plan="enterprise")
    await ensure_tenant_user_row("alice", email="alice@example.com")
    await ensure_tenant_user_row("bob", email="bob@example.com")

    # Write a critique-style agent_report into Alice's per-tenant DB.
    async with get_tenant_session("alice") as session:
        session.add(
            AgentReport(
                user_id="alice",
                agent_role="plan_critique",
                response_text="alice plan looks good",
                model="sonnet",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    # And one into Bob's per-tenant DB.
    async with get_tenant_session("bob") as session:
        session.add(
            AgentReport(
                user_id="bob",
                agent_role="plan_critique",
                response_text="bob plan needs work",
                model="sonnet",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    # Verify no cross-tenant leak: alice's DB has only alice's row.
    async with get_tenant_session("alice") as session:
        rows = (await session.execute(select(AgentReport))).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == "alice"
        assert "alice" in rows[0].response_text

    async with get_tenant_session("bob") as session:
        rows = (await session.execute(select(AgentReport))).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == "bob"
        assert "bob" in rows[0].response_text

    # Control DB has both tenants registered.
    tenants = list(await list_tenants())
    user_ids = {t.user_id for t in tenants}
    assert {"alice", "bob"}.issubset(user_ids)
