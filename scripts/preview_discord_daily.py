"""Run the production read/review briefing without sending; optionally refresh today's own post."""
import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main(replace_today=False):
    import httpx

    from argosy.services.chat_advisor.contracts import Principal
    from argosy.services.chat_advisor.daily_overview import build_daily_overview
    from argosy.services.chat_advisor.outbound import OutboundFilter
    from argosy.services.chat_advisor.retrieval import RetrievalService
    from argosy.services.chat_advisor.store import ChatStore
    from argosy.state.db import get_session_factory
    from argosy.transport.discord_advisor.config import load_bot_token, load_config

    cfg = load_config()
    binding = cfg.bindings[0]
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo(cfg.timezone))
    principal = Principal(binding.household_user_id, binding.guild_id, binding.channel_id, binding.user_id)
    answer = await build_daily_overview(RetrievalService(), principal, now=now)
    body = OutboundFilter.redact(f"**Argosy · {local:%d %b}**\n{answer.text}")
    artifact = {"generated_at": now.isoformat(), "body": body,
                "citations": [asdict(c) for c in answer.citations], "sent": False}
    target = ROOT / "tmp" / "discord-daily-preview.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(body, flush=True)
    if not replace_today:
        print("PREVIEW ONLY; saved to", target, flush=True)
        return
    if len(body) > 1900:
        raise RuntimeError("Brief is too long; inspect the preview before publishing")
    store = ChatStore(get_session_factory(), binding.household_user_id)
    await store.ensure_binding(provider="discord", guild_id=binding.guild_id,
                               channel_id=binding.channel_id, provider_user_id=binding.user_id)
    row = await store.latest_sent_outbox(f"overview:{local.date().isoformat()}")
    if row is None or not row.sent_message_id:
        raise RuntimeError("No daily post for today to replace; no message was sent")
    async with httpx.AsyncClient(headers={"Authorization": "Bot " + load_bot_token()}, timeout=20) as client:
        base = "https://discord.com/api/v10"
        me = await client.get(base + "/users/@me")
        me.raise_for_status()
        url = f"{base}/channels/{binding.channel_id}/messages/{row.sent_message_id}"
        previous = await client.get(url)
        previous.raise_for_status()
        if previous.json()["author"]["id"] != me.json()["id"]:
            raise RuntimeError("Refusing to edit a message from another author")
        artifact["previous_body"] = previous.json()["content"]
        target.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        result = await client.patch(url, json={"content": body, "allowed_mentions": {"parse": []}})
        result.raise_for_status()
    await store._update_outbox(row.id, body=body,
        citations_json=json.dumps(artifact["citations"], default=str))
    artifact.update(sent=True, edited_message_id=row.sent_message_id)
    target.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Updated today's existing post:", row.sent_message_id, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace-today", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.replace_today))
