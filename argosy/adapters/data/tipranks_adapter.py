"""TipRanks adapter (Phase 4).

Source: ``https://www.tipranks.com/stocks/<TICKER>/forecast`` and the
sibling pages ``.../blogger-opinions`` and ``.../hedge-funds-activity``.

TipRanks aggregates analyst ratings, blogger sentiment, and hedge-fund
13F-derived signals for individual stocks. Their *free* tier limits
unauthenticated traffic to ~10 lookups per day per IP. We MUST not fan
out aggressive parallel calls; tests therefore exercise pure parsing,
not bulk request loops.

Methods:

  - ``get_analyst_consensus(ticker)`` — (consensus_label, average_price_target,
    num_buy, num_hold, num_sell, last_updated).
  - ``get_blogger_sentiment(ticker)`` — bullish_pct + bearish_pct.
  - ``get_hedge_fund_signal(ticker)`` — hedge_funds_holding +
    recent_change.

24h cache; we never re-hit within a day. On any unparsable response we
raise ``MissingDataSourceError`` rather than returning partial data
(the LLM downstream is better off with an empty section + warning than
with hallucinated numbers).

Test injection:

  - ``http_client=fake`` exposing ``async get(url, *, headers=None) ->
    Response``-shaped object.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking."""
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0

_log = get_logger("argosy.adapters.tipranks")


TIPRANKS_BASE = "https://www.tipranks.com"
FORECAST_URL_TPL = f"{TIPRANKS_BASE}/stocks/{{ticker}}/forecast"
BLOGGER_URL_TPL = f"{TIPRANKS_BASE}/stocks/{{ticker}}/blogger-opinions"
HEDGEFUND_URL_TPL = f"{TIPRANKS_BASE}/stocks/{{ticker}}/hedge-funds-activity"

DEFAULT_TIMEOUT = 15.0
DEFAULT_TTL_SECONDS = 60 * 60 * 24       # 24h


def _user_agent() -> str:
    from argosy import __version__

    return (
        f"Argosy/{__version__} "
        "(https://github.com/anthropics/claude-code; "
        "analyst sentiment fetcher)"
    )


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _user_agent(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }


