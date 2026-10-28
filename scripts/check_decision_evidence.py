"""Run an analysis-only ticker decision through the production evidence review."""
import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argosy.services.decision_funnel.deep_decision import run_deep_decision
from argosy.services.youtube_analysis import _portfolio_context


async def main(ticker, user_id):
    result = await run_deep_decision(user_id=user_id, ticker=ticker,
        force=True, execution_policy="analysis_only", subject_type="core_candidate",
        positions_summary=_portfolio_context(user_id),
        user_constraints="Independent research, not permission to trade. No deployment budget supplied. "
                         "Do not equate cash balances with unreserved funding. Respect current recorded "
                         "constraints. Review buy and no-initiation equally; distinguish an owned HOLD "
                         "from not initiating an unowned name. Do not assume an influencer's purchase "
                         "is right or wrong. Do not convert currencies or invent available sale proceeds.")
    print(json.dumps(dataclasses.asdict(result), default=str))
    if str(getattr(result, "status", "")) in {"error", "failed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--user-id", default="ariel")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, args.user_id))
