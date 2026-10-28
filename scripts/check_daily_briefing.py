"""Real daily editor/reviewer preview. Reads live data; never sends Discord or trades.

Run: .venv/Scripts/python.exe scripts/check_daily_briefing.py
Uses configured models (billable); prints the review receipt as well as the draft.
"""
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argosy.config import get_settings
from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.daily_overview import build_daily_overview
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.transport.discord_advisor.config import load_config


async def main():
    config = load_config()
    binding = config.bindings[0]
    principal = Principal(binding.household_user_id, binding.guild_id, binding.channel_id, binding.user_id)
    retrieval = RetrievalService(db_path=Path(get_settings().home) / "db" / "argosy.db")
    try:
        answer = await build_daily_overview(retrieval, principal, now=datetime.now(UTC))
        print(json.dumps({"review_status": answer.review_status, "text": answer.text,
                          "citations": [c.__dict__ for c in answer.citations],
                          "review_trace": answer.review_trace}, ensure_ascii=False, default=str, indent=2))
        return 0 if answer.review_status == "accepted" else 1
    finally:
        retrieval.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
