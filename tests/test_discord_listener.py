"""Tests for the Discord listener (sprint commit #16).

NO real Discord calls. The test injects a fake ``DiscordClient`` via
the ``client_factory`` parameter; the production websockets path is
exercised only through static analysis (and a smoke import).

Coverage:
  - ``load_creds`` returns None when file missing.
  - ``load_creds`` raises ValueError on malformed payloads
    (multiple shapes).
  - ``load_creds`` returns a valid DiscordCreds when well-formed.
  - End-to-end: a message-received event runs through the extractor
    and persists a ``news_signals`` row with the right shape.
  - Idempotency: re-dispatching the same message-id is a no-op.
  - Max-age filter: an old message is dropped before extraction.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.discord_attachment_fetcher import Attachment
from argosy.services.discord_listener import (
    DiscordCreds,
    MessageEvent,
    load_creds,
    run_discord_listener,
)
from argosy.state.models import Base, NewsSignal

_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
_CHANNEL_ID = 1234567890
_SERVER_ID = 9876543210
_FAKE_TOKEN = "MTk1NDg2NDU0OTQ4OTQ5MTI0.GExample.token_value_padding_xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    """File-backed SQLite session factory with Base.metadata.create_all."""
    db_path = tmp_path / "discord_listener.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield SF
    finally:
        engine.dispose()


@pytest.fixture
def creds() -> DiscordCreds:
    return DiscordCreds(
        bot_token=_FAKE_TOKEN,
        channel_id=_CHANNEL_ID,
        server_id=_SERVER_ID,
    )


# ---------------------------------------------------------------------------
# Fake Discord client
# ---------------------------------------------------------------------------


class _FakeDiscordClient:
    """Drop-in replacement for the real client. Yields a pre-seeded
    list of MessageEvents from ``messages()``."""

    def __init__(self, events: list[MessageEvent]) -> None:
        self._events = events
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def messages(self) -> Any:
        for event in self._events:
            yield event


def _make_client_factory(events: list[MessageEvent]):
    """Return a callable conforming to ``ClientFactory``."""
    holder: dict[str, _FakeDiscordClient] = {}

    def factory(_creds: DiscordCreds) -> _FakeDiscordClient:
        client = _FakeDiscordClient(events)
        holder["client"] = client
        return client

    return factory, holder


# ---------------------------------------------------------------------------
# load_creds tests
# ---------------------------------------------------------------------------


def test_load_creds_returns_none_when_file_missing(tmp_path) -> None:
    """No creds file → ``None`` (dormant), not an error."""
    missing = tmp_path / "does_not_exist.json"
    assert load_creds(missing) is None


def test_load_creds_returns_creds_when_well_formed(tmp_path) -> None:
    """Well-formed JSON → DiscordCreds with the expected fields."""
    path = tmp_path / "discord_creds.json"
    path.write_text(json.dumps({
        "bot_token": _FAKE_TOKEN,
        "channel_id": _CHANNEL_ID,
        "server_id": _SERVER_ID,
    }))

    result = load_creds(path)

    assert result is not None
    assert result.bot_token == _FAKE_TOKEN
    assert result.channel_id == _CHANNEL_ID
    assert result.server_id == _SERVER_ID


@pytest.mark.parametrize("payload,fragment", [
    # Not valid JSON at all
    ("not json {{{", "not valid JSON"),
    # Top-level is a list, not an object
    ('["bot_token", "x"]', "must contain a JSON object"),
    # Missing channel_id
    (json.dumps({"bot_token": _FAKE_TOKEN, "server_id": 1}), "channel_id"),
    # Missing all three
    ("{}", "bot_token"),
    # bot_token wrong type
    (json.dumps({
        "bot_token": 12345,
        "channel_id": _CHANNEL_ID,
        "server_id": _SERVER_ID,
    }), "bot_token"),
    # bot_token doesn't look like a Discord token
    (json.dumps({
        "bot_token": "not-a-real-token",
        "channel_id": _CHANNEL_ID,
        "server_id": _SERVER_ID,
    }), "Discord bot token"),
    # channel_id is a string instead of an int
    (json.dumps({
        "bot_token": _FAKE_TOKEN,
        "channel_id": "1234567890",
        "server_id": _SERVER_ID,
    }), "channel_id"),
    # server_id is a bool — caught by the bool/int guard
    (json.dumps({
        "bot_token": _FAKE_TOKEN,
        "channel_id": _CHANNEL_ID,
        "server_id": True,
    }), "server_id"),
])
def test_load_creds_raises_on_malformed(tmp_path, payload, fragment) -> None:
    """Malformed creds → ValueError mentioning the bad field."""
    path = tmp_path / "bad_creds.json"
    path.write_text(payload)

    with pytest.raises(ValueError) as excinfo:
        load_creds(path)
    assert fragment in str(excinfo.value)


# ---------------------------------------------------------------------------
# run_discord_listener — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_persists_message_through_extractor(
    session_factory, creds,
) -> None:
    """A fresh message in the right channel → one news_signals row with
    the extractor's normalized fields populated."""
    event = MessageEvent(
        message_id="111222333",
        channel_id=_CHANNEL_ID,
        content="$NVDA beat earnings, strong guidance. Record revenue.",
        timestamp=_NOW - timedelta(minutes=5),
    )
    factory, holder = _make_client_factory([event])

    await run_discord_listener(
        session_factory=session_factory,
        creds=creds,
        client_factory=factory,
        now=lambda: _NOW,
    )

    assert holder["client"].connected
    assert holder["client"].closed

    with session_factory() as s:
        rows = s.query(NewsSignal).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "discord"
        assert row.source_ref == "msg-111222333"
        assert row.raw_text == event.content
        # Extractor normalized fields
        assert "NVDA" in json.loads(row.parsed_tickers)
        assert row.sentiment == "positive"
        assert row.source_trust == "medium"
        # Evidence excerpt is non-empty and bounded
        assert 1 <= len(row.evidence_excerpt) <= 280


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_idempotent_on_repeated_message_id(
    session_factory, creds,
) -> None:
    """Re-dispatching the same message-id → still 1 row."""
    event = MessageEvent(
        message_id="555",
        channel_id=_CHANNEL_ID,
        content="$NVDA earnings beat.",
        timestamp=_NOW - timedelta(minutes=1),
    )
    # Two copies of the same event.
    factory, _holder = _make_client_factory([event, event])

    await run_discord_listener(
        session_factory=session_factory,
        creds=creds,
        client_factory=factory,
        now=lambda: _NOW,
    )

    with session_factory() as s:
        rows = s.query(NewsSignal).all()
        assert len(rows) == 1
        assert rows[0].source_ref == "msg-555"


