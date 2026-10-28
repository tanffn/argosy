"""Replay a saved real chat turn through production readers/models; never deliver.

Usage: python scripts/check_recent_discord_reply.py [--inbound-message-id ID]
No new chat requests, subscriptions, trades or Discord messages are created.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import quote

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.config import get_settings
from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.conversation import (
    ChatIntent,
    ConversationRouterAgent,
    ConversationService,
)
from argosy.services.chat_advisor.history import serialize_history
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.chat_advisor.store import ChatStore
from argosy.transport.discord_advisor.config import load_config


class NoActions:
    async def request(self, *args, **kwargs):
        raise AssertionError("Read replay cannot dispatch actions")
    request_research = retry = cancel = request


class ReadOnlyRouter(ConversationRouterAgent):
    async def run(self, **kwargs):
        report = await super().run(**kwargs)
        if report.output.intent != ChatIntent.READ or report.output.refresh_sources:
            raise AssertionError("Preview accepts saved-record reads only; no refresh or action dispatch")
        return report


def load_saved_turn(path, bindings, message_id=None):
    """Resolve exact configured identity before selecting a parent or thread turn."""
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for binding in bindings:
            identity = db.execute("SELECT id FROM chat_bindings WHERE household_user_id=? "
                "AND provider='discord' AND guild_id=? AND channel_id=? AND provider_user_id=?",
                (binding.household_user_id, binding.guild_id, binding.channel_id, binding.user_id)).fetchone()
            if identity is None:
                continue
            selector = " AND inbound_message_id=?" if message_id else " AND conversation_key=?"
            turn = db.execute("SELECT * FROM chat_turns WHERE household_user_id=? AND binding_id=?"
                + selector + " ORDER BY created_at DESC LIMIT 1",
                (binding.household_user_id, identity["id"], message_id or binding.channel_id)).fetchone()
            if turn is not None:
                return dict(turn), binding
    raise ValueError("No matching saved turn within configured Discord identities")


async def main(message_id=None):
    path = Path(get_settings().home) / "db" / "argosy.db"
    turn, binding = load_saved_turn(path, load_config().bindings, message_id)
    thread_id = turn["conversation_key"] if turn["conversation_key"] != binding.channel_id else None
    principal = Principal(binding.household_user_id, binding.guild_id, binding.channel_id,
                          binding.user_id, thread_id=thread_id)
    # Replay the same delivered-notification history as the worker, not just
    # user chat turns. Otherwise bare numbered follow-ups have no referent.
    uri = "sqlite+aiosqlite:///file:" + quote(path.as_posix(), safe="/:") + "?mode=ro&uri=true"
    history_engine = create_async_engine(uri)
    @event.listens_for(history_engine.sync_engine, "connect")
    def read_only(connection, _):
        connection.execute("PRAGMA query_only=ON")
    try:
        store = ChatStore(async_sessionmaker(history_engine, expire_on_commit=False),
                          binding.household_user_id, binding_id=turn["binding_id"])
        history = serialize_history(await store.history(
            turn["conversation_key"], before_at=datetime.fromisoformat(turn["created_at"])))
    finally:
        await history_engine.dispose()
    retrieval = RetrievalService(db_path=path)
    try:
        answer = await ConversationService(
            retrieval, NoActions(), route_agent_factory=lambda user: ReadOnlyRouter(user_id=user)
        ).answer(
            principal, turn["question"], "read-only-preview", history=history)
        print(json.dumps({"question": turn["question"], "review_status": answer.review_status,
                          "text": answer.text, "review_trace": answer.review_trace},
                         ensure_ascii=False, default=str, indent=2))
        return 0 if answer.review_status == "accepted" else 1
    finally:
        retrieval.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbound-message-id")
    raise SystemExit(asyncio.run(main(parser.parse_args().inbound_message_id)))
