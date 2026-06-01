"""Finnhub adapter (Phase 2).

Wraps the `finnhub-python` package for company news + earnings calendar.
Free tier covers our Phase 2 needs (60 calls/min). Reads its API key via
`argosy.secrets.get_secret(...)` first, env var (`FINNHUB_API_KEY`)
fallback. Cached per SDD §8.3 (15min for news, 24h for calendar).

Tests inject a fake `client` exposing `company_news(symbol, _from, to)`
and `earnings_calendar(_from, to, symbol, international)`.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from argosy.adapters import MissingAPIKeyError, MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.secrets import get_external_api_key, get_secret
from argosy.services.adapter_outcomes import track_adapter_call

KEYCHAIN_KEY = "argosy.finnhub.api_key"
ENV_VAR = "FINNHUB_API_KEY"
EXTERNAL_KEYS_PROVIDER = "finnhub"


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking.

    Serializes via json.dumps with default=str so any embedded date/datetime
    objects don't blow up. Best-effort only — on serialization failure we
    return 0 (treated as "empty" by the outcome builder, which is the
    truthful signal when we can't tell).
    """
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0


def _resolve_api_key() -> str:
    try:
        v = get_secret(KEYCHAIN_KEY)
    except Exception:  # pragma: no cover - defensive
        v = None
    if v:
        return v
    env_v = os.environ.get(ENV_VAR)
    if env_v:
        return env_v
    file_v = get_external_api_key(EXTERNAL_KEYS_PROVIDER)
    if file_v:
        return file_v
    raise MissingAPIKeyError(
        provider="Finnhub", keychain_key=KEYCHAIN_KEY, env_var=ENV_VAR
    )


