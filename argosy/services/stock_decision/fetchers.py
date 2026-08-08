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


def news_fetcher(ticker: str, *, lookback_days: int = 14, max_items: int = 5) -> str | None:
    """Recent company-news headlines for ``ticker`` via finnhub (sync, cached)."""
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
            return None
        heads = [
            (item.get("headline") or "").strip()
            for item in raw[:max_items]
            if isinstance(item, dict) and item.get("headline")
        ]
        return "; ".join(h for h in heads if h) or None
    except Exception as exc:  # noqa: BLE001 — best-effort; absent field is fine
        log.info("stock_decision.news_fetch_miss", ticker=ticker, err=str(exc)[:160])
        return None


def fundamentals_fetcher(ticker: str) -> str | None:
    """Compact fundamentals snapshot via finnhub basic financials (sync, cached)."""
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
            return None
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
        return "; ".join(parts) if parts else None
    except Exception as exc:  # noqa: BLE001
        log.info(
            "stock_decision.fundamentals_fetch_miss",
            ticker=ticker, err=str(exc)[:160],
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


def default_fetchers(db: Any, user_id: str) -> dict[str, Callable[[str], "str | None"]]:
    """The live fetcher registry for a holdings review / candidate decision."""
    return {
        "news": news_fetcher,
        "fundamentals": fundamentals_fetcher,
        "sentiment": sentiment_fetcher,
        "price": price_fetcher,
        "thesis": make_thesis_fetcher(db, user_id),
    }


__all__ = [
    "news_fetcher",
    "price_fetcher",
    "fundamentals_fetcher",
    "sentiment_fetcher",
    "make_thesis_fetcher",
    "default_fetchers",
    "render_instrument_monitoring_meta",
]