# ---------------------------------------------------------------------------
# max_message_age_minutes filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_drops_messages_older_than_max_age(
    session_factory, creds,
) -> None:
    """Message older than max_message_age_minutes → never persisted."""
    old_event = MessageEvent(
        message_id="oldmsg-1",
        channel_id=_CHANNEL_ID,
        content="$NVDA stale headline from yesterday.",
        timestamp=_NOW - timedelta(hours=24),  # >>60 min
    )
    fresh_event = MessageEvent(
        message_id="freshmsg-2",
        channel_id=_CHANNEL_ID,
        content="$NVDA fresh headline.",
        timestamp=_NOW - timedelta(minutes=3),
    )
    factory, _holder = _make_client_factory([old_event, fresh_event])

    await run_discord_listener(
        session_factory=session_factory,
        creds=creds,
        client_factory=factory,
        max_message_age_minutes=60,
        now=lambda: _NOW,
    )

    with session_factory() as s:
        rows = s.query(NewsSignal).all()
        assert len(rows) == 1
        assert rows[0].source_ref == "msg-freshmsg-2"


# ---------------------------------------------------------------------------
# Cross-channel filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_ignores_messages_from_other_channels(
    session_factory, creds,
) -> None:
    """Defensive: gateway shouldn't send these, but if it does, drop."""
    foreign = MessageEvent(
        message_id="foreign-1",
        channel_id=_CHANNEL_ID + 1,
        content="$NVDA cross-channel leak.",
        timestamp=_NOW - timedelta(minutes=1),
    )
    factory, _holder = _make_client_factory([foreign])

    await run_discord_listener(
        session_factory=session_factory,
        creds=creds,
        client_factory=factory,
        now=lambda: _NOW,
    )

    with session_factory() as s:
        assert s.query(NewsSignal).count() == 0


