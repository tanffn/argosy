"""Durable held-ticker earnings calendar checks and analyst signals."""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from argosy.state.models import EarningsCoverageReceipt, NewsSignal

_PROVIDER = "yfinance"
_NEWS_SOURCE = "yf_earnings"


@dataclass(frozen=True)
class EarningsEvent:
    event_at: datetime
    eps_estimate: float | None = None
    reported_eps: float | None = None
    surprise_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["event_at"] = self.event_at.isoformat()
        return value


@dataclass(frozen=True)
class EarningsCoverageSummary:
    attempted: int
    succeeded: int
    empty: int
    failures: int
    recent_reported_events: int
    signals_persisted: int
    signals_duplicate: int
    errors: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "errors": list(self.errors),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def fetch_yfinance_earnings(ticker: str) -> list[EarningsEvent]:
    """Fetch normalized historical/upcoming earnings dates for one ticker."""
    import yfinance as yf

    frame = yf.Ticker(ticker).get_earnings_dates(limit=12)
    if frame is None or getattr(frame, "empty", True):
        return []
    events: list[EarningsEvent] = []
    for index, row in frame.iterrows():
        raw_at = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
        if isinstance(raw_at, date) and not isinstance(raw_at, datetime):
            raw_at = datetime.combine(raw_at, datetime.min.time(), tzinfo=UTC)
        if not isinstance(raw_at, datetime):
            continue
        events.append(EarningsEvent(
            event_at=_utc(raw_at),
            eps_estimate=_number(row.get("EPS Estimate")),
            reported_eps=_number(row.get("Reported EPS")),
            surprise_pct=_number(
                row.get("Surprise(%)", row.get("Surprise (%)"))
            ),
        ))
    return sorted(events, key=lambda item: item.event_at)


def _write_receipt(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    checked_at: datetime,
    status: str,
    events: list[EarningsEvent],
    error: str | None,
) -> None:
    reported = [event.event_at for event in events if event.event_at <= checked_at]
    upcoming = [event.event_at for event in events if event.event_at > checked_at]
    values = {
        "user_id": user_id,
        "ticker": ticker,
        "check_date": checked_at.date(),
        "checked_at": checked_at,
        "provider": _PROVIDER,
        "status": status,
        "source_url": f"https://finance.yahoo.com/quote/{ticker}/calendar/",
        "events_json": json.dumps(
            [event.to_dict() for event in events],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "latest_reported_at": max(reported) if reported else None,
        "next_scheduled_at": min(upcoming) if upcoming else None,
        "error_message": error,
    }
    if session.get_bind().dialect.name == "sqlite":
        statement = sqlite_insert(EarningsCoverageReceipt).values(**values)
        excluded = statement.excluded
        session.execute(statement.on_conflict_do_update(
            index_elements=["user_id", "ticker", "provider", "check_date"],
            set_={
                "checked_at": excluded.checked_at,
                "status": excluded.status,
                "source_url": excluded.source_url,
                "events_json": excluded.events_json,
                "latest_reported_at": excluded.latest_reported_at,
                "next_scheduled_at": excluded.next_scheduled_at,
                "error_message": excluded.error_message,
            },
        ))
        return
    row = session.execute(select(EarningsCoverageReceipt).where(
        EarningsCoverageReceipt.user_id == user_id,
        EarningsCoverageReceipt.ticker == ticker,
        EarningsCoverageReceipt.provider == _PROVIDER,
        EarningsCoverageReceipt.check_date == checked_at.date(),
    )).scalar_one_or_none()
    if row is None:
        session.add(EarningsCoverageReceipt(**values))
        return
    for field, value in values.items():
        setattr(row, field, value)


def _event_sentiment(event: EarningsEvent) -> str:
    if event.surprise_pct is None:
        return "neutral"
    if event.surprise_pct <= -5:
        return "negative"
    if event.surprise_pct >= 5:
        return "positive"
    return "neutral"


def _write_event_signal(
    session: Session,
    *,
    ticker: str,
    event: EarningsEvent,
) -> bool:
    source_url = f"https://finance.yahoo.com/quote/{ticker}/calendar/"
    source_ref = f"{source_url}#event={event.event_at.isoformat()}"
    existing = session.execute(select(NewsSignal.id).where(
        NewsSignal.source == _NEWS_SOURCE,
        NewsSignal.source_ref == source_ref,
    )).first()
    if existing is not None:
        return False
    detail = (
        f"{ticker} earnings event at {event.event_at.isoformat()}; "
        f"EPS estimate={event.eps_estimate}; reported EPS={event.reported_eps}; "
        f"surprise_pct={event.surprise_pct}."
    )
    session.add(NewsSignal(
        source=_NEWS_SOURCE,
        source_ref=source_ref,
        received_at=event.event_at,
        parsed_tickers=json.dumps([ticker]),
        event_keywords=json.dumps(["earnings", "earnings_calendar"]),
        sentiment=_event_sentiment(event),
        source_trust="medium",
        evidence_excerpt=detail,
        raw_text=detail,
    ))
    session.flush()
    return True


def run_earnings_calendar_checks(
    session: Session,
    *,
    user_id: str,
    tickers: Iterable[str],
    checked_at: datetime | None = None,
    fetcher: Callable[[str], list[EarningsEvent]] = fetch_yfinance_earnings,
    recent_days: int = 10,
) -> EarningsCoverageSummary:
    """Check every held ticker and emit recent-event analyst evidence."""
    now = _utc(checked_at or datetime.now(UTC))
    symbols = sorted({str(ticker).strip().upper() for ticker in tickers if ticker})
    succeeded = empty = failures = recent = persisted = duplicates = 0
    errors: list[dict[str, str]] = []
    cutoff = now - timedelta(days=max(1, int(recent_days)))
    for ticker in symbols:
        try:
            events = fetcher(ticker)
            status = "ok" if events else "empty"
            _write_receipt(
                session,
                user_id=user_id,
                ticker=ticker,
                checked_at=now,
                status=status,
                events=events,
                error=None,
            )
            succeeded += 1
            empty += not events
            for event in events:
                if not cutoff <= event.event_at <= now:
                    continue
                recent += 1
                with session.begin_nested():
                    if _write_event_signal(session, ticker=ticker, event=event):
                        persisted += 1
                    else:
                        duplicates += 1
        except Exception as exc:  # noqa: BLE001 - per-ticker isolation
            message = str(exc)[:300]
            failures += 1
            errors.append({"ticker": ticker, "error": message})
            _write_receipt(
                session,
                user_id=user_id,
                ticker=ticker,
                checked_at=now,
                status="error",
                events=[],
                error=message,
            )
    session.flush()
    return EarningsCoverageSummary(
        attempted=len(symbols),
        succeeded=succeeded,
        empty=int(empty),
        failures=failures,
        recent_reported_events=recent,
        signals_persisted=persisted,
        signals_duplicate=duplicates,
        errors=tuple(errors),
    )


__all__ = [
    "EarningsCoverageSummary",
    "EarningsEvent",
    "fetch_yfinance_earnings",
    "run_earnings_calendar_checks",
]
