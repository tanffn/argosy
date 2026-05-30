"""Discord attachment fetcher — shared helper for listener + backfill.

The alpha-report Discord channel posts its daily report as a ``.txt``
attachment with a short caption ("Today's report"); both the live
gateway listener (:mod:`argosy.services.discord_listener`) and the
14-day REST backfill (:mod:`argosy.services.predictions.discord_backfill`)
need to download these attachments, decode them, and feed the combined
text (caption + attachment body) into the news extractor and alpha-call
parser.

Single source of truth so the two ingest paths cannot drift on:

* The size cap (``_MAX_ATTACHMENT_BYTES``, default 1 MiB).
* The MIME / extension whitelist (text/* OR ``.txt`` / ``.md`` / ``.csv``).
* Decoder fallback chain (UTF-8 → latin-1; latin-1 always decodes any
  byte sequence so the chain terminates).
* Per-attachment HTTP timeout (~10 s; the CDN normally answers in
  well under a second; if a single attachment takes longer than 10 s
  we'd rather skip it than stall the ingest loop).

Discord CDN URL handling
------------------------

The ``url`` field on an attachment dict is a Discord-CDN signed URL —
the signature lives in the query string (``?ex=...&is=...&hm=...``).
NO ``Authorization: Bot ...`` header is needed; sending one is harmless
but unnecessary, and we deliberately do NOT forward the bot token to
the CDN host (defense in depth — the CDN isn't on the same trust
boundary as the API host).

Codex single-dispatch review focus
----------------------------------

* Max-size guard: enforced via the ``Content-Length`` response header
  BEFORE streaming the body when present; if the header is missing,
  the body is read in chunks and cut at ``max_bytes`` to avoid an
  unbounded download.
* Decoder fallback chain: UTF-8 first (the Discord docs say uploaded
  text is UTF-8 by default; bots routinely upload from non-UTF-8
  Windows hosts producing CP-1252 / latin-1 bytes). latin-1 always
  decodes; chain terminates there.
* Timeout: 10 s per attachment.
* Caption + attachment ordering: caller appends attachment text AFTER
  the caption so user-supplied prefix wins for alpha-call regex
  precedence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Maximum number of bytes we'll download from a single attachment.
#: 1 MiB is generous for a daily-report .txt (typical: ~10-50 KB); any
#: file larger than this is almost certainly a binary or a misuse, and
#: ingesting megabytes of text per message would balloon the
#: ``news_signals.raw_text`` column. Configurable via the
#: ``max_bytes`` kwarg to :func:`fetch_text_attachments`.
MAX_ATTACHMENT_BYTES: int = 1_048_576

#: Per-attachment HTTP timeout, in seconds. Discord's CDN normally
#: answers in <500 ms; a 10 s ceiling means a slow CDN can't stall an
#: entire ingest cycle but a transient stall still completes.
REQUEST_TIMEOUT_S: float = 10.0

#: Filename extensions that we treat as text even when the
#: ``content_type`` field is missing or generic (Discord sometimes
#: serves ``application/octet-stream`` for ``.txt`` uploads).
_TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".csv"})

#: Pre-compiled regex for the extension-based fallback. Case-insensitive
#: so ``.TXT`` and ``.txt`` both match.
_TEXT_EXT_RE = re.compile(
    r"\.(txt|md|csv)$", re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    """One file attachment on a Discord message.

    Mirrors the subset of fields Discord puts on
    ``payload['d']['attachments'][i]`` (gateway dispatch) and
    ``message['attachments'][i]`` (REST API) that we care about. Both
    transports use the same shape, so this dataclass is the lingua
    franca between the listener and the backfill.

    Attributes
    ----------
    id
        Discord snowflake (string-shaped int).
    filename
        Original upload filename (e.g. ``Alpha Report 5-29-2026.txt``).
        Used as the extension fallback when ``content_type`` is missing
        or generic.
    content_type
        MIME type as reported by Discord (e.g. ``text/plain``). May be
        an empty string if the API omits it.
    size
        Reported file size in bytes. We still check ``Content-Length``
        on the HTTP response (Discord could lie or the CDN could
        re-pack); this field is informational + supports a cheap
        pre-fetch guard.
    url
        Discord-CDN signed URL. The signature is in the query string;
        no ``Authorization`` header is required.
    """

    id: str
    filename: str
    content_type: str
    size: int
    url: str


# ---------------------------------------------------------------------------
# Parsing helpers (gateway / REST → Attachment)
# ---------------------------------------------------------------------------


def parse_attachments(raw: object) -> list[Attachment]:
    """Convert the raw ``attachments`` JSON list to typed ``Attachment``s.

    Defensive on every field — Discord's response shape is documented
    but a future API change could omit fields. Missing / non-list →
    returns ``[]``. Per-attachment parse errors are logged at DEBUG and
    that attachment is skipped (we still process the rest).

    Args:
        raw: The ``attachments`` field from a Discord message JSON
            object. Expected to be ``list[dict]``; may be ``None`` or
            absent on messages with no attachments.

    Returns:
        List of typed attachments. Empty if no parseable entries.
    """
    if not isinstance(raw, list):
        return []
    out: list[Attachment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                Attachment(
                    id=str(entry["id"]),
                    filename=str(entry.get("filename", "")),
                    content_type=str(entry.get("content_type", "") or ""),
                    size=int(entry.get("size", 0) or 0),
                    url=str(entry["url"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(
                "discord_attachment_fetcher: skipping malformed "
                "attachment entry: %s",
                exc,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Classification: is this a text attachment we should fetch?
# ---------------------------------------------------------------------------


def is_text_attachment(att: Attachment) -> bool:
    """Decide whether an attachment is text we should download.

    The Discord-published ``content_type`` is the primary signal; we
    fall back to the filename extension because some clients
    (especially mobile + bots uploading raw bytes via the REST API)
    don't set a ``content_type`` and Discord then defaults to
    ``application/octet-stream`` even for ``.txt`` files.

    Returns ``True`` iff:

    * ``content_type`` starts with ``text/``, OR
    * ``filename`` ends with one of ``.txt`` / ``.md`` / ``.csv``
      (case-insensitive).
    """
    ct = (att.content_type or "").lower()
    if ct.startswith("text/"):
        return True
    if _TEXT_EXT_RE.search(att.filename or ""):
        return True
    return False


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def _decode_body(body: bytes) -> str:
    """Decode a downloaded attachment body to ``str``.

    Tries UTF-8 first (the documented Discord default + what most
    well-behaved clients upload). Falls back to latin-1 on
    ``UnicodeDecodeError`` — latin-1 maps every byte 0x00-0xFF to a
    single Unicode codepoint so it ALWAYS succeeds; this guarantees the
    chain terminates without raising. We accept that some accented
    Windows-source bytes may render slightly off in the latin-1 path
    rather than dropping the attachment entirely; the news extractor's
    Stage-1 regexes are ASCII-tolerant and the user-visible
    ``evidence_excerpt`` is a 280-char window so the cost of a wrong
    codepoint is bounded.
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        logger.info(
            "discord_attachment_fetcher: UTF-8 decode failed; "
            "falling back to latin-1 (probably a Windows-source .txt)",
        )
        return body.decode("latin-1")


