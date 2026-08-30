"""Queryable proof of held-position news and earnings-event coverage."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import (
    EarningsCoverageReceipt,
    HoldingReview,
    JobRun,
    Lot,
    NewsSignal,
)


def _tickers(value: str | None) -> set[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return set()
    return {
        str(item).strip().upper()
        for item in parsed
        if str(item).strip()
    } if isinstance(parsed, list) else set()


def _keywords(row: NewsSignal) -> set[str]:
    try:
        parsed = json.loads(row.event_keywords or "[]")
    except (TypeError, ValueError):
        parsed = []
    words = {str(item).strip().lower() for item in parsed if str(item).strip()}
    text = f"{row.evidence_excerpt or ''} {row.raw_text or ''}".lower()
    if "earning" in text or "quarterly call" in text or "conference call" in text:
        words.add("earnings")
    return words


def _verification(row: HoldingReview | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        payload = json.loads(row.evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    value = payload.get("verification") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else None


def _review_payload(row: HoldingReview | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(row.evidence_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_news_coverage(
    session: Session,
    *,
    user_id: str,
    as_of: datetime | None = None,
    lookback_days: int = 10,
) -> dict[str, Any]:
    """Show what monitoring actually saw; never infer missing call coverage."""

    from argosy.services.jobs.news_daily import resolve_holdings_split

    now = as_of or datetime.now(UTC)
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    holdings = resolve_holdings_split(session, user_id)
    symbols = sorted({ticker.upper() for ticker in holdings.single_stocks})
    signals = session.execute(
        select(NewsSignal)
        .where(NewsSignal.received_at >= cutoff)
        .order_by(NewsSignal.received_at.desc(), NewsSignal.id.desc())
    ).scalars().all()
    reviews = session.execute(
        select(HoldingReview)
        .where(
            HoldingReview.user_id == user_id,
            HoldingReview.reviewed_at >= cutoff,
        )
        .order_by(HoldingReview.reviewed_at.desc(), HoldingReview.id.desc())
    ).scalars().all()
    latest_review: dict[str, HoldingReview] = {}
    for row in reviews:
        latest_review.setdefault((row.symbol or "").upper(), row)
    earnings_checks = session.execute(
        select(EarningsCoverageReceipt)
        .where(
            EarningsCoverageReceipt.user_id == user_id,
            EarningsCoverageReceipt.checked_at >= cutoff,
        )
        .order_by(
            EarningsCoverageReceipt.checked_at.desc(),
            EarningsCoverageReceipt.id.desc(),
        )
    ).scalars().all()
    latest_calendar_check: dict[str, EarningsCoverageReceipt] = {}
    latest_filing_check: dict[str, EarningsCoverageReceipt] = {}
    for row in earnings_checks:
        ticker = (row.ticker or "").upper()
        if row.provider == "yfinance":
            latest_calendar_check.setdefault(ticker, row)
        elif row.provider == "sec_edgar":
            latest_filing_check.setdefault(ticker, row)
    lot_symbols = {
        str(symbol or "").upper()
        for symbol in session.execute(
            select(Lot.ticker).where(Lot.user_id == user_id)
        ).scalars().all()
        if symbol
    }

    per_symbol: list[dict[str, Any]] = []
    for symbol in symbols:
        matching = [row for row in signals if symbol in _tickers(row.parsed_tickers)]
        earnings = [row for row in matching if "earnings" in _keywords(row)]
        latest = matching[0] if matching else None
        latest_earnings = earnings[0] if earnings else None
        review = latest_review.get(symbol)
        review_payload = _review_payload(review)
        verification = _verification(review)
        calendar_check = latest_calendar_check.get(symbol)
        filing_check = latest_filing_check.get(symbol)
        event_candidates = [
            row.latest_reported_at
            for row in (calendar_check, filing_check)
            if row is not None and row.latest_reported_at is not None
        ]
        event_at = max(event_candidates, key=_time_rank) if event_candidates else None
        event_after_review = (
            event_at is not None
            and (
                review is None
                or _time_rank(review.reviewed_at) < _time_rank(event_at)
            )
        )
        per_symbol.append(
            {
                "ticker": symbol,
                "latest_news_at": latest.received_at.isoformat() if latest else None,
                "latest_news_source": latest.source if latest else None,
                "latest_news_sentiment": latest.sentiment if latest else None,
                "latest_earnings_signal_at": (
                    latest_earnings.received_at.isoformat() if latest_earnings else None
                ),
                "earnings_calendar_checked_at": (
                    calendar_check.checked_at.isoformat()
                    if calendar_check is not None
                    else None
                ),
                "earnings_calendar_status": (
                    calendar_check.status if calendar_check is not None else "missing"
                ),
                "earnings_calendar_source_url": (
                    calendar_check.source_url if calendar_check is not None else None
                ),
                "latest_reported_earnings_at": (
                    event_at.isoformat() if event_at is not None else None
                ),
                "next_scheduled_earnings_at": (
                    calendar_check.next_scheduled_at.isoformat()
                    if calendar_check is not None
                    and calendar_check.next_scheduled_at is not None
                    else None
                ),
                "earnings_calendar_error": (
                    calendar_check.error_message
                    if calendar_check is not None
                    else None
                ),
                "sec_filing_checked_at": (
                    filing_check.checked_at.isoformat()
                    if filing_check is not None
                    else None
                ),
                "sec_filing_status": (
                    filing_check.status if filing_check is not None else "missing"
                ),
                "sec_filing_source_url": (
                    filing_check.source_url if filing_check is not None else None
                ),
                "latest_sec_filing_at": (
                    filing_check.latest_reported_at.isoformat()
                    if filing_check is not None
                    and filing_check.latest_reported_at is not None
                    else None
                ),
                "sec_filing_error": (
                    filing_check.error_message if filing_check is not None else None
                ),
                "earnings_review_gap": (
                    "A reported earnings event is newer than the latest holdings "
                    "review. Argosy has not yet produced a post-earnings verdict."
                    if event_after_review
                    else None
                ),
                "latest_review_at": review.reviewed_at.isoformat() if review else None,
                "latest_review_verdict": review.verdict if review else None,
                "latest_review_outcome": review.outcome if review else None,
                "latest_review_reason": review.reason if review else None,
                "verification_verdict": (
                    verification.get("verdict") if verification else None
                ),
                "verification_reason": (
                    verification.get("reason") if verification else None
                ),
                "position_usd": review.position_usd if review else None,
                "portfolio_weight_pct": review_payload.get("portfolio_weight_pct"),
                "portfolio_total_usd": review_payload.get("portfolio_total_usd"),
                "tax_lots_available": symbol in lot_symbols,
                "execution_blocker": (
                    "Tax lots/cost basis are missing, so after-tax sale proceeds "
                    "cannot be validated."
                    if review is not None
                    and review.verdict in {"SELL", "TRIM"}
                    and symbol not in lot_symbols
                    else None
                ),
            }
        )

    jobs: dict[str, dict[str, Any] | None] = {}
    for name in (
        "earnings_calendar_daily",
        "sec_earnings_daily",
        "news_daily",
        "holdings_review",
    ):
        row = session.execute(
            select(JobRun)
            .where(JobRun.job_name == name)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        jobs[name] = (
            {
                "status": row.status,
                "started_at": row.started_at.isoformat(),
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "error": row.error_message,
            }
            if row is not None
            else None
        )

    return {
        "as_of": now.isoformat(),
        "lookback_days": lookback_days,
        "summary": {
            "held_single_stocks": len(symbols),
            "with_recent_news": sum(row["latest_news_at"] is not None for row in per_symbol),
            "with_earnings_signal": sum(
                row["latest_earnings_signal_at"] is not None for row in per_symbol
            ),
            "with_recent_review": sum(row["latest_review_at"] is not None for row in per_symbol),
            "with_calendar_check": sum(
                row["earnings_calendar_status"] in {"ok", "empty"}
                for row in per_symbol
            ),
            "calendar_check_errors": sum(
                row["earnings_calendar_status"] == "error"
                for row in per_symbol
            ),
            "with_primary_filing_check": sum(
                row["sec_filing_status"] in {"ok", "empty"}
                for row in per_symbol
            ),
            "primary_filing_errors": sum(
                row["sec_filing_status"] == "error"
                for row in per_symbol
            ),
            "with_recent_primary_filing": sum(
                row["latest_sec_filing_at"] is not None
                and _time_rank(datetime.fromisoformat(row["latest_sec_filing_at"]))
                >= _time_rank(cutoff)
                for row in per_symbol
            ),
            "with_primary_filing_evidence": sum(
                row["latest_sec_filing_at"] is not None for row in per_symbol
            ),
            "recent_events_awaiting_review": sum(
                row["earnings_review_gap"] is not None for row in per_symbol
            ),
            "full_earnings_call_coverage": False,
            "coverage_gap": (
                "Per-ticker calendar and primary SEC results-filing receipts are "
                "connected. Issuer call-transcript receipts are not yet connected, "
                "so filing coverage must not be described as full call review."
            ),
        },
        "jobs": jobs,
        "holdings": per_symbol,
    }


__all__ = ["build_news_coverage"]


def _time_rank(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()
