"""Read model for recommendation counterfactuals, independent of execution."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.current_recommendations import AUTONOMOUS_PROPOSAL_SOURCES
from argosy.services.predictions.writers import DEEP_DECISION_VERDICT_SOURCE
from argosy.state.models import (
    ActionProposal,
    Prediction,
    PredictionOutcome,
    Proposal,
    ScanState,
    Verdict,
)

_ACTIONABLE = frozenset({"BUY", "ADD", "SELL", "TRIM"})
_WIN = frozenset({"hit_target", "expired_positive"})
_MISS = frozenset({"hit_stop", "expired_negative"})


def _json(value: str | None) -> dict[str, Any]:
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def _lifecycle(proposal: Proposal | None) -> str:
    if proposal is None:
        return "verdict_only"
    return {
        "executed_live": "executed",
        "filled": "executed",
        "rejected": "declined",
        "expired": "not_bought",
        "cancelled": "reconciled_not_selected",
        "awaiting_human": "open",
        "approved": "approved",
        "cooling": "open",
    }.get(str(proposal.status or "").lower(), str(proposal.status or "unknown"))


def _surfaced_lifecycle(proposal: ActionProposal | None) -> str:
    if proposal is None:
        return "recommendation_recorded"
    expires_at = proposal.expires_at
    if (
        str(proposal.status or "").lower() == "open"
        and expires_at is not None
        and _time_rank(expires_at) <= _time_rank(datetime.now(UTC))
    ):
        return "not_bought"
    return {
        "open": "not_bought_yet",
        "accepted": "accepted_not_fill_confirmed",
        "rejected": "declined",
        "expired": "not_bought",
        "cancelled": "not_bought",
        "canceled": "not_bought",
        "superseded": "reconciled_not_selected",
    }.get(str(proposal.status or "").lower(), str(proposal.status or "unknown"))


def _grade(kind: str | None) -> str:
    if kind in _WIN:
        return "win"
    if kind in _MISS:
        return "miss"
    if kind == "expired_neutral":
        return "neutral"
    return "pending"


def _time_rank(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def build_recommendation_scorecard(
    session: Session,
    *,
    user_id: str,
    recent_limit: int = 40,
) -> dict[str, Any]:
    """Return all recommendation clocks plus decision/execution disposition.

    A declined or expired recommendation remains fully scoreable.  Proposal
    status is presentation metadata only; it never gates outcome evaluation.
    """

    verdicts = session.execute(
        select(Verdict).where(
            Verdict.user_id == user_id,
            Verdict.verdict.in_(sorted(_ACTIONABLE)),
        )
    ).scalars().all()
    verdict_by_id = {int(row.id): row for row in verdicts}
    run_ids = {
        int(row.source_decision_run_id)
        for row in verdicts
        if row.source_decision_run_id is not None
    }
    proposals = session.execute(
        select(Proposal).where(
            Proposal.user_id == user_id,
            Proposal.source.in_(AUTONOMOUS_PROPOSAL_SOURCES),
            Proposal.action.in_(("buy", "sell")),
        )
    ).scalars().all()
    proposal_by_run: dict[int, Proposal] = {}
    for row in proposals:
        if row.decision_run_id is None or int(row.decision_run_id) not in run_ids:
            continue
        prior = proposal_by_run.get(int(row.decision_run_id))
        if prior is None or int(row.id) > int(prior.id):
            proposal_by_run[int(row.decision_run_id)] = row

    predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == DEEP_DECISION_VERDICT_SOURCE,
            Prediction.archived == 0,
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    surfaced_predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == "signal_stream:order_sheet",
            Prediction.archived == 0,
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    radar_predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == "signal_stream:radar_observation",
            Prediction.archived == 0,
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    eligible_radar_tickers = {
        str(ticker or "").upper()
        for ticker in session.execute(
            select(ScanState.ticker).where(
                ScanState.user_id == user_id,
                ScanState.status == "active",
            )
        ).scalars()
        if str(ticker or "").strip()
    }
    clocked_radar_tickers = {
        str(row.ticker or "").upper()
        for row in radar_predictions
        if int(
            _json(row.source_ref).get("horizon_days")
            or row.timeframe_days
            or 0
        ) == 180
        and str(row.ticker or "").strip()
    }
    eligible_with_clock = eligible_radar_tickers & clocked_radar_tickers
    missing_radar_clocks = sorted(
        eligible_radar_tickers - clocked_radar_tickers
    )
    prediction_ids = [
        int(row.id)
        for row in [*predictions, *surfaced_predictions, *radar_predictions]
    ]
    outcomes = (
        session.execute(
            select(PredictionOutcome).where(
                PredictionOutcome.prediction_id.in_(prediction_ids)
            )
        ).scalars().all()
        if prediction_ids
        else []
    )
    latest_outcome: dict[int, PredictionOutcome] = {}
    for row in outcomes:
        prior = latest_outcome.get(int(row.prediction_id))
        rank = (_time_rank(row.evaluated_at), int(row.id or 0))
        prior_rank = (
            (_time_rank(prior.evaluated_at), int(prior.id or 0))
            if prior is not None
            else None
        )
        if prior_rank is None or rank > prior_rank:
            latest_outcome[int(row.prediction_id)] = row

    lines: list[dict[str, Any]] = []
    horizons_by_verdict: dict[int, set[int]] = defaultdict(set)
    for prediction in predictions:
        ref = _json(prediction.source_ref)
        try:
            verdict_id = int(ref.get("verdict_id"))
        except (TypeError, ValueError):
            continue
        verdict = verdict_by_id.get(verdict_id)
        if verdict is None:
            continue
        horizon = int(ref.get("horizon_days") or prediction.timeframe_days or 30)
        horizon = 180 if horizon >= 180 else 30
        horizons_by_verdict[verdict_id].add(horizon)
        proposal = (
            proposal_by_run.get(int(verdict.source_decision_run_id))
            if verdict.source_decision_run_id is not None
            else None
        )
        outcome = latest_outcome.get(int(prediction.id))
        kind = outcome.outcome_kind if outcome is not None else None
        pnl = float(outcome.pnl_pct) if outcome is not None and outcome.pnl_pct is not None else None
        raw_move = None
        if pnl is not None:
            raw_move = (-pnl if prediction.direction == "short" else pnl) * 100.0
        lines.append(
            {
                "prediction_id": int(prediction.id),
                "verdict_id": verdict_id,
                "proposal_id": int(proposal.id) if proposal is not None else None,
                "ticker": verdict.subject,
                "action": verdict.verdict,
                "recommended_at": prediction.event_at.isoformat(),
                "horizon_days": horizon,
                "due_at": prediction.evaluation_due_at.isoformat(),
                "disposition": _lifecycle(proposal),
                "proposal_status": proposal.status if proposal is not None else None,
                "outcome_kind": kind,
                "grade": _grade(kind),
                "signed_pnl_pct": pnl * 100.0 if pnl is not None else None,
                "ticker_move_pct": raw_move,
                "evaluated_at": (
                    outcome.evaluated_at.isoformat()
                    if outcome is not None and outcome.evaluated_at is not None
                    else None
                ),
            }
        )

    surfaced_refs = [_json(row.source_ref) for row in surfaced_predictions]
    surfaced_proposal_ids = {
        int(ref["proposal_id"])
        for ref in surfaced_refs
        if ref.get("proposal_id") is not None
    }
    surfaced_proposals = (
        session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.id.in_(surfaced_proposal_ids),
            )
        ).scalars().all()
        if surfaced_proposal_ids
        else []
    )
    surfaced_by_id = {int(row.id): row for row in surfaced_proposals}
    surfaced_lines: list[dict[str, Any]] = []
    for prediction, ref in zip(surfaced_predictions, surfaced_refs, strict=True):
        # The order sheet also carries its authored catalyst/expectation clock.
        # This scorecard's comparable cohort is the explicit independent 180d
        # counterfactual, so do not double-count the same displayed line.
        if int(ref.get("horizon_days") or 0) != 180:
            continue
        try:
            proposal_id = int(ref.get("proposal_id"))
        except (TypeError, ValueError):
            proposal_id = None
        horizon = int(ref.get("horizon_days") or prediction.timeframe_days or 30)
        horizon = 180 if horizon >= 180 else 30
        proposal = surfaced_by_id.get(proposal_id) if proposal_id is not None else None
        outcome = latest_outcome.get(int(prediction.id))
        kind = outcome.outcome_kind if outcome is not None else None
        pnl = (
            float(outcome.pnl_pct)
            if outcome is not None and outcome.pnl_pct is not None
            else None
        )
        raw_move = None
        if pnl is not None:
            raw_move = (-pnl if prediction.direction == "short" else pnl) * 100.0
        surfaced_lines.append(
            {
                "prediction_id": int(prediction.id),
                "proposal_id": proposal_id,
                "ticker": prediction.ticker,
                "action": str(ref.get("action") or "").upper(),
                "recommended_at": prediction.event_at.isoformat(),
                "horizon_days": horizon,
                "due_at": prediction.evaluation_due_at.isoformat(),
                "disposition": _surfaced_lifecycle(proposal),
                "proposal_status": proposal.status if proposal is not None else None,
                "outcome_kind": kind,
                "grade": _grade(kind),
                "signed_pnl_pct": pnl * 100.0 if pnl is not None else None,
                "ticker_move_pct": raw_move,
                "evaluated_at": (
                    outcome.evaluated_at.isoformat()
                    if outcome is not None and outcome.evaluated_at is not None
                    else None
                ),
            }
        )

    horizon_stats: dict[str, dict[str, Any]] = {}
    for horizon in (30, 180):
        cohort = [row for row in lines if row["horizon_days"] == horizon]
        graded = [row for row in cohort if row["grade"] != "pending"]
        wins = sum(row["grade"] == "win" for row in graded)
        misses = sum(row["grade"] == "miss" for row in graded)
        neutrals = sum(row["grade"] == "neutral" for row in graded)
        pnls = [float(row["signed_pnl_pct"]) for row in graded if row["signed_pnl_pct"] is not None]
        horizon_stats[f"{horizon}d"] = {
            "scheduled": len(cohort),
            "graded": len(graded),
            "wins": wins,
            "misses": misses,
            "neutral": neutrals,
            "win_rate": wins / (wins + misses) if wins + misses else None,
            "avg_signed_pnl_pct": sum(pnls) / len(pnls) if pnls else None,
        }

    surfaced_horizon_stats: dict[str, dict[str, Any]] = {}
    for horizon in (30, 180):
        cohort = [row for row in surfaced_lines if row["horizon_days"] == horizon]
        graded = [row for row in cohort if row["grade"] != "pending"]
        wins = sum(row["grade"] == "win" for row in graded)
        misses = sum(row["grade"] == "miss" for row in graded)
        neutrals = sum(row["grade"] == "neutral" for row in graded)
        pnls = [
            float(row["signed_pnl_pct"])
            for row in graded
            if row["signed_pnl_pct"] is not None
        ]
        surfaced_horizon_stats[f"{horizon}d"] = {
            "scheduled": len(cohort),
            "graded": len(graded),
            "wins": wins,
            "misses": misses,
            "neutral": neutrals,
            "win_rate": wins / (wins + misses) if wins + misses else None,
            "avg_signed_pnl_pct": sum(pnls) / len(pnls) if pnls else None,
        }

    radar_lines: list[dict[str, Any]] = []
    for prediction in radar_predictions:
        ref = _json(prediction.source_ref)
        if int(ref.get("horizon_days") or prediction.timeframe_days or 0) != 180:
            continue
        outcome = latest_outcome.get(int(prediction.id))
        kind = outcome.outcome_kind if outcome is not None else None
        grade = _grade(kind)
        recommended = any(
            verdict.subject.upper() == str(prediction.ticker or "").upper()
            and verdict.verdict.upper() in {"BUY", "ADD"}
            and 0 <= (_time_rank(verdict.created_at) - _time_rank(prediction.event_at)) <= 7 * 86400
            for verdict in verdicts
        )
        radar_lines.append({
            "prediction_id": int(prediction.id),
            "ticker": prediction.ticker,
            "observed_at": prediction.event_at.isoformat(),
            "due_at": prediction.evaluation_due_at.isoformat(),
            "recommended": recommended,
            "grade": grade,
            "outcome_kind": kind,
            "ticker_move_pct": (
                float(outcome.pnl_pct) * 100.0
                if outcome is not None and outcome.pnl_pct is not None
                else None
            ),
        })
    radar_graded = [row for row in radar_lines if row["grade"] != "pending"]
    radar_winners = [row for row in radar_graded if row["grade"] == "win"]
    missed_radar_winners = [row for row in radar_winners if not row["recommended"]]

    verdict_ids_with_any = set(horizons_by_verdict)
    proposal_run_ids = {
        int(row.decision_run_id)
        for row in proposals
        if row.decision_run_id is not None
    }
    legacy_without_verdict = sum(
        row.decision_run_id is None or int(row.decision_run_id) not in {
            int(v.source_decision_run_id)
            for v in verdicts
            if v.source_decision_run_id is not None
        }
        for row in proposals
    )
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "horizons": horizon_stats,
        "recent": lines[: max(1, int(recent_limit))],
        "surfaced_order_sheets": {
            "horizons": surfaced_horizon_stats,
            "recent": surfaced_lines[: max(1, int(recent_limit))],
        },
        "radar_opportunities": {
            "scheduled_180d": len(radar_lines),
            "graded_180d": len(radar_graded),
            "winners_180d": len(radar_winners),
            "recommended_winners_180d": sum(
                bool(row["recommended"]) for row in radar_winners
            ),
            "missed_winners_180d": len(missed_radar_winners),
            "recent_missed_winners": missed_radar_winners[:10],
        },
        "coverage": {
            "actionable_verdicts": len(verdicts),
            "with_any_forecast": len(verdict_ids_with_any),
            "with_30d_forecast": sum(30 in value for value in horizons_by_verdict.values()),
            "with_180d_forecast": sum(180 in value for value in horizons_by_verdict.values()),
            "autonomous_proposals": len(proposals),
            "legacy_proposals_without_verdict_link": int(legacy_without_verdict),
            "linked_decision_runs": len(proposal_run_ids),
            "eligible_radar_names": len(eligible_radar_tickers),
            "radar_names_with_180d_clock": len(eligible_with_clock),
            "radar_clock_coverage_pct": (
                len(eligible_with_clock) / len(eligible_radar_tickers) * 100.0
                if eligible_radar_tickers
                else None
            ),
            "radar_clock_status": (
                "empty"
                if not eligible_radar_tickers
                else "complete"
                if not missing_radar_clocks
                else "partial"
            ),
            "radar_names_missing_180d_clock": missing_radar_clocks[:50],
            "universe_recall_status": "radar_only",
            "universe_recall_reason": (
                (
                    "Every currently eligible radar name has a dated 180d clock. "
                    if eligible_radar_tickers and not missing_radar_clocks
                    else (
                        f"Only {len(eligible_with_clock)} of "
                        f"{len(eligible_radar_tickers)} currently eligible radar "
                        "names have a dated 180d clock. "
                        if eligible_radar_tickers
                        else "There are no currently eligible radar names to clock. "
                    )
                )
                + "Argosy can measure winners it saw but did not recommend only for "
                "clocked radar names; full-market winners no configured source surfaced "
                "remain outside measurable recall."
            ),
        },
    }


__all__ = ["build_recommendation_scorecard"]
