"""DB-backed rate cache for the FX module."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.fx.errors import FXRateUnavailable
from argosy.state.models import FxRate


def get_rate(session: Session, on: date, currency: str) -> Decimal | None:
    """Return the cached rate (units of ILS per 1 unit of `currency`) for `on`,
    or None if not in cache.
    """
    row = session.query(FxRate).filter_by(date=on, currency=currency).first()
    return row.rate if row else None


def put_rates(
    session: Session, rows: list[tuple[date, str, Decimal]],
    source: str = "boi",
) -> int:
    """Insert (date, currency, rate) tuples idempotently. Returns count inserted
    (existing rows are left alone — same-day re-fetches don't overwrite).
    """
    if not rows:
        return 0
    inserted = 0
    for d, ccy, r in rows:
        existing = session.query(FxRate).filter_by(date=d, currency=ccy).first()
        if existing is not None:
            continue
        session.add(FxRate(date=d, currency=ccy, rate=Decimal(str(r)), source=source))
        inserted += 1
    session.flush()
    return inserted


def find_walkback(
    session: Session, on: date, currency: str, max_days: int = 7,
) -> Decimal:
    """Look up the rate for `on`, walking back day-by-day up to `max_days` if
    the exact date isn't cached. Used for weekends and holidays.

    Raises FXRateUnavailable if no rate is found within the walkback window.
    """
    for i in range(max_days + 1):
        candidate = on - timedelta(days=i)
        rate = get_rate(session, candidate, currency)
        if rate is not None:
            return rate
    raise FXRateUnavailable(
        f"No rate for {currency} on {on} (walked back {max_days} days)"
    )
