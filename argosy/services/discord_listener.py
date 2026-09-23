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

Connection safety
-----------------

The production transport uses discord.py (already a project dependency),
including session RESUME, heartbeat acknowledgements and reconnect backoff.
It waits for authenticated READY before reporting connected. Durable per-token
login/IDENTIFY budgets and an OS process lock protect against restart storms.
Authentication/configuration failures stop the job with an actionable error.
This feed is separate from the private conversational Discord bot.

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
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from argosy.services.discord_attachment_fetcher import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    fetch_text_attachments,
)
from argosy.services.news_extractor import extract
from argosy.services.predictions.parsers import extract_alpha_call_from_text
from argosy.services.predictions.writers import write_discord_prediction
from argosy.state.models import NewsSignal

logger = logging.getLogger(__name__)


# Long-form alpha-report skip thresholds — kept module-level so tests can
# pin contracts via inspection. Mirrors the same constants in
# ``argosy/services/predictions/discord_backfill.py``. Posts longer than
# ``LONG_FORM_BODY_CHAR_THRESHOLD`` chars OR with more than
# ``LONG_FORM_NEWLINE_THRESHOLD`` newlines are routed to the
# ``alpha_report_analyst`` Opus pipeline instead of the regex parser
# (which produces one false-positive ``Prediction`` per long post).
LONG_FORM_BODY_CHAR_THRESHOLD: int = 500
LONG_FORM_NEWLINE_THRESHOLD: int = 5


def _is_long_form_alpha_report(text: str | None) -> bool:
    """True when ``text`` is a long-form post that the regex parser
    should NOT attempt — the ``alpha_report_analyst`` cron handles
    these instead.

    Why both thresholds: a length-only gate misses short multi-paragraph
    posts (a 400-char post with 8 newlines is still long-form
    commentary); a newline-only gate misses a 2-KB single-paragraph
    rant. OR-ing both is generous on the safe side — false-positive
    long-form classifications just defer the regex parse to the next
    cron, which is harmless.
    """
    if not text:
        return False
    if len(text) > LONG_FORM_BODY_CHAR_THRESHOLD:
        return True
    if text.count("\n") > LONG_FORM_NEWLINE_THRESHOLD:
        return True
    return False


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

    missing = [k for k in ("channel_id", "server_id") if k not in payload]
    if "bot_token" not in payload and "token_secret" not in payload:
        missing.append("bot_token")
    if missing:
        raise ValueError(
            f"Discord creds file {creds_path} is missing required field(s): "
            f"{', '.join(missing)}"
        )

    if "token_secret" in payload:
        # Fixed key keeps this integration isolated from the private advisor.
        if payload["token_secret"] != "discord_listener_bot_token":
            raise ValueError("Discord listener token_secret must be discord_listener_bot_token")
        from argosy.secrets import get_secret
        bot_token = get_secret("discord_listener_bot_token")
        if not bot_token:
            raise ValueError("Discord listener keychain token missing; run discord-listener setup")
    else:
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


# Default client factory uses the maintained SDK. Tests may pass a stub.
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
    on_disconnected: Callable[[], None] | None = None,
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
            tests). ``None`` uses the maintained Discord SDK.
        now: Override the wallclock for age comparisons (for tests).
            ``None`` uses ``datetime.now(timezone.utc)``.
        on_connected: Optional callback after authenticated READY. Callback
            failures are logged without interrupting ingestion.

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
    def status_changed(connected: bool) -> None:
        callback = on_connected if connected else on_disconnected
        if callback:
            try:
                callback()
            except Exception:
                logger.exception("discord_listener: status callback failed")
    if hasattr(client, "set_status_callback"):
        client.set_status_callback(status_changed)
    logger.info(
        "discord_listener: connecting to channel %s on server %s",
        creds.channel_id, creds.server_id,
    )
    try:
        await client.connect()
    except BaseException:
        # Includes cancelled/failed authentication: don't leak sockets, SDK
        # tasks or the cross-process lease before the message-loop finally.
        await client.close()
        raise
    logger.info("discord_listener: connected, awaiting messages")

    # The production client has received authenticated READY, not just HELLO.
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

    Long-form skip (alpha_report_analyst contract): the regex parser
    is designed for tight messages (``BUY $NVDA target $150 stop $130``).
    Long-form Discord posts (multi-page Meet Kevin-style commentary)
    were producing one false-positive prediction per post when the
    regex hit the first matching pattern inside paragraphs of prose.
    For ``len > 500`` chars OR ``> 5`` newlines we skip — the
    :func:`alpha_report_analyst_runner.run_pending_batch` cron picks
    up these posts and runs the Opus analyst instead. Tight messages
    keep the regex path.
    """
    parse_text = effective_text if effective_text is not None else event.content
    if _is_long_form_alpha_report(parse_text):
        logger.debug(
            "discord_listener: skipping regex parser, long-form report; "
            "analyst will handle (message_id=%s, len=%d, newlines=%d)",
            event.message_id,
            len(parse_text or ""),
            (parse_text or "").count("\n"),
        )
        return
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
# Default client — maintained SDK with durable safety budgets
# ---------------------------------------------------------------------------


def _default_client_factory(creds: DiscordCreds) -> DiscordClient:
    """Construct the guarded, session-resuming production transport."""
    from argosy.services.discord_feed_gateway import DiscordFeedGateway
    return DiscordFeedGateway(creds)


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
