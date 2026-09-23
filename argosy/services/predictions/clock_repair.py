"""Audited arithmetic repair of legacy Alpha clocks, grounded in saved analyses."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import (
    AlphaReportAnalysis,
    AuditLog,
    EvaluationMethod,
    NewsSignal,
    Prediction,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def repair_alpha_prediction_horizons(session: Session) -> dict[str, int]:
    """Correct the known writer cap without changing any authored judgments.

    Caller owns the transaction. Refuse missing/contradictory provenance rather
    than infer intent. Preserve prediction IDs, event times, prices, archive state
    and every historical outcome; append before/after clock evidence to audit_log.
    Once corrected, repeated calls are no-ops. Requires scoring-contract migration
    so old checkpoint outcomes cannot remain eligible for current calibration.
    """
    candidates = session.scalars(select(Prediction).where(
        Prediction.source == "discord_alpha_report",
        Prediction.timeframe_days == 180,
        Prediction.evaluation_method == "fixed_lookahead_30d",
    )).all()
    if candidates:
        contract = session.scalar(select(EvaluationMethod.scoring_contract).where(
            EvaluationMethod.method_name == "fixed_lookahead_180d_entry_backfilled"
        ))
        if contract != "fixed_lookahead_180d":
            raise ValueError("Scoring-contract migration is required before repairing prediction clocks")
    for prediction in candidates:
        ref = json.loads(prediction.source_ref)
        analysis = session.get(AlphaReportAnalysis, int(ref["analysis_id"]))
        signal = session.get(NewsSignal, int(ref["news_signal_id"]))
        if (analysis is None or signal is None
                or analysis.user_id != prediction.user_id
                or analysis.news_signal_id != signal.id
                or _utc(signal.received_at) != _utc(prediction.event_at)):
            raise ValueError(f"Prediction {prediction.id}: cannot verify source clock provenance")
        ticker = str(prediction.ticker or "").upper()
        if ref.get("kind") == "signal":
            supported = any(
                str(item.get("ticker", "")).upper() == ticker
                and item.get("timeframe") == "long"
                for item in json.loads(analysis.ticker_signals_json)
            )
        elif ref.get("kind") == "pick":
            supported = any(
                str(item.get("ticker", "")).upper() == ticker
                for item in json.loads(analysis.structural_picks_json)
            )
        else:
            supported = False
        if not supported:
            raise ValueError(f"Prediction {prediction.id}: saved analysis does not support 180 days")
        if _utc(prediction.evaluation_due_at) != _utc(prediction.event_at) + timedelta(days=30):
            raise ValueError(f"Prediction {prediction.id}: clock differs from known legacy cap")
        before = {
            "timeframe_days": prediction.timeframe_days,
            "evaluation_method": prediction.evaluation_method,
            "evaluation_due_at": _utc(prediction.evaluation_due_at).isoformat(),
        }
        prediction.evaluation_method = "fixed_lookahead_180d"
        prediction.evaluation_due_at = _utc(prediction.event_at) + timedelta(days=180)
        session.add(AuditLog(
            user_id=prediction.user_id, event_type="prediction.clock_repaired",
            entity_type="prediction", entity_id=str(prediction.id),
            payload_json=json.dumps({
                "repair": "alpha_long_horizon_v1",
                "reason": "Legacy writer capped an authored 180-day prediction at 30 days",
                "analysis_id": analysis.id, "news_signal_id": signal.id,
                "before": before,
                "after": {"timeframe_days": 180,
                          "evaluation_method": prediction.evaluation_method,
                          "evaluation_due_at": prediction.evaluation_due_at.isoformat()},
                "historical_outcomes_preserved": True,
            }, sort_keys=True),
        ))
    session.flush()
    return {"repaired": len(candidates)}
