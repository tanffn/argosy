"""Preview/apply the same idempotent clock repairs used by the daily evaluator.

No LLMs, new investment judgments, approvals, trades or invented prices.
Without --apply, the transaction is rolled back. Receipt includes before/after
coverage; original timestamps remain on predictions, actual creation time is now.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argosy.services.predictions.proposal_clocks import (  # noqa: E402
    ensure_proposal_prediction_horizons,
)
from argosy.services.predictions.writers import (  # noqa: E402
    ensure_deep_verdict_prediction_horizons,
)
from argosy.services.recommendation_scorecard import build_recommendation_scorecard  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--user-id", default="ariel")
    args = parser.parse_args()
    engine = create_engine("sqlite:///" + (ROOT / "db/argosy.db").as_posix())
    with Session(engine) as db:
        # Explicit outer BEGIN: nested writer savepoints must never commit a
        # preview independently under sqlite3 legacy transaction control.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        before = build_recommendation_scorecard(db, user_id=args.user_id)["coverage"]
        verdicts = ensure_deep_verdict_prediction_horizons(db, user_id=args.user_id)
        proposals = ensure_proposal_prediction_horizons(db, user_id=args.user_id)
        after = build_recommendation_scorecard(db, user_id=args.user_id)["coverage"]
        if args.apply:
            db.commit()
        else:
            db.rollback()
    engine.dispose()
    print(json.dumps({"applied": args.apply, "verdicts": verdicts, "proposals": proposals,
                      "before": before, "after": after}, indent=2))


if __name__ == "__main__":
    main()
