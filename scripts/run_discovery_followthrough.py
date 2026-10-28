"""Exercise the same automatic research-only follow-through used by the daily job."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argosy.services.discovery_followthrough import (
    DailyResearchBudget,
    audit_recent_screens,
    import_archived_audit,
    queue_summary,
    run_due_reviews,
)
from argosy.services.research_catalog import research_session
from argosy.state.db import init_engine


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--audit", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit-only", action="store_true")
    mode.add_argument("--status-only", action="store_true")
    mode.add_argument("--scheduled-tick", action="store_true")
    args = parser.parse_args()
    init_engine()
    result = {}
    if args.scheduled_tick:
        from argosy.orchestrator.loops.discovery_funnel_loop import DiscoveryFunnelLoop
        result["scheduled_tick"] = await DiscoveryFunnelLoop(user_id=args.user_id).tick()
    if bool(args.snapshot) != bool(args.audit):
        parser.error("--snapshot and --audit must be supplied together")
    if args.snapshot:
        result["bootstrap"] = import_archived_audit(args.user_id, args.snapshot, args.audit)
    if not args.status_only and not args.scheduled_tick:
        result["audit"] = await audit_recent_screens(args.user_id)
        if not args.audit_only:
            result["reviews"] = await run_due_reviews(args.user_id, budget=DailyResearchBudget(args.user_id))
    with research_session() as db:
        result["queue"] = queue_summary(db, args.user_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result.get("audit", {}).get("errors")
                    or result.get("reviews", {}).get("errors")
                    or (result.get("scheduled_tick") or {}).get("error")))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
