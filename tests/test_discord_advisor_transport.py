from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.services.chat_advisor.contracts import ChatAnswer, Citation, OutboxEvent
from argosy.services.chat_advisor.store import ChatStore
from argosy.state.chat_models import ChatTurn
from argosy.state.models import Base, User
from argosy.transport.discord_advisor import job as job_module
from argosy.transport.discord_advisor.auth import InboundIdentity, authorize
from argosy.transport.discord_advisor.config import DiscordAdvisorConfig, DiscordBindingConfig
from argosy.transport.discord_advisor.gateway import (
    DiscordAdvisorAccessError,
    DiscordPyGateway,
    FakeGateway,
    InboundMessage,
)
from argosy.transport.discord_advisor.job import DiscordAdvisorJob, build_discord_advisor_job
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker


def _config() -> DiscordAdvisorConfig:
    return DiscordAdvisorConfig(
        enabled=True,
        bindings=(
            DiscordBindingConfig(
                guild_id="1", channel_id="2", user_id="3", household_user_id="owner"
            ),
        ),
    )


def test_authorization_rejects_dm_foreign_bot_webhook_and_unowned_thread():
    cfg = _config()
    rejected = [
        InboundIdentity(None, "2", "3"),
        InboundIdentity("1", "999", "3"),
        InboundIdentity("1", "2", "999"),
        InboundIdentity("1", "2", "3", is_bot=True),
        InboundIdentity("1", "2", "3", is_webhook=True),
        InboundIdentity("1", "8", "3", parent_channel_id="2", thread_id="8", thread_owner_id="999"),
    ]
    assert all(authorize(cfg, event) is None for event in rejected)
    principal = authorize(
        cfg,
        InboundIdentity("1", "8", "3", parent_channel_id="2", thread_id="8", thread_owner_id="3"),
    )
    assert principal is not None and principal.household_user_id == "owner"


def test_disabled_factory_is_neutral_and_missing_token_is_actionable():
    assert build_discord_advisor_job(config=DiscordAdvisorConfig()) is None
    job = build_discord_advisor_job(config=_config(), token="", gateway=FakeGateway())
    assert isinstance(job, DiscordAdvisorJob)
    status = job.status_snapshot()
    assert status["enabled"] is True and status["configured"] is False
    assert "keyring" in status["repair"]


@pytest.mark.asyncio
async def test_fake_gateway_nonce_is_idempotent():
    gateway = FakeGateway()
    one = await gateway.send("2", "hello", nonce="persisted")
    two = await gateway.send("2", "hello", nonce="persisted")
    assert one == two
    assert len(gateway.sent) == 1


class _Dispatcher:
    def __init__(self, store):
        self.store = store

    async def recover(self):
        return None

    async def stop(self):
        return None


class _Conversation:
    def __init__(self, _retrieval, _dispatcher):
        pass

    async def answer(self, _principal, text, _message_id, history=()):
        if history and history[-1].get("citations"):
            return ChatAnswer("context: v-1")
        return ChatAnswer(
            f"answer: {text}",
            [Citation("verdict", "v-1", "2026-09-19T00:00:00Z")],
        )


async def _worker_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(User(id="owner"))
        await session.commit()
    return engine, sessions


