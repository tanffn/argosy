"""RSS feed fetcher for per-ticker news (Stage 1 source adapter).

Sprint commit #13 of the plan/execute/monitor reorg. Fetches a free
RSS feed (Yahoo Finance) for each configured ticker and returns
``ExtractedSignal`` rows ready for persistence.

Library choice: ``feedparser`` is NOT in the project's ``pyproject.toml``
(checked at commit-time — only ``yfinance`` / ``finnhub-python`` /
``fredapi`` are pulled for market data, none of which expose a generic
feed reader for arbitrary RSS endpoints). We therefore use the stdlib —
``urllib.request`` for HTTP plus ``xml.etree.ElementTree`` for the RSS
2.0 / Atom XML parse. Yahoo Finance's feed is plain RSS 2.0 (``rss >
channel > item > title|link|description|pubDate``) which is trivial
to parse without a dependency.

Cadence: spec §5.2 calls for a 4-hour poll; the scheduler binding
lands in commit #15 (cron / cadence_state wiring). This module exposes
``poll_rss_for_tickers`` for the orchestrator; it does not schedule
itself.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from argosy.services.news_extractor import ExtractedSignal, extract

logger = logging.getLogger(__name__)


# Yahoo Finance per-ticker headline RSS feed. The free, no-key endpoint
# the daily-automation spec calls for in §5.2. Per-ticker poll keeps
# the per-call payload small (~20 items / ticker) so the 4-hour
# cadence stays under any reasonable rate limit.
YAHOO_RSS_TEMPLATE = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
)

# Cap items returned per single ``poll_rss_for_tickers`` call. The
# extractor is cheap but a long list of stale headlines would still
# bloat the news_signals table. 50 items is ~10 minutes of human-read
# triage if every item flagged — well above the realistic per-poll
# volume even for a basket of active tickers.
_MAX_ITEMS_PER_CALL = 50

# Per-request HTTP timeout. Yahoo's feed is usually <1s; 10s leaves
# headroom for network jitter but won't stall the orchestrator if the
# feed is degraded.
_HTTP_TIMEOUT_SECONDS = 10


def poll_rss_for_tickers(
    tickers: list[str],
    *,
    fetch: object = None,
    now: datetime | None = None,
) -> list[ExtractedSignal]:
    """Poll the Yahoo Finance RSS feed for each ticker; extract each item.

    Args:
        tickers: Ticker symbols (canonical uppercase). One HTTP request
            per ticker — caller is responsible for sanity-bounding the
            list.
        fetch: Optional injected fetcher with signature
            ``(url: str) -> bytes``. Used by tests to mock HTTP without
            ``responses`` (not a project dep). Defaults to the stdlib
            ``urllib`` fetcher.
        now: Optional override for "now" used as the fallback
            ``received_at`` when an RSS item lacks a parseable
            ``pubDate``. Defaults to ``datetime.now(timezone.utc)``.

    Returns:
        Up to ``_MAX_ITEMS_PER_CALL`` ``ExtractedSignal`` rows across
        all tickers, in the order they were fetched. Items with empty
        title+description are dropped (no evidence to cite).
    """
    fetcher = fetch if fetch is not None else _default_fetch
    now_dt = now if now is not None else datetime.now(UTC)
    out: list[ExtractedSignal] = []

    for ticker in tickers:
        if len(out) >= _MAX_ITEMS_PER_CALL:
            break
        url = YAHOO_RSS_TEMPLATE.format(ticker=ticker)
        try:
            payload = fetcher(url)  # type: ignore[operator]
        except (urllib.error.URLError, TimeoutError) as exc:
            # Don't fail the whole poll because one ticker's feed is
            # transiently unreachable. Log + skip.
            logger.warning("rss_feed fetch failed for %s: %s", ticker, exc)
            continue
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("rss_feed unexpected error for %s: %s", ticker, exc)
            continue

        items = _parse_rss(payload)
        for item in items:
            if len(out) >= _MAX_ITEMS_PER_CALL:
                break
            raw_text = _compose_raw_text(item)
            if not raw_text.strip():
                continue
            received_at = _resolve_received_at(item.get("pubDate"), now_dt)
            source_ref = item.get("link") or item.get("guid") or ""
            if not source_ref:
                # Without a stable ref the dedup index can't function;
                # synthesize one from ticker + pubdate + title hash.
                source_ref = f"{ticker}|{received_at.isoformat()}|{item.get('title', '')[:64]}"
            signal = extract(
                source="rss",
                source_ref=source_ref,
                raw_text=raw_text,
                received_at=received_at,
            )
            out.append(signal)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_fetch(url: str) -> bytes:
    """Stdlib HTTP GET. User-Agent identifies Argosy so Yahoo's
    side can rate-limit us specifically rather than blocking all
    Python-stdlib UAs."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "argosy-news-ingest/0.1 (+https://argosy.local)"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _parse_rss(payload: bytes) -> list[dict[str, str]]:
    """Parse an RSS 2.0 payload into a list of ``{title, link, description,
    pubDate, guid}`` dicts. Returns ``[]`` on malformed XML (logged)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        logger.warning("rss_feed parse error: %s", exc)
        return []

    items: list[dict[str, str]] = []
    # RSS 2.0: <rss><channel><item>... — channel may be the root in
    # some malformed feeds, so search broadly.
    for item in root.iter("item"):
        rec: dict[str, str] = {}
        for child in item:
            tag = child.tag.split("}", 1)[-1]  # strip namespace if any
            if tag in {"title", "link", "description", "pubDate", "guid"}:
                rec[tag] = (child.text or "").strip()
        items.append(rec)
    return items


def _compose_raw_text(item: dict[str, str]) -> str:
    """Concatenate title + description for downstream extraction. The
    extractor handles whitespace cleanup so we just join with a space.
    """
    parts = [item.get("title", ""), item.get("description", "")]
    return " ".join(p for p in parts if p)


def _resolve_received_at(pub_date_raw: str | None, fallback: datetime) -> datetime:
    """Parse an RFC-2822 ``pubDate`` into a timezone-aware UTC datetime.

    Yahoo's feed uses standard ``pubDate`` strings like
    ``Mon, 27 May 2026 14:32:00 GMT``. ``parsedate_to_datetime`` is the
    stdlib's canonical parser. Returns ``fallback`` when missing or
    unparseable.
    """
    if not pub_date_raw:
        return fallback
    try:
        dt = parsedate_to_datetime(pub_date_raw)
    except (TypeError, ValueError):
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
