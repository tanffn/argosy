"""Historical recommendation counterfactuals, independent of execution.

Includes retention-archived recommendations so old calls remain accountable.
This historical report is not the active-only source-reliability training cohort.
Both surfaces require outcomes compatible with the prediction's current contract.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.current_recommendations import AUTONOMOUS_PROPOSAL_SOURCES
from argosy.services.predictions.benchmark import (
    BENCHMARK_SYMBOL,
    BENCHMARK_VERSION,
)
from argosy.services.predictions.proposal_clocks import (
    PROPOSAL_CLOCK_SOURCE,
    proposals_without_verdict,
)
from argosy.services.predictions.outcomes import authoritative_outcomes
from argosy.services.predictions.writers import (
    DEEP_DECISION_VERDICT_SOURCE,
    DISCOVERY_EVALUATION_SOURCE,
)
from argosy.state.models import (
    ActionProposal,
    Prediction,
    PredictionBenchmarkOutcome,
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


def _order_line_notional(
    proposal: ActionProposal | None, *, ticker: str, action: str
) -> float | None:
    if proposal is None:
        return None
    payload = _json(proposal.suggested_payload)
    sheet = payload.get("order_sheet")
    if not isinstance(sheet, dict):
        return None
    lines = sheet.get("lines")
    if not isinstance(lines, list):
        return None
    for line in lines:
        if not isinstance(line, dict):
            continue
        if str(line.get("symbol") or "").upper() != ticker.upper():
            continue
        if str(line.get("action") or "").upper() != action.upper():
            continue
        try:
            value = float(line.get("notional_usd"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


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


def _horizon_bucket(days: int) -> int:
    if days >= 365:
        return 365
    if days >= 180:
        return 180
    return 30


def _self_eval_grade(*, recommendation: str, outcome_kind: str | None) -> str:
    base = _grade(outcome_kind)
    if base == "pending":
        return base
    if recommendation in {"WATCH", "PASS", "NO-GO", "NO_GO"}:
        return {
            "win": "missed_opportunity",
            "miss": "correct_skip",
            "neutral": "inconclusive",
        }.get(base, base)
    return base


def _self_eval_report(
    *,
    ticker: str,
    recommendation: str,
    due_at: datetime,
    outcome: PredictionOutcome | None,
    grade: str,
    benchmark: PredictionBenchmarkOutcome | None = None,
) -> str:
    if outcome is None:
        return (
            f"Pending: Argosy will evaluate {recommendation} {ticker} "
            f"automatically on {due_at.date().isoformat()}."
        )
    if grade == "pending":
        return (
            f"Not scored: {recommendation} {ticker} lacks usable historical price evidence. "
            f"{outcome.notes or outcome.outcome_kind}. This is not a win or a loss."
        )
    entry = float(outcome.entry_price_used) if outcome.entry_price_used is not None else None
    exit_price = float(outcome.exit_price_used) if outcome.exit_price_used is not None else None
    pnl = float(outcome.pnl_pct) * 100.0 if outcome.pnl_pct is not None else None
    details: list[str] = []
    if entry is not None and exit_price is not None:
        details.append(f"price ${entry:,.2f} to ${exit_price:,.2f}")
    if pnl is not None:
        details.append(f"direction-adjusted return {pnl:+.1f}%")
    detail_text = "; ".join(details) if details else "price evidence unavailable"
    evaluated = outcome.evaluated_at.date().isoformat()
    report = (
        f"Evaluated {evaluated}: {recommendation} {ticker} was "
        f"{grade.replace('_', ' ')}; {detail_text}."
    )
    if benchmark is None:
        return report
    subject = float(benchmark.subject_return_pct) * 100.0
    spy = float(benchmark.benchmark_return_pct) * 100.0
    excess = float(benchmark.decision_excess_return_pct) * 100.0
    result = "beat" if excess > 0.1 else "lagged" if excess < -0.1 else "matched"
    return (
        f"{report} Against SPY over the same period: {ticker} {subject:+.1f}%, "
        f"SPY {spy:+.1f}%; the {recommendation} decision {result} the benchmark "
        f"by {abs(excess):.1f} percentage points."
    )


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
    proposal_by_run: dict[tuple[int, str], Proposal] = {}
    for row in proposals:
        if row.decision_run_id is None or int(row.decision_run_id) not in run_ids:
            continue
        key = (int(row.decision_run_id), row.ticker.upper())
        prior = proposal_by_run.get(key)
        if prior is None or int(row.id) > int(prior.id):
            proposal_by_run[key] = row

    legacy_proposals = proposals_without_verdict(session, user_id=user_id)
    legacy_by_id = {int(row.id): row for row in legacy_proposals}
    legacy_predictions = list(session.scalars(select(Prediction).where(
        Prediction.user_id == user_id,
        Prediction.source == PROPOSAL_CLOCK_SOURCE,
    )))
    # If actual verdict lineage is later restored, its clocks are authoritative.
    # Keep historical proposal clocks in DB but do not double-count the call.
    legacy_predictions = [row for row in legacy_predictions
                          if _json(row.source_ref).get("proposal_id") in legacy_by_id]
    legacy_horizons: dict[int, set[int]] = defaultdict(set)
    for row in legacy_predictions:
        legacy_horizons[int(_json(row.source_ref)["proposal_id"])].add(int(row.timeframe_days or 0))

    predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == DEEP_DECISION_VERDICT_SOURCE,
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    surfaced_predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == "signal_stream:order_sheet",
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    radar_predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == "signal_stream:radar_observation",
        )
        .order_by(Prediction.event_at.desc(), Prediction.id.desc())
    ).scalars().all()
    discovery_predictions = session.execute(
        select(Prediction)
        .where(
            Prediction.user_id == user_id,
            Prediction.source == DISCOVERY_EVALUATION_SOURCE,
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
    scored_predictions = [
            *predictions,
            *surfaced_predictions,
            *radar_predictions,
            *discovery_predictions,
            *legacy_predictions,
    ]
    latest_outcome = {
        key: item[1] for key, item in authoritative_outcomes(session, scored_predictions).items()
    }
    outcome_ids = [int(row.id) for row in latest_outcome.values()]
    benchmark_rows = (
        session.execute(
            select(PredictionBenchmarkOutcome).where(
                PredictionBenchmarkOutcome.prediction_outcome_id.in_(outcome_ids),
                PredictionBenchmarkOutcome.benchmark_symbol == BENCHMARK_SYMBOL,
                PredictionBenchmarkOutcome.benchmark_version == BENCHMARK_VERSION,
            )
        ).scalars().all()
        if outcome_ids
        else []
    )
    benchmark_by_outcome_id = {
        int(row.prediction_outcome_id): row for row in benchmark_rows
    }

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
        horizon = _horizon_bucket(
            int(ref.get("horizon_days") or prediction.timeframe_days or 30)
        )
        horizons_by_verdict[verdict_id].add(horizon)
        proposal = (
            proposal_by_run.get((int(verdict.source_decision_run_id), verdict.subject.upper()))
            if verdict.source_decision_run_id is not None
            else None
        )
        outcome = latest_outcome.get(int(prediction.id))
        benchmark = (
            benchmark_by_outcome_id.get(int(outcome.id))
            if outcome is not None
            else None
        )
        kind = outcome.outcome_kind if outcome is not None else None
        pnl = (
            float(outcome.pnl_pct)
            if outcome is not None and outcome.pnl_pct is not None
            else None
        )
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
                "benchmark_symbol": (
                    benchmark.benchmark_symbol if benchmark is not None else None
                ),
                "benchmark_return_pct": (
                    float(benchmark.benchmark_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
                "decision_excess_return_pct": (
                    float(benchmark.decision_excess_return_pct) * 100.0
                    if benchmark is not None
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
        if int(ref.get("horizon_days") or 0) not in {180, 365}:
            continue
        try:
            proposal_id = int(ref.get("proposal_id"))
        except (TypeError, ValueError):
            proposal_id = None
        horizon = _horizon_bucket(
            int(ref.get("horizon_days") or prediction.timeframe_days or 30)
        )
        proposal = surfaced_by_id.get(proposal_id) if proposal_id is not None else None
        action = str(ref.get("action") or "").upper()
        outcome = latest_outcome.get(int(prediction.id))
        benchmark = (
            benchmark_by_outcome_id.get(int(outcome.id))
            if outcome is not None
            else None
        )
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
                "action": action,
                "notional_usd": _order_line_notional(
                    proposal,
                    ticker=str(prediction.ticker or ""),
                    action=action,
                ),
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
                "benchmark_symbol": (
                    benchmark.benchmark_symbol if benchmark is not None else None
                ),
                "benchmark_return_pct": (
                    float(benchmark.benchmark_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
                "decision_excess_return_pct": (
                    float(benchmark.decision_excess_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
            }
        )

    horizon_stats: dict[str, dict[str, Any]] = {}
    for horizon in (30, 180, 365):
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
    for horizon in (30, 180, 365):
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

    shadow_sheets: list[dict[str, Any]] = []
    surfaced_groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in surfaced_lines:
        if row["proposal_id"] is None:
            continue
        surfaced_groups[(int(row["proposal_id"]), int(row["horizon_days"]))].append(row)
    for (proposal_id, horizon), group in surfaced_groups.items():
        buys = [
            row
            for row in group
            if row["action"] in {"BUY", "ADD"}
            and row["notional_usd"] is not None
            and row["decision_excess_return_pct"] is not None
            and row["ticker_move_pct"] is not None
            and row["benchmark_return_pct"] is not None
        ]
        capital = sum(float(row["notional_usd"]) for row in buys)
        if not buys or capital <= 0:
            continue
        weighted_subject = sum(
            float(row["ticker_move_pct"]) * float(row["notional_usd"]) / capital
            for row in buys
        )
        weighted_benchmark = sum(
            float(row["benchmark_return_pct"]) * float(row["notional_usd"]) / capital
            for row in buys
        )
        weighted_excess = weighted_subject - weighted_benchmark
        shadow_sheets.append({
            "proposal_id": proposal_id,
            "recommended_at": max(str(row["recommended_at"]) for row in buys),
            "horizon_days": horizon,
            "allocated_usd": capital,
            "line_count": len(buys),
            "weighted_subject_return_pct": weighted_subject,
            "weighted_benchmark_return_pct": weighted_benchmark,
            "weighted_excess_return_pct": weighted_excess,
            "grade": (
                "beat" if weighted_excess > 0.1 else "lag"
                if weighted_excess < -0.1 else "tie"
            ),
        })
    shadow_sheets.sort(
        key=lambda row: (row["recommended_at"], row["proposal_id"]), reverse=True
    )

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

    self_evaluations: list[dict[str, Any]] = []
    self_eval_sources = [
        *(('portfolio_verdict', row) for row in predictions),
        *(('trade_plan', row) for row in surfaced_predictions),
        *(('discovery', row) for row in discovery_predictions),
        *(('legacy_proposal', row) for row in legacy_predictions),
    ]
    for source_label, prediction in self_eval_sources:
        ref = _json(prediction.source_ref)
        verdict = None
        recommendation = str(
            ref.get("verdict") or ref.get("action") or ""
        ).upper()
        conviction = ref.get("conviction")
        expectation = str(
            ref.get("expectation") or ref.get("estimation") or ""
        ).strip()
        disposition = "recommendation_recorded"
        if source_label == "portfolio_verdict":
            try:
                verdict = verdict_by_id.get(int(ref.get("verdict_id")))
            except (TypeError, ValueError):
                verdict = None
            if verdict is not None:
                recommendation = verdict.verdict.upper()
                conviction = verdict.conviction
                expectation = verdict.reasoning_md or expectation
                proposal = (
                    proposal_by_run.get((int(verdict.source_decision_run_id), verdict.subject.upper()))
                    if verdict.source_decision_run_id is not None
                    else None
                )
                disposition = _lifecycle(proposal)
        elif source_label == "trade_plan":
            try:
                proposal = surfaced_by_id.get(int(ref.get("proposal_id")))
            except (TypeError, ValueError):
                proposal = None
            disposition = _surfaced_lifecycle(proposal)
        elif source_label == "legacy_proposal":
            proposal = legacy_by_id.get(ref.get("proposal_id"))
            disposition = _lifecycle(proposal)

        outcome = latest_outcome.get(int(prediction.id))
        benchmark = (
            benchmark_by_outcome_id.get(int(outcome.id))
            if outcome is not None
            else None
        )
        outcome_kind = outcome.outcome_kind if outcome is not None else None
        grade = _self_eval_grade(
            recommendation=recommendation,
            outcome_kind=outcome_kind,
        )
        signed_pnl = (
            float(outcome.pnl_pct) * 100.0
            if outcome is not None and outcome.pnl_pct is not None
            else None
        )
        ticker_move = signed_pnl
        if ticker_move is not None and prediction.direction == "short":
            ticker_move = -ticker_move
        target = str(ref.get("success_measure") or "").strip()
        if not target:
            if prediction.target_price is not None:
                target = f"Target price ${float(prediction.target_price):,.2f}"
                if prediction.stop_price is not None:
                    target += f"; falsified at ${float(prediction.stop_price):,.2f}"
            else:
                target = (
                    "Direction-adjusted return at the evaluation date; "
                    "+10% is a strong win and -10% a strong miss, with the "
                    "full observed return reported."
                )
        self_evaluations.append(
            {
                "prediction_id": int(prediction.id),
                "ticker": str(prediction.ticker or ""),
                "recommendation": recommendation or prediction.direction.upper(),
                "conviction": conviction,
                "source": source_label,
                "source_record_id": ref.get("proposal_id") if source_label == "legacy_proposal" else None,
                "recommended_at": prediction.event_at.isoformat(),
                "horizon_days": int(prediction.timeframe_days or 30),
                "evaluation_date": prediction.evaluation_due_at.isoformat(),
                "entry_price": (
                    float(prediction.entry_price)
                    if prediction.entry_price is not None
                    else None
                ),
                "target": target,
                "expectation": expectation,
                "disposition": disposition,
                "status": (
                    "pending" if outcome is None else "unscorable"
                    if grade == "pending" else "evaluated"
                ),
                "grade": grade,
                "outcome_kind": outcome_kind,
                "ticker_move_pct": ticker_move,
                "signed_pnl_pct": signed_pnl,
                "exit_price": (
                    float(outcome.exit_price_used)
                    if outcome is not None and outcome.exit_price_used is not None
                    else None
                ),
                "evaluated_at": (
                    outcome.evaluated_at.isoformat()
                    if outcome is not None and outcome.evaluated_at is not None
                    else None
                ),
                "benchmark_symbol": (
                    benchmark.benchmark_symbol if benchmark is not None else None
                ),
                "benchmark_return_pct": (
                    float(benchmark.benchmark_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
                "subject_return_pct": (
                    float(benchmark.subject_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
                "decision_excess_return_pct": (
                    float(benchmark.decision_excess_return_pct) * 100.0
                    if benchmark is not None
                    else None
                ),
                "benchmark_grade": (
                    "beat"
                    if benchmark is not None
                    and float(benchmark.decision_excess_return_pct) > 0.001
                    else "lag"
                    if benchmark is not None
                    and float(benchmark.decision_excess_return_pct) < -0.001
                    else "tie"
                    if benchmark is not None
                    else "pending"
                ),
                "report": _self_eval_report(
                    ticker=str(prediction.ticker or ""),
                    recommendation=recommendation or prediction.direction.upper(),
                    due_at=prediction.evaluation_due_at,
                    outcome=outcome,
                    grade=grade,
                    benchmark=benchmark,
                ),
            }
        )
    self_evaluations.sort(
        key=lambda row: (
            row["recommended_at"],
            -int(row["horizon_days"]),
            int(row["prediction_id"]),
        ),
        reverse=True,
    )
    benchmark_evaluations = [
        row
        for row in self_evaluations
        if row["decision_excess_return_pct"] is not None
    ]
    benchmark_beats = sum(
        row["benchmark_grade"] == "beat" for row in benchmark_evaluations
    )
    benchmark_lags = sum(
        row["benchmark_grade"] == "lag" for row in benchmark_evaluations
    )
    benchmark_ties = sum(
        row["benchmark_grade"] == "tie" for row in benchmark_evaluations
    )
    benchmark_excesses = [
        float(row["decision_excess_return_pct"])
        for row in benchmark_evaluations
    ]
    self_eval_limit = max(1, int(recent_limit))
    visible_self_evaluations = [
        *[row for row in self_evaluations if row["status"] == "unscorable"][
            :self_eval_limit
        ],
        *[row for row in self_evaluations if row["status"] == "evaluated"][
            :self_eval_limit
        ],
        *[row for row in self_evaluations if row["status"] == "pending"][
            : self_eval_limit * 2
        ],
    ]

    verdict_ids_with_any = set(horizons_by_verdict)
    proposal_run_ids = {
        int(row.decision_run_id)
        for row in proposals
        if row.decision_run_id is not None
    }
    legacy_without_verdict = len(legacy_proposals)
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "history_scope": "all_saved_recommendations_including_retention_archives",
        "calibration_scope_note": (
            "Historical accountability report, including retention-archived calls. "
            "Live source-reliability weights use only non-archived predictions. "
            "Both select outcomes matching the current scoring contract."
        ),
        "horizons": horizon_stats,
        "benchmark": {
            "symbol": BENCHMARK_SYMBOL,
            "version": BENCHMARK_VERSION,
            "basis": (
                "Pre-tax decision alpha versus SPY adjusted EOD: previous close "
                "before the recommendation through the outcome date."
            ),
            "compared": len(benchmark_evaluations),
            "beats": benchmark_beats,
            "lags": benchmark_lags,
            "ties": benchmark_ties,
            "beat_rate": (
                benchmark_beats / (benchmark_beats + benchmark_lags)
                if benchmark_beats + benchmark_lags
                else None
            ),
            "avg_excess_return_pct": (
                sum(benchmark_excesses) / len(benchmark_excesses)
                if benchmark_excesses
                else None
            ),
        },
        "recent": lines[: max(1, int(recent_limit))],
        "self_evaluations": visible_self_evaluations,
        "legacy_proposal_evaluations": [
            row for row in self_evaluations
            if row["source"] == "legacy_proposal" and row["horizon_days"] == 180
        ],
        "surfaced_order_sheets": {
            "horizons": surfaced_horizon_stats,
            "recent": surfaced_lines[: max(1, int(recent_limit))],
        },
        "shadow_order_sheets": {
            "basis": (
                "Notional-weighted BUY/ADD lines exactly as authored, whether or "
                "not filled; pre-tax and before trading costs. SELL/TRIM funding "
                "legs are reported separately, not treated as invested capital."
            ),
            "evaluated": len(shadow_sheets),
            "beats": sum(row["grade"] == "beat" for row in shadow_sheets),
            "lags": sum(row["grade"] == "lag" for row in shadow_sheets),
            "recent": shadow_sheets[: max(1, int(recent_limit))],
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
            "unscorable_evaluations": sum(row["status"] == "unscorable" for row in self_evaluations),
            "actionable_verdicts": len(verdicts),
            "with_any_forecast": len(verdict_ids_with_any),
            "with_30d_forecast": sum(30 in value for value in horizons_by_verdict.values()),
            "with_180d_forecast": sum(180 in value for value in horizons_by_verdict.values()),
            "with_365d_forecast": sum(365 in value for value in horizons_by_verdict.values()),
            "autonomous_proposals": len(proposals),
            "legacy_proposals_without_verdict_link": int(legacy_without_verdict),
            "legacy_proposals_with_all_clocks": sum(
                {30, 180, 365} <= legacy_horizons[int(row.id)] for row in legacy_proposals
            ),
            "legacy_proposals_missing_clocks": [
                {"proposal_id": int(row.id), "ticker": row.ticker,
                 "missing_horizons": sorted({30, 180, 365} - legacy_horizons[int(row.id)])}
                for row in legacy_proposals
                if not {30, 180, 365} <= legacy_horizons[int(row.id)]
            ],
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