class FinnhubAdapter:
    """Finnhub wrapper. Cached. Inject `client` in tests."""

    PROVIDER = "finnhub"

    def __init__(self, *, client: Any | None = None, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import finnhub  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingDataSourceError(
                "finnhub-python package is not installed. Run: uv add finnhub-python"
            ) from exc
        api_key = self._api_key or _resolve_api_key()
        self._client = finnhub.Client(api_key=api_key)
        return self._client

    async def get_company_news(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        ttl_seconds: int = 60 * 15,  # SDD §8.3: 15min
    ) -> list[dict[str, Any]]:
        """Return list of headline dicts for `symbol` within [start, end]."""
        with track_adapter_call("finnhub_news", target=symbol) as _outcome:
            client = self._resolve_client()
            key = f"company_news:{symbol}:{start.isoformat()}:{end.isoformat()}"

            def _fetch() -> list[dict[str, Any]]:
                raw = client.company_news(symbol, _from=start.isoformat(), to=end.isoformat())
                if not raw:
                    return []
                # Normalize: take the keys we care about.
                out: list[dict[str, Any]] = []
                for item in raw:
                    out.append(
                        {
                            "headline": item.get("headline") or "",
                            "summary": item.get("summary") or "",
                            "url": item.get("url") or "",
                            "source": item.get("source") or "",
                            "datetime": item.get("datetime"),
                        }
                    )
                return out

            payload = await cached_call(
                kind=CacheKind.NEWS,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def get_company_financials(
        self,
        symbol: str,
        *,
        ttl_seconds: int = 60 * 60 * 24,  # SDD §8.3: 24h fundamentals-class
    ) -> dict[str, Any]:
        """Return a curated dict of fundamentals metrics for ``symbol``.

        Wraps Finnhub's ``/stock/metric?symbol=<t>&metric=all`` endpoint
        (exposed via the SDK as ``company_basic_financials(symbol, "all")``).
        The raw payload carries dozens of keys under ``metric``; this
        method returns a curated subset matched to the keys the
        ``FundamentalsAnalystAgent`` prompt advertises (pe_ratio, peg,
        ev_ebitda, growth, debt/equity, 52w range, beta, dividend yield).

        Raises:
            MissingAPIKeyError: when no API key resolved.
            MissingDataSourceError: when Finnhub returns an empty
                ``metric`` block (typical for non-US listings / unsupported
                tickers) so the caller can skip + degrade gracefully.
        """
        with track_adapter_call("finnhub_financials", target=symbol) as _outcome:
            client = self._resolve_client()
            key = f"basic_financials:{symbol}:all"

            def _fetch() -> dict[str, Any]:
                raw = client.company_basic_financials(symbol, "all")
                if not isinstance(raw, dict):
                    raise MissingDataSourceError(
                        f"finnhub: unexpected payload type for {symbol}: {type(raw).__name__}"
                    )
                metric = raw.get("metric") if isinstance(raw.get("metric"), dict) else None
                if not metric:
                    raise MissingDataSourceError(
                        f"finnhub: empty metrics for {symbol} (likely non-US / unsupported)"
                    )
                # Curated subset; keys match the FundamentalsAnalystAgent
                # prompt advertised fields. Missing source values stay None.
                return {
                    "pe_ratio_ttm": metric.get("peTTM"),
                    "pe_normalized_annual": metric.get("peNormalizedAnnual"),
                    "pe_ratio": metric.get("peTTM") or metric.get("peNormalizedAnnual"),
                    "peg_ratio": metric.get("pegRatio") or metric.get("pegTTM"),
                    "eps_ttm": metric.get("epsTTM"),
                    "market_cap_m": metric.get("marketCapitalization"),
                    "revenue_per_share_ttm": metric.get("revenuePerShareTTM"),
                    "revenue_growth_yoy": metric.get("revenueGrowthTTMYoy"),
                    "earnings_growth_yoy": metric.get("epsGrowthTTMYoy"),
                    "gross_margin_ttm": metric.get("grossMarginTTM"),
                    "operating_margin_ttm": metric.get("operatingMarginTTM"),
                    "net_margin_ttm": metric.get("netProfitMarginTTM"),
                    "debt_to_equity": metric.get("totalDebt/totalEquityQuarterly"),
                    "ev_ebitda": metric.get("currentEv/freeCashFlowTTM") or metric.get("enterpriseValue/EBITDATTM"),
                    "dividend_yield": metric.get("dividendYieldIndicatedAnnual"),
                    "52w_high": metric.get("52WeekHigh"),
                    "52w_low": metric.get("52WeekLow"),
                    "beta": metric.get("beta"),
                    "source_url": f"https://finnhub.io/api/v1/stock/metric?symbol={symbol}",
                }

            payload = await cached_call(
                kind=CacheKind.NEWS,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def get_social_sentiment(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        ttl_seconds: int = 60 * 60 * 6,  # 6h — social signal moves faster than fundamentals
    ) -> dict[str, Any]:
        """Reddit + Twitter mention/score aggregate for ``symbol``.

        T3.2 fallback path: when TipRanks's blogger-opinions page blocks
        us with HTTP 403 the SentimentAnalystAgent goes data-starved.
        This wraps Finnhub's ``/stock/social-sentiment`` endpoint
        (``stock_social_sentiment(symbol, _from, to)`` on the SDK) and
        maps the response into the same ``{bullish_pct, bearish_pct,
        ticker, source_url}`` dict shape ``TipRanksAdapter.get_blogger_sentiment``
        returns, so callers don't need to know the source switched.

        Mapping:
        - Sum ``positiveScore`` and ``negativeScore`` across all reddit +
          twitter rows in the window.
        - ``bullish_pct = positive / (positive + negative) * 100``,
          ``bearish_pct = negative / (positive + negative) * 100``.
        - Falls back to mention-weighted counts when scores are zero
          (some Finnhub responses include only ``mention`` + ``score``).

        Coverage caveat — see T3.2 notes: Finnhub's free tier limits
        social-sentiment to US-listed equities and the methodology differs
        from TipRanks blogger aggregation. Bullish/bearish percentages
        are NOT directly comparable across providers; the SentimentAnalyst
        prompt treats them as a directional signal, not an exact metric.

        Raises:
            MissingAPIKeyError: when no API key resolved.
            MissingDataSourceError: when Finnhub returns an empty payload
                (typical for non-US listings / unsupported tickers) so the
                caller can decide whether to skip or degrade.
        """
        with track_adapter_call("finnhub_social", target=symbol) as _outcome:
            client = self._resolve_client()
            end_d = end or date.today()
            start_d = start or (end_d - timedelta(days=7))
            key = f"social_sentiment:{symbol}:{start_d.isoformat()}:{end_d.isoformat()}"

            def _fetch() -> dict[str, Any]:
                raw = client.stock_social_sentiment(
                    symbol, _from=start_d.isoformat(), to=end_d.isoformat()
                )
                if not isinstance(raw, dict):
                    raise MissingDataSourceError(
                        f"finnhub social-sentiment: unexpected payload type for "
                        f"{symbol}: {type(raw).__name__}"
                    )
                reddit = raw.get("reddit") or []
                twitter = raw.get("twitter") or []
                if not isinstance(reddit, list):
                    reddit = []
                if not isinstance(twitter, list):
                    twitter = []
                rows = reddit + twitter
                if not rows:
                    raise MissingDataSourceError(
                        f"finnhub social-sentiment: empty payload for {symbol} "
                        "(likely non-US / unsupported)"
                    )
                pos_score = 0.0
                neg_score = 0.0
                pos_mentions = 0.0
                neg_mentions = 0.0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        pos_score += float(row.get("positiveScore") or 0)
                        neg_score += float(row.get("negativeScore") or 0)
                        pos_mentions += float(row.get("positiveMention") or 0)
                        neg_mentions += float(row.get("negativeMention") or 0)
                    except (TypeError, ValueError):
                        continue
                # Prefer scores; fall back to mention counts when both
                # scores are zero (Finnhub sometimes returns only mentions).
                if pos_score + neg_score > 0:
                    total = pos_score + neg_score
                    bullish_pct = round(pos_score / total * 100.0, 2)
                    bearish_pct = round(neg_score / total * 100.0, 2)
                elif pos_mentions + neg_mentions > 0:
                    total = pos_mentions + neg_mentions
                    bullish_pct = round(pos_mentions / total * 100.0, 2)
                    bearish_pct = round(neg_mentions / total * 100.0, 2)
                else:
                    # Rows present but no usable scores/mentions — surface
                    # as empty so the caller can skip rather than emit a
                    # phantom 0/0 signal.
                    raise MissingDataSourceError(
                        f"finnhub social-sentiment: no scored rows for {symbol}"
                    )
                return {
                    "ticker": symbol,
                    "bullish_pct": bullish_pct,
                    "bearish_pct": bearish_pct,
                    "source_url": (
                        f"https://finnhub.io/api/v1/stock/social-sentiment?symbol={symbol}"
                    ),
                }

            payload = await cached_call(
                kind=CacheKind.NEWS,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def get_earnings_calendar(
        self,
        *,
        start: date,
        end: date,
        symbol: str | None = None,
        ttl_seconds: int = 60 * 60 * 24,  # SDD §8.3: 24h fundamentals-class
    ) -> list[dict[str, Any]]:
        """Return list of earnings events between [start, end]."""
        client = self._resolve_client()
        key = f"earnings:{symbol or '*'}:{start.isoformat()}:{end.isoformat()}"

        def _fetch() -> list[dict[str, Any]]:
            raw = client.earnings_calendar(
                _from=start.isoformat(),
                to=end.isoformat(),
                symbol=symbol or "",
                international=False,
            )
            if isinstance(raw, dict):
                return list(raw.get("earningsCalendar", []) or [])
            if isinstance(raw, list):
                return raw
            return []

        return await cached_call(
            kind=CacheKind.NEWS,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )


__all__ = ["FinnhubAdapter"]
