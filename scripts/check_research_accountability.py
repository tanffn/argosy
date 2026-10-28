"""Read-only proof that an approved research assessment has all dated clocks."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3


def main(item_id):
    root = Path(__file__).resolve().parents[1]
    db = sqlite3.connect((root / "db/argosy.db").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    item = db.execute("SELECT * FROM research_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        raise ValueError("Research item not found")
    payload = json.loads(item["analysis_json"])
    assert payload.get("evidence_review", {}).get("status") == "approved", "Review not approved"
    assert not payload.get("_delivery_pending"), "Delivery still pending"
    assessment = payload["assessment_id"]
    rows = db.execute("""SELECT ticker, event_at, evaluation_due_at, timeframe_days, source_ref
        FROM predictions WHERE user_id=? AND source='signal_stream:research_disposition'
        AND json_extract(source_ref, '$.assessment_id')=? ORDER BY ticker, timeframe_days""",
        (item["user_id"], assessment)).fetchall()
    recs = payload["synthesis"]["ticker_recommendations"]
    for rec in recs:
        clocks = [r for r in rows if r["ticker"] == rec["ticker"]]
        assert {r["timeframe_days"] for r in clocks} == {30, 180, 365}, rec["ticker"]
        for row in clocks:
            ref = json.loads(row["source_ref"])
            assert ref["verdict"] == rec["disposition"].upper()
            assert ref["expectation"] == rec["rationale"].strip()
            assert datetime.fromisoformat(row["evaluation_due_at"]) == datetime.fromisoformat(row["event_at"]) + timedelta(days=row["timeframe_days"])
            assert not ref["executable"]
    print(json.dumps({"item_id": item_id, "assessment_id": assessment,
        "review": "approved", "capture": payload.get("capture"), "recommendations": len(recs),
        "clocks": len(rows), "schedule": sorted({(r["timeframe_days"], r["evaluation_due_at"][:10]) for r in rows}),
        "calls": [{"ticker": r["ticker"], "call": r["disposition"]} for r in recs]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id")
    main(parser.parse_args().item_id)
