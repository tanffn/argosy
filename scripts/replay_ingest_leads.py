"""Restore missing research-lead handoffs from original open ingest receipts.

Dry-run by default. --apply calls the normal ingest router, with the original
source timestamp, never a new LLM verdict, trade, approval, or source analysis.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.ingest_recommendation_router import route_ingest_recommendations
from argosy.state.db import create_sync_engine
from argosy.state.models import ActionProposal, ScanState


def replay(session: Session, *, user_id: str, apply: bool = False) -> dict:
    rows = session.execute(select(ActionProposal).where(
        ActionProposal.user_id == user_id, ActionProposal.status == "open",
        ActionProposal.dedup_key.like("ingest_%"),
    ).order_by(ActionProposal.surfaced_at, ActionProposal.id)).scalars().all()
    restored = []
    # Choose the latest source receipt per ticker, so replay cannot regress a
    # newer nomination or repeatedly refresh the same symbol from old mentions.
    latest = {}
    for row in rows:
        payload = json.loads(row.suggested_payload or "{}")
        ticker = str(payload.get("ticker") or "").strip().upper()
        if ticker and row.dedup_key.startswith(("ingest_watch|", "ingest_buy|")):
            latest[ticker] = (row, payload)
    for ticker, (row, payload) in sorted(latest.items()):
        state = session.get(ScanState, {"user_id": user_id, "ticker": ticker})
        if state is not None and state.nomination_evidence_json:
            continue
        if not payload.get("source_kind") or not payload.get("source_id"):
            raise ValueError(f"Ingest receipt {row.id} lacks source identity")
        observed = row.surfaced_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        restored.append({"ticker": ticker, "receipt_id": row.id,
                         "observed_at": observed.isoformat()})
        if apply:
            route_ingest_recommendations(session, user_id=user_id,
                source_kind=payload["source_kind"], source_id=payload["source_id"],
                source_url=None, now=observed, recommendations=[{
                    "ticker": ticker,
                    "disposition": "watch" if row.dedup_key.startswith("ingest_watch|") else "buy",
                    "rationale": row.rationale_md,
                    "next_step": payload.get("next_step") or "",
                    "confidence": "LOW",  # original structured confidence unavailable
                }])
    return {"applied": apply, "count": len(restored), "leads": restored}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with Session(create_sync_engine()) as session:
        print(json.dumps(replay(session, user_id=args.user_id, apply=args.apply)))
