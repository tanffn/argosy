"""One-shot, bounded deployment after scheduled research drains; no job cancellation."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RECEIPT = ROOT / "tmp" / "discord-deployment.json"


def record(state: str, **details) -> None:
    payload = {"state": state, "updated_at": datetime.now(UTC).isoformat(), **details}
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    pending = RECEIPT.with_suffix(".new")
    pending.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pending.replace(RECEIPT)
    print(json.dumps(payload), flush=True)


def active_jobs() -> list[dict]:
    database = ROOT / "db" / "argosy.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT id,job_name,started_at FROM job_runs WHERE status='running'"
        ).fetchall()
    return [{"id": row[0], "name": row[1], "started_at": row[2]} for row in rows]


def board_receipts() -> list[str]:
    from argosy.transport.discord_advisor.config import load_config

    binding = load_config().bindings[0]
    with sqlite3.connect((ROOT / "db" / "argosy.db").as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT o.sent_message_id FROM notification_outbox o JOIN chat_bindings b ON b.id=o.binding_id "
            "WHERE o.household_user_id=? AND b.guild_id=? AND b.channel_id=? "
            "AND o.category='status_board' AND o.status='board_sent' AND o.semantic_key LIKE ?",
            (binding.household_user_id, binding.guild_id, binding.channel_id,
             f"status-board:{binding.status_channel_id}:%"),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


async def ready_message() -> str:
    import discord

    from argosy.transport.discord_advisor.config import load_bot_token, load_config
    from argosy.transport.discord_advisor.gateway import DiscordPyGateway

    binding = load_config().bindings[0]
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(load_bot_token())
        guild = await client.fetch_guild(int(binding.guild_id))
        channel = next(c for c in await guild.fetch_channels() if c.id == int(binding.channel_id))
        DiscordPyGateway._assert_private_delivery_channel(channel)
        message = await channel.send(
            "**Argosy is online.**\nSend a new message here: **Hi** or "
            "**How many research sources do we follow?**\n\n"
            f"Scheduled jobs are in <#{binding.status_channel_id}>. The board refreshes in place every minute "
            "while this PC is awake and online.\nNo trades or approvals through chat.",
            nonce="argosy-ready-20260919", allowed_mentions=discord.AllowedMentions.none(),
        )
        return str(message.id)


def main(wait_minutes: int) -> int:
    # The existing reloader revalidates ownership and running jobs before stopping
    # anything. This precheck only avoids repeated failed restart attempts.
    deadline = time.monotonic() + wait_minutes * 60
    while time.monotonic() < deadline:
        jobs = active_jobs()
        if jobs:
            record("waiting_for_jobs", active_jobs=jobs)
            time.sleep(30)
            continue
        record("reloading")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "scripts" / "reload_decision_operations.ps1")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=65,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            if active_jobs():
                continue
            record("failed", phase="reload", detail=(result.stderr or result.stdout)[-1200:])
            return 1
        break
    else:
        record("deferred", reason="Idle deployment window did not arrive within the bounded wait")
        return 2
    deadline = time.monotonic() + 120
    last = {}
    while time.monotonic() < deadline:
        try:
            response = httpx.get("http://127.0.0.1:8000/api/health/discord-advisor", timeout=5)
            response.raise_for_status()
            last = response.json()
            messages = board_receipts()
            if last.get("status") == "connected" and not last.get("attention_required") and messages:
                ready_id = asyncio.run(ready_message())
                record("ready", connection=last, status_board_message_ids=messages, ready_message_id=ready_id,
                       inbound_chat_verified=False)
                return 0
        except (httpx.HTTPError, sqlite3.Error) as exc:
            last = {"error_type": type(exc).__name__}
        time.sleep(5)
    record("failed", phase="live_verification", connection=last)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-minutes", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.wait_minutes <= 180:
        parser.error("wait-minutes must be between 1 and 180")
    lock_path = ROOT / "tmp" / "discord-deployment.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SystemExit("A Discord deployment watcher is already running") from None
        try:
            raise SystemExit(main(args.wait_minutes))
        except Exception as exc:
            record("failed", error_type=type(exc).__name__)
            raise
