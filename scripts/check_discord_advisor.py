"""Exercise private-chat retrieval against the real database, without an LLM.

This is NOT a live Discord or fleet verification. Output contains coverage and
record references, never financial values, source bodies, or credentials.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.retrieval import RetrievalService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    reader = RetrievalService(db_path=args.database)
    principal = Principal(args.user, "local-probe", "local-probe", "local-probe")
    results = []
    errors = 0
    try:
        for topic in sorted(reader.TOPICS):
            try:
                result = reader.read(principal, topic, limit=3)
                results.append({
                    "topic": topic,
                    "status": "read_with_warnings" if result.warnings else "read",
                    "has_data": result.data is not None,
                    "references": len(result.citations),
                    "warning_count": len(result.warnings),
                })
            except Exception as exc:
                errors += 1
                results.append({"topic": topic, "status": "error", "error_type": type(exc).__name__})
    finally:
        if reader.engine is not None:
            reader.engine.dispose()
    print(json.dumps({
        "verification": "real_database_read_only",
        "live_discord_verified": False,
        "live_fleet_verified": False,
        "errors": errors,
        "topics": results,
    }, indent=2))
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
