"""Read-only opportunity coverage and first-observation outcome audit.

This is not an alpha estimate: the cohort is the saved radar universe, not all
stocks, and price outcomes do not establish that an earlier decision was wrong.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.predictions.outcomes import authoritative_outcomes
from argosy.state.models import DecisionRun, Prediction, Proposal, ScanState


def _object(value: str | None) -> dict:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def build_discovery_audit(session: Session, *, user_id: str, horizon_days: int = 30) -> dict:
    """One first recorded fleet decision per ticker/horizon; otherwise radar-only.

    All saved outcomes, including archived predictions, remain accountable.
    Use the canonical active scoring-contract selector, not the most favorable
    historical version or every repeated daily observation as an independent bet.
    """
    if horizon_days not in (30, 180, 365):
        raise ValueError("horizon_days must be 30, 180 or 365")
    predictions = list(session.scalars(select(Prediction).where(
        Prediction.user_id == user_id,
        Prediction.source.in_(("signal_stream:radar_observation",
                               "signal_stream:discovery_evaluation")),
    ).order_by(Prediction.event_at, Prediction.id)))
    radar: dict[str, Prediction] = {}
    fleet: dict[str, Prediction] = {}
    fleet_any: dict[str, Prediction] = {}
    seen: dict[str, Prediction] = {}
    for prediction in predictions:
        if not prediction.ticker:
            continue
        seen.setdefault(prediction.ticker, prediction)
        if prediction.source.endswith(":discovery_evaluation"):
            fleet_any.setdefault(prediction.ticker, prediction)
        if prediction.timeframe_days != horizon_days:
            continue
        target = fleet if prediction.source.endswith(":discovery_evaluation") else radar
        target.setdefault(prediction.ticker, prediction)
    selected = [fleet.get(ticker) or fleet_any.get(ticker) or radar.get(ticker) or seen[ticker]
                for ticker in sorted(seen)]
    outcomes = authoritative_outcomes(session, [p for p in selected if p.timeframe_days == horizon_days])
    lifecycle: dict[str, Proposal] = {}
    for proposal in session.scalars(select(Proposal).where(
        Proposal.user_id == user_id, Proposal.action == "buy",
    ).order_by(Proposal.created_at, Proposal.id)):
        lifecycle[proposal.ticker] = proposal
    rows = []
    for prediction in selected:
        reference = _object(prediction.source_ref)
        result = outcomes.get(prediction.id)
        outcome = result[1] if result else None
        pnl = float(outcome.pnl_pct) if outcome is not None and outcome.pnl_pct is not None else None
        if pnl is not None and not math.isfinite(pnl):
            pnl = None
        verdict = reference.get("verdict") if prediction.ticker in fleet_any else None
        proposal = lifecycle.get(prediction.ticker)
        rows.append({
            "ticker": prediction.ticker,
            "first_seen_at": seen[prediction.ticker].event_at.isoformat(),
            "evaluated_decision_at": prediction.event_at.isoformat(),
            "prediction_id": prediction.id,
            "verdict": verdict,
            "stage": "fleet_reviewed" if verdict else "observed_without_fleet_grade",
            "rationale": reference.get("expectation") or reference.get("estimation"),
            "gross_return": pnl,
            "outcome_status": "missing_horizon_clock" if prediction.timeframe_days != horizon_days else "scored" if pnl is not None else (
                "unscorable" if outcome is not None else "pending_or_missing"
            ),
            "outcome_date": str(outcome.exit_trigger_date) if outcome is not None else None,
            "evaluation_method": outcome.evaluation_method if outcome is not None else None,
            "entry_price_policy": reference.get("entry_price_policy") or "legacy_or_unspecified",
            "entry_price_used": float(outcome.entry_price_used) if outcome is not None and outcome.entry_price_used is not None else None,
            "exit_price_used": float(outcome.exit_price_used) if outcome is not None and outcome.exit_price_used is not None else None,
            "latest_buy_proposal": None if proposal is None else {
                "id": proposal.id, "status": proposal.status,
                "created_at": proposal.created_at.isoformat(),
            },
        })
    scored = [row for row in rows if row["gross_return"] is not None]
    skipped = [row for row in scored if row["verdict"] in ("PASS", "WATCH")]
    scan = list(session.scalars(select(ScanState).where(ScanState.user_id == user_id)))
    for state in scan:
        if state.ticker in seen:
            continue
        # Memory can survive a failed telemetry insert. These are tracked
        # records without a dated clock, NOT fabricated historical predictions.
        rows.append({
            "ticker": state.ticker, "first_seen_at": state.created_at.isoformat(),
            "evaluated_decision_at": None, "prediction_id": None, "verdict": None,
            "stage": "tracked_without_observation_clock",
            "rationale": "Tracking memory exists but no dated observation clock is available",
            "gross_return": None, "outcome_status": "missing_observation_clock",
            "outcome_date": None, "evaluation_method": None,
            "entry_price_used": None, "exit_price_used": None, "latest_buy_proposal": None,
        })
    recent_research_rows = list(session.scalars(select(DecisionRun).where(
        DecisionRun.user_id == user_id,
        DecisionRun.decision_kind.in_(("discovery_research", "discovery_screen")),
        DecisionRun.started_at >= datetime.now(UTC) - timedelta(days=1),
    ).order_by(DecisionRun.started_at.desc())))
    # Show each subject's latest state, not a resolved failure from earlier in
    # the same day. Earlier attempts remain in the decision history.
    recent_by_ticker = {}
    scan_by_ticker = {row.ticker: row for row in scan}
    for run in recent_research_rows:
        state = scan_by_ticker.get(run.ticker)
        if run.decision_kind == "discovery_screen" and state and state.last_estimated_at:
            completed_at = state.last_estimated_at.replace(tzinfo=UTC) if state.last_estimated_at.tzinfo is None else state.last_estimated_at
            failed_at = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
            if completed_at > failed_at:
                continue  # a later real screen recovered; old failure remains history
        recent_by_ticker.setdefault(run.ticker, run)
    failed = [run for run in recent_by_ticker.values() if run.status in ("blocked", "cancelled", "failed")]
    running = any(run.status == "running" for run in recent_by_ticker.values())
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "horizon_days": horizon_days,
        "research_status": {
            "status": "incomplete" if failed else "running" if running else "completed" if recent_by_ticker else "unknown",
            "attempted": len(recent_by_ticker), "deferred": None,
            "failures": [{"ticker": run.ticker, "reason": _object(run.notes_json).get("blocked_reason")
                          or "Research did not complete; no investment verdict"} for run in failed],
        },
        "recent_research": [{
            "run_id": run.id, "ticker": run.ticker, "status": run.status,
            "started_at": run.started_at.isoformat(),
            "reason": _object(run.notes_json).get("blocked_reason"),
        } for run in recent_by_ticker.values()],
        "cohort": "first_saved_fleet_decision_per_ticker_else_first_radar_observation",
        "limitations": (
            "Observed names only, not all market winners or a survivorship-free backtest. "
            "Gross price diagnostics, not sized/after-tax portfolio performance or executable fills. "
            "Backfilled entries remain labelled in evaluation_method. Legacy records may use "
            "scan/radar observation prices, not executable recommendation-time entries. Short-term gains do not "
            "validate a long-term thesis. A latest proposal may precede or follow the scored decision."
        ),
        "coverage": {
            "unique_names": len(rows), "dated_names": len(seen),
            "tracked_without_clock": len(rows) - len(seen), "fleet_reviewed": len(fleet_any),
            "radar_without_fleet_grade": sum(row["verdict"] is None for row in rows),
            "scored": len(scored), "unscored": len(rows) - len(scored),
            "verdicts": dict(Counter(row["verdict"] or "NOT_REVIEWED" for row in rows)),
            "current_stages": dict(Counter(row.status for row in scan)),
            "skipped_then_rose": sum(row["gross_return"] > 0 for row in skipped),
            "skipped_then_fell": sum(row["gross_return"] < 0 for row in skipped),
        },
        "missed_upside_examples": sorted(
            (row for row in skipped if row["gross_return"] > 0),
            key=lambda row: row["gross_return"], reverse=True,
        )[:5],
        "avoided_decline_examples": sorted(
            (row for row in skipped if row["gross_return"] < 0),
            key=lambda row: row["gross_return"],
        )[:5],
        "rows": rows,
    }
