"""Current-question outcome selection, shared by scorecards and feedback readers.

Method versions can supersede one another only within the same scoring contract.
For example, backfilling a missing entry improves a 30-day score, but does not
turn it into a 180-day score. Historical outcomes are never deleted here.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from argosy.state.models import EvaluationMethod, Prediction, PredictionOutcome


def authoritative_outcome_ids(prediction_ids: Iterable[int] | None = None):
    """Composable SQL selection with the same contract and ordering as the view.

    Archive/tenant cohorts belong to the caller. Do not use raw outcome existence
    to decide whether the current prediction has been evaluated.
    """
    method = aliased(EvaluationMethod)
    current = aliased(EvaluationMethod)
    ranked = (
        select(PredictionOutcome.id.label("id"), func.row_number().over(
            partition_by=PredictionOutcome.prediction_id,
            order_by=(method.method_version.desc(), PredictionOutcome.evaluated_at.desc(),
                      PredictionOutcome.id.desc()),
        ).label("rn"))
        .select_from(PredictionOutcome)
        .join(Prediction, Prediction.id == PredictionOutcome.prediction_id)
        .join(method, method.method_name == PredictionOutcome.evaluation_method)
        .join(current, current.method_name == Prediction.evaluation_method)
        .where(method.is_active == 1,
               func.coalesce(method.scoring_contract, method.method_name)
               == func.coalesce(current.scoring_contract, current.method_name))
        .correlate(None)
    )
    if prediction_ids is not None:
        ranked = ranked.where(PredictionOutcome.prediction_id.in_(list(prediction_ids)))
    rows = ranked.subquery()
    return select(rows.c.id).where(rows.c.rn == 1)


def authoritative_outcomes(
    session: Session, predictions: Iterable[Prediction],
) -> dict[int, tuple[Prediction, PredictionOutcome, EvaluationMethod]]:
    """One highest active compatible-version outcome per current prediction.

    Missing registry entries are not evidence. A retired base method may still
    identify the question; its active successor must declare the same contract.
    Contract fallback to the exact method name makes new registrations safe by
    default rather than treating unrelated methods in one family as comparable.
    """
    by_id = {int(p.id): p for p in predictions}
    if not by_id:
        return {}
    registry = {m.method_name: m for m in session.scalars(select(EvaluationMethod))}
    chosen: dict[int, tuple[Prediction, PredictionOutcome, EvaluationMethod]] = {}
    # Bound SQLite variable count for large historical ledgers.
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        rows = session.scalars(select(PredictionOutcome).where(
            PredictionOutcome.id.in_(authoritative_outcome_ids(ids[start:start + 500]))
        ))
        for outcome in rows:
            prediction = by_id[outcome.prediction_id]
            chosen[prediction.id] = (prediction, outcome, registry[outcome.evaluation_method])
    return chosen
