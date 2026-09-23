from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.services.chat_advisor.contracts import OutboxEvent
from argosy.services.chat_advisor.store import ChatStore


@pytest.mark.asyncio
async def test_migrated_chat_store_is_idempotent_and_tenant_bound(alembic_engine_at_head):
    with alembic_engine_at_head.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) VALUES ('owner', 'free', CURRENT_TIMESTAMP), ('other', 'free', CURRENT_TIMESTAMP)"
            )
        )
    async_engine = create_async_engine(
        str(alembic_engine_at_head.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    )
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        owner = ChatStore(sessions, "owner")
        await owner.ensure_binding(
            provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
        )
        first = await owner.create_request("100", "2", [" nvda ", "NVDA"], prompt_text="Dated news evidence")
        second = await owner.create_request("100", "2", ["QURE"])
        assert first.id == second.id
        assert first.instruments == ["NVDA"]
        assert (await owner.get_request(first.id)).prompt_text == "Dated news evidence"
        assert second.prompt_text == "Dated news evidence"
        assert await owner.count_active_requests() == 1

        other = ChatStore(sessions, "other", binding_id=owner.binding_id)
        assert await other.get_request(first.id) is None

        row1 = await owner.enqueue_outbox(
            OutboxEvent("owner:action:changed", "v1", "action", "hello")
        )
        row2 = await owner.enqueue_outbox(
            OutboxEvent("owner:action:changed", "v1", "action", "changed text")
        )
        assert row1.id == row2.id
        assert row1.nonce == row2.nonce
    finally:
        await async_engine.dispose()


def test_migration_has_six_chat_records_progress_and_policy(alembic_engine_at_head):
    inspector = sa.inspect(alembic_engine_at_head)
    expected = {
        "chat_bindings",
        "chat_threads",
        "chat_turns",
        "chat_analysis_requests",
        "notification_outbox",
        "chat_cursors",
        "chat_agent_progress",
    }
    assert expected <= set(inspector.get_table_names())
    columns = {column["name"] for column in inspector.get_columns("decision_runs")}
    assert "execution_policy" in columns
