"""Run the production whole-book comparison step, or replay only saved evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker

from argosy.services.research_catalog import research_database_url
from argosy.services.shadow_daily import run_user
from argosy.services.shadow_portfolio import project, report
from argosy.state.db import create_sync_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay-only", action="store_true")
    mode.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    engine = create_sync_engine(research_database_url(args.user_id))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        if args.status_only or args.replay_only:
            with factory() as db:
                if args.replay_only:
                    project(db, user_id=args.user_id)
                    db.commit()
                output = report(db, args.user_id)
        else:
            output = run_user(factory, args.user_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
