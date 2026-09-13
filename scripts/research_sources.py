"""Administer shared research without launching paid fleet jobs.

Run with .venv/Scripts/python.exe scripts/research_sources.py status|backfill|seed|poll
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import json
from argosy.services.research_catalog import research_session, upsert_source, list_sources, index_existing_events, index_youtube
from argosy.services.research_connectors import fetch_public, parse_feed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "backfill", "seed", "poll"])
    parser.add_argument("--user", default="ariel")
    args = parser.parse_args()
    if args.command == "backfill":
        from argosy.config import get_settings
        count = 0
        failures = []
        for path in sorted((get_settings().logs_dir / "youtube").glob("**/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not payload.get("video", {}).get("video_id"):
                    continue
                index_youtube(payload, user_id=args.user)
                count += 1
            except Exception as exc:
                failures.append({"file": str(path), "error": str(exc)[:300]})
        # Repair entity-to-ticker associations from the initial legacy projection;
        # keep the original claim text, raw payload and evaluation history.
        from argosy.state.research_models import ResearchClaim, ResearchItem
        from argosy.services.research_catalog import symbol
        from sqlalchemy import select
        repaired = 0
        with research_session() as session:
            for claim, item in session.execute(select(ResearchClaim, ResearchItem).join(ResearchItem).where(ResearchClaim.user_id == args.user)):
                raw = json.loads(claim.payload_json)
                call = raw.get("speaker_call") or {}
                named = (json.loads(item.analysis_json).get("claims") or {}).get("named_tickers", [])
                valid = {symbol(t) for t in named} | {symbol(call.get("ticker") or raw.get("ticker"))}
                if claim.ticker and claim.ticker not in valid:
                    claim.ticker = None
                    repaired += 1
            session.commit()
        print(json.dumps({"youtube_artifacts": count, "existing_events": index_existing_events(user_id=args.user), "entity_links_repaired": repaired, "failures": failures}))
    elif args.command == "seed":
        sources = [
            ("Yet Another Value Blog", "rss", "https://www.yetanothervalueblog.com/feed", 65),
            ("SEC 13D ownership plans", "sec13d", "all", 45),
            ("Fund letters (pasted originals)", "manual", "fund-letters", 70),
        ]
        for name, kind, reference, priority in sources:
            with research_session() as session:
                source = upsert_source(session, user_id=args.user, name=name, kind=kind, reference=reference, priority=priority)
                session.commit()
                print(json.dumps({"source": name, "id": source.id}))
        from argosy.services.youtube_intelligence import subscribe_youtube_source
        for reference in ["https://www.youtube.com/@yetanothervaluepodcast", "https://www.youtube.com/@ChitChatStocks"]:
            try:
                source = subscribe_youtube_source(reference, user_id=args.user)
                print(json.dumps({"youtube": source["channel_name"], "id": source["id"]}))
            except Exception as exc:
                print(json.dumps({"youtube": reference, "error": str(exc)[:300]}))
    elif args.command == "poll":
        from argosy.services.research_worker import poll_sources, queue_youtube
        async def poll():
            return {"documents": await poll_sources(user_id=args.user), "youtube": await queue_youtube(user_id=args.user)}
        print(json.dumps(asyncio.run(poll()), default=str))
    else:
        with research_session() as session:
            print(json.dumps(list_sources(session, args.user), default=str, indent=2))


if __name__ == "__main__":
    main()
