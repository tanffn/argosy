"""FX (foreign-exchange) module — daily rate cache + BoI client + convert helpers.

All rates are stored as units of ILS per 1 unit of currency. Cross-rates
(e.g. USD -> EUR) are derived via two hops through ILS at lookup time.

Public API:
- ``convert(session, amount, from_ccy, to_ccy, on)`` — convert at the rate on `on`.
- ``rate(session, from_ccy, to_ccy, on)`` — raw rate for the pair on `on`.
- ``warm_cache(session, start, end, currencies)`` — bulk-prefetch from BoI.

Failure mode: every call raises ``FXRateUnavailable`` when no rate can be
found (cache miss + walkback exhausted + online fetch failed). Callers
choose whether to fall back gracefully or propagate the error.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.fx import boi_client, cache
from argosy.services.fx.errors import FXRateUnavailable

__all__ = ["convert", "rate", "warm_cache", "FXRateUnavailable"]


def _normalize(ccy: str) -> str:
    s = ccy.strip().upper()
    return "ILS" if s == "NIS" else s


def _resolve_to_ils(session: Session, ccy: str, on: date) -> Decimal:
    """Get rate (ILS per 1 unit of ccy) on `on`. Cache -> walkback -> BoI fetch.

    Last-ditch fallback: if no exact/walkback rate is available even after
    an online fetch attempt, return the NEAREST cached rate (any direction).
    This handles two real cases:
      (a) Sparse cache — historical txs older than anything cached.
      (b) BoI public endpoint that ignores `start`/`end` and returns only
          the latest snapshot, leaving us with only forward-of-tx rates.
    The fallback is an approximation; UI labels NIS-converted values as
    such. The alternative — leaving every historical USD row uncoverted
    forever — is worse.
    """
    cached = cache.get_rate(session, on, ccy)
    if cached is not None:
        return cached
    try:
        return cache.find_walkback(session, on, ccy)
    except FXRateUnavailable:
        pass
    from datetime import timedelta
    rows = boi_client.fetch_range(on - timedelta(days=7), on + timedelta(days=7), [ccy])
    cache.put_rates(session, rows)
    try:
        return cache.find_walkback(session, on, ccy)
    except FXRateUnavailable:
        pass
    # Approximation: nearest cached rate, regardless of direction.
    from argosy.state.models import FxRate
    rate_after = (
        session.query(FxRate)
        .filter(FxRate.currency == ccy, FxRate.date >= on)
        .order_by(FxRate.date.asc())
        .first()
    )
    if rate_after is not None:
        return rate_after.rate
    rate_before = (
        session.query(FxRate)
        .filter(FxRate.currency == ccy, FxRate.date < on)
        .order_by(FxRate.date.desc())
        .first()
    )
    if rate_before is not None:
        return rate_before.rate
    raise FXRateUnavailable(
        f"No rate for {ccy} on {on}; cache empty for this currency"
    )


def rate(session: Session, from_ccy: str, to_ccy: str, on: date) -> Decimal:
    """Return the rate (units of `to_ccy` per 1 unit of `from_ccy`) on `on`."""
    f = _normalize(from_ccy)
    t = _normalize(to_ccy)
    if f == t:
        return Decimal("1.0")
    if f == "ILS":
        return Decimal("1.0") / _resolve_to_ils(session, t, on)
    if t == "ILS":
        return _resolve_to_ils(session, f, on)
    # Cross-rate via ILS.
    f_to_ils = _resolve_to_ils(session, f, on)
    t_to_ils = _resolve_to_ils(session, t, on)
    return f_to_ils / t_to_ils


def convert(
    session: Session, amount: float, from_ccy: str, to_ccy: str, on: date,
) -> Decimal:
    """Convert ``amount`` from ``from_ccy`` to ``to_ccy`` using the rate on `on`."""
    return Decimal(str(amount)) * rate(session, from_ccy, to_ccy, on)


def warm_cache(
    session: Session, start: date, end: date, currencies: list[str],
) -> int:
    """Bulk-prefetch BoI rates for [start, end] x currencies. Returns inserted count."""
    rows = boi_client.fetch_range(start, end, currencies)
    n = cache.put_rates(session, rows)
    return n


def is_fx_stale(
    session: Session,
    *,
    on: date | None = None,
    currencies: tuple[str, ...] = ("USD",),
    max_stale_days: int = 1,
) -> bool:
    """True when the newest cached rate for ANY of ``currencies`` is missing or
    older than ``max_stale_days``. The single source of the freshness check used
    by both ``refresh_if_stale`` (should-I-fetch) and the period directive
    (is-the-directive-computed-on-stale-FX)."""
    from argosy.state.models import FxRate

    today = on or date.today()
    for ccy in currencies:
        row = (
            session.query(FxRate)
            .filter(FxRate.currency == ccy.strip().upper())
            .order_by(FxRate.date.desc())
            .first()
        )
        if row is None:
            return True
        rdate = row.date
        if isinstance(rdate, str):
            rdate = date.fromisoformat(rdate)
        if (today - rdate).days > max_stale_days:
            return True
    return False


def refresh_if_stale(
    session: Session,
    *,
    on: date | None = None,
    currencies: tuple[str, ...] = ("USD",),
    max_stale_days: int = 1,
    lookback_days: int = 20,
) -> bool:
    """On-demand freshness: if the newest cached rate for ``currencies`` is older
    than ``max_stale_days``, pull the recent window from BoI so downstream
    consumers never compute on stale FX. Returns True if a refresh was performed.
    Best-effort: a BoI failure leaves the cache as-is (caller still has the
    last-known rate). Called when the user REQUESTS an allocation — Argosy fetches
    fresh rather than serving a stale rate."""
    from datetime import timedelta

    today = on or date.today()
    if not is_fx_stale(session, on=today, currencies=currencies, max_stale_days=max_stale_days):
        return False
    try:
        warm_cache(session, today - timedelta(days=lookback_days), today, list(currencies))
        session.commit()
        return True
    except Exception:  # noqa: BLE001 — best-effort; last-known rate remains
        session.rollback()
        return False