async def _eventually(predicate, *, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cursorless_recovery_only_answers_post_binding_messages(tmp_path):
    from datetime import timedelta

    from discord.utils import time_snowflake

    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(), "test-token", gateway, session_factory=sessions,
        retrieval_factory=lambda _: object(), conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=False,
    )
    try:
        await worker.initialize()
        store = worker._stores[("1", "2", "3")]
        created = await store.binding_created_at()
        old_id = str(time_snowflake(created - timedelta(seconds=1)))
        new_id = str(time_snowflake(created + timedelta(seconds=1)))
        outsider_id = str(time_snowflake(created + timedelta(seconds=2)))
        gateway.history = [
            InboundMessage(old_id, InboundIdentity("1", "2", "3"), "old"),
            InboundMessage(new_id, InboundIdentity("1", "2", "3"), "Hi"),
            InboundMessage(outsider_id, InboundIdentity("1", "2", "999"), "private?"),
        ]
        await worker.catch_up()
        assert len(gateway.sent) == 1
        assert gateway.sent[0]["text"] == "answer: Hi"
        assert (await store.cursor()).last_message_id == new_id
        await worker.catch_up()
        assert len(gateway.sent) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_real_sqlite_authorizes_deduplicates_and_persists(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    task = asyncio.create_task(worker.run())
    try:
        await _eventually(lambda: gateway.started and bool(worker._stores))
        unauthorized = InboundMessage("9", InboundIdentity("1", "2", "999"), "private?")
        await gateway.inbound.put(unauthorized)
        allowed = InboundMessage("10", InboundIdentity("1", "2", "3"), "status?")
        await gateway.inbound.put(allowed)
        await _eventually(lambda: len(gateway.sent) == 1)
        await gateway.inbound.put(allowed)
        await asyncio.sleep(0.05)
        assert len(gateway.sent) == 1
        await gateway.inbound.put(
            InboundMessage(
                "11",
                InboundIdentity("1", "2", "3"),
                "why?",
                reply_to_message_id=gateway.sent[0]["id"],
            )
        )
        await _eventually(lambda: len(gateway.sent) == 2 and gateway.sent[1]["text"] == "context: v-1")
        assert gateway.sent[1]["text"] == "context: v-1"
        await gateway.inbound.put(
            InboundMessage(
                "12",
                InboundIdentity("1", "2", "3"),
                "why?",
                reply_to_message_id="999999",
            )
        )
        await _eventually(lambda: len(gateway.sent) == 3 and "won’t guess" in gateway.sent[2]["text"])
        assert "won’t guess" in gateway.sent[2]["text"]
        await asyncio.sleep(0.05)
        async with sessions() as session:
            turns = list((await session.scalars(select(ChatTurn))).all())
        assert len(turns) == 3
        assert all(turn.status == "completed" for turn in turns)
        assert turns[0].response == "answer: status?"
        assert "private?" not in {turn.question for turn in turns}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_restart_retries_pending_thread_delivery_with_citations_and_nonce(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(
        provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
    )
    turn, _ = await store.create_turn(
        inbound_message_id="20", conversation_key="88", question="why?"
    )
    base_nonce = await store.reserve_turn_delivery(
        turn.id,
        response="persisted answer",
        citations=[Citation("decision_run", "42", "2026-09-19T00:00:00Z")],
    )
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    task = asyncio.create_task(worker.run())
    try:
        await _eventually(lambda: len(gateway.sent) == 1)
        sent = gateway.sent[0]
        assert sent["channel_id"] == "88"
        assert "decision_run:42" not in sent["text"]  # Provenance stays in persisted context.
        assert len(sent["nonce"]) <= 25 and sent["nonce"] != base_nonce
        async with sessions() as session:
            persisted = await session.get(ChatTurn, turn.id)
            assert persisted.status == "completed"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_auth_failure_propagates_without_orphan_ready_task(tmp_path):
    engine, sessions = await _worker_db(tmp_path)

    class LoginFailure(Exception):
        pass

    class FailingGateway(FakeGateway):
        async def start(self, token: str) -> None:
            raise LoginFailure("bad token")

    gateway = FailingGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "bad",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    with pytest.raises(LoginFailure):
        await asyncio.wait_for(worker.run(), timeout=1)
    await asyncio.sleep(0)
    assert not gateway.started
    await engine.dispose()


@pytest.mark.asyncio
async def test_access_validation_is_terminal_actionable_and_precedes_db_init(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    gateway.access_error = DiscordAdvisorAccessError(
        "channel_not_private",
        "The configured advisor channel is visible to @everyone.",
        "Deny @everyone View Channel and explicitly allow only the owner and bot.",
    )
    job = DiscordAdvisorJob(
        _config(),
        "valid-token",
        gateway=gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    await asyncio.wait_for(job.run(), timeout=1)
    status = job.status_snapshot()
    assert status["connection"] == "stopped"
    assert "@everyone" in status["error"]
    assert status["repair"].startswith("Deny @everyone")
    assert job._worker is not None and job._worker._stores == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_run_clears_runtime_error_without_reloading_injected_inputs(
    monkeypatch,
):
    attempts = 0

    class Worker:
        status_board_error = None

        def __init__(self, config, token, gateway, *, on_ready, **_kwargs):
            assert config is injected_config
            assert token == "injected-token"
            self.gateway = gateway
            self.on_ready = on_ready

        async def run(self):
            nonlocal attempts
            attempts += 1
            self.on_ready()
            if attempts == 1:
                raise DiscordAdvisorAccessError("bot_not_invited", "not invited", "Invite the bot.")

    monkeypatch.setattr(
        job_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not reload injected config")),
    )
    monkeypatch.setattr(
        job_module,
        "load_bot_token",
        lambda: (_ for _ in ()).throw(AssertionError("must not reload injected token")),
    )
    injected_config = _config()
    job = DiscordAdvisorJob(
        injected_config,
        "injected-token",
        gateway=FakeGateway(),
        worker_factory=Worker,
    )
    await job.run()
    assert job.status_snapshot()["error"] == "not invited"
    await job.run()
    status = job.status_snapshot()
    assert attempts == 2
    assert status["error"] is None
    assert status["configured"] is True


@pytest.mark.asyncio
async def test_production_factory_reloads_saved_config_and_token_on_each_explicit_run(
    monkeypatch,
):
    configs = [_config(), _config(), DiscordAdvisorConfig()]
    tokens = ["initial-token", "fresh-token"]
    observed = []

    class Worker:
        status_board_error = "status board delayed"

        def __init__(self, config, token, gateway, *, on_ready, **_kwargs):
            observed.append((config, token))
            self.gateway = gateway
            self.on_ready = on_ready

        async def run(self):
            self.on_ready()

    monkeypatch.setattr(job_module, "load_config", lambda: configs.pop(0))
    monkeypatch.setattr(job_module, "load_bot_token", lambda: tokens.pop(0))
    monkeypatch.setattr(job_module, "DiscordPyGateway", lambda: object())
    job = build_discord_advisor_job(worker_factory=Worker)
    assert isinstance(job, DiscordAdvisorJob)
    await job.run()
    assert observed[-1][1] == "fresh-token"
    status = job.status_snapshot()
    assert status["error"] is None
    assert status["status_board_error"] == "status board delayed"

    # A later saved disable is neutral and does not reuse the prior token.
    await job.run()
    status = job.status_snapshot()
    assert status["enabled"] is False
    assert status["error"] is None
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_production_reconnect_surfaces_new_invalid_config(monkeypatch):
    job = DiscordAdvisorJob(
        _config(),
        "old-token",
        reload_runtime_configuration=True,
    )
    monkeypatch.setattr(
        job_module,
        "load_config",
        lambda: (_ for _ in ()).throw(ValueError("invalid saved JSON")),
    )
    await job.run()
    status = job.status_snapshot()
    assert status["configured"] is False
    assert status["error"].startswith("Invalid Discord advisor configuration")
    assert "discord_advisor.json" in status["repair"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("not_invited", "bot_not_invited"),
        ("guild_mismatch", "channel_guild_mismatch"),
        ("bot_permissions", "bot_permissions"),
        ("owner_view", "owner_cannot_view"),
        ("public", "channel_not_private"),
    ],
)
async def test_discord_gateway_validates_binding_privacy_and_permissions(case, code):
    bot = object()
    owner = object()
    everyone = object()
    guild = SimpleNamespace(id=1, me=bot, default_role=everyone)
    channel_guild = guild if case != "guild_mismatch" else SimpleNamespace(id=999)

    def permissions(member):
        if member is bot:
            return SimpleNamespace(
                view_channel=True,
                send_messages=case != "bot_permissions",
                read_message_history=True,
            )
        if member is owner:
            return SimpleNamespace(view_channel=case != "owner_view")
        return SimpleNamespace(view_channel=case == "public")

    channel = SimpleNamespace(guild=channel_guild, permissions_for=permissions)
    guild.get_channel = lambda _channel_id: channel
    guild.get_member = lambda _user_id: owner
    client = SimpleNamespace(get_guild=lambda _guild_id: None if case == "not_invited" else guild)
    gateway = DiscordPyGateway.__new__(DiscordPyGateway)
    gateway.client = client
    with pytest.raises(DiscordAdvisorAccessError) as caught:
        await gateway.validate_access(_config().bindings)
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_send_and_edit_recheck_cached_channel_privacy_immediately():
    sent = []
    edited = []
    everyone = object()
    guild = SimpleNamespace(default_role=everyone)

    class Message:
        id = 77

        async def edit(self, **kwargs):
            edited.append(kwargs)

    class Channel:
        def __init__(self):
            self.guild = guild
            self.public = True

        def permissions_for(self, member):
            assert member is everyone
            return SimpleNamespace(view_channel=self.public)

        async def send(self, *args, **kwargs):
            sent.append((args, kwargs))
            return Message()

        async def fetch_message(self, message_id):
            assert message_id == 77
            return Message()

    channel = Channel()
    gateway = DiscordPyGateway.__new__(DiscordPyGateway)
    gateway._channel = lambda _channel_id: asyncio.sleep(0, result=channel)
    with pytest.raises(DiscordAdvisorAccessError) as send_error:
        await gateway.send("2", "financial reply", nonce="n")
    with pytest.raises(DiscordAdvisorAccessError) as edit_error:
        await gateway.edit("2", "77", "status")
    assert send_error.value.code == edit_error.value.code == "channel_not_private"
    assert sent == [] and edited == []

    # discord.py mutates the cached channel permission state from gateway events;
    # the next send observes that state without a REST fetch.
    channel.public = False
    assert await gateway.send("2", "financial reply", nonce="n") == "77"
    await gateway.edit("2", "77", "status")
    assert len(sent) == 1 and len(edited) == 1


@pytest.mark.asyncio
async def test_transient_send_is_retried_from_durable_pending_without_restart(tmp_path):
    engine, sessions = await _worker_db(tmp_path)

    class FlakyGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.failures = 1

        async def send(self, channel_id: str, text: str, *, nonce: str) -> str:
            if self.failures:
                self.failures -= 1
                raise OSError("temporary Discord outage")
            return await super().send(channel_id, text, nonce=nonce)

    gateway = FlakyGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    task = asyncio.create_task(worker.run())
    try:
        await _eventually(lambda: gateway.started and bool(worker._stores))
        await gateway.inbound.put(InboundMessage("30", InboundIdentity("1", "2", "3"), "status?"))
        await _eventually(lambda: len(gateway.sent) == 1)
        await asyncio.sleep(0.05)
        async with sessions() as session:
            turn = (await session.scalars(select(ChatTurn))).one()
        assert turn.status == "completed"
        assert gateway.failures == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_processing_delivery_retry_preserves_durable_failure(tmp_path):
    engine, sessions = await _worker_db(tmp_path)

    class FailingConversation:
        def __init__(self, _retrieval, _dispatcher):
            pass

        async def answer(self, *_args, **_kwargs):
            raise RuntimeError("private provider detail must not be persisted")

    class FlakyGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.failures = 1

        async def send(self, channel_id: str, text: str, *, nonce: str) -> str:
            if self.failures:
                self.failures -= 1
                raise OSError("temporary Discord outage")
            return await super().send(channel_id, text, nonce=nonce)

    gateway = FlakyGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=FailingConversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    task = asyncio.create_task(worker.run())
    try:
        await _eventually(lambda: gateway.started and bool(worker._stores))
        await gateway.inbound.put(InboundMessage("31", InboundIdentity("1", "2", "3"), "help"))
        await _eventually(lambda: len(gateway.sent) == 1)
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            async with sessions() as session:
                turn = (await session.scalars(select(ChatTurn))).one()
            if turn.status == "failed":
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"turn remained {turn.status}")
            await asyncio.sleep(0.01)
        assert turn.status == "failed"
        assert turn.error == "Internal processing failure (RuntimeError)"
        assert "private provider detail" not in turn.error
        assert turn.response_message_ids_json == '["1"]'
        assert "no confirmed result" in gateway.sent[0]["text"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_initialize_reconciles_dispatcher_before_recovering_queued_turns(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    seed = ChatStore(sessions, "owner")
    await seed.ensure_binding(
        provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
    )
    await seed.create_turn(inbound_message_id="recover-1", conversation_key="2", question="analyze")
    events = []

    class OrderedDispatcher(_Dispatcher):
        async def recover(self):
            events.append("dispatcher")

    class OrderedConversation:
        def __init__(self, _retrieval, _dispatcher):
            pass

        async def answer(self, *_args, **_kwargs):
            events.append("conversation")
            return ChatAnswer("recovered")

    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=OrderedConversation,
        dispatcher_factory=OrderedDispatcher,
        notification_producer=False,
    )
    await worker.initialize()
    assert events == ["dispatcher", "conversation"]
    async with sessions() as session:
        turn = (await session.scalars(select(ChatTurn))).one()
    assert turn.status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovered_poison_turn_becomes_failed_and_does_not_block_later_turn(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    seed = ChatStore(sessions, "owner")
    await seed.ensure_binding(
        provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
    )
    await seed.create_turn(inbound_message_id="poison", conversation_key="2", question="poison")
    await seed.create_turn(inbound_message_id="healthy", conversation_key="2", question="healthy")

    class RecoveryConversation:
        def __init__(self, _retrieval, _dispatcher):
            pass

        async def answer(self, _principal, text, *_args, **_kwargs):
            if text == "poison":
                raise ValueError("bad durable input")
            return ChatAnswer("healthy answer")

    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=RecoveryConversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    await worker.initialize()
    async with sessions() as session:
        turns = list((await session.scalars(select(ChatTurn).order_by(ChatTurn.created_at))).all())
    assert [turn.status for turn in turns] == ["failed", "completed"]
    assert len(gateway.sent) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_analysis_dictionary_outcomes_persist_decision_run_citations(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(
        provider="discord", guild_id="1", channel_id="2", provider_user_id="3"
    )
    request = await store.create_request("analysis-1", "2", ["NVDA"])
    await store.update_request(
        request.id,
        status="completed",
        result={"outcomes": [{"decision_run_id": 42, "verdict_id": 17, "status": "completed", "as_of": "2026-09-18T00:00:00Z"}]},
    )
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    await worker._watch_analysis(store, request.id, "2")
    async with sessions() as session:
        final_turn = (
            await session.scalars(
                select(ChatTurn).where(ChatTurn.question == f"Analysis result {request.id}")
            )
        ).one()
    citations = json.loads(final_turn.citations_json)
    assert citations[0]["record_type"] == "decision_run"
    assert citations[0]["record_id"] == "42"
    assert citations[1]["record_type"] == "verdict"
    assert citations[1]["record_id"] == "17"
    assert citations[1]["as_of"] == "2026-09-18T00:00:00Z"
    assert all("decision_run:42" not in sent["text"] for sent in gateway.sent)
    await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_exit_at_ready_prevents_worker_initialization(tmp_path):
    engine, sessions = await _worker_db(tmp_path)

    class DeadAtReadyGateway(FakeGateway):
        async def start(self, token: str) -> None:
            self.started = True
            await self._ready.put(True)
            await asyncio.sleep(0)

    gateway = DeadAtReadyGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    with pytest.raises(RuntimeError, match="gateway exited"):
        await asyncio.wait_for(worker.run(), timeout=1)
    assert worker._stores == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_critical_notifications_dedup_push_changes_and_resolve(tmp_path):
    engine, sessions = await _worker_db(tmp_path)

    class Producer:
        def __init__(self):
            self.events = [OutboxEvent("owner:a:change", "v1", "alert.critical", "first")]

        def current_events(self, _principal):
            return self.events

        def still_current(self, _principal, event):
            return any(
                candidate.semantic_key == event.semantic_key
                and candidate.material_version == event.material_version
                for candidate in self.events
            )

    producer = Producer()
    from datetime import time
    cfg = _config().model_copy(update={"daily_overview_time": time(23, 59, 59)})
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        cfg,
        "test-token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=producer,
    )
    await worker.initialize()
    await worker.pump_notifications()
    await worker.pump_notifications()
    assert len(gateway.sent) == 1
    assert gateway.edits == []

    producer.events = [OutboxEvent("owner:a:change", "v2", "alert.critical", "changed")]
    await worker.pump_notifications()
    assert len(gateway.sent) == 2
    assert gateway.sent[-1]["text"] == "🔴 changed"

    producer.events = []
    await worker.pump_notifications()
    assert len(gateway.sent) == 2
    assert gateway.edits[-1]["message_id"] == gateway.sent[-1]["id"]
    assert gateway.edits[-1]["text"].startswith("No longer actionable")
    producer.events = [OutboxEvent("owner:a:change", "v2", "alert.critical", "changed")]
    await worker.pump_notifications()
    await worker.pump_notifications()
    assert len(gateway.sent) == 3  # Same problem returns after a recorded recovery.
    await engine.dispose()
