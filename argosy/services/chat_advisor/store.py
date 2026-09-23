"""The only read/write repository exposed to private chat transports."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from argosy.services.chat_advisor.contracts import AnalysisRequest, Citation, OutboxEvent
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.state.chat_models import (
    ChatAgentProgress,
    ChatAnalysisRequest,
    ChatBinding,
    ChatCursor,
    ChatThread,
    ChatTurn,
    NotificationOutbox,
)

SessionFactory = Callable[[], AsyncSession]
_ACTIVE = ("queued", "running", "cancelling")


def _now() -> datetime:
    return datetime.now(UTC)


def _request(row: ChatAnalysisRequest) -> AnalysisRequest:
    return AnalysisRequest(
        id=row.id,
        instruments=json.loads(row.instruments_json),
        state=row.status,
        run_ids=json.loads(row.run_ids_json),
        error=row.error,
        prompt_text=row.prompt_text or "",
    )


class ChatStore:
    """Tenant-bound durable chat repository; no method accepts a tenant override."""

    def __init__(
        self,
        session_factory: SessionFactory,
        household_user_id: str,
        *,
        binding_id: int | None = None,
    ) -> None:
        if not household_user_id:
            raise ValueError("household_user_id is required")
        self._sessions = session_factory
        self.household_user_id = household_user_id
        self.binding_id = binding_id
        self._progress_locks: dict[str, asyncio.Lock] = {}

    def _binding(self) -> int:
        if self.binding_id is None:
            raise RuntimeError("chat binding has not been established")
        return self.binding_id

    async def ensure_binding(
        self,
        *,
        provider: str,
        guild_id: str,
        channel_id: str,
        provider_user_id: str,
        enabled: bool = True,
    ) -> int:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatBinding).where(
                        ChatBinding.provider == provider,
                        ChatBinding.guild_id == guild_id,
                        ChatBinding.channel_id == channel_id,
                        ChatBinding.provider_user_id == provider_user_id,
                        ChatBinding.household_user_id == self.household_user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ChatBinding(
                    provider=provider,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    provider_user_id=provider_user_id,
                    household_user_id=self.household_user_id,
                    enabled=enabled,
                    read_scope="{}",
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
            elif row.enabled != enabled:
                row.enabled = enabled
                await session.commit()
            self.binding_id = row.id
            return row.id

    async def binding_created_at(self) -> datetime:
        """Authorization boundary for bounded recovery before the first received message."""
        async with self._sessions() as session:
            value = (await session.execute(select(ChatBinding.created_at).where(
                ChatBinding.id == self._binding(),
                ChatBinding.household_user_id == self.household_user_id,
            ))).scalar_one()
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    async def register_thread(
        self,
        thread_id: str,
        owner_provider_user_id: str,
        *,
        recommendation_id: str | None = None,
        recommendation_version: str | None = None,
    ) -> None:
        async with self._sessions() as session:
            session.add(
                ChatThread(
                    binding_id=self._binding(),
                    thread_id=thread_id,
                    owner_provider_user_id=owner_provider_user_id,
                    recommendation_id=recommendation_id,
                    recommendation_version=recommendation_version,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def thread_owned(self, thread_id: str, owner_provider_user_id: str) -> bool:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(ChatThread)
                    .where(
                        ChatThread.binding_id == self._binding(),
                        ChatThread.thread_id == thread_id,
                        ChatThread.owner_provider_user_id == owner_provider_user_id,
                    )
                )
            ) == 1

    async def create_turn(
        self, *, inbound_message_id: str, conversation_key: str, question: str
    ) -> tuple[ChatTurn, bool]:
        async with self._sessions() as session:
            existing = (
                await session.execute(
                    select(ChatTurn).where(
                        ChatTurn.binding_id == self._binding(),
                        ChatTurn.inbound_message_id == inbound_message_id,
                        ChatTurn.household_user_id == self.household_user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return existing, False
            row = ChatTurn(
                id=str(uuid.uuid4()),
                binding_id=self._binding(),
                household_user_id=self.household_user_id,
                inbound_message_id=inbound_message_id,
                conversation_key=conversation_key,
                question=question,
                status="queued",
                citations_json="[]",
            )
            session.add(row)
            try:
                await session.commit()
                await session.refresh(row)
                return row, True
            except IntegrityError:
                await session.rollback()
                row = (
                    await session.execute(
                        select(ChatTurn).where(
                            ChatTurn.binding_id == self._binding(),
                            ChatTurn.inbound_message_id == inbound_message_id,
                            ChatTurn.household_user_id == self.household_user_id,
                        )
                    )
                ).scalar_one()
                return row, False

    async def turn_message_ids(self, turn_id: str) -> list[str]:
        async with self._sessions() as session:
            value = (await session.execute(select(ChatTurn.response_message_ids_json).where(
                ChatTurn.id == turn_id,
                ChatTurn.binding_id == self._binding(),
                ChatTurn.household_user_id == self.household_user_id,
            ))).scalar_one()
            return json.loads(value or "[]")

    async def save_turn_progress_message(self, turn_id: str, message_id: str) -> None:
        # Same message becomes the final answer; existing field preserves crash recovery.
        await self._update_turn(turn_id, response_message_ids_json=json.dumps([message_id]))

    async def complete_turn(
        self, turn_id: str, *, response: str, citations: Sequence[Citation] = ()
    ) -> None:
        await self._update_turn(
            turn_id,
            status="completed",
            response=response,
            citations_json=json.dumps([c.__dict__ for c in citations], default=str),
            error=None,
        )

    async def reserve_turn_delivery(
        self,
        turn_id: str,
        *,
        response: str,
        citations: Sequence[Citation] = (),
        processing_error: str | None = None,
    ) -> str:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatTurn).where(
                        ChatTurn.id == turn_id,
                        ChatTurn.household_user_id == self.household_user_id,
                        ChatTurn.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            if row.response_nonce is not None and row.status in {
                "delivery_pending",
                "completed",
                "failed",
            }:
                return row.response_nonce
            row.response_nonce = secrets.token_hex(12)
            row.response = response
            row.citations_json = json.dumps([c.__dict__ for c in citations], default=str)
            row.status = "delivery_pending"
            row.error = processing_error
            row.updated_at = _now()
            await session.commit()
            return row.response_nonce

    async def mark_turn_delivered(self, turn_id: str, message_ids: Sequence[str]) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatTurn).where(
                        ChatTurn.id == turn_id,
                        ChatTurn.household_user_id == self.household_user_id,
                        ChatTurn.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            # Delivery is complete, but an error response remains a failed
            # processing outcome for audit/history purposes.
            row.status = "failed" if row.error else "completed"
            row.response_message_ids_json = json.dumps(list(message_ids))
            row.updated_at = _now()
            await session.commit()

    async def pending_turn_deliveries(self) -> list[ChatTurn]:
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(ChatTurn)
                        .where(
                            ChatTurn.household_user_id == self.household_user_id,
                            ChatTurn.binding_id == self._binding(),
                            ChatTurn.status == "delivery_pending",
                        )
                        .order_by(ChatTurn.created_at)
                    )
                ).scalars()
            )

    async def queued_turns(self) -> list[ChatTurn]:
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(ChatTurn)
                        .where(
                            ChatTurn.household_user_id == self.household_user_id,
                            ChatTurn.binding_id == self._binding(),
                            ChatTurn.status == "queued",
                        )
                        .order_by(ChatTurn.created_at)
                    )
                ).scalars()
            )

    async def record_analysis_final(
        self,
        request_id: str,
        conversation_key: str,
        response: str,
        citations: Sequence[Citation] = (),
    ) -> None:
        origin = await self.analysis_origin_turn(request_id)
        if origin is not None:
            await self.complete_turn(origin.id, response=response, citations=citations)
            return
        inbound_id = f"af-{request_id.replace('-', '')[:29]}"
        row, _ = await self.create_turn(
            inbound_message_id=inbound_id,
            conversation_key=conversation_key,
            question=f"Analysis result {request_id}",
        )
        await self.complete_turn(row.id, response=response, citations=citations)

    async def analysis_origin_turn(self, request_id: str) -> ChatTurn | None:
        """Resolve the owned UI message, including retries with synthetic inbound IDs."""
        request = await self._request_row(request_id)
        async with self._sessions() as session:
            candidates = (await session.scalars(select(ChatTurn).where(
                ChatTurn.household_user_id == self.household_user_id,
                ChatTurn.binding_id == self._binding(),
                ChatTurn.conversation_key == request.channel_id,
            ).order_by(ChatTurn.created_at.desc()))).all()
            if request.progress_message_id:
                for turn in candidates:
                    if request.progress_message_id in json.loads(turn.response_message_ids_json):
                        return turn
            return next((turn for turn in candidates
                         if turn.inbound_message_id == request.inbound_message_id), None)

    async def adopt_analysis_message(self, request_id: str, turn_id: str) -> bool:
        """Durably hand the existing reply to new work; never steal another run's UI."""
        async with self._sessions() as session:
            request = (await session.scalars(select(ChatAnalysisRequest).where(
                ChatAnalysisRequest.id == request_id,
                ChatAnalysisRequest.household_user_id == self.household_user_id,
                ChatAnalysisRequest.binding_id == self._binding(),
            ))).one()
            turn = (await session.scalars(select(ChatTurn).where(
                ChatTurn.id == turn_id,
                ChatTurn.household_user_id == self.household_user_id,
                ChatTurn.binding_id == self._binding(),
            ))).one()
            ids = json.loads(turn.response_message_ids_json)
            if request.progress_message_id:
                return request.progress_message_id in ids
            # Cancellation/status messages must not take over the original request.
            if request.inbound_message_id != turn.inbound_message_id and not (
                request.retry_of_id and request.inbound_message_id.startswith("retry-")
            ):
                return False
            if not ids:
                return False
            request.progress_message_id = ids[0]
            request.channel_id = turn.conversation_key
            request.updated_at = _now()
            await session.commit()
            return True

    async def analysis_for_turn(self, turn_id: str) -> AnalysisRequest | None:
        ids = await self.turn_message_ids(turn_id)
        async with self._sessions() as session:
            turn = (await session.scalars(select(ChatTurn).where(
                ChatTurn.id == turn_id,
                ChatTurn.household_user_id == self.household_user_id,
                ChatTurn.binding_id == self._binding(),
            ))).one()
            request = (await session.scalars(select(ChatAnalysisRequest).where(
                ChatAnalysisRequest.household_user_id == self.household_user_id,
                ChatAnalysisRequest.binding_id == self._binding(),
                (ChatAnalysisRequest.inbound_message_id == turn.inbound_message_id)
                | ChatAnalysisRequest.progress_message_id.in_(ids),
            ))).first()
            return _request(request) if request else None

    async def fail_turn(self, turn_id: str, error: str) -> None:
        await self._update_turn(turn_id, status="failed", error=error)

    async def _update_turn(self, turn_id: str, **values: Any) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatTurn).where(
                        ChatTurn.id == turn_id,
                        ChatTurn.household_user_id == self.household_user_id,
                        ChatTurn.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = _now()
            await session.commit()

    async def history(self, conversation_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatTurn)
                        .where(
                            ChatTurn.household_user_id == self.household_user_id,
                            ChatTurn.binding_id == self._binding(),
                            ChatTurn.conversation_key == conversation_key,
                            ChatTurn.status == "completed",
                        )
                        .order_by(ChatTurn.created_at.desc())
                        .limit(min(max(limit, 1), 100))
                    )
                )
                .scalars()
                .all()
            )
            events = [
                (r.created_at.replace(tzinfo=UTC), {
                    "question": r.question,
                    "response": r.response,
                    "citations": json.loads(r.citations_json),
                    "message_id": next(iter(json.loads(r.response_message_ids_json or "[]")), None),
                    "observed_at": r.created_at.replace(tzinfo=UTC).isoformat(),
                })
                for r in reversed(rows)
            ]
            binding = await session.get(ChatBinding, self._binding())
            # A thread started from a delivered notice has the same Discord ID
            # as that notice. Inherit only that origin, not all parent notices.
            # Notices remain assistant context, never user authority.
            if binding and binding.household_user_id == self.household_user_id:
                notice_query = select(NotificationOutbox).where(
                    NotificationOutbox.household_user_id == self.household_user_id,
                    NotificationOutbox.binding_id == self._binding(),
                    NotificationOutbox.status.in_(("sent", "superseded", "resolved")),
                    NotificationOutbox.sent_message_id.is_not(None),
                    NotificationOutbox.category.not_in(("status_board", "status.jobs")),
                    NotificationOutbox.semantic_key != "system:critical-jobs",
                )
                if conversation_key != binding.channel_id:
                    notice_query = notice_query.where(NotificationOutbox.sent_message_id == conversation_key)
                notices = (await session.execute(notice_query.order_by(
                    NotificationOutbox.created_at.desc()).limit(min(max(limit, 1), 100)))).scalars()
                events.extend((r.created_at.replace(tzinfo=UTC), {
                    "question": None, "response": OutboundFilter.redact(r.body),
                    "citations": json.loads(r.citations_json or "[]"),
                    "message_id": r.sent_message_id,
                    "observed_at": r.created_at.replace(tzinfo=UTC).isoformat(),
                }) for r in notices)
            events.sort(key=lambda item: item[0])
            return [item for _, item in events[-min(max(limit, 1), 100):]]

    async def reply_context(
        self, provider_message_id: str, *, conversation_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve only a delivered message in this tenant and conversation.

        Legacy callers default to the parent channel, never all private threads.
        """
        async with self._sessions() as session:
            binding = await session.get(ChatBinding, self._binding())
            if binding is None or binding.household_user_id != self.household_user_id:
                return None
            conversation_key = conversation_key or binding.channel_id
            turns = (
                await session.execute(
                    select(ChatTurn).where(
                        ChatTurn.household_user_id == self.household_user_id,
                        ChatTurn.binding_id == self._binding(),
                        ChatTurn.status == "completed",
                        ChatTurn.conversation_key == conversation_key,
                    )
                )
            ).scalars()
            for turn in turns:
                if provider_message_id in json.loads(turn.response_message_ids_json or "[]"):
                    return {
                        "content": turn.response or "",
                        "citations": json.loads(turn.citations_json or "[]"),
                        "message_id": provider_message_id,
                        "observed_at": turn.created_at.replace(tzinfo=UTC).isoformat(),
                    }
            if conversation_key != binding.channel_id and provider_message_id != conversation_key:
                return None  # Only the exact originating notice may cross into its own thread.
            outbox = (
                (
                    await session.execute(
                        select(NotificationOutbox).where(
                            NotificationOutbox.household_user_id == self.household_user_id,
                            NotificationOutbox.binding_id == self._binding(),
                            NotificationOutbox.sent_message_id == provider_message_id,
                            NotificationOutbox.status.in_(("sent", "superseded", "resolved")),
                            NotificationOutbox.category.not_in(("status_board", "status.jobs")),
                            NotificationOutbox.semantic_key != "system:critical-jobs",
                        )
                    )
                )
                .scalars()
                .first()
            )
            if outbox is None:
                return None
            return {
                "content": outbox.body,
                "citations": json.loads(outbox.citations_json or "[]"),
                "message_id": provider_message_id,
                "observed_at": outbox.created_at.replace(tzinfo=UTC).isoformat(),
            }

    async def create_request(
        self,
        inbound_message_id: str,
        channel_id: str,
        instruments: Sequence[str],
        prompt_text: str = "",
    ) -> AnalysisRequest:
        normalized = sorted({str(i).strip().upper() for i in instruments if str(i).strip()})
        if not normalized:
            raise ValueError("at least one instrument is required")
        async with self._sessions() as session:
            q = select(ChatAnalysisRequest).where(
                ChatAnalysisRequest.binding_id == self._binding(),
                ChatAnalysisRequest.inbound_message_id == inbound_message_id,
                ChatAnalysisRequest.household_user_id == self.household_user_id,
            )
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing:
                return _request(existing)
            row = ChatAnalysisRequest(
                id=str(uuid.uuid4()),
                binding_id=self._binding(),
                household_user_id=self.household_user_id,
                inbound_message_id=inbound_message_id,
                channel_id=channel_id,
                instruments_json=json.dumps(normalized),
                prompt_text=prompt_text,
                run_ids_json="[]",
                result_json="[]",
                status="queued",
                progress_cursor=0,
            )
            session.add(row)
            try:
                await session.commit()
                await session.refresh(row)
            except IntegrityError:
                await session.rollback()
                row = (await session.execute(q)).scalar_one()
            return _request(row)

    async def get_request_by_inbound(self, inbound_message_id: str) -> AnalysisRequest | None:
        return await self._get_request(ChatAnalysisRequest.inbound_message_id == inbound_message_id)

    async def get_request(self, request_id: str) -> AnalysisRequest | None:
        return await self._get_request(ChatAnalysisRequest.id == request_id)

    async def request_result(self, request_id: str) -> Any | None:
        async with self._sessions() as session:
            value = await session.scalar(
                select(ChatAnalysisRequest.result_json).where(
                    ChatAnalysisRequest.id == request_id,
                    ChatAnalysisRequest.household_user_id == self.household_user_id,
                    ChatAnalysisRequest.binding_id == self._binding(),
                )
            )
            return json.loads(value) if value is not None else None

    async def _get_request(self, criterion: Any) -> AnalysisRequest | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        criterion,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one_or_none()
            return _request(row) if row else None

    async def count_active_requests(self) -> int:
        async with self._sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ChatAnalysisRequest)
                    .where(
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.status.in_(_ACTIVE),
                    )
                )
                or 0
            )

    async def update_request(
        self,
        request_id: str,
        status: str | None = None,
        error: str | None = None,
        result: Any | None = None,
        *,
        state: str | None = None,
    ) -> AnalysisRequest:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            row.status = status or state or row.status
            row.error = error
            if result is not None:
                row.result_json = json.dumps(result, default=str)
            row.updated_at = _now()
            await session.commit()
            await session.refresh(row)
            return _request(row)

    async def link_run(self, request_id: str, run_id: int) -> AnalysisRequest:
        row = await self._request_row(request_id)
        ids = json.loads(row.run_ids_json)
        if run_id not in ids:
            ids.append(run_id)
        async with self._sessions() as session:
            current = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            current.run_ids_json = json.dumps(ids)
            current.updated_at = _now()
            await session.commit()
            await session.refresh(current)
            return _request(current)

    async def _request_row(self, request_id: str) -> ChatAnalysisRequest:
        async with self._sessions() as session:
            return (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()

    async def retry_request(self, request_id: str) -> AnalysisRequest:
        source = await self._request_row(request_id)
        async with self._sessions() as session:
            row = ChatAnalysisRequest(
                id=str(uuid.uuid4()),
                binding_id=self._binding(),
                household_user_id=self.household_user_id,
                inbound_message_id=f"retry-{secrets.token_hex(12)}",
                channel_id=source.channel_id,
                instruments_json=source.instruments_json,
                prompt_text=source.prompt_text,
                run_ids_json="[]",
                result_json="[]",
                status="queued",
                progress_cursor=0,
                retry_of_id=source.id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _request(row)

    async def list_recoverable_requests(self) -> list[AnalysisRequest]:
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatAnalysisRequest)
                        .where(
                            ChatAnalysisRequest.household_user_id == self.household_user_id,
                            ChatAnalysisRequest.binding_id == self._binding(),
                            ChatAnalysisRequest.status.in_(_ACTIVE),
                        )
                        .order_by(ChatAnalysisRequest.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_request(r) for r in rows]

    async def set_progress_message(self, request_id: str, message_id: str, cursor: int = 0) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            row.progress_message_id = message_id
            row.progress_cursor = cursor
            row.updated_at = _now()
            await session.commit()

    async def set_request_channel(self, request_id: str, channel_id: str) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            row.channel_id = channel_id
            row.updated_at = _now()
            await session.commit()

    async def reserve_progress_delivery(self, request_id: str) -> tuple[str, str | None]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            if row.progress_nonce is None:
                row.progress_nonce = secrets.token_hex(12)
            await session.commit()
            return row.progress_nonce, row.progress_message_id

    async def reserve_final_delivery(self, request_id: str) -> tuple[str, list[str]]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            if row.final_nonce is None:
                row.final_nonce = secrets.token_hex(12)
            await session.commit()
            return row.final_nonce, json.loads(row.final_message_ids_json)

    async def mark_final_delivered(self, request_id: str, message_ids: Sequence[str]) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChatAnalysisRequest).where(
                        ChatAnalysisRequest.id == request_id,
                        ChatAnalysisRequest.household_user_id == self.household_user_id,
                        ChatAnalysisRequest.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            row.final_message_ids_json = json.dumps(list(message_ids))
            row.updated_at = _now()
            await session.commit()

    async def analysis_delivery_rows(self) -> list[ChatAnalysisRequest]:
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(ChatAnalysisRequest)
                        .where(
                            ChatAnalysisRequest.household_user_id == self.household_user_id,
                            ChatAnalysisRequest.binding_id == self._binding(),
                            ChatAnalysisRequest.status.in_(
                                ("queued", "running", "completed", "failed", "cancelled")
                            ),
                        )
                        .order_by(ChatAnalysisRequest.created_at)
                    )
                ).scalars()
            )

    async def append_progress(
        self,
        request_id: str,
        run_id: int | None,
        agent: str,
        state: str,
        detail: str | None = None,
        error: str | None = None,
        dedup_key: str | None = None,
    ) -> int:
        lock = self._progress_locks.setdefault(request_id, asyncio.Lock())
        async with lock, self._sessions() as session:
            owned = await session.scalar(
                select(func.count())
                .select_from(ChatAnalysisRequest)
                .where(
                    ChatAnalysisRequest.id == request_id,
                    ChatAnalysisRequest.household_user_id == self.household_user_id,
                )
            )
            if not owned:
                raise KeyError(request_id)
            seq = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(ChatAgentProgress.sequence), 0)).where(
                            ChatAgentProgress.request_id == request_id
                        )
                    )
                    or 0
                )
                + 1
            )
            row = ChatAgentProgress(
                request_id=request_id,
                run_id=run_id,
                sequence=seq,
                dedup_key=dedup_key or f"{run_id}:{agent}:{state}:{seq}",
                agent=agent,
                state=state,
                detail=detail,
                error=error,
            )
            session.add(row)
            try:
                await session.commit()
                return seq
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(ChatAgentProgress.sequence).where(
                        ChatAgentProgress.request_id == request_id,
                        ChatAgentProgress.dedup_key == row.dedup_key,
                    )
                )
                if existing is None:
                    raise
                return int(existing)

    async def progress_rows(self, request_id: str, *, after: int = 0) -> list[ChatAgentProgress]:
        async with self._sessions() as session:
            owned = await session.scalar(
                select(func.count())
                .select_from(ChatAnalysisRequest)
                .where(
                    ChatAnalysisRequest.id == request_id,
                    ChatAnalysisRequest.household_user_id == self.household_user_id,
                )
            )
            if not owned:
                return []
            return list(
                (
                    await session.execute(
                        select(ChatAgentProgress)
                        .where(
                            ChatAgentProgress.request_id == request_id,
                            ChatAgentProgress.sequence > after,
                        )
                        .order_by(ChatAgentProgress.sequence)
                    )
                ).scalars()
            )

    async def enqueue_outbox(self, event: OutboxEvent) -> NotificationOutbox:
        async with self._sessions() as session:
            q = select(NotificationOutbox).where(
                NotificationOutbox.binding_id == self._binding(),
                NotificationOutbox.semantic_key == event.semantic_key,
                NotificationOutbox.material_version == event.material_version,
                NotificationOutbox.household_user_id == self.household_user_id,
            )
            row = (await session.execute(q)).scalar_one_or_none()
            if row:
                return row
            row = NotificationOutbox(
                id=str(uuid.uuid4()),
                binding_id=self._binding(),
                household_user_id=self.household_user_id,
                semantic_key=event.semantic_key,
                material_version=event.material_version,
                category=event.category,
                body=event.body,
                citations_json=json.dumps([c.__dict__ for c in event.citations], default=str),
                nonce=secrets.token_hex(12),
                supersedes_message_id=event.supersedes_message_id,
                status="pending",
                attempts=0,
            )
            session.add(row)
            try:
                await session.commit()
                await session.refresh(row)
            except IntegrityError:
                await session.rollback()
                row = (await session.execute(q)).scalar_one()
            return row

    async def reserve_status_board_page(
        self, event: OutboxEvent, *, nonce: str
    ) -> NotificationOutbox:
        """Reserve one stable status-board page outside notification delivery.

        Board rows use dedicated statuses so the ordinary notification pump
        cannot send, resolve, or supersede them. The caller supplies the
        deterministic Discord nonce (maximum 25 characters).
        """
        if event.category != "status_board":
            raise ValueError("status-board reservation requires category=status_board")
        if not nonce or len(nonce) > 25:
            raise ValueError("status-board nonce must be 1..25 characters")
        async with self._sessions() as session:
            q = select(NotificationOutbox).where(
                NotificationOutbox.binding_id == self._binding(),
                NotificationOutbox.household_user_id == self.household_user_id,
                NotificationOutbox.semantic_key == event.semantic_key,
                NotificationOutbox.material_version == event.material_version,
                NotificationOutbox.category == "status_board",
            )
            row = (await session.execute(q)).scalar_one_or_none()
            if row is not None:
                return row
            row = NotificationOutbox(
                id=str(uuid.uuid4()),
                binding_id=self._binding(),
                household_user_id=self.household_user_id,
                semantic_key=event.semantic_key,
                material_version=event.material_version,
                category="status_board",
                body=event.body,
                citations_json="[]",
                nonce=nonce,
                status="board_pending",
                attempts=0,
            )
            session.add(row)
            try:
                await session.commit()
                await session.refresh(row)
            except IntegrityError:
                await session.rollback()
                row = (await session.execute(q)).scalar_one()
            return row

    async def status_board_pages(self, semantic_prefix: str) -> list[NotificationOutbox]:
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.binding_id == self._binding(),
                            NotificationOutbox.household_user_id == self.household_user_id,
                            NotificationOutbox.category == "status_board",
                            NotificationOutbox.semantic_key.startswith(semantic_prefix),
                            NotificationOutbox.status.in_(("board_pending", "board_sent")),
                        )
                        .order_by(NotificationOutbox.semantic_key)
                    )
                ).scalars()
            )

    async def mark_status_board_sent(self, outbox_id: str, *, message_id: str, body: str) -> None:
        await self._update_status_board(
            outbox_id, status="board_sent", sent_message_id=message_id, body=body, error=None
        )

    async def update_status_board_body(self, outbox_id: str, *, body: str) -> None:
        """Persist exact content only after the gateway edit succeeded."""
        await self._update_status_board(outbox_id, body=body, error=None)

    async def _update_status_board(self, outbox_id: str, **values: Any) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.id == outbox_id,
                        NotificationOutbox.household_user_id == self.household_user_id,
                        NotificationOutbox.binding_id == self._binding(),
                        NotificationOutbox.category == "status_board",
                    )
                )
            ).scalar_one()
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = _now()
            await session.commit()

    async def pending_outbox(
        self, *, now: datetime | None = None, limit: int = 25
    ) -> list[NotificationOutbox]:
        moment = now or _now()
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.household_user_id == self.household_user_id,
                            NotificationOutbox.binding_id == self._binding(),
                            NotificationOutbox.status == "pending",
                            (
                                (NotificationOutbox.retry_after.is_(None))
                                | (NotificationOutbox.retry_after <= moment)
                            ),
                        )
                        .order_by(NotificationOutbox.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )

    async def latest_unsent_outbox(self, semantic_key: str) -> NotificationOutbox | None:
        """Reuse a drafted daily brief across retries/restarts, including delayed retries."""
        async with self._sessions() as session:
            return (await session.execute(select(NotificationOutbox).where(
                NotificationOutbox.household_user_id == self.household_user_id,
                NotificationOutbox.binding_id == self._binding(),
                NotificationOutbox.semantic_key == semantic_key,
                NotificationOutbox.status.in_(["pending", "failed"]),
            ).order_by(NotificationOutbox.created_at.desc()).limit(1))).scalar_one_or_none()

    async def latest_sent_outbox(self, semantic_key: str) -> NotificationOutbox | None:
        async with self._sessions() as session:
            return (
                await session.execute(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.household_user_id == self.household_user_id,
                        NotificationOutbox.binding_id == self._binding(),
                        NotificationOutbox.semantic_key == semantic_key,
                        NotificationOutbox.status == "sent",
                    )
                    .order_by(NotificationOutbox.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def last_resolved_alert(self, semantic_key: str) -> NotificationOutbox | None:
        """Recovery boundary so a recurring critical issue can alert again."""
        async with self._sessions() as session:
            return (await session.execute(select(NotificationOutbox).where(
                NotificationOutbox.household_user_id == self.household_user_id,
                NotificationOutbox.binding_id == self._binding(),
                NotificationOutbox.semantic_key == semantic_key,
                NotificationOutbox.category == "alert.critical",
                NotificationOutbox.status == "resolved",
            ).order_by(NotificationOutbox.updated_at.desc()).limit(1))).scalar_one_or_none()

    async def active_sent_outbox(self) -> list[NotificationOutbox]:
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(NotificationOutbox).where(
                            NotificationOutbox.household_user_id == self.household_user_id,
                            NotificationOutbox.binding_id == self._binding(),
                            NotificationOutbox.status == "sent",
                        )
                    )
                ).scalars()
            )

    async def close_sent_outbox(self, outbox_id: str, *, status: str) -> None:
        if status not in {"superseded", "resolved"}:
            raise ValueError("invalid sent outbox terminal status")
        await self._update_outbox(outbox_id, status=status)

    async def supersede_prior_outbox(self, current_id: str, semantic_key: str) -> None:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.household_user_id == self.household_user_id,
                        NotificationOutbox.binding_id == self._binding(),
                        NotificationOutbox.semantic_key == semantic_key,
                        NotificationOutbox.id != current_id,
                        NotificationOutbox.status == "sent",
                    )
                )
            ).scalars()
            for row in rows:
                row.status = "superseded"
                row.updated_at = _now()
            await session.commit()

    async def mark_outbox_sent(self, outbox_id: str, message_id: str) -> None:
        await self._update_outbox(outbox_id, status="sent", sent_message_id=message_id, error=None)

    async def mark_outbox_failed(
        self, outbox_id: str, error: str, retry_after: datetime | None
    ) -> None:
        await self._update_outbox(
            outbox_id, status="pending", error=error, retry_after=retry_after, increment=True
        )

    async def suppress_outbox(self, outbox_id: str) -> None:
        await self._update_outbox(outbox_id, status="suppressed")

    async def _update_outbox(self, outbox_id: str, increment: bool = False, **values: Any) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.id == outbox_id,
                        NotificationOutbox.household_user_id == self.household_user_id,
                        NotificationOutbox.binding_id == self._binding(),
                    )
                )
            ).scalar_one()
            for key, value in values.items():
                setattr(row, key, value)
            if increment:
                row.attempts += 1
            row.updated_at = _now()
            await session.commit()

    async def update_cursor(
        self,
        channel_id: str,
        *,
        last_message_id: str | None = None,
        gateway_session_id: str | None = None,
        gateway_sequence: int | None = None,
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(ChatCursor, self._binding())
            if row is None:
                row = ChatCursor(binding_id=self._binding(), channel_id=channel_id)
                session.add(row)
            if last_message_id is not None and (
                row.last_message_id is None or int(last_message_id) > int(row.last_message_id)
            ):
                row.last_message_id = last_message_id
            if gateway_session_id is not None:
                row.gateway_session_id = gateway_session_id
            if gateway_sequence is not None:
                row.gateway_sequence = gateway_sequence
            row.updated_at = _now()
            await session.commit()

    async def cursor(self) -> ChatCursor | None:
        async with self._sessions() as session:
            return await session.get(ChatCursor, self._binding())


__all__ = ["ChatStore"]
