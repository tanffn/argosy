"""In-process Discord advisor worker with durable inbound/outbound recovery."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from discord.utils import time_snowflake

from argosy.services.chat_advisor.analysis_dispatch import AnalysisDispatcher
from argosy.services.chat_advisor.contracts import ChatAnswer, Citation
from argosy.services.chat_advisor.conversation import ConversationService
from argosy.services.chat_advisor.history import serialize_history
from argosy.services.chat_advisor.notification_policy import (
    CRITICAL,
    DAILY,
    OPERATIONAL,
    delivery_events,
)
from argosy.services.chat_advisor.notifications import NotificationProducer
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.services.chat_advisor.presentation import render_analysis_result
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.chat_advisor.store import ChatStore
from argosy.state.db import get_session_factory

from .auth import InboundIdentity, authorize
from .config import DiscordAdvisorConfig
from .gateway import DiscordAdvisorAccessError, Gateway, InboundMessage
from .progress import ProgressEditor


class DiscordAdvisorWorker:
    def __init__(
        self,
        config: DiscordAdvisorConfig,
        token: str,
        gateway: Gateway,
        *,
        session_factory=None,
        retrieval_factory=None,
        notification_producer=None,
        conversation_factory=ConversationService,
        dispatcher_factory=AnalysisDispatcher,
        on_ready=None,
        max_conversations: int = 8,
        status_registry=None,
        notification_retry_seconds: float = 60.0,
    ) -> None:
        self.config = config
        self.token = token
        self.gateway = gateway
        self.session_factory = session_factory or get_session_factory()
        self.retrieval_factory = retrieval_factory or (lambda _principal: RetrievalService())
        self.conversation_factory = conversation_factory
        self.dispatcher_factory = dispatcher_factory
        self.on_ready = on_ready
        self.status_registry = status_registry
        self.status_board_error: str | None = None
        self.notification_error: str | None = None
        self._notification_retry_seconds = max(0.01, notification_retry_seconds)
        self.notification_producer = (
            None
            if notification_producer is False
            else notification_producer or NotificationProducer(retrieval=RetrievalService())
        )
        self._concurrency = asyncio.Semaphore(max(1, max_conversations))
        self._stores: dict[tuple[str, str, str], ChatStore] = {}
        self._services: dict[tuple[str, str, str], ConversationService] = {}
        self._dispatchers: list[object] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._gateway_task: asyncio.Task | None = None
        self._analysis_tasks: set[asyncio.Task] = set()
        self._daily_drafts: dict[tuple, asyncio.Task] = {}
        self._delivery_wakeup = asyncio.Event()
        self._background_failures: asyncio.Queue[Exception] = asyncio.Queue()

    async def initialize(self) -> None:
        for binding in self.config.bindings:
            key = (binding.guild_id, binding.channel_id, binding.user_id)
            store = ChatStore(self.session_factory, binding.household_user_id)
            await store.ensure_binding(
                provider="discord",
                guild_id=binding.guild_id,
                channel_id=binding.channel_id,
                provider_user_id=binding.user_id,
            )
            self._stores[key] = store
            # Retrieval resolves tenant DB from the authenticated Principal.
            principal = authorize(
                self.config, InboundIdentity(binding.guild_id, binding.channel_id, binding.user_id)
            )
            assert principal is not None
            dispatcher = self.dispatcher_factory(store)
            self._dispatchers.append(dispatcher)
            self._services[key] = self.conversation_factory(
                self.retrieval_factory(principal), dispatcher
            )
            # Reconcile pre-existing fleet work before recovering chat turns.
            # A recovered chat turn may create and auto-schedule a new request;
            # recover() must never mistake that fresh running request for work
            # interrupted by the prior process.
            await dispatcher.recover()
            await self._recover_queued(store, self._services[key], principal)
            await self._recover_deliveries(store, binding.channel_id)
            for row in await store.analysis_delivery_rows():
                if not json.loads(row.final_message_ids_json or "[]"):
                    self._start_analysis_watcher(store, row.id, row.channel_id)

    async def run(self) -> None:
        self._gateway_task = asyncio.create_task(self.gateway.start(self.token))
        ready_task = asyncio.create_task(self.gateway.wait_ready())
        tasks: set[asyncio.Task] = {self._gateway_task, ready_task}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if self._gateway_task in done and ready_task not in done:
                await self._gateway_task
                raise RuntimeError("Discord gateway exited before READY")
            await ready_task
            if self._gateway_task.done():
                await self._gateway_task
                raise RuntimeError("Discord gateway exited immediately after READY")
            await self.gateway.validate_access(self._access_bindings())
            await self.initialize()
            await self.catch_up()
            if self.on_ready is not None:
                self.on_ready()
            tasks.discard(ready_task)
            tasks.add(asyncio.create_task(self._consume_messages()))
            tasks.add(asyncio.create_task(self._catchup_loop()))
            tasks.add(asyncio.create_task(self._delivery_retry_loop()))
            tasks.add(asyncio.create_task(self._raise_background_failure()))
            if self.notification_producer is not None:
                tasks.add(asyncio.create_task(self._notification_loop()))
            if any(binding.status_channel_id for binding in self.config.bindings):
                tasks.add(asyncio.create_task(self._status_board_loop()))
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                await task
            if self._gateway_task in done:
                raise RuntimeError("Discord gateway exited unexpectedly")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in self._analysis_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, *self._analysis_tasks, return_exceptions=True)
            for dispatcher in self._dispatchers:
                stop = getattr(dispatcher, "stop", None)
                if stop is not None:
                    await stop()
            await self.gateway.close()

    async def _consume_messages(self) -> None:
        active: set[asyncio.Task] = set()
        try:
            async for message in self.gateway.messages():
                # This is the sole inbound boundary. Rejections happen before any store/model lookup.
                principal = authorize(self.config, message.identity)
                if principal is None:
                    continue
                await self.gateway.defer(message)
                await self._concurrency.acquire()
                task = asyncio.create_task(self._bounded_handle(principal, message))
                active.add(task)
                for finished in [candidate for candidate in active if candidate.done()]:
                    active.discard(finished)
                    finished.result()
            if active:
                await asyncio.gather(*active)
        finally:
            for task in active:
                if not task.done():
                    task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    async def _bounded_handle(self, principal, message: InboundMessage) -> None:
        try:
            await self._handle(principal, message)
        except Exception as exc:
            self._background_failures.put_nowait(exc)
        finally:
            self._concurrency.release()

    async def _raise_background_failure(self) -> None:
        raise await self._background_failures.get()

    async def _handle(self, principal, message: InboundMessage) -> None:
        key = (principal.guild_id, principal.channel_id, principal.user_id)
        store = self._stores[key]
        conversation_key = principal.thread_id or principal.channel_id
        lock = self._locks.setdefault(
            f"{principal.household_user_id}:{conversation_key}", asyncio.Lock()
        )
        async with lock:
            turn, created = await store.create_turn(
                inbound_message_id=message.id,
                conversation_key=conversation_key,
                question=message.content,
            )
            if not created:
                return
            delivery_reserved = False
            try:
                target = principal.thread_id or principal.channel_id
                progress_id = None
                try:
                    progress_id = await self.gateway.send(
                        target, "Working on your message…",
                        nonce=hashlib.sha256(f"chat-progress:{turn.id}".encode()).hexdigest()[:24],
                    )
                    await store.save_turn_progress_message(turn.id, progress_id)
                except DiscordAdvisorAccessError:
                    raise
                except Exception:
                    pass  # A cosmetic acknowledgement failure cannot prevent the answer.

                async def progress(text: str) -> None:
                    if progress_id:
                        try:
                            await self.gateway.edit(target, progress_id, OutboundFilter.redact(text))
                        except DiscordAdvisorAccessError:
                            raise
                        except Exception:
                            pass

                # Injectable older test/alternate services may not expose progress.
                service = self._services[key]
                progress_kwargs = ({"progress": progress}
                                   if "progress" in inspect.signature(service.answer).parameters else {})
                if "react" in inspect.signature(service.answer).parameters:
                    async def react(emoji: str) -> None:
                        await self.gateway.set_reaction(target, message.id, emoji)
                    progress_kwargs["react"] = react
                history = await store.history(conversation_key)
                serialized_history = serialize_history(history)
                if message.reply_to_message_id:
                    reply_context = await store.reply_context(
                        message.reply_to_message_id, conversation_key=conversation_key)
                    if reply_context is None:
                        answer = ChatAnswer(
                            "I can’t resolve that replied-to message to an owned Argosy record, so I won’t guess which recommendation you mean."
                        )
                    else:
                        serialized_history.append(
                            {
                                "role": "assistant",
                                **reply_context,
                            }
                        )
                        answer = await self._services[key].answer(
                            principal,
                            message.content,
                            message.id,
                            history=serialized_history,
                            **progress_kwargs,
                        )
                else:
                    answer = await self._services[key].answer(
                        principal, message.content, message.id, history=serialized_history,
                        **progress_kwargs,
                    )
                if answer.analysis_request_id:
                    await store.adopt_analysis_message(answer.analysis_request_id, turn.id)
                nonce = await store.reserve_turn_delivery(
                    turn.id, response=answer.text, citations=answer.citations
                )
                delivery_reserved = True
                ids = await self._send_chunks(
                    principal.thread_id or principal.channel_id,
                    answer.text,
                    answer.citations,
                    nonce,
                    existing_ids=[progress_id] if progress_id else None,
                )
                await store.mark_turn_delivered(turn.id, ids)
                if answer.analysis_request_id:
                    await store.adopt_analysis_message(answer.analysis_request_id, turn.id)
                await store.update_cursor(principal.channel_id, last_message_id=message.id)
                await self._after_turn_delivery(store, turn.id, target, message.id)
            except DiscordAdvisorAccessError:
                raise
            except Exception as exc:
                if not delivery_reserved:
                    await self._deliver_processing_failure(store, turn.id, conversation_key, exc)
                    return
                self._delivery_wakeup.set()

    async def _deliver_processing_failure(
        self, store: ChatStore, turn_id: str, conversation_key: str, exc: Exception
    ) -> None:
        body = (
            "I couldn't finish handling this message, so I have no confirmed result to show. "
            "Please check Argosy's status or try again."
        )
        error = f"Internal processing failure ({type(exc).__name__})"
        nonce = await store.reserve_turn_delivery(
            turn_id, response=body, citations=[], processing_error=error
        )
        try:
            ids = await self._send_chunks(conversation_key, body, [], nonce,
                                          existing_ids=await store.turn_message_ids(turn_id))
        except DiscordAdvisorAccessError:
            raise
        except Exception:
            # The durable pending row retains both the safe response and its
            # failed processing outcome for the in-process delivery retry.
            self._delivery_wakeup.set()
            return
        await store.mark_turn_delivered(turn_id, ids)

    async def _send_chunks(self, channel_id: str, text: str, citations, nonce: str,
                           existing_ids: list[str] | None = None) -> list[str]:
        ids: list[str] = []
        for index, chunk in enumerate(OutboundFilter.chunk(text, citations)):
            if existing_ids and index < len(existing_ids):
                await self.gateway.edit(channel_id, existing_ids[index], chunk)
                ids.append(existing_ids[index])
                continue
            # Discord caps nonce at 25 chars. Stable hash preserves per-chunk retry identity.
            chunk_nonce = hashlib.sha256(f"{nonce}:{index}".encode()).hexdigest()[:24]
            ids.append(
                await self.gateway.send(channel_id, OutboundFilter.redact(chunk), nonce=chunk_nonce)
            )
        return ids

    async def _recover_deliveries(self, store: ChatStore, channel_id: str) -> None:
        for turn in await store.pending_turn_deliveries():
            raw = json.loads(turn.citations_json or "[]")
            citations = [Citation(**item) for item in raw]
            ids = await self._send_chunks(
                turn.conversation_key or channel_id,
                turn.response or "",
                citations,
                turn.response_nonce or secrets.token_hex(12),
                existing_ids=json.loads(turn.response_message_ids_json or "[]"),
            )
            await store.mark_turn_delivered(turn.id, ids)
            await self._after_turn_delivery(
                store, turn.id, turn.conversation_key or channel_id, turn.inbound_message_id,
                failed=bool(turn.error),
            )

    async def _after_turn_delivery(self, store, turn_id, target, inbound_id, *, failed=False):
        request = await store.analysis_for_turn(turn_id)
        if request is not None:
            await store.adopt_analysis_message(request.id, turn_id)
            self._start_analysis_watcher(store, request.id, target)
        elif failed:
            await self.gateway.set_reaction(target, inbound_id, "⚠️")
        else:
            await self.gateway.finish_reaction(target, inbound_id)

    async def _recover_queued(
        self, store: ChatStore, service: ConversationService, principal
    ) -> None:
        for turn in await store.queued_turns():
            target = turn.conversation_key or principal.channel_id
            resumed_principal = (
                principal
                if target == principal.channel_id
                else type(principal)(
                    household_user_id=principal.household_user_id,
                    guild_id=principal.guild_id,
                    channel_id=principal.channel_id,
                    user_id=principal.user_id,
                    thread_id=target,
                )
            )
            try:
                history = serialize_history(await store.history(target))
                answer = await service.answer(
                    resumed_principal, turn.question, turn.inbound_message_id, history=history
                )
            except DiscordAdvisorAccessError:
                raise
            except Exception as exc:
                await self._deliver_processing_failure(store, turn.id, target, exc)
                continue
            if answer.analysis_request_id:
                await store.adopt_analysis_message(answer.analysis_request_id, turn.id)
            nonce = await store.reserve_turn_delivery(
                turn.id, response=answer.text, citations=answer.citations
            )
            try:
                ids = await self._send_chunks(target, answer.text, answer.citations, nonce,
                                              existing_ids=json.loads(turn.response_message_ids_json or "[]"))
            except DiscordAdvisorAccessError:
                raise
            except Exception:
                self._delivery_wakeup.set()
                continue
            await store.mark_turn_delivered(turn.id, ids)
            if answer.analysis_request_id:
                await store.adopt_analysis_message(answer.analysis_request_id, turn.id)
            await self._after_turn_delivery(store, turn.id, target, turn.inbound_message_id)

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        local = (
            (now or datetime.now(UTC))
            .astimezone(ZoneInfo(self.config.timezone))
            .time()
            .replace(tzinfo=None)
        )
        start, end = self.config.quiet_hours_start, self.config.quiet_hours_end
        return start <= local < end if start < end else local >= start or local < end

    async def catch_up(self) -> None:
        """Bounded valid-channel history recovery after an invalid gateway resume."""
        for binding in self.config.bindings:
            store = self._stores[(binding.guild_id, binding.channel_id, binding.user_id)]
            cursor = await store.cursor()
            after = cursor.last_message_id if cursor else None
            if after is None:
                # Recover messages missed after authorization, never pre-binding history.
                # This is only a query boundary, not a fabricated received-message cursor.
                after = str(time_snowflake(await store.binding_created_at()))
            rows = await self.gateway.fetch_after(
                binding.channel_id, after, self.config.catchup_limit
            )
            for row in rows:
                principal = authorize(self.config, row.identity)
                if principal is not None:
                    await self._handle(principal, row)

    async def edit_progress(self, channel_id: str, message_id: str, text: str) -> None:
        await self.gateway.edit(channel_id, message_id, OutboundFilter.redact(text))

    def _start_analysis_watcher(self, store: ChatStore, request_id: str, channel_id: str) -> None:
        if any(
            getattr(task, "_argosy_request_id", None) == request_id and not task.done()
            for task in self._analysis_tasks
        ):
            return
        task = asyncio.create_task(self._watch_analysis(store, request_id, channel_id))
        task._argosy_request_id = request_id  # type: ignore[attr-defined]
        self._analysis_tasks.add(task)
        task.add_done_callback(self._analysis_finished)

    def _analysis_finished(self, task: asyncio.Task) -> None:
        self._analysis_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._background_failures.put_nowait(error)

    async def _watch_analysis(self, store: ChatStore, request_id: str, channel_id: str) -> None:
        origin = await store.analysis_origin_turn(request_id)
        if origin is not None:
            await store.adopt_analysis_message(request_id, origin.id)
        progress_nonce, message_id = await store.reserve_progress_delivery(request_id)
        if message_id is None:
            nonce = hashlib.sha256(f"{progress_nonce}:progress".encode()).hexdigest()[:24]
            message_id = await self.gateway.send(
                channel_id, f"Analysis {request_id}: queued", nonce=nonce
            )
            await store.set_progress_message(request_id, message_id)
        editor = ProgressEditor(store, self.gateway)
        while True:
            request = await store.get_request(request_id)
            if request is None:
                return
            if request.state in {"completed", "failed", "cancelled"}:
                break
            await editor.update(request_id, channel_id, message_id, force_heartbeat=True)
            await asyncio.sleep(10)
        nonce, delivered = await store.reserve_final_delivery(request_id)
        if delivered:
            return
        result = await store.request_result(request_id)
        body = render_analysis_result(request_id, request.state, result, request.error)
        if isinstance(result, dict):
            result_rows = result.get("outcomes", [])
        else:
            result_rows = result if isinstance(result, list) else []
        citations = [
            Citation(
                record_type="decision_run",
                record_id=str(item["decision_run_id"]),
                as_of=str(item.get("as_of") or "unknown"),
            )
            for item in result_rows
            if isinstance(item, dict) and item.get("decision_run_id")
        ]
        citations.extend(
            Citation("verdict", str(item["verdict_id"]), str(item.get("as_of") or "unknown"))
            for item in result_rows if isinstance(item, dict) and item.get("verdict_id")
        )
        await store.record_analysis_final(request_id, channel_id, body, citations)
        ids = await self._send_chunks(channel_id, body, citations, nonce,
                                      existing_ids=[message_id])
        if origin is not None:
            await store.mark_turn_delivered(origin.id, ids)
            if request.state == "completed":
                await self.gateway.finish_reaction(channel_id, origin.inbound_message_id)
            else:
                await self.gateway.set_reaction(channel_id, origin.inbound_message_id,
                                                "⚠️" if request.state == "failed" else "⏹️")
        await store.mark_final_delivered(request_id, ids)

    async def _notification_loop(self) -> None:
        while True:
            try:
                await self.pump_notifications()
            except DiscordAdvisorAccessError:
                raise
            except Exception as exc:
                # A producer failure means the feed is incomplete. Do not run
                # outbox closure against partial state, and do not take chat or
                # the status board down with this optional delivery surface.
                self.notification_error = (
                    f"Proactive notification refresh failed ({type(exc).__name__}). "
                    "Existing notifications were left unchanged; chat remains available."
                )
            else:
                self.notification_error = None
            await asyncio.sleep(self._notification_retry_seconds)

    def _access_bindings(self):
        return self.config.bindings + tuple(
            binding.model_copy(update={"channel_id": binding.status_channel_id})
            for binding in self.config.bindings if binding.status_channel_id
        )

    async def _status_board_loop(self) -> None:
        from .status_board import publish_status_board

        while True:
            try:
                if self.status_registry is None:
                    raise RuntimeError("Scheduled-job registry is unavailable")
                for binding in self.config.bindings:
                    if not binding.status_channel_id:
                        continue
                    store = self._stores[(binding.guild_id, binding.channel_id, binding.user_id)]
                    await publish_status_board(
                        registry=self.status_registry, gateway=self.gateway, store=store,
                        channel_id=binding.status_channel_id, timezone=self.config.timezone,
                    )
                self.status_board_error = None
            except DiscordAdvisorAccessError:
                raise
            except Exception as exc:
                self.status_board_error = (
                    f"Scheduled-job status board could not refresh ({type(exc).__name__}). "
                    "Its last refresh timestamp is stale; chat remains available."
                )
            await asyncio.sleep(60)

    async def _delivery_retry_loop(self) -> None:
        """Retry durable chat responses without requiring a process restart."""
        delay = 1.0
        while True:
            failed = False
            for binding in self.config.bindings:
                store = self._stores[(binding.guild_id, binding.channel_id, binding.user_id)]
                try:
                    await self._recover_deliveries(store, binding.channel_id)
                except DiscordAdvisorAccessError:
                    raise
                except Exception:
                    failed = True
            try:
                await asyncio.wait_for(
                    self._delivery_wakeup.wait(), timeout=delay if failed else 10.0
                )
                self._delivery_wakeup.clear()
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0) if failed else 1.0

    async def _catchup_loop(self) -> None:
        async for _ in self.gateway.reconnects():
            await self.gateway.validate_access(self._access_bindings())
            await self.catch_up()

    async def _daily_event(self, event, principal, store, key):
        if await store.latest_sent_outbox(event.semantic_key) is not None:
            return event
        saved = await store.latest_unsent_outbox(event.semantic_key)
        if saved is not None:
            if saved.material_version.startswith(event.material_version + ":"):
                return replace(event, body=saved.body, material_version=saved.material_version,
                               citations=[Citation(**c) for c in json.loads(saved.citations_json or "[]")])
            # A current recommendation changed/expired after drafting. Do not
            # replay the obsolete brief as current after a send failure/restart.
            await store.suppress_outbox(saved.id)
        builder = getattr(self.notification_producer, "daily_overview", None)
        if builder is None:
            return event  # Explicit unavailable placeholder, never fabricated research.
        if self.in_quiet_hours():
            return None
        draft_key = (*key, event.semantic_key, event.material_version)
        task = self._daily_drafts.get(draft_key)
        if task is None:
            for old_key, old_task in list(self._daily_drafts.items()):
                if old_key[:3] == key and old_key != draft_key:
                    if not old_task.done():
                        old_task.cancel()
                    del self._daily_drafts[old_key]
            task = asyncio.create_task(asyncio.wait_for(
                builder(principal, now=datetime.now(UTC)), timeout=240))
            self._daily_drafts[draft_key] = task
            self._analysis_tasks.add(task)
            task.add_done_callback(self._analysis_tasks.discard)
            return None
        if not task.done():
            return None  # Never hold urgent alerts behind the summary LLM.
        try:
            answer = task.result()
        except Exception:
            answer = ChatAnswer("Today's research summary is unavailable; I can't verify whether there are worthwhile updates.")
        body = event.body.split("\n", 1)[0] + "\n" + answer.text
        return replace(event, body=body, citations=answer.citations,
                       material_version=event.material_version + ":" + hashlib.sha256(body.encode()).hexdigest()[:32])

    async def pump_notifications(self) -> None:
        """Project canonical Inbox events into the durable outbox, then deliver."""
        if self.notification_producer is None:
            return
        for binding in self.config.bindings:
            key = (binding.guild_id, binding.channel_id, binding.user_id)
            principal = authorize(
                self.config, InboundIdentity(binding.guild_id, binding.channel_id, binding.user_id)
            )
            assert principal is not None
            store = self._stores[key]
            events = await asyncio.to_thread(self.notification_producer.current_events, principal)
            if self.status_registry is not None:
                from .status_board import critical_job_events

                events.extend(await critical_job_events(self.status_registry, store))
            events = delivery_events(
                events, now=datetime.now(UTC), timezone=self.config.timezone,
                overview_time=self.config.daily_overview_time,
            )
            projected = []
            for event in events:
                if event.category == DAILY:
                    event = await self._daily_event(event, principal, store, key)
                if event is not None:
                    projected.append(event)
            events = projected
            for index, event in enumerate(events):
                if event.category in {CRITICAL, OPERATIONAL}:
                    resolved = await store.last_resolved_alert(event.semantic_key)
                    if resolved is not None:
                        version = hashlib.sha256(
                            f"{event.material_version}:{resolved.id}".encode()
                        ).hexdigest()
                        events[index] = replace(event, material_version=version)
            current_keys = {event.semantic_key for event in events}
            for prior in await store.active_sent_outbox():
                if prior.category in {"status_board", DAILY}:
                    continue
                if prior.semantic_key == "system:critical-jobs":
                    # Retire the old mixed-channel policy, not the underlying errors.
                    await self.gateway.edit(binding.channel_id, prior.sent_message_id,
                        "Background task status has moved to #argosy-status. This notice was unrelated to your chat query; it does not mean the task failures are resolved.")
                    await store.close_sent_outbox(prior.id, status="superseded")
                    continue
                if prior.category not in {CRITICAL, OPERATIONAL}:
                    # Retire the former per-item delivery policy without rewriting history.
                    await store.close_sent_outbox(prior.id, status="superseded")
                    continue
                if prior.semantic_key in current_keys or not prior.sent_message_id:
                    continue
                await self.gateway.edit(
                    (binding.status_channel_id or binding.channel_id) if prior.category == OPERATIONAL else binding.channel_id,
                    prior.sent_message_id,
                    OutboundFilter.redact(
                        "Background task status changed; see the current task board for details."
                        if prior.category == OPERATIONAL else
                        "No longer actionable; see the current Argosy Inbox for its latest state."
                    ),
                )
                await store.close_sent_outbox(prior.id, status="resolved")
            for event in events:
                prior = await store.latest_sent_outbox(event.semantic_key)
                if event.category == DAILY and prior is not None:
                    continue  # Durable once per local date, including process restarts.
                await store.enqueue_outbox(event)
            for row in await store.pending_outbox():
                if row.category == "status_board":
                    continue
                event = next((e for e in events if e.semantic_key == row.semantic_key
                              and e.material_version == row.material_version), None)
                if event is None or (row.category == DAILY and
                                    await store.latest_sent_outbox(row.semantic_key) is not None):
                    await store.suppress_outbox(row.id)
                    continue
                if self.in_quiet_hours() and row.category != CRITICAL:
                    continue
                try:
                    target = (binding.status_channel_id or binding.channel_id) if row.category == OPERATIONAL else binding.channel_id
                    # Evidence remains in the durable outbox/reply context, not a source dump.
                    prefix = "🔴 " if row.category == CRITICAL else ""
                    chunks = OutboundFilter.chunk(prefix + row.body)
                    message_ids: list[str] = []
                    if row.supersedes_message_id:
                        await self.gateway.edit(
                            target,
                            row.supersedes_message_id,
                            OutboundFilter.redact(chunks[0]),
                        )
                        message_ids.append(row.supersedes_message_id)
                        chunks = chunks[1:]
                    for index, chunk in enumerate(chunks):
                        nonce = hashlib.sha256(f"{row.nonce}:{index}".encode()).hexdigest()[:24]
                        message_ids.append(
                            await self.gateway.send(
                                target, OutboundFilter.redact(chunk), nonce=nonce
                            )
                        )
                    await store.mark_outbox_sent(
                        row.id, message_ids[0] if message_ids else row.supersedes_message_id or ""
                    )
                    await store.supersede_prior_outbox(row.id, row.semantic_key)
                except DiscordAdvisorAccessError:
                    raise
                except Exception as exc:
                    await store.mark_outbox_failed(
                        row.id,
                        f"{type(exc).__name__}: {str(exc)[:500]}",
                        datetime.now(UTC) + timedelta(minutes=5),
                    )


__all__ = ["DiscordAdvisorWorker"]
