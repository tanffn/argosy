"""Discord 14-day history backfill — Spec C commit #7.

One-shot path that walks the configured Discord channel BACKWARDS via
the REST API, parses each message, and writes a ``predictions`` row
for every actionable alpha call. Companion to the live
:func:`argosy.services.discord_listener.run_discord_listener` gateway
listener: the live path catches NEW messages going forward, this path
backfills historical messages so the reliability ledger has a sample
to score on day one.

Per spec §7.2:

    1. Load creds from ``~/.argosy/discord_creds.json``. Missing →
       graceful error in the summary (NOT an exception); no rows
       written.
    2. Walk the channel via
       ``GET /channels/{channel_id}/messages?limit=100&before=<msg_id>``,
       paginated, until the oldest fetched message is older than
       ``lookback_days`` ago OR the channel runs out of history.
    3. For each message:
         * Parse via
           :func:`argosy.services.predictions.parsers.extract_alpha_call_from_text`.
         * Skip silently if no ticker + direction were extracted —
           backfill is gated on actionable per the live listener's
           contract (spec §3 anti-collision).
         * Call
           :func:`argosy.services.predictions.writers.write_discord_prediction`.
           Idempotency on ``(source='discord', message_id=v1|predictions|
           discord|{channel}.{msg})`` means re-runs return existing rows
           (no duplicates) — see :func:`BackfillSummary` for the
           ``predictions_deduped`` counter.
    4. Return a :class:`BackfillSummary` capturing counts + first error.

Rate-limit handling
-------------------

Discord's REST API publishes a 50 req/sec global limit plus per-route
limits surfaced via response headers (``X-RateLimit-Remaining``,
``X-RateLimit-Reset-After``, ``Retry-After`` on 429). The backfill is
sequential (single in-flight request at a time, ~5 pages of 100
messages each for a typical 14-day window), so we sit well below the
global cap. We DO respect 429 responses by sleeping for the
``Retry-After`` value before retrying the same page; we DO honor the
per-route ``X-RateLimit-Remaining: 0`` signal by sleeping
``X-RateLimit-Reset-After`` seconds before the next request. Both
hooks land in :func:`_fetch_page`.

Hindsight-bias killer
---------------------

Per spec §2.3 the writer takes the price snapshot at ``event_at`` —
the message's Discord ``timestamp`` field — NOT at backfill-run time.
``write_discord_prediction`` is the gate; this module just passes
``event_at`` through verbatim.

Reuse vs the live listener
--------------------------

The live listener and this backfill share:

* The parser :func:`extract_alpha_call_from_text` (so live + backfill
  parse the same way; if live misses a message, so does backfill —
  reliability score is honest).
* The writer :func:`write_discord_prediction` (so live + backfill
  share the dedup partition; a message ingested live, then encountered
  again by backfill, returns the existing row).
* The creds loader :func:`argosy.services.discord_listener.load_creds`
  (same JSON file, same validation; backfill cannot accidentally read
  half-typed test creds).

What they do NOT share: this module writes predictions via the REST
API; the live listener writes via the websocket gateway. They are
independent transports.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from argosy.services.discord_attachment_fetcher import (
    MAX_ATTACHMENT_BYTES,
    fetch_text_attachments,
    parse_attachments,
)
from argosy.services.discord_listener import (
    DiscordCreds,
    _default_creds_path,
    load_creds,
)
from argosy.services.predictions.parsers import extract_alpha_call_from_text
from argosy.services.predictions.writers import (
    discord_message_id,
    write_discord_prediction,
)

logger = logging.getLogger(__name__)


# Long-form alpha-report skip thresholds — kept in sync with
# ``argosy/services/discord_listener.py``. Posts above either threshold
# are deferred to the ``alpha_report_analyst`` Opus cron rather than
# fed to the regex parser (which produced one false-positive
# Prediction per long-form post when it hit the first matching
# pattern inside paragraphs of prose).
LONG_FORM_BODY_CHAR_THRESHOLD: int = 500
LONG_FORM_NEWLINE_THRESHOLD: int = 5


def _is_long_form_alpha_report(text: str | None) -> bool:
    """True when ``text`` is a long-form post the regex parser should
    not attempt. Mirror of
    :func:`argosy.services.discord_listener._is_long_form_alpha_report`
    — duplicated rather than imported to avoid a service-layer ->
    listener-layer dependency cycle."""
    if not text:
        return False
    if len(text) > LONG_FORM_BODY_CHAR_THRESHOLD:
        return True
    if text.count("\n") > LONG_FORM_NEWLINE_THRESHOLD:
        return True
    return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default user_id stamped on backfilled predictions. Single-tenant
#: today (matches :mod:`argosy.services.discord_listener` convention);
#: a multi-tenant rollout (SDD §12.5) plumbs the tenant id from the
#: route handler / JobRegistry context.
DEFAULT_USER_ID = "ariel"

#: Discord API base URL. v10 is the current stable API as of 2026.
DISCORD_API_BASE = "https://discord.com/api/v10"

#: Per-page limit on ``GET /channels/{id}/messages``. Discord's max
#: per docs is 100; using the max minimises round-trips for the
#: 14-day window (~5 pages of 100 = ~500 messages, typical channel).
PAGE_LIMIT = 100

#: Hard cap on pagination iterations so a misbehaving API (or an
#: unbounded-history channel) cannot spin the backfill forever. Each
#: page is up to ``PAGE_LIMIT`` messages, so 200 pages = up to 20k
#: messages — generous for a 14-day window but bounded.
MAX_PAGES = 200

#: Per-request timeout for the Discord REST call. 30 s is generous —
#: Discord normally returns in well under a second; if a single call
#: takes longer than 30 s we're better off retrying than waiting.
REQUEST_TIMEOUT_S = 30.0

#: Cap on the Retry-After sleep we'll honor on a 429 before giving up
#: on the run. Discord rarely backs us off for more than a few
#: seconds; if we ever see >120 s we assume a misconfiguration and
#: surface the run as failed.
MAX_RETRY_AFTER_S = 120.0


# ---------------------------------------------------------------------------
# Public DTOs
# ---------------------------------------------------------------------------


@dataclass
class BackfillSummary:
    """Counts + first-error capture for one backfill run.

    Returned by :func:`backfill_discord_predictions` so callers (the
    JobRegistry tick body) can surface human-readable progress to the
    admin UI via ``job_runs.output_summary``.

    Attributes
    ----------
    messages_scanned
        Total messages fetched + inspected (parsed OR skipped).
    predictions_written
        Predictions newly INSERTed into the ledger.
    predictions_deduped
        Predictions that the writer's idempotency contract folded into
        an existing row (re-runs over an already-backfilled window).
    messages_unparseable
        Messages whose body did not yield a (ticker, direction) pair
        via the parser — skipped, not written.
    messages_long_form_skipped
        Messages skipped because they were long-form (> 500 chars OR
        > 5 newlines) and so are handled by the
        ``alpha_report_analyst`` cron instead of the regex parser.
        Reported separately from ``messages_unparseable`` so the
        operator can see how many posts deferred to the LLM path.
    pages_fetched
        Number of paginated REST calls made.
    errors
        Free-form list of ``"<reason>: <details>"`` strings. The first
        fatal error short-circuits the run; per-message non-fatals
        accumulate. An empty list means "fully clean run".
    creds_path
        The credentials file path consulted, for log/UI clarity.
    """

    messages_scanned: int = 0
    predictions_written: int = 0
    predictions_deduped: int = 0
    messages_unparseable: int = 0
    messages_long_form_skipped: int = 0
    pages_fetched: int = 0
    errors: list[str] = field(default_factory=list)
    creds_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for ``job_runs.output_summary``.

        Lists + ints only — no datetimes — so the JSON serializer in
        :meth:`JobRegistry._close_job_run` (sort_keys + default=str)
        doesn't have edge cases.
        """
        return {
            "messages_scanned": self.messages_scanned,
            "predictions_written": self.predictions_written,
            "predictions_deduped": self.predictions_deduped,
            "messages_unparseable": self.messages_unparseable,
            "messages_long_form_skipped": self.messages_long_form_skipped,
            "pages_fetched": self.pages_fetched,
            "errors": list(self.errors),
            "creds_path": self.creds_path,
        }


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Async fetcher callable matching :func:`_fetch_page` — overridable
#: for tests so we don't hit a real httpx client over the network.
#: Signature: ``(channel_id, before_id, bot_token) -> list[dict]``
#: where each dict is a Discord message JSON object.
PageFetcher = Callable[
    [int, str | None, str], Awaitable[list[dict[str, Any]]]
]


