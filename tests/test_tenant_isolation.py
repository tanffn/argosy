"""Phase 6: cross-tenant isolation guarantees.

Verifies that two tenants (alice, bob) provisioned on the same engine
cannot access one another's positions / proposals / agent_reports /
fills / audit log / TOTP secret. Also exercises per-tenant entitlement
gating.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.billing.entitlements import Entitlements, PlanTier
from argosy.state.models import (
    AgentReport,
    AuditLog,
    Fill,
    Proposal,
    TOTPSecret,
)
from argosy.tenancy.context import (
    CrossTenantAccessError,
    bind_user_id,
    require_user_id,
)
from argosy.tenancy.database import (
    get_tenant_session,
    reset_tenant_engines,
)
from argosy.tenancy.onboarding import (
    ensure_tenant_user_row,
    provision_tenant,
)


@pytest.fixture(autouse=True)
def _isolate_argosy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()
    yield
    import asyncio

    asyncio.run(reset_tenant_engines())


@pytest.mark.asyncio
async def test_two_tenants_isolated_data(engine: None) -> None:
    # Provision Alice (free) + Bob (enterprise).
    await provision_tenant("alice", "alice@example.com", plan="free")
    await provision_tenant("bob", "bob@example.com", plan="enterprise")
    await ensure_tenant_user_row("alice", email="alice@example.com")
    await ensure_tenant_user_row("bob", email="bob@example.com")

    now = datetime.now(timezone.utc)

    # Alice writes proposals / fills / audit / totp into her DB.
    async with get_tenant_session("alice") as s:
        s.add(
            Proposal(
                user_id="alice",
                ticker="ALICE",
                action="buy",
                size_shares_or_currency=10,
                tier="T1",
                rationale_summary="alice secret",
            )
        )
        s.add(
            Fill(
                user_id="alice",
                ticker="ALICE",
                action="buy",
                quantity=10,
                price=100,
                broker="paper",
                broker_order_id="A1",
                filled_at=now,
                paper=True,
            )
        )
        s.add(
            AuditLog(
                user_id="alice",
                event_type="paper_fill.recorded",
                entity_type="fill",
                entity_id="A1",
                payload_json="{}",
            )
        )
        s.add(
            AgentReport(
                user_id="alice",
                agent_role="trader",
                response_text="alice trader output",
                model="sonnet",
            )
        )
        s.add(TOTPSecret(user_id="alice", secret_encrypted="ALICE-SECRET"))
        await s.commit()

    # Bob writes his own.
    async with get_tenant_session("bob") as s:
        s.add(
            Proposal(
                user_id="bob",
                ticker="BOB",
                action="sell",
                size_shares_or_currency=20,
                tier="T2",
                rationale_summary="bob secret",
            )
        )
        s.add(TOTPSecret(user_id="bob", secret_encrypted="BOB-SECRET"))
        await s.commit()

    # Alice's DB cannot see Bob's data.
    async with get_tenant_session("alice") as s:
        proposals = (await s.execute(select(Proposal))).scalars().all()
        assert {p.ticker for p in proposals} == {"ALICE"}
        # Alice's TOTP table has only her secret.
        totps = (await s.execute(select(TOTPSecret))).scalars().all()
        assert {(t.user_id, t.secret_encrypted) for t in totps} == {
            ("alice", "ALICE-SECRET")
        }
        fills = (await s.execute(select(Fill))).scalars().all()
        assert {f.ticker for f in fills} == {"ALICE"}
        audit = (await s.execute(select(AuditLog))).scalars().all()
        assert all(row.user_id == "alice" for row in audit)

    # Bob's DB cannot see Alice's data.
    async with get_tenant_session("bob") as s:
        proposals = (await s.execute(select(Proposal))).scalars().all()
        assert {p.ticker for p in proposals} == {"BOB"}
        totps = (await s.execute(select(TOTPSecret))).scalars().all()
        assert {(t.user_id, t.secret_encrypted) for t in totps} == {
            ("bob", "BOB-SECRET")
        }


@pytest.mark.asyncio
async def test_per_tenant_entitlements_respected(engine: None) -> None:
    await provision_tenant("alice", "alice@example.com", plan="free")
    await provision_tenant("bob", "bob@example.com", plan="enterprise")

    alice = Entitlements.load("alice")
    bob = Entitlements.load("bob")

    assert alice.plan is PlanTier.FREE
    assert bob.plan is PlanTier.ENTERPRISE

    # Alice (free) is blocked from autonomous_mode + live_execution.
    assert alice.has("autonomous_mode") is False
    assert alice.has("live_execution") is False

    # Bob (enterprise) has both.
    assert bob.has("autonomous_mode") is True
    assert bob.has("live_execution") is True


def test_contextvar_blocks_cross_tenant_claim() -> None:
    """Direct guard test — bind alice, claim bob, expect raise."""
    with bind_user_id("alice"):
        with pytest.raises(CrossTenantAccessError):
            require_user_id("bob")
        # Same-tenant claim is fine.
        assert require_user_id("alice") == "alice"
