"""Immutable research-disposition clocks, distinct from executable advice."""
import hashlib
import json
from datetime import datetime
from sqlalchemy import select, func

from argosy.services.predictions.writers import _insert_prediction

SOURCE = "signal_stream:research_disposition"


def record_disposition(session, *, user_id, source_kind, source_id, source_url,
                       ticker, disposition, rationale, next_step, confidence, observed_at, assessment_id):
    ref = dict(kind=SOURCE, source_kind=source_kind, source_id=str(source_id),
               source_url=source_url, ticker=ticker, verdict=disposition.upper(),
               conviction=str(getattr(confidence, "value", confidence)).upper(),
               expectation=rationale.strip(), next_step=next_step.strip(),
               executable=False, assessment_id=assessment_id,
               evaluation_kind="price_opportunity_observation",
               success_measure="Observe the ticker and SPY over the same period; report missed upside or avoided loss. "
                               "This is research prioritization, not a short position, calibrated return forecast or trade P&L.")
    version = hashlib.sha256(json.dumps([user_id, assessment_id, ticker]).encode()).hexdigest()
    return tuple(_insert_prediction(session, user_id, source=SOURCE,
        source_ref={**ref, "horizon_days": days, "assessment_version": version},
        message_id=f"research:{version}:{days}d", ticker=ticker, direction="long",
        event_at=observed_at, timeframe_days=days, preserve_long_horizon=days in (180, 365))
        for days in (30, 180, 365))


def ensure_research_clocks(session, *, user_id=None):
    """Recover a crash between approved research persistence and clock publication.

    Only reviewed saved results qualify; never fabricate earlier recommendations
    from a current ticker list. Existing version keys preserve original clocks.
    """
    from argosy.state.research_models import ResearchItem, ResearchSource
    from argosy.state.models import Prediction
    from argosy.services.research_catalog import symbol
    stmt = select(ResearchItem, ResearchSource).join(ResearchSource).where(
        ResearchItem.analyzed_at.is_not(None),
        func.json_extract(ResearchItem.analysis_json, "$.evidence_review.status") == "approved")
    if user_id is not None:
        stmt = stmt.where(ResearchItem.user_id == user_id)
    count = 0
    before = session.scalar(select(func.count()).select_from(Prediction).where(Prediction.source == SOURCE))
    for item, source in session.execute(stmt):
        payload = json.loads(item.analysis_json)
        if not payload.get("assessment_id"):
            continue  # Legacy unversioned drafts are not new assessment events.
        for rec in payload.get("synthesis", {}).get("ticker_recommendations", []):
            ticker = symbol(rec.get("ticker"))
            disposition = str(rec.get("disposition", "")).lower()
            if not ticker or disposition not in {"watch", "pass", "buy"}:
                continue
            record_disposition(session, user_id=item.user_id,
                source_kind="youtube" if source.kind == "youtube" else "research",
                source_id=item.external_id if source.kind == "youtube" else item.id,
                assessment_id=payload["assessment_id"],
                source_url=item.url, ticker=ticker, disposition=disposition,
                rationale=str(rec.get("rationale") or ""), next_step=str(rec.get("next_step") or ""),
                confidence=rec.get("confidence", "LOW"), observed_at=(datetime.fromisoformat(payload["assessed_at"])
                    if payload.get("assessed_at") else item.analyzed_at))
            count += 1
    after = session.scalar(select(func.count()).select_from(Prediction).where(Prediction.source == SOURCE))
    return {"assessments": count, "clocks_created": after-before}
