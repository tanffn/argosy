"""Versioned S&P 500-relative evidence for evaluated recommendations.

This module does arithmetic, not investment judgment.  It compares the
persisted subject outcome with a same-period SPY adjusted-EOD proxy and stores
the comparison separately so neither the original prediction nor its absolute
outcome is rewritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.predictions.evaluator import (
    Bar,
    EvaluatorAdapterError,
    PriceFetcher,
    _normalize_rows,
)
from argosy.state.models import (
    Prediction,
    PredictionBenchmarkOutcome,
    PredictionOutcome,
)

_log = get_logger("argosy.services.predictions.benchmark")

BENCHMARK_SYMBOL = "SPY"
BENCHMARK_VERSION = "spy_adjusted_eod_previous_close_v1"
_AVOID_RECOMMENDATIONS = frozenset({"SELL", "TRIM", "WATCH", "PASS", "NO-GO", "NO_GO"})


@dataclass
class BenchmarkSummary:
    compared: int = 0
    skipped_existing: int = 0
    missing_price_data: int = 0
    adapter_errors: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "benchmark_version": BENCHMARK_VERSION,
            "compared": self.compared,
            "skipped_existing": self.skipped_existing,
            "missing_price_data": self.missing_price_data,
            "adapter_errors": self.adapter_errors,
        }


def _source_ref(prediction: Prediction) -> dict[str, Any]:
    try:
        value = json.loads(prediction.source_ref or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _comparison_mode(prediction: Prediction) -> str:
    ref = _source_ref(prediction)
    recommendation = str(ref.get("verdict") or ref.get("action") or "").upper()
    if prediction.direction == "short" or recommendation in _AVOID_RECOMMENDATIONS:
        return "avoid_vs_benchmark"
    return "own_vs_benchmark"


def _raw_subject_return(prediction: Prediction, outcome: PredictionOutcome) -> float:
    signed = float(outcome.pnl_pct)
    return -signed if prediction.direction == "short" else signed


def _latest_bar_before(bars: list[Bar], cutoff: date) -> Bar | None:
    eligible = [bar for bar in bars if bar.bar_date < cutoff]
    return eligible[-1] if eligible else None


def _latest_bar_through(bars: list[Bar], cutoff: date) -> Bar | None:
    eligible = [bar for bar in bars if bar.bar_date <= cutoff]
    return eligible[-1] if eligible else None


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 6)))


def default_benchmark_price_fetcher(
    ticker: str, start: date, end: date
) -> list[Bar] | None:
    """Fetch explicit dividend/split-adjusted EOD bars for the benchmark."""

    try:
        import asyncio

        from argosy.adapters import MissingDataSourceError
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
    except ImportError as exc:  # pragma: no cover - production dependency
        raise EvaluatorAdapterError(f"yfinance import failed: {exc}") from exc

    try:
        payload = asyncio.run(
            YFinanceAdapter().get_eod_prices(
                [ticker], start, end, auto_adjust=True, ttl_seconds=0
            )
        )
    except MissingDataSourceError:
        return None
    except Exception as exc:  # pragma: no cover - transient provider failure
        raise EvaluatorAdapterError(
            f"adjusted benchmark fetch failed for {ticker}: {exc}"
        ) from exc
    return _normalize_rows(payload.get(ticker) or [])


def ensure_prediction_benchmark_outcomes(
    session: Session,
    *,
    user_id: str | None = None,
    price_fetcher: PriceFetcher = default_benchmark_price_fetcher,
    batch_size: int = 500,
) -> BenchmarkSummary:
    """Backfill missing SPY comparisons for persisted numeric outcomes.

    Entry is SPY's last close strictly before the recommendation date. This
    avoids using a same-day close that was unknowable for an intraday call.
    Exit is the last close through the subject outcome's exit date.  A single
    broad SPY request serves the whole batch; reruns are idempotent under the
    unique ``(outcome, symbol, version)`` key.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    comparison_exists = (
        select(PredictionBenchmarkOutcome.id)
        .where(
            PredictionBenchmarkOutcome.prediction_outcome_id
            == PredictionOutcome.id
        )
        .where(PredictionBenchmarkOutcome.benchmark_symbol == BENCHMARK_SYMBOL)
        .where(PredictionBenchmarkOutcome.benchmark_version == BENCHMARK_VERSION)
        .exists()
    )
    stmt = (
        select(PredictionOutcome, Prediction)
        .join(Prediction, Prediction.id == PredictionOutcome.prediction_id)
        .where(PredictionOutcome.pnl_pct.is_not(None))
        .where(Prediction.ticker.is_not(None))
        .where(~comparison_exists)
        .order_by(PredictionOutcome.evaluated_at.asc(), PredictionOutcome.id.asc())
        .limit(batch_size)
    )
    if user_id is not None:
        stmt = stmt.where(Prediction.user_id == user_id)
    pending = list(session.execute(stmt).all())
    summary = BenchmarkSummary()
    if not pending:
        return summary

    entry_cutoffs = [prediction.event_at.date() for _, prediction in pending]
    exit_cutoffs = [
        outcome.exit_trigger_date or prediction.evaluation_due_at.date()
        for outcome, prediction in pending
    ]
    fetch_start = min(entry_cutoffs) - timedelta(days=10)
    # YFinance's end is exclusive; the +1 also remains safe for injected
    # inclusive fetchers because rows are sliced again below.
    fetch_end = max(exit_cutoffs) + timedelta(days=1)
    try:
        fetched = price_fetcher(BENCHMARK_SYMBOL, fetch_start, fetch_end)
    except EvaluatorAdapterError as exc:
        summary.adapter_errors = len(pending)
        _log.warning("prediction_benchmark.fetch_failed", error=str(exc)[:240])
        return summary
    except Exception as exc:  # noqa: BLE001 - preserve next-run retry
        summary.adapter_errors = len(pending)
        _log.warning("prediction_benchmark.fetch_failed", error=str(exc)[:240])
        return summary

    bars = sorted(list(fetched or []), key=lambda bar: bar.bar_date)
    if not bars:
        summary.missing_price_data = len(pending)
        return summary

    for outcome, prediction in pending:
        entry_cutoff = prediction.event_at.date()
        exit_cutoff = outcome.exit_trigger_date or prediction.evaluation_due_at.date()
        entry_bar = _latest_bar_before(bars, entry_cutoff)
        exit_bar = _latest_bar_through(bars, exit_cutoff)
        if entry_bar is None or exit_bar is None or exit_bar.bar_date < entry_bar.bar_date:
            summary.missing_price_data += 1
            continue

        benchmark_return = (exit_bar.close - entry_bar.close) / entry_bar.close
        subject_return = _raw_subject_return(prediction, outcome)
        mode = _comparison_mode(prediction)
        if mode == "avoid_vs_benchmark":
            excess = benchmark_return - subject_return
        else:
            excess = subject_return - benchmark_return

        session.add(
            PredictionBenchmarkOutcome(
                prediction_outcome_id=int(outcome.id),
                benchmark_symbol=BENCHMARK_SYMBOL,
                benchmark_version=BENCHMARK_VERSION,
                comparison_mode=mode,
                benchmark_start_date=entry_bar.bar_date,
                benchmark_end_date=exit_bar.bar_date,
                benchmark_entry_price=Decimal(str(entry_bar.close)),
                benchmark_exit_price=Decimal(str(exit_bar.close)),
                benchmark_return_pct=_decimal(benchmark_return),
                subject_return_pct=_decimal(subject_return),
                decision_excess_return_pct=_decimal(excess),
                evidence_json=json.dumps(
                    {
                        "proxy": "SPY",
                        "price_basis": "adjusted EOD from Yahoo Finance",
                        "entry_convention": "last close strictly before recommendation date",
                        "exit_convention": "last close through subject outcome exit date",
                        "pre_tax": True,
                    },
                    sort_keys=True,
                ),
            )
        )
        summary.compared += 1

    session.flush()
    return summary


__all__ = [
    "BENCHMARK_SYMBOL",
    "BENCHMARK_VERSION",
    "BenchmarkSummary",
    "default_benchmark_price_fetcher",
    "ensure_prediction_benchmark_outcomes",
]