# ---------------------------------------------------------------------------
# Attachment ingest — caption + .txt body both reach the extractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_combines_caption_and_txt_attachment_into_raw_text(
    session_factory, creds,
) -> None:
    """Daily-report channel posts as a ``.txt`` file with a short caption.

    The listener must HTTPS-GET the attachment URL, decode it as
    UTF-8, and feed BOTH the caption AND the downloaded text to the
    extractor — so ``news_signals.raw_text`` includes BOTH (caption
    first, attachment after; codex review focus — caption-first
    ordering means a user-supplied alpha-call prefix wins regex
    precedence).
    """
    caption = "Today's report"
    attachment_body = (
        "Alpha Report 5/29/2026\n"
        "BUY $NVDA target $180 stop $135\n"
        "(record revenue beat, strong guidance)"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Confirm signed-URL contract: bot token NEVER reaches the CDN
        # (either no Authorization header, or our explicit empty scrub).
        auth = request.headers.get("Authorization")
        assert auth in (None, "")
        body_bytes = attachment_body.encode("utf-8")
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body_bytes)),
            },
            content=body_bytes,
        )

    attachment = Attachment(
        id="att-1",
        filename="Alpha Report 5-29-2026.txt",
        content_type="text/plain",
        size=len(attachment_body.encode("utf-8")),
        url="https://cdn.discordapp.com/attachments/1/2/report.txt?ex=abc&hm=def",
    )
    event = MessageEvent(
        message_id="9001",
        channel_id=_CHANNEL_ID,
        content=caption,
        timestamp=_NOW - timedelta(minutes=2),
        attachments=[attachment],
    )
    factory, _holder = _make_client_factory([event])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        await run_discord_listener(
            session_factory=session_factory,
            creds=creds,
            client_factory=factory,
            now=lambda: _NOW,
            http_client=http_client,
        )

    with session_factory() as s:
        rows = s.query(NewsSignal).all()
        assert len(rows) == 1
        row = rows[0]
        # Caption appears in raw_text...
        assert caption in row.raw_text
        # ...AND the attachment body too.
        assert "Alpha Report 5/29/2026" in row.raw_text
        assert "BUY $NVDA target $180 stop $135" in row.raw_text
        # Caption-first ordering — caption's position precedes the
        # attachment body in the stored raw_text.
        assert row.raw_text.find(caption) < row.raw_text.find(
            "Alpha Report 5/29/2026"
        )
        # Extractor still surfaced NVDA from the combined text.
        assert "NVDA" in json.loads(row.parsed_tickers)


@pytest.mark.asyncio
async def test_listener_with_image_attachment_only_uses_caption(
    session_factory, creds,
) -> None:
    """Non-text attachment (image) → not fetched; caption-only feeds
    the extractor. No HTTP call attempted."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, content=b"should-not-happen")

    image_att = Attachment(
        id="img-1",
        filename="screenshot.png",
        content_type="image/png",
        size=12345,
        url="https://cdn.discordapp.com/img.png?sig=1",
    )
    event = MessageEvent(
        message_id="9002",
        channel_id=_CHANNEL_ID,
        content="$NVDA earnings beat — screenshot attached.",
        timestamp=_NOW - timedelta(minutes=2),
        attachments=[image_att],
    )
    factory, _holder = _make_client_factory([event])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        await run_discord_listener(
            session_factory=session_factory,
            creds=creds,
            client_factory=factory,
            now=lambda: _NOW,
            http_client=http_client,
        )

    assert calls == []
    with session_factory() as s:
        rows = s.query(NewsSignal).all()
        assert len(rows) == 1
        assert rows[0].raw_text == event.content
