"""Discord gateway listener for daily-automation news ingest.

Sprint commit #16 of the plan/execute/monitor reorg. Lights up the
``discord`` source path that ``news_ingest`` reserved in commit #13.

Sprint A commit #6 update
-------------------------

Production now drives this listener through
:class:`argosy.services.jobs.discord_listener_job.DiscordListenerJob`
(a :class:`~argosy.orchestrator.loops.base.LongRunningJob` registered
with the :class:`~argosy.services.jobs.registry.JobRegistry`). The
JobRegistry's supervisor opens an audit row, ``await``-s
``run_discord_listener``, and applies exponential-backoff restart on
crashes. The external-cron expectation that earlier shipped with this
module is retired.

The ``argosy discord-ingest`` CLI in ``argosy/cli/discord_ingest.py``
is kept as a one-shot smoke test only.

Setup (Ariel's machine)
-----------------------

The bot reads credentials from ``~/.argosy/discord_creds.json``::

    {
      "bot_token": "MT...your-bot-token-here...",
      "channel_id": 1234567890,
      "server_id":  9876543210
    }

If the file is missing, ``load_creds`` returns ``None`` and the bot
stays dormant — the supervisor that schedules
``run_discord_listener`` calls ``load_creds`` first and skips the
listener if credentials are not present. This keeps fresh checkouts /
CI green without requiring real Discord tokens.

Implementation note — discord.py vs raw websockets
--------------------------------------------------

``discord.py`` is NOT in ``pyproject.toml``. To avoid adding a heavy
dependency for what is effectively a one-channel read-only listener, we
talk to the Discord gateway directly using the ``websockets`` library
(already pulled in transitively by FastAPI / uvicorn). The protocol
surface we need is small:

* Opcode 10  ``HELLO``    — server announces ``heartbeat_interval``.
* Opcode  1  ``HEARTBEAT`` — periodic keep-alive (sequence number).
* Opcode  2  ``IDENTIFY``  — auth payload with bot token + intents.
* Opcode  0  ``DISPATCH``  — ``MESSAGE_CREATE`` is the only event we
                             care about. Other dispatches are ignored.

We deliberately do NOT implement RESUME / sharding / voice / reactions /
slash-commands. Restart on disconnect is delegated to the caller (cron
or supervisor) per the task spec.

Codex BLOCKER #2 isolation contract
-----------------------------------

The bot is a PASSIVE READER. It never parses message content as
commands ("/buy NVDA" / "ignore previous instructions" / ...). Every
message body is fed verbatim to the Stage 1 extractor whose ticker
whitelist drops non-whitelisted symbols and whose normalized fields are
the only thing the Stage 2 LLM ever sees. The raw text is stored on
``news_signals.raw_text`` for the user's citation display ONLY — it
never reaches an LLM prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field as dataclasses_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from argosy.services.discord_attachment_fetcher import (
    Attachment,
    MAX_ATTACHMENT_BYTES,
    fetch_text_attachments,
    parse_attachments,
)
from argosy.services.news_extractor import extract
from argosy.services.predictions.parsers import extract_alpha_call_from_text
from argosy.services.predictions.writers import write_discord_prediction
from argosy.state.models import NewsSignal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


# Bot tokens issued by Discord begin with one of these prefixes today.
# Codex IMPORTANT (commit #16 review): the prior list included a bare
# "M" prefix which is overly permissive — almost any string starting
# with M would pass. Tightened to longer-known prefixes only. We still
# accept "Bot " (legacy inline form) for backward compat.
_DISCORD_TOKEN_PREFIXES: tuple[str, ...] = ("MT", "ND", "OD", "Bot ", "MTI", "MTk", "ODQ")
# Minimum reasonable token length — Discord tokens are 59-72 chars
# typically; reject anything obviously truncated.
_DISCORD_TOKEN_MIN_LEN: int = 50


@dataclass(frozen=True)
class DiscordCreds:
    """Bot credentials loaded from ``~/.argosy/discord_creds.json``."""

    bot_token: str
    channel_id: int
    server_id: int


def _default_creds_path() -> Path:
    """``~/.argosy/discord_creds.json`` — expanded with ``os.path.expanduser``
    so it works on both POSIX and Windows (no hardcoded ``HOME`` assumption)."""
    return Path(os.path.expanduser("~")) / ".argosy" / "discord_creds.json"


def load_creds(path: Path | None = None) -> DiscordCreds | None:
    """Load Discord bot credentials from disk.

    Args:
        path: Override the default location. ``None`` uses
            ``~/.argosy/discord_creds.json``.

    Returns:
        A frozen ``DiscordCreds`` if the file exists and is well-formed.
        ``None`` if the file is missing — the bot stays dormant (no
        error, no log spam).

    Raises:
        ValueError: If the file exists but is malformed (not JSON, not an
            object, missing a required field, wrong type, or the
            ``bot_token`` does not look like a Discord token).
    """
    creds_path = path if path is not None else _default_creds_path()
    if not creds_path.exists():
        return None

    # Codex IMPORTANT (commit #16 review): catch OSError/PermissionError
    # explicitly so the caller (CLI) can map them to the "malformed
    # creds" exit code rather than letting them propagate as
    # unclassified RuntimeError.
    try:
        raw_text = creds_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"Discord creds file {creds_path} could not be read: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Discord creds file {creds_path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Discord creds file {creds_path} must contain a JSON object, "
            f"got {type(payload).__name__}"
        )

    missing = [k for k in ("bot_token", "channel_id", "server_id") if k not in payload]
    if missing:
        raise ValueError(
            f"Discord creds file {creds_path} is missing required field(s): "
            f"{', '.join(missing)}"
        )

    bot_token = payload["bot_token"]
    channel_id = payload["channel_id"]
    server_id = payload["server_id"]

    if not isinstance(bot_token, str) or not bot_token.strip():
        raise ValueError(
            f"Discord creds file {creds_path}: bot_token must be a non-empty string"
        )
    if not any(bot_token.startswith(p) for p in _DISCORD_TOKEN_PREFIXES):
        raise ValueError(
            f"Discord creds file {creds_path}: bot_token does not look like a "
            "Discord bot token (expected one of "
            f"{_DISCORD_TOKEN_PREFIXES})"
        )
    if len(bot_token) < _DISCORD_TOKEN_MIN_LEN:
        raise ValueError(
            f"Discord creds file {creds_path}: bot_token is too short "
            f"(got {len(bot_token)} chars, need >= {_DISCORD_TOKEN_MIN_LEN}). "
            "Truncated token?"
        )
    if not isinstance(channel_id, int) or isinstance(channel_id, bool):
        raise ValueError(
            f"Discord creds file {creds_path}: channel_id must be an integer"
        )
    if not isinstance(server_id, int) or isinstance(server_id, bool):
        raise ValueError(
            f"Discord creds file {creds_path}: server_id must be an integer"
        )

    return DiscordCreds(
        bot_token=bot_token,
        channel_id=channel_id,
        server_id=server_id,
    )


# ---------------------------------------------------------------------------
# Discord gateway client interface (so tests can inject a fake)
# ---------------------------------------------------------------------------


class _MessageEvent(Protocol):
    """Shape of a MESSAGE_CREATE event surfaced to the dispatch handler.

    The real Discord gateway sends a JSON payload with many fields; we
    only consume these five. Tests can construct a plain object/dict
    that satisfies this Protocol via ``.message_id`` / etc.

    ``attachments`` was added when the alpha-report channel started
    posting daily reports as ``.txt`` file uploads (caption + file). A
    message with no attachments has an empty list — never ``None``.
    """

    @property
    def message_id(self) -> str: ...
    @property
    def channel_id(self) -> int: ...
    @property
    def content(self) -> str: ...
    @property
    def timestamp(self) -> datetime: ...
    @property
    def attachments(self) -> list[Attachment]: ...


@dataclass(frozen=True)
class MessageEvent:
    """Concrete carrier for MESSAGE_CREATE events.

    ``attachments`` defaults to an empty list so existing tests that
    don't care about file uploads can construct a ``MessageEvent``
    with only the four required fields.
    """

    message_id: str
    channel_id: int
    content: str
    timestamp: datetime
    attachments: list[Attachment] = dataclasses_field(default_factory=list)


class DiscordClient(Protocol):
    """Minimal interface the listener needs from a Discord client.

    The default factory wires up a thin websockets-based client
    (see ``_RawWebsocketsDiscordClient`` below). Tests pass a fake.
    """

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def messages(self) -> Any:
        """Async iterator yielding ``MessageEvent`` objects."""
        ...


# ---------------------------------------------------------------------------
# Public listener entry point
# ---------------------------------------------------------------------------


# Default client factory uses raw websockets. Tests pass a stub.
ClientFactory = Callable[[DiscordCreds], DiscordClient]


async def run_discord_listener(
    session_factory: Callable[[], Session],
    *,
    creds: DiscordCreds,
    known_tickers: frozenset[str] | None = None,
    max_message_age_minutes: int = 60,
    client_factory: ClientFactory | None = None,
    now: Callable[[], datetime] | None = None,
    on_connected: Callable[[], None] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Connect to the Discord gateway and persist incoming messages.

    Idempotent on ``(source='discord', source_ref='msg-{id}')``. The
    function does NOT auto-restart on error — the caller (the
    JobRegistry supervisor in production; ``argosy discord-ingest`` for
    one-shot smoke) handles restarts. Every connect / disconnect /
    message event is logged at INFO so an operator can tail the log.

    Args:
        session_factory: Zero-arg callable that returns a SQLAlchemy
            ``Session``. One session is opened per message so a long-
            running listener doesn't hold a transaction open for hours.
        creds: Validated credentials. Caller obtains via ``load_creds``.
        known_tickers: Override the Stage 1 ticker whitelist. ``None`` →
            extractor default.
        max_message_age_minutes: Skip messages older than this many
            minutes. Default 60. Prevents re-ingesting an entire channel
            history on reconnect.
        client_factory: Override the Discord client constructor (for
            tests). ``None`` uses the raw-websockets default.
        now: Override the wallclock for age comparisons (for tests).
            ``None`` uses ``datetime.now(timezone.utc)``.
        on_connected: Optional zero-arg callback fired AFTER the gateway
            transport handshake completes (HELLO opcode received +
            IDENTIFY sent + heartbeat scheduled) — i.e. immediately
            after the awaited ``client.connect()`` returns without
            raising. Used by
            :class:`~argosy.services.jobs.discord_listener_job.DiscordListenerJob`
            to flip its ``connection_status()`` from ``"reconnecting"``
            to ``"connected"``. Default ``None`` is a no-op — existing
            tests + the CLI smoke path are unaffected (Sprint A
            commit #6).

            Semantics (codex review BLOCKER on commit #6): the callback
            fires at the GATEWAY-TRANSPORT-CONNECTED point — i.e. HELLO
            + heartbeat are in flight, IDENTIFY has been SENT but the
            gateway's IDENTIFY-ACK / ``READY`` dispatch has NOT yet
            been received. This is a deliberate trade-off: firing here
            lights up the admin-UI green dot promptly on a healthy
            gateway, at the cost of a brief false-positive window if
            the bot token has been revoked (the gateway will close the
            connection seconds later when it rejects IDENTIFY; the
            listener observes that as a clean disconnect + the
            supervisor opens a fresh cycle). A stricter "fire on first
            ``READY`` dispatch" semantic would require parsing the
            ``READY`` opcode in the raw-websockets client — a follow-on
            if false-positives become a real operator pain. Callback
            exceptions are caught + logged but do NOT bring the
            listener down — a flaky status hook should not crash
            ingestion.

    Returns:
        None — the coroutine runs until the client iterator stops, then
        returns. Exceptions propagate; the supervisor handles them.
    """
    factory = client_factory if client_factory is not None else _default_client_factory
    now_fn = now if now is not None else (lambda: datetime.now(timezone.utc))
    # Codex NIT (commit #16 review): guard against negative values that
    # would silently drop every incoming message.
    if max_message_age_minutes < 0:
        raise ValueError(
            f"max_message_age_minutes must be >= 0; got "
            f"{max_message_age_minutes}"
        )
    max_age = timedelta(minutes=max_message_age_minutes)

    client = factory(creds)
    logger.info(
        "discord_listener: connecting to channel %s on server %s",
        creds.channel_id, creds.server_id,
    )
    await client.connect()
    logger.info("discord_listener: connected, awaiting messages")

    # Sprint A commit #6 — fire the status hook AFTER client.connect()
    # returns successfully. For the real client, ``connect()`` returns
    # once it has received the HELLO opcode and SENT IDENTIFY (see
    # ``_RawWebsocketsDiscordClient.connect``: HELLO is verified +
    # IDENTIFY is dispatched + the heartbeat task is started, all
    # before the function returns). IDENTIFY-ACK / ``READY`` arrive
    # later inside the messages loop; this hook is therefore a
    # "transport connected, auth in flight" signal — see the
    # docstring's BLOCKER note for the deliberate trade-off.
    if on_connected is not None:
        try:
            on_connected()
        except Exception:  # pragma: no cover - defensive
            logger.exception("discord_listener: on_connected callback raised")

    # Attachment fetcher needs an httpx.AsyncClient. We open one for
    # the lifetime of the listener so each MESSAGE_CREATE doesn't pay
    # connection-setup cost; the CDN-keepalive amortizes across the
    # daily-report cadence. Caller may inject one (tests do, to avoid
    # real CDN hits).
    own_http_client = http_client is None
    effective_http_client = http_client or httpx.AsyncClient()
    try:
        async for event in await _ensure_async_iter(client.messages()):
            await _handle_message(
                event,
                session_factory=session_factory,
                channel_id=creds.channel_id,
                known_tickers=known_tickers,
                max_age=max_age,
                now_fn=now_fn,
                http_client=effective_http_client,
            )
    finally:
        logger.info("discord_listener: disconnecting")
        await client.close()
        if own_http_client:
            await effective_http_client.aclose()


