"""Replay completed research checkpoints on a read-only live DB copy; no LLM/trades."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.config import get_settings
from argosy.services.allocation_research import pending_tasks, refresh_due_research
from argosy.services.order_sheet import PendingResearch
from argosy.state.models import ScanState


def main():
    now = datetime.now(UTC)
    source = Path(get_settings().db_file).resolve()
    with tempfile.TemporaryDirectory(prefix="argosy-research-replay-") as tmp:
        target = Path(tmp) / "copy.db"
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
            reader.close()
        engine = create_engine(f"sqlite:///{target}")
        try:
            with Session(engine) as db:
                tasks = [r for r in pending_tasks(db, "ariel") if r.next_review_date <= now.date()]
                if not tasks:
                    raise RuntimeError("No due task to replay")
                # Refuse any input that could cause real research/model calls.
                for row in tasks:
                    item = PendingResearch.model_validate_json(row.payload_json)
                    saved = json.loads(row.result_json or "{}").get("checkpoint", {})
                    expected = hashlib.sha256(item.model_dump_json().encode()).hexdigest()
                    if (saved.get("input_hash") != expected
                            or saved.get("as_of", "")[:10] != now.date().isoformat()
                            or set(saved.get("tickers", {})) != set(item.tickers)):
                        raise RuntimeError("Complete matching same-day checkpoint required; refusing model work")
                before = {r.ticker: (r.status, r.rank, r.last_radar_at)
                          for r in db.query(ScanState).filter_by(user_id="ariel")}
                result = refresh_due_research(db, "ariel", now=now, retry_failed=True)
                assert result["refreshed"] and not result["failures"], result
                after = {r.ticker: (r.status, r.rank, r.last_radar_at)
                         for r in db.query(ScanState).filter_by(user_id="ariel")}
                assert before == after, "Radar lifecycle changed"
                assert all(r.last_error is None and r.status == "pending" for r in tasks)
                print(json.dumps({"refreshed_tasks": len(result["refreshed"]),
                                  "failures": result["failures"],
                                  "radar_lifecycle_unchanged": True,
                                  "pending_questions_not_auto_resolved": True,
                                  "live_db_modified": False, "model_calls": 0,
                                  "new_order_sheet_proven": False}))
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
