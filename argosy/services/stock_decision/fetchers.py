"""Default best-effort fetchers that assemble a per-stock research bundle from
LIVE data sources (finnhub company news + yfinance price + the plan's own thesis).

Each fetcher returns a compact text summary or ``None``; every one is wrapped so a
missing key / network failure yields ``None`` (the field is simply absent from the
bundle and the decision agent lowers confidence + records the gap). These are
DIRECT data pulls, not the heavy LLM analysts — fast enough to run per-name.

IMPORTANT: news / fundamentals / sentiment MUST NOT call ``asyncio.run`` against
the shared async DB session (``cached_call`` → ``db_mod.get_session``). The
holdings-review job already runs inside a worker thread spawned from the main
event loop; ``asyncio.run`` creates a *new* loop and the shared aiosqlite pool
raises ``Queue is bound to a different event loop``. That silent miss emptied
every news field on 2026-08-07 and made 71 HOLDs look like reasoned decisions.

This module uses a **sync** cache path (same tables / TTLs as ``cached_call``,
via a short-lived sync SQLAlchemy engine — same pattern as
``cache.purge_cache_entry``) plus a shared Finnhub client, adapter-outcome
tracking, and a 60/min throttle. Stream E may later make ``cached_call`` itself
sync-safe; keep this cache-using until then — do not bypass the cache.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from argosy.logging import get_logger

log = get_logger(__name__)

# Finnhub free tier = 60 calls/min. Leave headroom for concurrent surfaces.
_MIN_INTERVAL_SEC = 1.05
_throttle_lock = threading.Lock()
_last_finnhub_call_at = 0.0

# Shared adapter — one client for the process (resolves key once).
_adapter_lock = threading.Lock()
_shared_adapter: Any | None = None
_yfinance_adapter_lock = threading.Lock()
_shared_yfinance_adapter_instance: Any | None = None

# Process-local TTL mirror (avoids hammering SQLite on every ticker in a job).
_mem_cache: dict[str, tuple[float, Any]] = {}
_mem_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _shared_finnhub_adapter() -> Any:
    global _shared_adapter
    with _adapter_lock:
        if _shared_adapter is None:
            from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

            _shared_adapter = FinnhubAdapter()
        return _shared_adapter


def _shared_yfinance_adapter() -> Any:
    """One lazily-created Yahoo adapter for synchronous review fetches."""
    global _shared_yfinance_adapter_instance
    with _yfinance_adapter_lock:
        if _shared_yfinance_adapter_instance is None:
            from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

            _shared_yfinance_adapter_instance = YFinanceAdapter()
        return _shared_yfinance_adapter_instance


def _throttle_finnhub() -> None:
    """Block until we respect the free-tier call budget."""
    global _last_finnhub_call_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_finnhub_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_finnhub_call_at = time.monotonic()


def _mem_get(key: str) -> Any | None:
    with _mem_lock:
        hit = _mem_cache.get(key)
        if hit is None:
            return None
        expires, payload = hit
        if expires <= time.time():
            _mem_cache.pop(key, None)
            return None
        return payload


def _mem_set(key: str, payload: Any, ttl_seconds: int) -> None:
    with _mem_lock:
        _mem_cache[key] = (time.time() + max(ttl_seconds, 0), payload)


def _sync_kv_get(provider: str, key: str) -> Any | None:
    """Read ``kv_cache`` via sync ORM Session (no asyncio / aiosqlite).

    Must use ``sessionmaker`` + ORM entities — ``conn.execute(select(Model))``
    on a Core connection yields Row tuples, so ``.scalar_one_or_none()`` returns
    the first *column* and ``row.expires_at`` raises AttributeError.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import KvCacheEntry

    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    try:
        with SessionLocal() as session:
            row = session.execute(
                select(KvCacheEntry).where(
                    (KvCacheEntry.provider == provider) & (KvCacheEntry.key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            exp = row.expires_at
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp is not None and exp > _utcnow():
                return json.loads(row.payload_json)
            return None
    except (AttributeError, TypeError):
        # Programming defects (wrong row shape, etc.) must NOT look like a miss.
        raise
    except Exception as exc:  # noqa: BLE001 — operational DB / JSON only
        log.error(
            "stock_decision.sync_cache_read_error",
            provider=provider, key=key, err=str(exc)[:200],
        )
        return None
    finally:
        engine.dispose()


def _sync_kv_put(
    provider: str, key: str, payload: Any, *, ttl_seconds: int,
) -> None:
    import hashlib

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import KvCacheEntry

    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    payload_json = json.dumps(payload, default=str)
    now = _utcnow()
    expires = now + timedelta(seconds=max(ttl_seconds, 0))
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    try:
        with SessionLocal() as session:
            existing = session.execute(
                select(KvCacheEntry).where(
                    (KvCacheEntry.provider == provider) & (KvCacheEntry.key == key)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    KvCacheEntry(
                        provider=provider,
                        key=key,
                        payload_json=payload_json,
                        retrieved_at=now,
                        expires_at=expires,
                        payload_hash=payload_hash,
                    )
                )
            else:
                existing.payload_json = payload_json
                existing.retrieved_at = now
                existing.expires_at = expires
                existing.payload_hash = payload_hash
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stock_decision.sync_cache_write_error",
            provider=provider, key=key, err=str(exc)[:200],
        )
    finally:
        engine.dispose()


def _cached_finnhub_fetch(
    *,
    cache_key: str,
    ttl_seconds: int,
    outcome_name: str,
    target: str,
    fetch: Callable[[], Any],
) -> Any:
    """Process mem-cache → sync DB cache → throttled live fetch + outcomes."""
    mem = _mem_get(cache_key)
    if mem is not None:
        return mem
    db_hit = _sync_kv_get("finnhub", cache_key)
    if db_hit is not None:
        _mem_set(cache_key, db_hit, ttl_seconds)
        return db_hit

    from argosy.services.adapter_outcomes import track_adapter_call

    with track_adapter_call(outcome_name, target=target) as _outcome:
        _throttle_finnhub()
        payload = fetch()
        try:
            _outcome.set_payload_size_bytes(
                len(json.dumps(payload, default=str)) if payload is not None else 0
            )
        except Exception:  # noqa: BLE001
            pass
    _mem_set(cache_key, payload, ttl_seconds)
    try:
        _sync_kv_put("finnhub", cache_key, payload, ttl_seconds=ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return payload


def _cached_yfinance_fetch(
    *,
    cache_key: str,
    ttl_seconds: int,
    outcome_name: str,
    target: str,
    fetch: Callable[[], Any],
) -> Any:
    """Sync Yahoo cache path for the worker-thread holdings review."""
    mem_key = f"yfinance:{cache_key}"
    mem = _mem_get(mem_key)
    if mem is not None:
        return mem
    db_hit = _sync_kv_get("yfinance", cache_key)
    if db_hit is not None:
        _mem_set(mem_key, db_hit, ttl_seconds)
        return db_hit

    from argosy.services.adapter_outcomes import track_adapter_call

    with track_adapter_call(outcome_name, target=target) as outcome:
        payload = fetch()
        try:
            outcome.set_payload_size_bytes(
                len(json.dumps(payload, default=str)) if payload is not None else 0
            )
        except Exception:  # noqa: BLE001
            pass
    _mem_set(mem_key, payload, ttl_seconds)
    try:
        _sync_kv_put("yfinance", cache_key, payload, ttl_seconds=ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return payload


def _yahoo_news(ticker: str, *, max_items: int) -> str | None:
    """Yahoo company-news fallback, preserving source and publication time."""
    from argosy.adapters.data.symbols import to_yahoo_symbol

    yf_symbol = to_yahoo_symbol(ticker)

    def _fetch() -> list[Any]:
        client = _shared_yfinance_adapter()._resolve_client()
        instrument = client.Ticker(yf_symbol)
        getter = getattr(instrument, "get_news", None)
        if callable(getter):
            try:
                return list(getter(count=max_items, tab="news") or [])
            except TypeError:
                return list(getter() or [])
        return list(getattr(instrument, "news", None) or [])

    raw = _cached_yfinance_fetch(
        cache_key=f"company_news:{yf_symbol}",
        ttl_seconds=60 * 15,
        outcome_name="yfinance_news",
        target=ticker,
        fetch=_fetch,
    )
    rendered: list[str] = []
    for item in list(raw or [])[:max_items]:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = str(content.get("title") or content.get("headline") or "").strip()
        if not title:
            continue
        published = content.get("pubDate") or item.get("providerPublishTime")
        if isinstance(published, (int, float)):
            published = datetime.fromtimestamp(published, tz=timezone.utc).date().isoformat()
        suffix = f" [{published}]" if published else ""
        rendered.append(f"{title}{suffix}")
    if not rendered:
        return None
    return "source=yfinance; " + "; ".join(rendered)


def _yahoo_fundamentals(ticker: str) -> str | None:
    """Yahoo fundamentals fallback used when Finnhub is absent/unavailable."""
    from argosy.adapters.data.symbols import to_yahoo_symbol

    yf_symbol = to_yahoo_symbol(ticker)

    def _fetch() -> dict[str, Any]:
        client = _shared_yfinance_adapter()._resolve_client()
        instrument = client.Ticker(yf_symbol)
        info = getattr(instrument, "info", None) or {}
        return info if isinstance(info, dict) else {}

    info = _cached_yfinance_fetch(
        cache_key=f"review_fundamentals:{yf_symbol}",
        ttl_seconds=60 * 60,
        outcome_name="yfinance_review_fundamentals",
        target=ticker,
        fetch=_fetch,
    )
    if not isinstance(info, dict) or not info:
        return None
    parts: list[str] = []
    for label, key in (
        ("PE(TTM)", "trailingPE"),
        ("forwardPE", "forwardPE"),
        ("mktCap", "marketCap"),
        ("revGrowth", "revenueGrowth"),
        ("earningsGrowth", "earningsGrowth"),
        ("profitMargin", "profitMargins"),
        ("beta", "beta"),
        ("dividendYield", "dividendYield"),
    ):
        value = info.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    if not parts:
        return None
    return (
        f"source=yfinance; as_of={_utcnow().date().isoformat()}; "
        + "; ".join(parts)
    )


def news_fetcher(ticker: str, *, lookback_days: int = 14, max_items: int = 5) -> str | None:
    """Recent company news: Finnhub primary, Yahoo fallback (sync, cached)."""
    try:
        today = date.today()
        start = today - timedelta(days=lookback_days)
        cache_key = f"company_news:{ticker}:{start.isoformat()}:{today.isoformat()}"

        def _fetch() -> list:
            client = _shared_finnhub_adapter()._resolve_client()
            raw = client.company_news(
                ticker, _from=start.isoformat(), to=today.isoformat(),
            )
            return list(raw or [])

        raw = _cached_finnhub_fetch(
            cache_key=cache_key,
            ttl_seconds=60 * 15,
            outcome_name="finnhub_news",
            target=ticker,
            fetch=_fetch,
        )
        if not raw:
            return _yahoo_news(ticker, max_items=max_items)
        heads = [
            (item.get("headline") or "").strip()
            for item in raw[:max_items]
            if isinstance(item, dict) and item.get("headline")
        ]
        rendered = "; ".join(h for h in heads if h)
        return f"source=finnhub; {rendered}" if rendered else _yahoo_news(
            ticker, max_items=max_items
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; absent field is fine
        log.info("stock_decision.news_fetch_miss", ticker=ticker, err=str(exc)[:160])
        try:
            return _yahoo_news(ticker, max_items=max_items)
        except Exception as fallback_exc:  # noqa: BLE001
            log.info(
                "stock_decision.news_fallback_miss",
                ticker=ticker, err=str(fallback_exc)[:160],
            )
            return None


def fundamentals_fetcher(ticker: str) -> str | None:
    """Compact fundamentals: Finnhub primary, Yahoo fallback (sync, cached)."""
    try:
        from argosy.adapters.data.symbols import to_finnhub_symbol

        fh = to_finnhub_symbol(ticker)
        cache_key = f"basic_financials:{fh}"

        def _fetch() -> dict:
            client = _shared_finnhub_adapter()._resolve_client()
            raw = client.company_basic_financials(fh, "all")
            return raw if isinstance(raw, dict) else {}

        raw = _cached_finnhub_fetch(
            cache_key=cache_key,
            ttl_seconds=60 * 60,
            outcome_name="finnhub_fundamentals",
            target=ticker,
            fetch=_fetch,
        )
        metric = raw.get("metric") if isinstance(raw, dict) else None
        if not isinstance(metric, dict) or not metric:
            return _yahoo_fundamentals(ticker)
        parts = []
        for label, key in (
            ("PE(TTM)", "peTTM"),
            ("PEG", "pegRatio"),
            ("mktCapM", "marketCapitalization"),
            ("revYoY", "revenueGrowthTTMYoy"),
            ("epsYoY", "epsGrowthTTMYoy"),
            ("beta", "beta"),
        ):
            v = metric.get(key)
            if v is not None:
                parts.append(f"{label}={v}")
        return (
            f"source=finnhub; {'; '.join(parts)}"
            if parts
            else _yahoo_fundamentals(ticker)
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "stock_decision.fundamentals_fetch_miss",
            ticker=ticker, err=str(exc)[:160],
        )
        try:
            return _yahoo_fundamentals(ticker)
        except Exception as fallback_exc:  # noqa: BLE001
            log.info(
                "stock_decision.fundamentals_fallback_miss",
                ticker=ticker, err=str(fallback_exc)[:160],
            )
            return None


def sentiment_fetcher(ticker: str) -> str | None:
    """Compact social-sentiment snapshot via finnhub (sync, cached; US listings).

    Returns ``None`` when scores are unavailable — a truthy
    ``"scores unavailable"`` string is NOT usable evidence (see
    ``bundle_has_sufficient_evidence``).
    """
    try:
        end_d = date.today()
        start_d = end_d - timedelta(days=7)
        cache_key = (
            f"social_sentiment:{ticker}:{start_d.isoformat()}:{end_d.isoformat()}"
        )

        def _fetch() -> dict:
            client = _shared_finnhub_adapter()._resolve_client()
            raw = client.stock_social_sentiment(
                ticker, _from=start_d.isoformat(), to=end_d.isoformat(),
            )
            return raw if isinstance(raw, dict) else {}

        raw = _cached_finnhub_fetch(
            cache_key=cache_key,
            ttl_seconds=60 * 15,
            outcome_name="finnhub_sentiment",
            target=ticker,
            fetch=_fetch,
        )
        if not isinstance(raw, dict):
            return None
        rows = list(raw.get("reddit") or []) + list(raw.get("twitter") or [])
        if not rows:
            return None
        pos = neg = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pos += float(row.get("positiveScore") or 0)
                neg += float(row.get("negativeScore") or 0)
            except (TypeError, ValueError):
                continue
        total = pos + neg
        if total <= 0:
            # Unusable — do not mint a truthy placeholder that fools the gate.
            return None
        bull = 100.0 * pos / total
        return (
            f"social bullish_pct={bull:.1f}; bearish_pct={100.0 - bull:.1f}; "
            f"n={len(rows)}"
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "stock_decision.sentiment_fetch_miss",
            ticker=ticker, err=str(exc)[:160],
        )
        return None


def price_fetcher(ticker: str) -> str | None:
    """Current price for ``ticker`` via the deploy quote provider (yfinance,
    UCITS-suffix aware). Best-effort."""
    try:
        from argosy.services.deployment_funnel.from_plan import SnapshotOrLiveProvider

        p = SnapshotOrLiveProvider().quote(ticker)
        return f"last price {p}" if p is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.price_fetch_miss", ticker=ticker, err=str(exc)[:120])
        return None


def render_instrument_monitoring_meta(inst: Any) -> str:
    """Render an instrument's recorded exit triggers / review anchor as a prompt
    suffix (empty string when none). The monitor agent's weakened/broken judgment
    must evaluate against the RECORDED invalidation conditions, not vibes."""
    parts: list[str] = []
    triggers = list(getattr(inst, "exit_triggers", None) or [])
    if triggers:
        parts.append(
            "EXIT TRIGGERS (recorded invalidation conditions): "
            + "; ".join(str(t) for t in triggers)
        )
    review_on = getattr(inst, "review_on", None)
    if review_on:
        parts.append(f"Review on: {review_on}")
    return (" " + " | ".join(parts)) if parts else ""


def _class_labels_by_symbol(db: Any, user_id: str, doc: Any) -> dict[str, str]:
    """Symbol -> canonical plan-class label for the LIVE book."""
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        row = get_latest_snapshot_row(db, user_id)
        if row is None:
            return {}
        rows = build_allocation_breakdown(row_to_snapshot(row), doc)
        out: dict[str, str] = {}
        for cat in rows:
            for h in cat.holdings:
                sym = (h.symbol or "").strip().upper()
                if sym and sym not in out:
                    out[sym] = cat.label
        return out
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.class_attribution_miss", err=str(exc)[:120])
        return {}


def make_thesis_fetcher(db: Any, user_id: str) -> Callable[[str], "str | None"]:
    """A fetcher that returns the current plan's stance on ``ticker``."""
    doc = None
    try:
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("stock_decision.thesis_load_miss", err=str(exc)[:120])
        doc = None

    class_by_label: dict[str, Any] = {}
    label_by_symbol: dict[str, str] = {}
    if doc is not None:
        try:
            from argosy.services.allocation_plan import normalize_sleeve_label

            class_by_label = {
                normalize_sleeve_label(getattr(c, "label", "") or ""): c
                for c in getattr(doc, "classes", []) or []
            }
        except Exception as exc:  # noqa: BLE001
            log.info("stock_decision.class_index_miss", err=str(exc)[:120])
        if db is not None:
            label_by_symbol = _class_labels_by_symbol(db, user_id, doc)

    def _fetch(ticker: str) -> str | None:
        if doc is None:
            return None
        t = (ticker or "").upper()
        for c in getattr(doc, "classes", []) or []:
            for inst in getattr(c, "instruments", []) or []:
                if (getattr(inst, "symbol", "") or "").upper() == t:
                    target = getattr(c, "target_pct", None)
                    stance = (
                        "plan wants to EXIT (0% target)"
                        if (target == 0)
                        else f"sleeve target {target}%"
                    )
                    rat = (
                        getattr(inst, "rationale", "")
                        or getattr(c, "rationale", "")
                        or ""
                    )[:200]
                    meta = render_instrument_monitoring_meta(inst)
                    return (
                        f"in sleeve '{getattr(c, 'label', '')}' ({stance}). "
                        f"{rat}{meta}"
                    ).strip()
        label = label_by_symbol.get(t)
        if label:
            c = class_by_label.get(label)
            if c is not None:
                primary = next(
                    (
                        i
                        for i in (getattr(c, "instruments", []) or [])
                        if getattr(i, "role", "") == "primary"
                    ),
                    None,
                ) or next(iter(getattr(c, "instruments", []) or []), None)
                primary_sym = (
                    getattr(primary, "symbol", "") if primary is not None else ""
                )
                target = getattr(c, "target_pct", None)
                rat = (getattr(c, "rationale", "") or "")[:200]
                return (
                    f"covers the '{getattr(c, 'label', '')}' sleeve "
                    f"(target {target}%) as a substitute"
                    + (f" for {primary_sym}" if primary_sym else "")
                    + f"; not plan-named. Class thesis: {rat}"
                ).strip()
        return "not a plan-target instrument (candidate or legacy holding)"

    return _fetch


def make_earnings_calendar_fetcher(
    db: Any,
    user_id: str,
) -> Callable[[str], "str | None"]:
    """Return the latest durable per-ticker earnings check for verdict input."""
    from sqlalchemy import select

    from argosy.state.models import EarningsCoverageReceipt

    def _fetch(ticker: str) -> str | None:
        if db is None:
            return None
        row = db.execute(
            select(EarningsCoverageReceipt)
            .where(
                EarningsCoverageReceipt.user_id == user_id,
                EarningsCoverageReceipt.ticker == ticker.strip().upper(),
                EarningsCoverageReceipt.provider == "yfinance",
            )
            .order_by(
                EarningsCoverageReceipt.checked_at.desc(),
                EarningsCoverageReceipt.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        parts = [
            "source=yfinance_earnings_calendar",
            f"checked_at={row.checked_at.isoformat()}",
            f"status={row.status}",
            f"source_url={row.source_url}",
        ]
        if row.latest_reported_at is not None:
            parts.append(
                f"latest_reported_at={row.latest_reported_at.isoformat()}"
            )
        if row.next_scheduled_at is not None:
            parts.append(
                f"next_scheduled_at={row.next_scheduled_at.isoformat()}"
            )
        if row.error_message:
            parts.append(f"error={row.error_message[:240]}")
        if row.events_json and row.events_json != "[]":
            parts.append(f"events={row.events_json[:2400]}")
        return "; ".join(parts)

    return _fetch


def make_earnings_filing_fetcher(
    db: Any,
    user_id: str,
) -> Callable[[str], "str | None"]:
    """Return the latest durable primary SEC results-filing receipt."""
    from sqlalchemy import select

    from argosy.state.models import EarningsCoverageReceipt

    def _fetch(ticker: str) -> str | None:
        if db is None:
            return None
        row = db.execute(
            select(EarningsCoverageReceipt)
            .where(
                EarningsCoverageReceipt.user_id == user_id,
                EarningsCoverageReceipt.ticker == ticker.strip().upper(),
                EarningsCoverageReceipt.provider == "sec_edgar",
            )
            .order_by(
                EarningsCoverageReceipt.checked_at.desc(),
                EarningsCoverageReceipt.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        parts = [
            "source=sec_edgar_primary_filing",
            f"checked_at={row.checked_at.isoformat()}",
            f"status={row.status}",
            f"source_url={row.source_url}",
        ]
        if row.latest_reported_at is not None:
            parts.append(f"latest_filing_at={row.latest_reported_at.isoformat()}")
        if row.error_message:
            parts.append(f"error={row.error_message[:240]}")
        if row.events_json and row.events_json != "[]":
            parts.append(f"filings={row.events_json[:4000]}")
        return "; ".join(parts)

    return _fetch


def make_tax_context_fetcher(
    db: Any,
    user_id: str,
    *,
    price_context: Callable[[str], str | None] | None = None,
) -> Callable[[str], "str | None"]:
    """Return authoritative sale-tax context when Argosy actually has it.

    NVDA uses the ingested Section-102/ESPP simulation and its canonical
    revaluation engine. Generic broker lots are described honestly and are not
    called actionable when basis is zero/missing.
    """
    from sqlalchemy import select

    from argosy.state.models import Lot

    def _fetch(ticker: str) -> str | None:
        if db is None:
            return None
        symbol = ticker.strip().upper()
        if symbol == "NVDA":
            from argosy.services.tax_simulation_ingest import (
                eligible_shares,
                eligibility_schedule,
                realization_tax_summary,
            )

            price_text = (price_context or price_fetcher)(symbol) or ""
            price_match = re.search(
                r"\blast(?:\s+price\s+|=)([0-9]+(?:\.[0-9]+)?)",
                price_text,
            )
            current_price = float(price_match.group(1)) if price_match else None
            aggregate = realization_tax_summary(
                db,
                user_id,
                current_nvda_price_usd=current_price,
            )
            if aggregate is None:
                return None
            eligible = eligible_shares(db, user_id, eligible=True) or 0.0
            breaking = eligible_shares(db, user_id, eligible=False) or 0.0
            schedule_by_date: dict[str, float] = {}
            for tranche in eligibility_schedule(db, user_id):
                key = tranche.eligible_date.isoformat()
                schedule_by_date[key] = schedule_by_date.get(key, 0.0) + tranche.shares
            next_dates = ",".join(
                f"{when}:{shares:.0f}sh"
                for when, shares in sorted(schedule_by_date.items())[:3]
            ) or "none"
            effective_rate = (
                aggregate.embedded_tax_at_revalue_usd
                / aggregate.gross_at_revalue_usd
                if aggregate.gross_at_revalue_usd > 0
                else 0.0
            )
            return (
                "source=authoritative_section_102_tax_engine; "
                f"simulation_date={aggregate.simulation_date}; "
                f"price_basis={'current' if aggregate.uses_current_price else 'simulation'}; "
                f"mark_usd={aggregate.revalue_price_usd:.2f}; "
                f"covered_shares={aggregate.total_shares:.0f}; "
                f"eligible_now_shares={eligible:.0f}; "
                f"breaking_now_shares={breaking:.0f}; "
                f"full_position_gross_usd={aggregate.gross_at_revalue_usd:.2f}; "
                f"full_position_tax_usd={aggregate.embedded_tax_at_revalue_usd:.2f}; "
                f"full_position_net_usd={aggregate.net_at_revalue_usd:.2f}; "
                f"effective_tax_on_gross={effective_rate:.6f}; "
                f"incomplete_shares={aggregate.incomplete_lot_shares:.0f}; "
                f"next_eligibility={next_dates}; "
                "exact partial-sale sizing must call resolve_authoritative_sale"
            )

        lots = db.execute(
            select(Lot).where(
                Lot.user_id == user_id,
                Lot.ticker == symbol,
            )
        ).scalars().all()
        if not lots:
            return None
        quantity = sum(float(row.quantity or 0.0) for row in lots)
        basis = sum(float(row.cost_basis_usd or 0.0) for row in lots)
        complete = bool(lots) and all(
            float(row.quantity or 0.0) > 0
            and float(row.cost_basis_usd or 0.0) > 0
            for row in lots
        )
        return (
            "source=broker_tax_lots; "
            f"status={'authoritative' if complete else 'incomplete'}; "
            f"lot_count={len(lots)}; covered_shares={quantity:.4f}; "
            f"cost_basis_usd={basis:.2f}; "
            + (
                "exact partial sale can use canonical HIFO after-tax resolver"
                if complete
                else "zero/missing basis prevents actionable after-tax proceeds"
            )
        )

    return _fetch


def default_fetchers(db: Any, user_id: str) -> dict[str, Callable[[str], "str | None"]]:
    """The live fetcher registry for a holdings review / candidate decision."""
    from argosy.services.research_inputs import render_research_inputs
    price_evidence: dict[str, str | None] = {}

    def _price(ticker: str) -> str | None:
        value = price_fetcher(ticker)
        price_evidence[ticker.strip().upper()] = value
        return value

    return {
        "research_inputs": lambda ticker: render_research_inputs(db, user_id=user_id, ticker=ticker),
        "news": news_fetcher,
        "earnings_calendar": make_earnings_calendar_fetcher(db, user_id),
        "earnings_filing": make_earnings_filing_fetcher(db, user_id),
        "fundamentals": fundamentals_fetcher,
        "sentiment": sentiment_fetcher,
        "price": _price,
        "thesis": make_thesis_fetcher(db, user_id),
        "tax": make_tax_context_fetcher(
            db,
            user_id,
            price_context=lambda ticker: price_evidence.get(ticker.strip().upper()),
        ),
    }


__all__ = [
    "news_fetcher",
    "price_fetcher",
    "fundamentals_fetcher",
    "sentiment_fetcher",
    "make_thesis_fetcher",
    "make_earnings_calendar_fetcher",
    "make_earnings_filing_fetcher",
    "make_tax_context_fetcher",
    "default_fetchers",
    "render_instrument_monitoring_meta",
]