async def _ensure_async_iter(maybe_awaitable: Any) -> Any:
    """``client.messages()`` may return either an async iterator directly
    or a coroutine that yields one. Normalize so callers can ``async for``."""
    if asyncio.iscoroutine(maybe_awaitable):
        return await maybe_awaitable
    return maybe_awaitable


async def _handle_message(
    event: _MessageEvent,
    *,
    session_factory: Callable[[], Session],
    channel_id: int,
    known_tickers: frozenset[str] | None,
    max_age: timedelta,
    now_fn: Callable[[], datetime],
    http_client: httpx.AsyncClient,
) -> None:
    """Process one MESSAGE_CREATE event end-to-end.

    Filters: wrong channel → drop; message older than ``max_age`` →
    drop. Otherwise: fetch any text attachments, concatenate
    caption + attachment text (caption FIRST so a user-supplied prefix
    wins for alpha-call regex precedence — see codex review focus),
    extract, idempotently persist on (discord, msg-{id}).
    """
    # Filter: wrong channel (the gateway shouldn't send these because
    # we identified for one guild, but be defensive).
    if event.channel_id != channel_id:
        logger.debug(
            "discord_listener: ignoring message from channel %s (want %s)",
            event.channel_id, channel_id,
        )
        return

    # Filter: too old. ``event.timestamp`` is the message's Discord
    # creation time; we compare against now.
    age = now_fn() - event.timestamp
    if age > max_age:
        logger.info(
            "discord_listener: dropping stale message %s (age=%s > max=%s)",
            event.message_id, age, max_age,
        )
        return

    source_ref = f"msg-{event.message_id}"
    received_at = event.timestamp

    # Fetch any text attachments. The alpha-report channel posts the
    # daily report as a ``.txt`` file with a caption — the caption is
    # in ``event.content`` and the actual report text is at the
    # attachment URL. We feed BOTH (caption first, then attachment) to
    # the extractor + alpha-call parser so the user-supplied caption's
    # any explicit alpha-call prefix wins regex precedence.
    attachment_text = await fetch_text_attachments(
        getattr(event, "attachments", []) or [],
        http_client=http_client,
        max_bytes=MAX_ATTACHMENT_BYTES,
    )
    effective_text = (
        f"{event.content}\n\n{attachment_text}"
        if attachment_text
        else event.content
    )

    # Idempotency check — open a fresh session per message so a long
    # listener doesn't hold a transaction open for hours.
    session = session_factory()
    try:
        if _already_ingested(session, source_ref):
            logger.info(
                "discord_listener: message %s already ingested, skipping",
                event.message_id,
            )
            return

        signal = extract(
            source="discord",
            source_ref=source_ref,
            raw_text=effective_text,
            received_at=received_at,
            known_tickers=known_tickers,
        )
        row = NewsSignal(
            source=signal.source,
            source_ref=signal.source_ref,
            received_at=signal.received_at,
            parsed_tickers=json.dumps(signal.parsed_tickers),
            event_keywords=json.dumps(signal.event_keywords),
            sentiment=signal.sentiment,
            source_trust=signal.source_trust,
            evidence_excerpt=signal.evidence_excerpt,
            raw_text=signal.raw_text,
        )
        session.add(row)
        session.commit()
        logger.info(
            "discord_listener: persisted message %s "
            "(tickers=%s, keywords=%s, sentiment=%s, attachments=%d)",
            event.message_id, signal.parsed_tickers,
            signal.event_keywords, signal.sentiment,
            len(getattr(event, "attachments", []) or []),
        )

        # Spec C commit #3 — predictions ledger writer wiring. GATE on
        # actionable: only write a prediction when the message body
        # parses to a (direction, ticker) pair. Chatter / off-topic
        # messages stay out of the ledger.
        _maybe_write_discord_prediction(
            session=session,
            news_signal_row=row,
            event=event,
            channel_id=channel_id,
            effective_text=effective_text,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _maybe_write_discord_prediction(
    *,
    session: Session,
    news_signal_row: NewsSignal,
    event: _MessageEvent,
    channel_id: int,
    effective_text: str | None = None,
) -> None:
    """Spec C commit #3 — emit a prediction row for actionable Discord calls.

    Gates on the message body containing a parseable (direction, ticker)
    pair via ``extract_alpha_call_from_text``. Non-actionable messages
    (chatter, off-topic) skip silently.

    ``effective_text`` is the caption+attachment combined text; when
    omitted (older callers) we fall back to ``event.content`` for
    backward compat. The parser sees the combined text so an alpha
    call that lives only in the attached daily-report ``.txt`` gets a
    ledger entry.

    Per [[feedback_ask_dont_assume]] the writer is best-effort: any
    failure here logs + swallows so a bad prediction write never blocks
    a legitimate NewsSignal ingest.
    """
    parse_text = effective_text if effective_text is not None else event.content
    call = extract_alpha_call_from_text(parse_text)
    if call is None:
        return
    # User id: discord_listener doesn't carry a per-message user_id
    # (single-tenant deployment); default to 'ariel' to match the
    # rest of Argosy's single-tenant convention. Multi-tenant
    # rollout (SDD §12.5) will plumb the tenant from the listener
    # supervisor.
    user_id = "ariel"
    # Wrap in a SAVEPOINT so a writer failure (FK violations against an
    # unseeded evaluation_method_registry; CHECK errors) rolls back only
    # the prediction insert — the NewsSignal commit upstream survives.
    try:
        with session.begin_nested():
            write_discord_prediction(
                session,
                user_id,
                message_id=str(event.message_id),
                channel_id=channel_id,
                ticker=call.ticker,
                direction=call.direction,
                target_price=call.target_price,
                stop_price=call.stop_price,
                event_at=event.timestamp,
                raw_text_ref=(
                    f"news_signals.id:{news_signal_row.id}"
                    if news_signal_row.id is not None
                    else None
                ),
            )
        session.commit()
    except Exception:  # noqa: BLE001 — never break ingest on writer failure
        logger.exception(
            "discord_listener: write_discord_prediction failed for message %s",
            event.message_id,
        )


def _already_ingested(session: Session, source_ref: str) -> bool:
    """Idempotency check against the (source, source_ref) unique index."""
    from sqlalchemy import select

    stmt = select(NewsSignal.id).where(
        NewsSignal.source == "discord",
        NewsSignal.source_ref == source_ref,
    )
    return session.execute(stmt).first() is not None


# ---------------------------------------------------------------------------
# Default client — raw websockets implementation
# ---------------------------------------------------------------------------


# Discord gateway version + URL. v10 is current as of 2026.
_DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway intents bitmask. We need:
#   GUILDS (1 << 0)           — channel metadata
#   GUILD_MESSAGES (1 << 9)   — receive MESSAGE_CREATE in guild channels
#   MESSAGE_CONTENT (1 << 15) — actually see ``content`` (privileged intent;
#                               must be enabled in the bot's app settings)
_INTENTS = (1 << 0) | (1 << 9) | (1 << 15)


def _default_client_factory(creds: DiscordCreds) -> DiscordClient:
    """Construct the production raw-websockets client."""
    return _RawWebsocketsDiscordClient(creds)


class _RawWebsocketsDiscordClient:
    """Minimal Discord gateway client over raw websockets.

    Implements just enough of the gateway protocol to subscribe to
    MESSAGE_CREATE events on the configured channel. Heartbeat is a
    background task; the public ``messages()`` async iterator yields
    ``MessageEvent`` for each MESSAGE_CREATE dispatch.

    This class is constructed by the default factory; tests pass their
    own ``DiscordClient`` and never exercise this code.
    """

    def __init__(self, creds: DiscordCreds) -> None:
        self._creds = creds
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sequence: int | None = None
        self._heartbeat_interval_ms: int | None = None
        self._closed = False

    async def connect(self) -> None:
        # Lazy import so the module imports cleanly even if websockets
        # is somehow unavailable; the test path never reaches this.
        import websockets  # type: ignore[import-not-found]

        self._ws = await websockets.connect(_DISCORD_GATEWAY_URL)
        hello = json.loads(await self._ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(
                f"discord_listener: expected HELLO opcode 10, got {hello.get('op')}"
            )
        self._heartbeat_interval_ms = int(hello["d"]["heartbeat_interval"])

        # IDENTIFY (opcode 2)
        await self._ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": self._creds.bot_token,
                "intents": _INTENTS,
                "properties": {
                    "os": "linux",
                    "browser": "argosy",
                    "device": "argosy",
                },
            },
        }))

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        assert self._heartbeat_interval_ms is not None
        interval_s = self._heartbeat_interval_ms / 1000.0
        while not self._closed:
            await asyncio.sleep(interval_s)
            if self._closed or self._ws is None:
                return
            try:
                await self._ws.send(json.dumps({"op": 1, "d": self._sequence}))
            except Exception as exc:  # pragma: no cover — network
                logger.warning("discord_listener: heartbeat failed: %s", exc)
                return

    async def messages(self) -> Any:
        assert self._ws is not None
        async for raw in self._ws:
            payload = json.loads(raw)
            seq = payload.get("s")
            if seq is not None:
                self._sequence = seq
            if payload.get("op") != 0:
                continue
            if payload.get("t") != "MESSAGE_CREATE":
                continue
            data = payload.get("d", {})
            try:
                yield MessageEvent(
                    message_id=str(data["id"]),
                    channel_id=int(data["channel_id"]),
                    content=str(data.get("content", "")),
                    timestamp=_parse_discord_ts(data["timestamp"]),
                    attachments=parse_attachments(data.get("attachments")),
                )
            except (KeyError, ValueError) as exc:  # pragma: no cover
                logger.warning(
                    "discord_listener: malformed MESSAGE_CREATE: %s", exc,
                )
                continue

    async def close(self) -> None:
        self._closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass


def _parse_discord_ts(value: str) -> datetime:
    """Discord sends ISO-8601 with microseconds and a ``+00:00`` offset
    (e.g. ``2026-05-29T14:32:11.123456+00:00``). ``fromisoformat`` handles
    that on Python 3.12. We normalize to UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Re-export the awaitable type hint so callers / tests can annotate
# their client_factory without importing the protocol.
__all__ = [
    "Attachment",
    "DiscordCreds",
    "DiscordClient",
    "MessageEvent",
    "load_creds",
    "run_discord_listener",
]


# Silence unused-import lints in some tooling — Awaitable is part of
# the public signature shape via DiscordClient.messages()'s return type.
_ = Awaitable
