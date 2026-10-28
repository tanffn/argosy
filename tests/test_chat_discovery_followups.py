"""Discovery list membership must never erase a delivered follow-up's evidence."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.history import serialize_history
from argosy.services.chat_advisor.read_team import ReadExecutor, ReadRequest, RecordReference
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.chat_advisor.store import ChatStore
from argosy.state.chat_models import ChatBinding, ChatTurn, NotificationOutbox
from argosy.state.models import AgentReport, Base, ScanState, User


def snowflake(at):
    return str((int(at.replace(tzinfo=UTC).timestamp() * 1000) - 1420070400000) << 22)


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")])
        db.flush()
        for ticker, status in [("AAA", "active"), ("BBB", "dropped"), ("CCC", "quarantined")]:
            db.add(ScanState(user_id="a", ticker=ticker, status=status, rank=1,
                             estimator_json=json.dumps({"one_line": ticker + " preliminary thesis"}),
                             last_estimated_at=datetime(2026, 10, 7),
                             quarantine_reason="liquidity" if status == "quarantined" else ""))
        db.add_all([
            AgentReport(id=1, user_id="a", agent_role="quick_estimator", decision_id="discovery:BBB",
                        response_text="Old preliminary thesis", created_at=datetime(2026, 10, 6)),
            AgentReport(id=2, user_id="a", agent_role="quick_estimator", decision_id="discovery:BBB",
                        response_text="Updated preliminary thesis", created_at=datetime(2026, 10, 7)),
            AgentReport(id=3, user_id="b", agent_role="quick_estimator", decision_id="discovery:BBB",
                        response_text="Other household secret", created_at=datetime(2026, 10, 8)),
            AgentReport(id=4, user_id="a", agent_role="quick_estimator", decision_id="discovery:ARCHIVED",
                        response_text="No scan cache remains", created_at=datetime(2026, 10, 7)),
        ])
        db.commit()
    reader = RetrievalService(db_path=path)
    yield reader, engine, path
    reader.engine.dispose()
    engine.dispose()


def principal():
    return Principal("a", "g", "c", "u")


def test_inventory_stays_active_but_exact_reads_retain_dropped_and_quarantined(archive):
    reader, _, _ = archive
    assert [r["ticker"] for r in reader.read(principal(), "discovery").data["candidates"]] == ["AAA"]
    for ticker, status in [("BBB", "dropped"), ("CCC", "quarantined")]:
        result = reader.read(principal(), "discovery", ticker=" " + ticker.lower() + " ")
        assert result.data["candidates"][0]["status"] == status
        assert result.data["candidates"][0]["research"] is None
        assert "Dropped is not SELL" in result.warnings[0]
    assert not reader.read(principal(), "discovery", ticker="BB").data["candidates"]


@pytest.mark.asyncio
async def test_typed_followup_reads_archive_and_stable_report_not_other_household(archive):
    reader, _, _ = archive
    executor = ReadExecutor(reader, principal())
    result = await executor.read(ReadRequest(ref=RecordReference(record_type="discovery", record_id="BBB")))
    assert executor.entries[-1][0]["reference_found"] is True
    saved = result.data["candidates"][0]["latest_saved_estimator_report"]
    assert saved["record_id"] == "2"
    assert saved["response"] == "Updated preliminary thesis"
    assert any(c.record_type == "agent_report" and c.record_id == "2" for c in result.citations)
    historical = await executor.read(ReadRequest(topic="reports", filters={"discovery_ticker": "BBB", "limit": 1}))
    assert [r["id"] for r in historical.data] == [2]
    older = reader.read(principal(), "reports", discovery_ticker="BBB", limit=1, offset=1)
    assert [r["id"] for r in older.data] == [1]
    assert not reader.read(principal(), "reports", record_id="3").data


def test_missing_cache_still_has_dated_report_history(archive):
    reader, _, _ = archive
    missing = reader.read(principal(), "discovery", ticker="ARCHIVED")
    assert missing.data["candidates"] == []
    continuation = missing.data["history_read"]
    result = reader.read(principal(), continuation["topic"], **continuation["filters"])
    assert result.data[0]["response"] == "No scan cache remains"


def test_changed_cache_has_changed_version_but_old_report_remains_addressable(archive):
    reader, engine, _ = archive
    before = reader.read(principal(), "discovery", ticker="BBB")
    with Session(engine) as db:
        row = db.get(ScanState, ("a", "BBB"))
        row.estimator_json = '{"one_line":"Different thesis"}'
        row.status = "active"
        db.commit()
    after = reader.read(principal(), "discovery", ticker="BBB")
    assert before.citations[-1].version != after.citations[-1].version
    assert reader.read(principal(), "reports", record_id="1").data[0]["response"] == "Old preliminary thesis"


@pytest.mark.asyncio
async def test_replay_history_includes_delivered_menu_excludes_future_and_other_binding(archive):
    _, engine, path = archive
    with Session(engine) as db:
        db.add(ChatBinding(id=1, household_user_id="a", provider="discord", guild_id="g",
                           channel_id="c", provider_user_id="u"))
        for key, owner, binding, state, at in [
            ("prior", "a", 1, "sent", 7), ("future", "a", 1, "sent", 9),
            ("draft", "a", 1, "pending", 7), ("other", "b", 2, "sent", 7),
        ]:
            db.add(NotificationOutbox(id=key, binding_id=binding, household_user_id=owner,
                    semantic_key=key, material_version=key, category="overview.daily",
                    body="1. BBB. Reply 1 for the screen rationale." if key == "prior" else key,
                    citations_json='[{"record_type":"agent_report","record_id":"1","as_of":"2026-10-06"}]',
                    nonce=key, status=state,
                    sent_message_id=snowflake(datetime(2026, 10, at)) if state == "sent" else None,
                    created_at=datetime(2026, 10, at)))
        db.add(ChatTurn(id="selected", binding_id=1, household_user_id="a", conversation_key="c",
                       inbound_message_id="inbound", question="1", response="Old failed reply",
                       response_nonce="selected", status="completed", created_at=datetime(2026, 10, 8)))
        db.commit()
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        store = ChatStore(async_sessionmaker(async_engine), "a", binding_id=1)
        history = serialize_history(await store.history("c", before_at=datetime(2026, 10, 8)))
        assert len(history) == 1
        assert history[0]["content"].startswith("1. BBB")
        assert history[0]["citations"][0]["record_id"] == "1"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_delivery_order_not_draft_order_and_no_future_completed_turn(archive):
    _, engine, path = archive
    with Session(engine) as db:
        db.add(ChatBinding(id=1, household_user_id="a", provider="discord", guild_id="g",
                           channel_id="c", provider_user_id="u"))
        for key, drafted, delivered in [("late", 5, 9), ("old", 7, 7), ("new", 6, 8)]:
            db.add(NotificationOutbox(id=key, binding_id=1, household_user_id="a",
                    semantic_key=key, material_version=key, category="overview.daily",
                    body=key, nonce=key, status="sent", sent_message_id=snowflake(datetime(2026, 10, delivered)),
                    created_at=datetime(2026, 10, drafted)))
        for index in range(101):
            db.add(NotificationOutbox(id=f"older-{index}", binding_id=1, household_user_id="a",
                    semantic_key=f"older-{index}", material_version="v", category="overview.daily",
                    body="older", nonce=f"older-{index}", status="sent",
                    sent_message_id=str(int(snowflake(datetime(2026, 10, 1))) + index),
                    created_at=datetime(2026, 10, 1)))
        db.add(NotificationOutbox(id="legacy", binding_id=1, household_user_id="a",
                semantic_key="legacy", material_version="v", category="overview.daily",
                body="legacy", nonce="legacy", status="sent", sent_message_id="legacy-provider-id",
                created_at=datetime(2026, 10, 1), updated_at=datetime(2026, 10, 8, 1)))
        db.add(ChatTurn(id="slow", binding_id=1, household_user_id="a", conversation_key="c",
                       inbound_message_id="slow", question="Earlier question", response="Later answer",
                       response_nonce="slow", status="completed", created_at=datetime(2026, 10, 5),
                       updated_at=datetime(2026, 10, 10)))
        db.commit()
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        store = ChatStore(async_sessionmaker(async_engine), "a", binding_id=1)
        rows = await store.history("c", before_at=datetime(2026, 10, 8, 12))
        assert [row["response"] for row in rows][-3:] == ["old", "new", "legacy"]
        assert len(rows) == 20
        current = await store.history("c")
        assert [row["response"] for row in current][-5:] == ["old", "new", "legacy", "late", "Later answer"]
    finally:
        await async_engine.dispose()


def test_saved_turn_replay_resolves_thread_and_configured_binding(archive):
    from scripts.check_recent_discord_reply import load_saved_turn
    _, engine, path = archive
    binding = SimpleNamespace(household_user_id="a", guild_id="g", channel_id="c", user_id="u")
    with Session(engine) as db:
        db.add_all([ChatBinding(id=1, household_user_id="a", provider="discord", guild_id="g",
                               channel_id="c", provider_user_id="u"),
                    ChatBinding(id=2, household_user_id="a", provider="discord", guild_id="g",
                               channel_id="c", provider_user_id="other")])
        for key, owner in [("thread", 1), ("foreign", 2)]:
            db.add(ChatTurn(id=key, binding_id=owner, household_user_id="a", conversation_key="thread-channel",
                           inbound_message_id=key, question="1", response="Answer", response_nonce=key,
                           status="completed", created_at=datetime(2026, 10, 8)))
        db.commit()
    turn, selected = load_saved_turn(path, [binding], "thread")
    assert selected is binding and turn["conversation_key"] == "thread-channel"
    with pytest.raises(ValueError, match="configured Discord identities"):
        load_saved_turn(path, [binding], "foreign")