class TipRanksAdapter:
    """Analyst-sentiment aggregator over tipranks.com.

    Args:
        http_client: object exposing ``async get(url, *, headers=None)``.
            Defaults to ``httpx.AsyncClient``.
        timeout_seconds: per-request timeout.
    """

    PROVIDER = "tipranks"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        finnhub: Any | None = None,
    ) -> None:
        self._http = http_client
        self._timeout = timeout_seconds
        # T3.2: optional Finnhub adapter used as the social-sentiment
        # fallback when TipRanks 403s (anti-bot) or otherwise fails.
        # Tracked under its own ``finnhub_social`` adapter-outcome row
        # so the agent tree surfaces BOTH outcomes (TipRanks failed AND
        # Finnhub succeeded / also failed) rather than a single
        # opaque "tipranks: http_error" leaf.
        self._finnhub = finnhub

    # ----- public API -------------------------------------------------

    async def get_analyst_consensus(
        self,
        ticker: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Fetch and parse the analyst forecast page.

        Returns a dict with ``ticker``, ``consensus_label``,
        ``average_price_target``, ``num_buy``, ``num_hold``,
        ``num_sell``, ``last_updated``, ``source_url``.

        Raises:
            ValueError: if ``ticker`` is empty.
            MissingDataSourceError: on outage / unparsable response.
        """
        if not ticker:
            raise ValueError("ticker is required")
        ticker_norm = ticker.strip().upper()
        url = FORECAST_URL_TPL.format(ticker=ticker_norm)

        async def _fetch() -> dict[str, Any]:
            text = await self._fetch_text(url)
            payload = _parse_analyst_consensus(text)
            payload["ticker"] = ticker_norm
            payload["source_url"] = url
            return payload

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"consensus:{ticker_norm}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    async def get_blogger_sentiment(
        self,
        ticker: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Bullish/bearish blogger sentiment for ``ticker``.

        T3.2: TipRanks's blogger-opinions page now reliably blocks
        unauthenticated scrapers (HTTP 403 anti-bot). When that
        happens — or any other HTTP / network failure — we fall back to
        ``FinnhubAdapter.get_social_sentiment`` if a Finnhub adapter was
        injected at construction time. Both attempts are tracked as
        separate adapter outcomes so the agent tree shows the user
        BOTH failures / both providers' contributions, not just an
        opaque "tipranks: http_error" leaf.

        On both-providers-fail (or no Finnhub injected and TipRanks
        failed), returns the zero-shape default
        ``{"ticker": <T>, "bullish_pct": 0.0, "bearish_pct": 0.0,
        "source_url": ""}`` rather than raising — the caller
        (SentimentAnalystAgent via ``_gather_social_payload``) already
        treats a zero/empty signal as "no usable sentiment". This
        behaviour change vs the prior raise-on-failure path is the
        whole point of T3.2: don't crash the synthesis run when the
        sentiment provider is down.
        """
        if not ticker:
            raise ValueError("ticker is required")
        ticker_norm = ticker.strip().upper()

        # 1. Try TipRanks first, tracked under "tipranks".
        tipranks_payload = await self._try_blogger_sentiment_via_tipranks(
            ticker_norm, ttl_seconds=ttl_seconds,
        )
        if tipranks_payload is not None:
            return tipranks_payload

        # 2. TipRanks failed. Try Finnhub social-sentiment as a fallback,
        #    tracked separately under "finnhub_social" (the call itself
        #    wraps track_adapter_call("finnhub_social", target=...)).
        if self._finnhub is not None:
            try:
                fallback = await self._finnhub.get_social_sentiment(ticker_norm)
                if isinstance(fallback, dict):
                    # Normalize to the same dict shape callers expect from
                    # the TipRanks happy-path return.
                    return {
                        "ticker": ticker_norm,
                        "bullish_pct": float(fallback.get("bullish_pct") or 0.0),
                        "bearish_pct": float(fallback.get("bearish_pct") or 0.0),
                        "source_url": fallback.get("source_url", ""),
                    }
            except Exception as exc:  # noqa: BLE001 — defensive
                # Finnhub's own track_adapter_call already recorded the
                # outcome; we just need to swallow so we can return the
                # zero-shape default below.
                _log.info(
                    "tipranks.finnhub_fallback_failed",
                    ticker=ticker_norm,
                    reason=str(exc).splitlines()[0],
                )

        # 3. Both providers failed (or no Finnhub injected). Return the
        #    zero-shape default rather than raise. Don't crash synthesis.
        return {
            "ticker": ticker_norm,
            "bullish_pct": 0.0,
            "bearish_pct": 0.0,
            "source_url": "",
        }

    async def _try_blogger_sentiment_via_tipranks(
        self,
        ticker_norm: str,
        *,
        ttl_seconds: int,
    ) -> dict[str, Any] | None:
        """Inner helper — returns the parsed payload or None on failure.

        Wraps the TipRanks HTTP + parse cycle in
        ``track_adapter_call("tipranks", ...)`` and records ``http_error``
        with the actual status code when the HTTP layer returns non-200,
        or ``exception`` when the network call itself failed. Returning
        ``None`` instead of raising lets ``get_blogger_sentiment`` decide
        whether to fall back to Finnhub — the outcome is already
        recorded so the agent tree sees the failure either way.
        """
        url = BLOGGER_URL_TPL.format(ticker=ticker_norm)

        with track_adapter_call("tipranks", target=ticker_norm) as _outcome:
            try:
                async def _fetch() -> dict[str, Any]:
                    text, _status = await self._fetch_text_with_status(url)
                    payload = _parse_blogger_sentiment(text)
                    payload["ticker"] = ticker_norm
                    payload["source_url"] = url
                    return payload

                payload = await cached_call(
                    kind=CacheKind.PRICES,
                    provider=self.PROVIDER,
                    key=f"blogger:{ticker_norm}",
                    ttl_seconds=ttl_seconds,
                    fetch=_fetch,
                )
            except _TipRanksHTTPError as exc:
                _outcome.record_http_error(
                    status_code=exc.status_code,
                    body=exc.body or f"HTTP {exc.status_code}",
                )
                return None
            except MissingDataSourceError as exc:
                # Network/parse failure without a status code (DNS, parse
                # error, etc.). Outcome falls through as "exception" via
                # the contextmanager's exception handling — but we
                # swallow here to enable the fallback path.
                _outcome.record_exception(exc)
                return None
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def get_hedge_fund_signal(
        self,
        ticker: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Hedge-fund 13F-derived signal for ``ticker``.

        Returns ``{"ticker", "hedge_funds_holding", "recent_change",
        "source_url"}``. ``recent_change`` is one of
        ``increased`` / ``decreased`` / ``unchanged`` / ``unknown``.
        """
        if not ticker:
            raise ValueError("ticker is required")
        ticker_norm = ticker.strip().upper()
        url = HEDGEFUND_URL_TPL.format(ticker=ticker_norm)

        async def _fetch() -> dict[str, Any]:
            text = await self._fetch_text(url)
            payload = _parse_hedge_fund_signal(text)
            payload["ticker"] = ticker_norm
            payload["source_url"] = url
            return payload

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"hedge:{ticker_norm}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    # ----- internals --------------------------------------------------

    async def _fetch_text(self, url: str) -> str:
        text, _status = await self._fetch_text_with_status(url)
        return text

    async def _fetch_text_with_status(self, url: str) -> tuple[str, int]:
        """Fetch ``url`` and return ``(text, status_code)``.

        On a non-200 response, raises ``_TipRanksHTTPError`` carrying
        the status code so callers can record an accurate
        ``adapter_outcomes`` row (HTTP 403 anti-bot vs HTTP 500 upstream).
        Other failures (DNS, connect, parse) still raise
        ``MissingDataSourceError`` to preserve the existing contract for
        the analyst-consensus + hedge-fund-signal call sites that haven't
        been migrated to status-aware tracking yet.
        """
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url)
            else:
                resp = await self._http.get(url, headers=_default_headers())
        except Exception as exc:
            _log.warning("tipranks.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"tipranks unreachable ({exc!s}); url={url}"
            ) from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            body_preview = None
            text_attr = getattr(resp, "text", None)
            if isinstance(text_attr, str):
                body_preview = text_attr[:500]
            raise _TipRanksHTTPError(
                f"tipranks returned HTTP {status} for {url}",
                status_code=status,
                body=body_preview,
            )
        text = getattr(resp, "text", None)
        if text is None:
            raw = getattr(resp, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return text, status


class _TipRanksHTTPError(MissingDataSourceError):
    """Internal HTTP-error type carrying the status code + body preview.

    Subclasses ``MissingDataSourceError`` so existing callers that catch
    that exception keep working unchanged; the extra fields are read by
    ``_try_blogger_sentiment_via_tipranks`` to populate
    ``adapter_outcomes`` with the actual status.
    """

    def __init__(
        self, message: str, *, status_code: int, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ----------------------------------------------------------------------
# HTML parsing — module-level for direct test exercise
# ----------------------------------------------------------------------


def _parse_analyst_consensus(html_text: str) -> dict[str, Any]:
    """Parse the forecast page → analyst-consensus dict.

    TipRanks renders most data twice: as visible HTML and as a
    JSON ``__NEXT_DATA__`` script blob. We prefer the JSON blob (more
    stable) and fall back to text extraction.
    """
    data = _extract_next_data(html_text)
    if data is not None:
        block = _walk_for_keys(data, ("consensuses", "priceTargets"))
        if block:
            return _consensus_from_next_data(block, raw=data)

    # Fallback: regex over visible HTML.
    consensus_label = _first_match(
        html_text,
        r'(?:Analyst Consensus|consensus)\s*[:>]?\s*(?:is\s*)?["“]?(Strong Buy|Moderate Buy|Hold|Moderate Sell|Strong Sell)',
    )
    avg_pt = _first_match_number(
        html_text,
        r'Average Price Target[^$0-9]{0,40}\$?\s*([0-9][0-9,.]*)',
    )
    num_buy = _first_match_int(html_text, r'([0-9]+)\s*Buy')
    num_hold = _first_match_int(html_text, r'([0-9]+)\s*Hold')
    num_sell = _first_match_int(html_text, r'([0-9]+)\s*Sell')

    if consensus_label is None and avg_pt is None and num_buy is None:
        raise MissingDataSourceError(
            "tipranks: could not parse analyst consensus from forecast page; "
            "the page layout may have changed."
        )
    return {
        "consensus_label": consensus_label or "",
        "average_price_target": avg_pt,
        "num_buy": num_buy or 0,
        "num_hold": num_hold or 0,
        "num_sell": num_sell or 0,
        "last_updated": "",
    }


def _consensus_from_next_data(
    block: dict[str, Any], *, raw: dict[str, Any]
) -> dict[str, Any]:
    """Map a Next.js ``__NEXT_DATA__`` consensus block to our schema."""
    consensus_label = ""
    average_pt: float | None = None
    num_buy = 0
    num_hold = 0
    num_sell = 0
    last_updated = ""

    consensuses = block.get("consensuses") if isinstance(block, dict) else None
    if isinstance(consensuses, list) and consensuses:
        # Most recent consensus is the last entry (or first — varies);
        # we prefer one with a readable rating field.
        for c in reversed(consensuses):
            if not isinstance(c, dict):
                continue
            rating = c.get("rating") or c.get("consensus") or c.get("ratingText")
            if rating:
                consensus_label = _normalize_consensus(str(rating))
                num_buy = int(c.get("nB") or c.get("numBuys") or c.get("buy") or 0)
                num_hold = int(c.get("nH") or c.get("numHolds") or c.get("hold") or 0)
                num_sell = int(c.get("nS") or c.get("numSells") or c.get("sell") or 0)
                last_updated = str(c.get("d") or c.get("date") or "")
                break

    pts = block.get("priceTargets") if isinstance(block, dict) else None
    if isinstance(pts, list) and pts:
        try:
            average_pt = float(pts[-1].get("priceTarget") or pts[-1].get("pt") or 0) or None
        except (ValueError, TypeError):
            average_pt = None

    if not consensus_label and average_pt is None:
        raise MissingDataSourceError(
            "tipranks: __NEXT_DATA__ blob has no parseable consensus / price-target."
        )
    return {
        "consensus_label": consensus_label,
        "average_price_target": average_pt,
        "num_buy": num_buy,
        "num_hold": num_hold,
        "num_sell": num_sell,
        "last_updated": last_updated,
    }


def _parse_blogger_sentiment(html_text: str) -> dict[str, Any]:
    data = _extract_next_data(html_text)
    if data is not None:
        wrapper = _walk_for_keys(data, ("bloggerSentiment", "blogger"))
        block: Any = None
        if isinstance(wrapper, dict):
            block = (
                wrapper.get("bloggerSentiment")
                or wrapper.get("blogger")
                or wrapper
            )
        if isinstance(block, dict):
            try:
                bullish_raw = (
                    block.get("bullishPct")
                    if block.get("bullishPct") is not None
                    else block.get("bullish")
                )
                bearish_raw = (
                    block.get("bearishPct")
                    if block.get("bearishPct") is not None
                    else block.get("bearish")
                )
                if bullish_raw is not None or bearish_raw is not None:
                    return {
                        "bullish_pct": float(bullish_raw or 0),
                        "bearish_pct": float(bearish_raw or 0),
                    }
            except (TypeError, ValueError):
                pass
    # Try both orderings: "78% bullish" (number-then-label) and
    # "bullish 78%" (label-then-number). Tipranks oscillates between
    # both phrasings depending on copy edits.
    bullish = (
        _first_match_number(
            html_text, r'([0-9]+(?:\.[0-9]+)?)\s*%\s*bullish', flags=re.IGNORECASE
        )
        or _first_match_number(
            html_text, r'bullish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%',
            flags=re.IGNORECASE,
        )
    )
    bearish = (
        _first_match_number(
            html_text, r'([0-9]+(?:\.[0-9]+)?)\s*%\s*bearish', flags=re.IGNORECASE
        )
        or _first_match_number(
            html_text, r'bearish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%',
            flags=re.IGNORECASE,
        )
    )
    if bullish is None and bearish is None:
        raise MissingDataSourceError(
            "tipranks: could not parse blogger sentiment; layout may have changed."
        )
    return {
        "bullish_pct": bullish or 0.0,
        "bearish_pct": bearish or 0.0,
    }


def _parse_hedge_fund_signal(html_text: str) -> dict[str, Any]:
    data = _extract_next_data(html_text)
    if data is not None:
        wrapper = _walk_for_keys(data, ("hedgeFundSignal", "hedgeFund"))
        block: Any = None
        if isinstance(wrapper, dict):
            block = (
                wrapper.get("hedgeFundSignal")
                or wrapper.get("hedgeFund")
                or wrapper
            )
        if isinstance(block, dict):
            holding = block.get("hedgeFundsHolding") or block.get("holdingFunds")
            change = (block.get("recentChange") or block.get("trend") or "").lower()
            try:
                holding_int: int | None = int(holding) if holding is not None else None
            except (TypeError, ValueError):
                holding_int = None
            if holding_int is not None or change:
                return {
                    "hedge_funds_holding": holding_int or 0,
                    "recent_change": _normalize_change(change),
                }
    holding_match = _first_match_int(
        html_text, r'([0-9]+)\s*hedge\s*fund', flags=re.IGNORECASE
    )
    change = "unknown"
    if re.search(r"\bincreased\b", html_text, re.IGNORECASE):
        change = "increased"
    elif re.search(r"\bdecreased\b", html_text, re.IGNORECASE):
        change = "decreased"
    elif re.search(r"\bunchanged\b", html_text, re.IGNORECASE):
        change = "unchanged"
    if holding_match is None and change == "unknown":
        raise MissingDataSourceError(
            "tipranks: could not parse hedge-fund signal; layout may have changed."
        )
    return {
        "hedge_funds_holding": holding_match or 0,
        "recent_change": change,
    }


# ----------------------------------------------------------------------
# Internal: __NEXT_DATA__ extractor + small text helpers
# ----------------------------------------------------------------------


def _extract_next_data(html_text: str) -> dict[str, Any] | None:
    """Pull and parse the ``__NEXT_DATA__`` script JSON, if present."""
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]+?)</script>',
        html_text,
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _walk_for_keys(node: Any, target_keys: tuple[str, ...]) -> Any:
    """DFS-walk ``node`` and return the first dict containing one of ``target_keys``.

    TipRanks puts the relevant block somewhere under
    ``props.pageProps.<varies>``; the path drifts release-to-release so
    we just walk the tree.
    """
    if isinstance(node, dict):
        for k in target_keys:
            if k in node:
                # Could either be the entire block or one field within
                # a "consensuses+priceTargets" wrapper. Prefer the dict
                # itself when both keys live together.
                return node
        for v in node.values():
            found = _walk_for_keys(v, target_keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk_for_keys(item, target_keys)
            if found is not None:
                return found
    return None


def _first_match(text: str, pattern: str, *, flags: int = 0) -> str | None:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return m.group(1).strip()


def _first_match_number(text: str, pattern: str, *, flags: int = 0) -> float | None:
    raw = _first_match(text, pattern, flags=flags)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _first_match_int(text: str, pattern: str, *, flags: int = 0) -> int | None:
    raw = _first_match(text, pattern, flags=flags)
    if raw is None:
        return None
    try:
        return int(float(raw.replace(",", "")))
    except ValueError:
        return None


def _normalize_consensus(raw: str) -> str:
    s = (raw or "").strip().lower()
    if "strong buy" in s:
        return "Strong Buy"
    if "moderate buy" in s or s == "buy":
        return "Moderate Buy"
    if "strong sell" in s:
        return "Strong Sell"
    if "moderate sell" in s or s == "sell":
        return "Moderate Sell"
    if "hold" in s:
        return "Hold"
    return raw.strip()


def _normalize_change(raw: str) -> str:
    s = (raw or "").strip().lower()
    if "increase" in s or "raised" in s:
        return "increased"
    if "decrease" in s or "lowered" in s:
        return "decreased"
    if "unchanged" in s or "stable" in s:
        return "unchanged"
    return "unknown"


__all__ = [
    "BLOGGER_URL_TPL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TTL_SECONDS",
    "FORECAST_URL_TPL",
    "HEDGEFUND_URL_TPL",
    "TIPRANKS_BASE",
    "TipRanksAdapter",
    "_extract_next_data",
    "_normalize_change",
    "_normalize_consensus",
    "_parse_analyst_consensus",
    "_parse_blogger_sentiment",
    "_parse_hedge_fund_signal",
]
