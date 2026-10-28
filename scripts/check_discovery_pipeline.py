"""Exercise actual discovery research, or print its read-only opportunity audit.

No trade proposal, order, approval or execution is created. --review persists
ordinary analysis-only fleet reports/decision lineage; no seams are replaced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def review(user_id: str, symbols: list[str]) -> list[dict]:
    from argosy.services.discovery_grader import grade_discovery_ticker

    async def one(symbol: str) -> dict:
        print(json.dumps({"ticker": symbol, "stage": "fleet_deployed"}), flush=True)
        try:
            pick = await grade_discovery_ticker(user_id, {"ticker": symbol})
            result = {"ticker": symbol, "status": "completed" if pick else "incomplete",
                      "pick": None if pick is None else {
                          "verdict": pick.verdict, "conviction": pick.conviction,
                          "thesis": pick.thesis_md, "cites": list(pick.cites),
                      }}
        except Exception as exc:
            result = {"ticker": symbol, "status": "failed", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    # Bound concurrency; these are real multi-agent reviews, not ticker fixtures.
    results = []
    for index in range(0, len(symbols), 2):
        results.extend(await asyncio.gather(*(one(s) for s in symbols[index:index + 2])))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--review", help="Comma-separated tickers for real analysis-only fleet reviews")
    parser.add_argument("--refresh", action="store_true", help="Run the real scheduled discovery tick (research only)")
    parser.add_argument("--horizon-days", type=int, choices=(30, 180, 365), default=30)
    args = parser.parse_args()
    if args.refresh:
        if args.review:
            parser.error("Choose --refresh or --review")
        from argosy.orchestrator.loops.discovery_funnel_loop import DiscoveryFunnelLoop
        from argosy.services.jobs.summary_status import derive_run_status

        result = asyncio.run(DiscoveryFunnelLoop(user_id=args.user_id).tick())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(derive_run_status(result)[0] == "error")
    if args.review:
        symbols = list(dict.fromkeys(s.strip().upper() for s in args.review.split(",") if s.strip()))
        if not symbols or len(symbols) > 10:
            parser.error("Review 1-10 tickers")
        results = asyncio.run(review(args.user_id, symbols))
        return int(any(row["status"] != "completed" for row in results))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from argosy.services.discovery_audit import build_discovery_audit

    engine = create_engine(f"sqlite:///file:{(ROOT / 'db/argosy.db').as_posix()}?mode=ro&uri=true")
    with Session(engine) as session:
        print(json.dumps(build_discovery_audit(session, user_id=args.user_id,
                                             horizon_days=args.horizon_days),
                         ensure_ascii=False, indent=2))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
