"""Persist evaluator no-progress evidence outside the tick transaction."""
from __future__ import annotations

import json
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.state.models import PredictionEvaluatorBatchFailure

_log = get_logger("argosy.services.predictions.evaluator_failure_audit")


def record_evaluator_batch_failure(
    *,
    session_factory: sessionmaker | None,
    bind_url: str | None = None,
    due_selected: int,
    unparseable: int,
    adapter_errors: int,
    overdue_unscored_remaining: int,
    prediction_ids: Sequence[int],
    summary: dict[str, Any],
    failure_reason: str,
) -> int | None:
    """Insert a failure row on a fresh connection and commit immediately.

    Survives the caller's ``session.rollback()`` so a repeatedly failing
    batch is visible even when unparseable outcomes are rolled back.
    """
    engine = None
    own_session = False
    session: Session | None = None
    try:
        if session_factory is not None:
            session = session_factory()
            own_session = True
        elif bind_url:
            engine = sa.create_engine(bind_url)
            session = Session(bind=engine)
            own_session = True
        else:
            _log.error("evaluator_failure_audit.no_session_factory")
            return None
        assert session is not None
        row = PredictionEvaluatorBatchFailure(
            due_selected=int(due_selected),
            unparseable=int(unparseable),
            adapter_errors=int(adapter_errors),
            overdue_unscored_remaining=int(overdue_unscored_remaining),
            prediction_ids_json=json.dumps([int(x) for x in prediction_ids]),
            summary_json=json.dumps(summary, default=str),
            failure_reason=failure_reason[:2000],
        )
        session.add(row)
        session.commit()
        return int(row.id) if row.id is not None else None
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "evaluator_failure_audit.persist_failed",
            error=str(exc)[:200],
        )
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        return None
    finally:
        if own_session and session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
