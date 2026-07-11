"""Bridge external nominations into radar state and the prediction ledger."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from argosy.services.predictions.writers import write_signal_stream_predictions
from argosy.services.signal_streams.base import SignalNomination
from argosy.services.trend_radar import LiquidityFilter, RawSignal, TrendCandidate
from argosy.state.models import ScanState


@dataclass
class NominationProcessSummary:
    active: int = 0
    quarantined: int = 0
    warning_only: int = 0
    predictions: int = 0
    candidates: list[TrendCandidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "quarantined": self.quarantined,
            "warning_only": self.warning_only,
            "predictions": self.predictions,
        }


def _candidate_for(
    nomination: SignalNomination,
) -> tuple[TrendCandidate, bool]:
    evidence = nomination.evidence
    price = evidence.get("price")
    market_cap = evidence.get("market_cap")
    average_volume = evidence.get("average_volume")
    raw = RawSignal(
        ticker=nomination.ticker.upper(),
        name=nomination.ticker.upper(),
        price=float(price) if price is not None else None,
        market_cap=float(market_cap) if market_cap is not None else None,
        avg_volume=(
            float(average_volume) if average_volume is not None else None
        ),
        families={f"SIGNAL_STREAM:{nomination.stream}"},
        reasons=[
            f"{nomination.stream}: {nomination.dedup_key}",
            f"strength={nomination.strength:.3f}",
        ],
    )
    liquidity = LiquidityFilter()
    payload = {
        "stream": nomination.stream,
        "dedup_key": nomination.dedup_key,
        "as_of": nomination.as_of.isoformat(),
        "direction": nomination.direction,
        "strength": nomination.strength,
        "evidence": nomination.evidence,
    }
    candidate = TrendCandidate(
        ticker=raw.ticker,
        name=raw.name,
        score=round(nomination.strength * 100, 1),
        families=tuple(raw.families),
        reasons=tuple(raw.reasons),
        price=raw.price,
        market_cap=raw.market_cap,
        dollar_volume=liquidity.dollar_volume(raw),
        pct_change=None,
        stream=nomination.stream,
        event_id=nomination.dedup_key,
        evidence=payload,
    )
    return candidate, liquidity.passes(raw)


def process_nominations(
    session: Session | None,
    *,
    user_id: str,
    nominations: Iterable[SignalNomination],
    persist: bool = True,
    observed_at: datetime | None = None,
) -> NominationProcessSummary:
    """Write predictions, routing only funnel-eligible nominations to radar."""
    observed_at = observed_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    else:
        observed_at = observed_at.astimezone(UTC)
    summary = NominationProcessSummary()
    for nomination in nominations:
        candidate_result = (
            _candidate_for(nomination)
            if nomination.route_to_funnel
            else None
        )
        if persist:
            if session is None:
                raise ValueError("session is required when persist=True")
            price = nomination.evidence.get("price")
            if price is None:
                raise ValueError(
                    f"{nomination.stream}:{nomination.dedup_key} has no entry price"
                )
            write_signal_stream_predictions(
                session,
                user_id,
                stream=nomination.stream,
                dedup_key=nomination.dedup_key,
                ticker=nomination.ticker,
                direction=nomination.direction,  # type: ignore[arg-type]
                event_at=observed_at,
                entry_price=float(price),
                evidence=nomination.evidence,
            )
            summary.predictions += 2

        if not nomination.route_to_funnel:
            summary.warning_only += 1
            continue

        assert candidate_result is not None
        candidate, liquid = candidate_result
        if candidate.evidence is not None:
            candidate.evidence["observed_at"] = observed_at.isoformat()
        summary.candidates.append(candidate)
        if liquid:
            summary.active += 1
        else:
            summary.quarantined += 1
        if not persist:
            continue
        assert session is not None
        row = session.get(
            ScanState, {"user_id": user_id, "ticker": candidate.ticker}
        )
        if row is None:
            row = ScanState(user_id=user_id, ticker=candidate.ticker)
            session.add(row)
        evidence_json = json.dumps(
            candidate.evidence, sort_keys=True, default=str
        )
        row.last_score = candidate.score
        row.radar_fingerprint = (
            f"s={candidate.score}|f={','.join(candidate.families)}"
            f"|stream={nomination.stream}|event={nomination.dedup_key}"
        )
        row.nomination_evidence_json = evidence_json
        row.status = "active" if liquid else "quarantined"
        row.quarantine_reason = "" if liquid else "failed-liquidity"
        row.last_radar_at = observed_at
        row.last_seen_at = observed_at
    if persist and session is not None:
        session.flush()
    return summary


__all__ = ["NominationProcessSummary", "process_nominations"]
