"""Independent reviewer regressions against actual chat SQLite persistence."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.services.chat_advisor.store import ChatStore
from argosy.state.models import Base, User


@pytest.fixture
async def review_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(User(id="owner"))
        await session.commit()
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(
        provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
    )
    try:
        yield store
    finally:
        await engine.dispose()


@pytest.mark.real_seam
async def test_parallel_agent_receipts_cannot_drop_or_kill_a_fleet(review_store):
    store = review_store
    request = await store.create_request("message-1", "2", ["TEST"])
    await asyncio.gather(
        *[
            store.append_progress(
                request.id, None, f"analyst-{i}", "started", dedup_key=f"agent-{i}-started"
            )
            for i in range(12)
        ]
    )
    records = await store.progress_rows(request.id)
    assert len(records) == 12
    assert len({record.sequence for record in records}) == 12
    assert {record.agent for record in records} == {f"analyst-{i}" for i in range(12)}


@pytest.mark.real_seam
async def test_cursor_cannot_regress_after_older_request_finishes(review_store):
    await review_store.update_cursor("2", last_message_id="900")
    await review_store.update_cursor("2", last_message_id="800")
    cursor = await review_store.cursor()
    assert cursor.last_message_id == "900"


@pytest.mark.real_seam
async def test_delivery_nonce_survives_duplicate_reservation(review_store):
    turn, _ = await review_store.create_turn(
        inbound_message_id="nonce-probe", conversation_key="2", question="status?"
    )
    first = await review_store.reserve_turn_delivery(turn.id, response="An answer")
    second = await review_store.reserve_turn_delivery(turn.id, response="An answer")
    assert first == second
