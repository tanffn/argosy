"""Explicitly re-review one saved research post through the production image/review path.

No trades. Persists real fleet reports, source analysis and dated research clocks.
"""
import argparse
import asyncio
import json
import sys
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
import psutil
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argosy.services.research_catalog import research_session
from argosy.services.research_worker import analyze_document, _process_claim, await_owned
from argosy.state.research_models import ResearchItem


async def main(item_id, user_id):
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        item = session.get(ResearchItem, item_id)
        if item is None or item.user_id != user_id:
            raise ValueError("Research item not found for this user")
        if item.status == "processing":
            raise ValueError("Research item already has an active owner")
        token = uuid.uuid4().hex
        state = json.loads(item.analysis_json or "{}")
        if state.get("_delivery_pending"):
            raise ValueError("Previous assessment delivery needs reconciliation before a new review")
        state.pop("_completed_payload", None)
        state.pop("_auto_completed_at", None)
        state["_auto_owner"] = {"token": token, "pid": os.getpid(),
                                "created_at": psutil.Process().create_time()}
        item.analysis_json = json.dumps(state)
        item.status = "processing"
        item.attempted_at = datetime.now(UTC)
        item.lease_until = item.attempted_at + timedelta(hours=3)
        item.attempts += 1
        session.commit()
    failure = await await_owned(_process_claim((item_id, "document", token), user_id=user_id,
                                               analyzer=analyze_document))
    if failure:
        raise RuntimeError(json.dumps(failure))
    with research_session() as session:
        payload = json.loads(session.get(ResearchItem, item_id).analysis_json)
    print(json.dumps({"item_id": item_id, "review_status": payload["evidence_review"]["status"],
        "image_evidence": payload["image_evidence"],
        "recommendations": payload["synthesis"]["ticker_recommendations"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id")
    parser.add_argument("--user-id", default="ariel")
    args = parser.parse_args()
    asyncio.run(main(args.item_id, args.user_id))