# ---------------------------------------------------------------------------
# Discord REST page fetcher (with rate-limit handling)
# ---------------------------------------------------------------------------


async def _fetch_page(
    channel_id: int,
    before_id: str | None,
    bot_token: str,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch one page of messages from Discord's REST API.

    Walks BACKWARDS via the ``before`` cursor: first call passes
    ``before_id=None`` to fetch the latest ``PAGE_LIMIT`` messages;
    subsequent calls pass the oldest message-id from the previous page
    to walk further back in time. Returns the raw decoded message
    objects (Discord-shaped dicts); the caller is responsible for
    timestamp parsing + lookback comparison.

    Rate-limit handling:

    * On HTTP 429: read ``Retry-After`` (in seconds; Discord supports
      fractional values), sleep, retry ONCE. A second 429 escalates
      to :class:`httpx.HTTPStatusError` so the caller can surface the
      sustained throttle to the admin UI.
    * On non-429 5xx: raise immediately. Transient retries are the
      JobRegistry's job (the run-now caller can re-fire the job).
    * On 4xx other than 429: raise immediately. These are
      misconfiguration (invalid token, missing channel, missing
      MESSAGE_CONTENT intent).

    The per-route ``X-RateLimit-Remaining`` header is checked
    OPPORTUNISTICALLY: when it reads ``"0"``, we sleep for
    ``X-RateLimit-Reset-After`` seconds BEFORE returning so the next
    call doesn't burn through Discord's bucket.

    Args:
        channel_id: Discord channel id (snowflake int).
        before_id: Walk-cursor — fetch messages OLDER than this id.
            ``None`` on the first page (fetch most-recent).
        bot_token: Validated bot token (caller verified format via
            :func:`load_creds`).
        client: Optional injected ``httpx.AsyncClient`` — tests pass a
            stub. Default opens its own transient client per call.
        sleep: Optional injected sleep — tests pass a no-op so 429
            handling exercises without wall-time delay. Default
            :func:`asyncio.sleep`.

    Returns:
        List of Discord message JSON objects (potentially empty when
        the channel has no more history before the cursor).
    """
    sleep_fn = sleep or asyncio.sleep
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    params: dict[str, Any] = {"limit": PAGE_LIMIT}
    if before_id is not None:
        params["before"] = before_id
    headers = {
        "Authorization": f"Bot {bot_token}",
        "User-Agent": "Argosy/1.0 (predictions-backfill)",
    }

    async def _do_request(http: httpx.AsyncClient) -> httpx.Response:
        return await http.get(
            url, params=params, headers=headers,
            timeout=REQUEST_TIMEOUT_S,
        )

    async def _handle(http: httpx.AsyncClient) -> list[dict[str, Any]]:
        # One retry on 429; second 429 escalates.
        for attempt in (1, 2):
            resp = await _do_request(http)
            if resp.status_code == 429:
                retry_after_raw = (
                    resp.headers.get("Retry-After")
                    or resp.headers.get("X-RateLimit-Reset-After")
                    or "1"
                )
                try:
                    retry_after_s = float(retry_after_raw)
                except (TypeError, ValueError):
                    retry_after_s = 1.0
                if retry_after_s > MAX_RETRY_AFTER_S:
                    # Sustained throttle — surface to caller so the
                    # admin UI shows "throttled, try later".
                    resp.raise_for_status()
                if attempt == 2:
                    # Second 429 in a row — escalate.
                    resp.raise_for_status()
                logger.info(
                    "discord_backfill: 429 received, sleeping %ss "
                    "(attempt %d)",
                    retry_after_s, attempt,
                )
                await sleep_fn(retry_after_s)
                continue
            resp.raise_for_status()
            # Opportunistic per-route remaining check.
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                reset_after_raw = resp.headers.get(
                    "X-RateLimit-Reset-After", "0"
                )
                try:
                    reset_after_s = float(reset_after_raw)
                except (TypeError, ValueError):
                    reset_after_s = 0.0
                if 0 < reset_after_s <= MAX_RETRY_AFTER_S:
                    logger.debug(
                        "discord_backfill: bucket exhausted, "
                        "pre-sleeping %ss",
                        reset_after_s,
                    )
                    await sleep_fn(reset_after_s)
            body = resp.json()
            if not isinstance(body, list):
                # Discord returns a list on success; an object would
                # be an error envelope. raise_for_status above should
                # already have rejected, but be defensive.
                raise httpx.HTTPError(
                    f"unexpected Discord response shape: {type(body).__name__}"
                )
            return body
        # Unreachable — the loop either returns or raises.
        raise RuntimeError("discord_backfill: _handle exhausted retries")

    if client is not None:
        return await _handle(client)
    async with httpx.AsyncClient() as transient_client:
        return await _handle(transient_client)


# ---------------------------------------------------------------------------
# Message timestamp parsing (same convention as discord_listener)
# ---------------------------------------------------------------------------


def _parse_discord_ts(value: str) -> datetime:
    """Discord sends ISO-8601 with microseconds + a ``+00:00`` offset.

    Mirrors :func:`argosy.services.discord_listener._parse_discord_ts`
    but kept here as a local copy so the backfill module does not
    depend on a private symbol from the listener (private leading-
    underscore convention).
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def backfill_discord_predictions(
    session: Session,
    *,
    lookback_days: int = 14,
    channel_id: int | None = None,
    bot_token: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    page_fetcher: PageFetcher | None = None,
    now: Callable[[], datetime] | None = None,
    attachment_http_client: httpx.AsyncClient | None = None,
) -> BackfillSummary:
    """Walk a Discord channel's history + write predictions for every
    actionable alpha call within the last ``lookback_days`` days.

    See module docstring for the algorithm; this function is the
    public seam.

    Args:
        session: live SQLAlchemy session. Caller owns commit / rollback.
            The function calls ``session.commit()`` after EACH
            successful prediction write so a mid-run crash preserves
            already-written rows (the writer's per-source dedup means
            a fresh run can safely re-walk the same window).
        lookback_days: How far back to fetch. Default 14 days per the
            original user ask. Larger values get larger; the
            ``MAX_PAGES`` ceiling caps unbounded walks at ~20k
            messages regardless.
        channel_id: Override the channel id from creds. ``None`` =
            use the ``channel_id`` from
            ``~/.argosy/discord_creds.json``.
        bot_token: Override the bot token from creds. ``None`` = same.
        user_id: Tenant the predictions are stamped to. Default
            ``"ariel"`` per the single-tenant convention.
        page_fetcher: Optional injected page-fetch coroutine for
            tests. Default :func:`_fetch_page` (real httpx).
        now: Optional clock for tests — controls the ``lookback`` cut
            so a fixed-history fixture can be tested against a frozen
            "now". Default :func:`datetime.now`.

    Returns:
        :class:`BackfillSummary` — never raises on operational errors
        (missing creds, transport blip mid-run). Programmer errors
        (bad ``lookback_days`` value) still raise ``ValueError``.

    Side effects:
        Inserts rows into ``predictions``; commits per insert.
    """
    if lookback_days <= 0:
        raise ValueError(
            f"lookback_days must be > 0; got {lookback_days!r}"
        )

    summary = BackfillSummary()
    now_fn = now or (lambda: datetime.now(timezone.utc))
    fetcher = page_fetcher or _fetch_page

    # Resolve creds. Caller-supplied values take precedence over the
    # file; this lets the test path skip the filesystem entirely.
    creds: DiscordCreds | None = None
    if channel_id is None or bot_token is None:
        try:
            creds = load_creds()
        except ValueError as exc:
            summary.errors.append(f"creds_invalid: {exc}")
            summary.creds_path = str(_default_creds_path())
            logger.warning(
                "discord_backfill: creds invalid — %s", exc
            )
            return summary
        if creds is None:
            summary.errors.append(
                "creds_missing: drop ~/.argosy/discord_creds.json to activate"
            )
            summary.creds_path = str(_default_creds_path())
            logger.warning(
                "discord_backfill: no creds at %s — nothing to do",
                _default_creds_path(),
            )
            return summary

        summary.creds_path = str(_default_creds_path())

    # Attachment fetcher shares one httpx client across the whole run
    # so CDN keepalive amortises across hundreds of attachments. Tests
    # inject a MockTransport-backed client to avoid real CDN hits.
    # Initialised AFTER the creds bail-out so a missing-creds run
    # doesn't open + leak a client.
    own_attachment_client = attachment_http_client is None
    attachment_client = attachment_http_client or httpx.AsyncClient()

    final_channel_id = channel_id if channel_id is not None else creds.channel_id  # type: ignore[union-attr]
    final_bot_token = bot_token if bot_token is not None else creds.bot_token  # type: ignore[union-attr]

    cutoff = now_fn() - timedelta(days=lookback_days)
    before_cursor: str | None = None

    for page_index in range(MAX_PAGES):
        try:
            page = await fetcher(
                final_channel_id, before_cursor, final_bot_token,
            )
        except httpx.HTTPStatusError as exc:
            summary.errors.append(
                f"http_error: status={exc.response.status_code} "
                f"page={page_index}"
            )
            logger.exception(
                "discord_backfill: HTTP error on page %d", page_index
            )
            break
        except httpx.HTTPError as exc:
            summary.errors.append(
                f"transport_error: {type(exc).__name__}: {exc} "
                f"page={page_index}"
            )
            logger.exception(
                "discord_backfill: transport error on page %d",
                page_index,
            )
            break
        summary.pages_fetched += 1

        if not page:
            # Channel ran out of history before lookback exhausted.
            logger.info(
                "discord_backfill: empty page at index %d — channel "
                "history exhausted",
                page_index,
            )
            break

        # Cursor for the NEXT page = the page's actual last-by-position
        # message id. Discord returns messages newest-first, so
        # ``page[-1]`` IS the oldest in the page regardless of whether
        # any individual message parses cleanly. Driving the cursor off
        # ``page[-1]["id"]`` (instead of off the oldest *parseable*
        # timestamp inside the loop) avoids the bug where a malformed
        # OLDEST message would let the cursor advance to a younger
        # message id — causing the next page to re-fetch already-
        # consumed messages, inflate ``messages_scanned``, and risk
        # an infinite same-cursor loop if the same malformed id keeps
        # appearing at position [-1]. (Spec C commit #7 codex review
        # IMPORTANT 1.) The id is read defensively — Discord ALWAYS
        # ships a non-null ``id`` on a message envelope, but if a
        # future API change ever omits it we want to log + bail rather
        # than crash.
        next_cursor: str | None
        try:
            next_cursor = str(page[-1]["id"])
        except (KeyError, TypeError) as exc:
            summary.errors.append(
                f"malformed_page_tail: {type(exc).__name__}: {exc}"
            )
            logger.warning(
                "discord_backfill: page %d tail has no id — stopping",
                page_index,
            )
            break

        oldest_in_page_ts: datetime | None = None
        for msg in page:
            summary.messages_scanned += 1
            try:
                msg_id = str(msg["id"])
                msg_ts = _parse_discord_ts(msg["timestamp"])
                msg_content = str(msg.get("content", ""))
                msg_attachments_raw = msg.get("attachments")
            except (KeyError, ValueError, TypeError) as exc:
                summary.errors.append(
                    f"malformed_message: {type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "discord_backfill: malformed message in page %d: %s",
                    page_index, exc,
                )
                continue

            # Track the page's oldest PARSEABLE timestamp for the
            # lookback-exhausted check below. The CURSOR for the next
            # page comes from ``page[-1]["id"]`` (set above) — NOT
            # from this min-timestamp scan — so a malformed oldest
            # message can't corrupt the cursor advance.
            if (
                oldest_in_page_ts is None
                or msg_ts < oldest_in_page_ts
            ):
                oldest_in_page_ts = msg_ts

            # Skip messages already older than our window — they're
            # in the page only because the cursor walks in chunks of
            # 100. We don't break here because the page is ordered
            # newest-first; later messages in the SAME page may also
            # be older but we still let the loop fall through to the
            # post-page lookback-exhausted check.
            if msg_ts < cutoff:
                continue

            # Fetch text attachments (alpha-report channel posts daily
            # reports as ``.txt`` uploads with a caption). Combined
            # text = caption first, then attachment body — caption-
            # first so a user-supplied prefix wins alpha-call regex
            # precedence. ``fetch_text_attachments`` never raises on
            # operational errors; HTTP failures log + skip that one
            # attachment.
            attachments = parse_attachments(msg_attachments_raw)
            if attachments:
                attachment_text = await fetch_text_attachments(
                    attachments,
                    http_client=attachment_client,
                    max_bytes=MAX_ATTACHMENT_BYTES,
                )
            else:
                attachment_text = ""
            effective_text = (
                f"{msg_content}\n\n{attachment_text}"
                if attachment_text
                else msg_content
            )

            # Long-form alpha-report skip — defer to the
            # ``alpha_report_analyst`` cron for posts that the regex
            # parser would mis-handle (multi-page commentary produces
            # one false-positive Prediction per first-matching pattern
            # in the prose). The NewsSignal row was already INSERTed
            # by an upstream ingest path; the analyst cron picks up
            # signals without an ``alpha_report_analyses`` row.
            if _is_long_form_alpha_report(effective_text):
                logger.debug(
                    "discord_backfill: skipping regex parser, long-form "
                    "report; analyst will handle (msg_id=%s, len=%d, "
                    "newlines=%d)",
                    msg_id,
                    len(effective_text or ""),
                    (effective_text or "").count("\n"),
                )
                summary.messages_long_form_skipped += 1
                continue

            call = extract_alpha_call_from_text(effective_text)
            if call is None:
                summary.messages_unparseable += 1
                continue

            # Write the prediction. The writer's idempotency contract
            # collapses re-runs into the same row; we observe whether
            # the row's ``id`` was already present pre-call by
            # checking row identity — the writer returns the EXISTING
            # row's instance unchanged on dedup hits, so a fresh
            # `is_dedup` check uses the SQLAlchemy attribute
            # ``_sa_instance_state.persistent`` semantics. Simpler:
            # snapshot the row count before/after and infer.
            try:
                pre_id = _peek_existing_id(
                    session,
                    message_id=msg_id,
                    channel_id=final_channel_id,
                )
                _ = write_discord_prediction(
                    session,
                    user_id,
                    message_id=msg_id,
                    channel_id=final_channel_id,
                    ticker=call.ticker,
                    direction=call.direction,
                    target_price=call.target_price,
                    stop_price=call.stop_price,
                    event_at=msg_ts,
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                summary.errors.append(
                    f"write_failed: msg_id={msg_id} "
                    f"{type(exc).__name__}: {exc}"
                )
                logger.exception(
                    "discord_backfill: write failed for message %s",
                    msg_id,
                )
                continue

            if pre_id is not None:
                summary.predictions_deduped += 1
            else:
                summary.predictions_written += 1

        # Pagination terminator: if the OLDEST PARSEABLE message in
        # this page is older than the lookback cutoff, the next page
        # would be entirely beyond our window — stop. (We still wrote
        # any in-window messages in this page above.)
        if oldest_in_page_ts is None:
            # Page had only malformed messages — bail to avoid an
            # infinite walk on a page that yields no usable timestamps.
            # The id-based cursor would still advance (we set it from
            # ``page[-1]["id"]`` above), but without any parseable
            # timestamp we can't bound the lookback so we stop
            # defensively rather than potentially walk all-history.
            logger.warning(
                "discord_backfill: page %d had no parseable messages "
                "— stopping",
                page_index,
            )
            break
        if oldest_in_page_ts < cutoff:
            logger.info(
                "discord_backfill: lookback exhausted at page %d "
                "(oldest=%s, cutoff=%s)",
                page_index, oldest_in_page_ts.isoformat(),
                cutoff.isoformat(),
            )
            break
        # Set up the cursor for the next page — from ``page[-1]["id"]``
        # captured BEFORE the per-message loop (Spec C commit #7 codex
        # review IMPORTANT 1).
        before_cursor = next_cursor
    else:
        # Hit MAX_PAGES without exiting via break.
        summary.errors.append(
            f"max_pages_reached: stopped after {MAX_PAGES} pages"
        )
        logger.warning(
            "discord_backfill: MAX_PAGES (%d) reached", MAX_PAGES
        )

    if own_attachment_client:
        await attachment_client.aclose()

    logger.info(
        "discord_backfill: done — scanned=%d written=%d deduped=%d "
        "unparseable=%d pages=%d errors=%d",
        summary.messages_scanned,
        summary.predictions_written,
        summary.predictions_deduped,
        summary.messages_unparseable,
        summary.pages_fetched,
        len(summary.errors),
    )
    return summary


def _peek_existing_id(
    session: Session,
    *,
    message_id: str,
    channel_id: int | str | None,
) -> int | None:
    """Return the existing predictions.id for this discord (channel,
    message_id) pair, or ``None`` if not yet written.

    Used by :func:`backfill_discord_predictions` to distinguish a NEW
    write from a dedup-hit when populating the ``BackfillSummary``
    counters. We consult the writer's public
    :func:`discord_message_id` helper (rather than inlining the
    formula) so the dedup-key shape has a single source of truth — a
    future ``v2|...`` version of the per-source key only needs to
    update one site (Spec C commit #7 codex review IMPORTANT 2).
    """
    from sqlalchemy import select

    from argosy.state.models import Prediction

    dedup_key = discord_message_id(
        channel_id=channel_id, message_id=message_id
    )
    stmt = select(Prediction.id).where(
        Prediction.source == "discord",
        Prediction.message_id == dedup_key,
    )
    return session.execute(stmt).scalar_one_or_none()


__all__ = [
    "BackfillSummary",
    "DEFAULT_USER_ID",
    "MAX_PAGES",
    "PAGE_LIMIT",
    "backfill_discord_predictions",
]