# ---------------------------------------------------------------------------
# HTTP fetch (one attachment)
# ---------------------------------------------------------------------------


async def _fetch_one(
    att: Attachment,
    *,
    http_client: httpx.AsyncClient,
    max_bytes: int,
) -> str | None:
    """Download one attachment's body to text. ``None`` on any failure.

    Implements the size guard in two layers:

    1. PRE-FETCH: if the response's ``Content-Length`` header is
       present and exceeds ``max_bytes``, abandon the download
       immediately (don't read the body).
    2. STREAMING CUT: if the header is missing (some CDN paths omit
       it), stream the body and stop reading once ``max_bytes`` have
       been received; the partial body is discarded — we don't want a
       half-truncated text file to confuse the parser.

    Errors are logged at WARNING but NEVER raise — ingest must
    continue across other attachments + messages even if one CDN URL
    has expired.

    Auth scrub: we explicitly send ``Authorization: ""`` and
    ``Cookie: ""`` on every request. The Discord CDN signature lives
    in the URL query string; the bot token has NO business reaching
    the CDN host. Codex review BLOCKER on this commit — if a future
    caller reuses an ``httpx.AsyncClient`` configured with default
    ``Authorization`` headers for the Discord REST API, those headers
    would otherwise be forwarded to the CDN. Per-request override
    nulls out that risk.
    """
    # Per-request header scrub — even if the injected client has a
    # default ``Authorization`` set (Discord-REST shared client, say),
    # we override to empty for the CDN call. ``User-Agent`` is set to
    # a stable Argosy identifier so CDN ops can identify us in their
    # logs without revealing internals.
    cdn_headers = {
        "Authorization": "",
        "Cookie": "",
        "User-Agent": "Argosy/1.0 (discord-attachment-fetcher)",
    }
    try:
        async with http_client.stream(
            "GET", att.url, timeout=REQUEST_TIMEOUT_S,
            headers=cdn_headers,
        ) as resp:
            if resp.status_code >= 400:
                logger.warning(
                    "discord_attachment_fetcher: HTTP %d fetching "
                    "attachment id=%s filename=%s — skipping",
                    resp.status_code, att.id, att.filename,
                )
                return None

            # Pre-fetch size guard via Content-Length.
            cl_header = resp.headers.get("Content-Length")
            if cl_header is not None:
                try:
                    cl = int(cl_header)
                except (TypeError, ValueError):
                    cl = -1
                if cl > max_bytes:
                    logger.warning(
                        "discord_attachment_fetcher: attachment id=%s "
                        "filename=%s exceeds max_bytes "
                        "(Content-Length=%d > %d) — skipping",
                        att.id, att.filename, cl, max_bytes,
                    )
                    return None

            # Stream body with an in-flight size cut. The +1 lets us
            # detect the over-limit case even when Content-Length was
            # absent: we read one extra byte beyond max_bytes, and if
            # we got it, the file is over the cap.
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    logger.warning(
                        "discord_attachment_fetcher: attachment id=%s "
                        "filename=%s exceeds max_bytes during stream "
                        "(received %d > %d) — skipping",
                        att.id, att.filename, len(buf), max_bytes,
                    )
                    return None
            body = bytes(buf)
    except httpx.TimeoutException as exc:
        logger.warning(
            "discord_attachment_fetcher: timeout fetching "
            "attachment id=%s filename=%s: %s — skipping",
            att.id, att.filename, exc,
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "discord_attachment_fetcher: transport error fetching "
            "attachment id=%s filename=%s: %s — skipping",
            att.id, att.filename, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never break ingest
        logger.warning(
            "discord_attachment_fetcher: unexpected error fetching "
            "attachment id=%s filename=%s: %s: %s — skipping",
            att.id, att.filename, type(exc).__name__, exc,
        )
        return None

    return _decode_body(body)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def fetch_text_attachments(
    attachments: list[Attachment],
    *,
    http_client: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> str:
    """Download + concatenate every text attachment as a single string.

    Iterates ``attachments`` in order; for each entry that
    :func:`is_text_attachment` accepts, opens an HTTPS GET against the
    CDN URL (no ``Authorization`` header — signature is in the query
    string), enforces ``max_bytes``, decodes UTF-8 (latin-1 fallback),
    and appends the result to the returned string with a ``\\n\\n``
    separator between attachments.

    Args:
        attachments: List of typed ``Attachment``s. Empty list → return
            empty string immediately (no HTTP client opened).
        http_client: Optional injected ``httpx.AsyncClient`` — tests
            pass a MockTransport-backed client to avoid real CDN hits.
            Default opens a transient client for the duration of the
            call.
        max_bytes: Per-attachment size cap. Default 1 MiB.

    Returns:
        Joined text of all successfully-fetched text attachments;
        empty string if the input list is empty OR no attachment was
        text OR all fetches failed.

    Never raises on operational errors (HTTP failures, timeouts,
    decode chain termination). Programmer errors (a ``None`` element
    in ``attachments``) WILL raise.
    """
    if not attachments:
        return ""

    text_atts = [a for a in attachments if is_text_attachment(a)]
    if not text_atts:
        return ""

    own_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        pieces: list[str] = []
        for att in text_atts:
            decoded = await _fetch_one(
                att, http_client=client, max_bytes=max_bytes,
            )
            if decoded is not None:
                pieces.append(decoded)
        return "\n\n".join(pieces)
    finally:
        if own_client:
            await client.aclose()


__all__ = [
    "Attachment",
    "MAX_ATTACHMENT_BYTES",
    "REQUEST_TIMEOUT_S",
    "fetch_text_attachments",
    "is_text_attachment",
    "parse_attachments",
]
