"""Durable operational limits for the passive Discord research feed.

No tokens are stored. Counters survive backend/CLI restarts; OS locks exclude
duplicate listeners and serialize history requests using the same credential.
These are conservative Argosy safety budgets, not Discord's advertised limits.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from argosy.orchestrator.loops.base import NonRetryableJobError


class DiscordFeedStopped(NonRetryableJobError):
    """Network activity stopped; surface the reason instead of auto-retrying."""


def normalize_bot_token(token: str) -> str:
    return token.strip().removeprefix("Bot ").strip()


class DiscordFeedSafety:
    def __init__(self, token: str, *, directory: Path | None = None, clock=time.time):
        if directory is None:
            from argosy.config import get_settings
            directory = get_settings().home / "runtime" / "discord-feed"
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.key = hashlib.sha256(normalize_bot_token(token).encode()).hexdigest()
        self.path = directory / "safety.sqlite3"
        self.clock = clock
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (credential TEXT, kind TEXT, at REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS blocks (credential TEXT PRIMARY KEY, reason TEXT, until REAL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def check(self):
        with self._db() as db:
            row = db.execute("SELECT reason,until FROM blocks WHERE credential=?", (self.key,)).fetchone()
        if row and (row[1] is None or row[1] > self.clock()):
            raise DiscordFeedStopped(row[0])

    def block(self, reason: str, *, seconds: float | None = None):
        until = None if seconds is None else self.clock() + seconds
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT until FROM blocks WHERE credential=?", (self.key,)).fetchone()
            if old and (old[0] is None or (until is not None and old[0] > until)):
                return
            db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?)", (self.key, reason, until))

    def clear_auth_block(self):
        """Explicit operator repair only; never reset request/IDENTIFY budgets."""
        with self._db() as db:
            db.execute("DELETE FROM blocks WHERE credential=? AND until IS NULL", (self.key,))

    def reserve(self, kind: str):
        now = self.clock()
        # IDENTIFY is separate from RESUME. Never clear these on successful READY.
        budgets = ((600, 3), (86400, 100)) if kind == "identify" else ((600, 10 if kind == "gateway" else 3),)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reason,until FROM blocks WHERE credential=?", (self.key,)).fetchone()
            if row and (row[1] is None or row[1] > now):
                raise DiscordFeedStopped(row[0])
            db.execute("DELETE FROM attempts WHERE at < ?", (now - 86400,))
            for window, limit in budgets:
                count = db.execute(
                    "SELECT count(*) FROM attempts WHERE credential=? AND kind=? AND at>?",
                    (self.key, kind, now - window),
                ).fetchone()[0]
                if count >= limit:
                    raise DiscordFeedStopped(
                        f"Discord feed paused: {kind} safety budget ({limit}/{window}s) exhausted. "
                        "Investigate reconnects; retry only after the budget window expires."
                    )
            db.execute("INSERT INTO attempts VALUES (?,?,?)", (self.key, kind, now))

    @contextmanager
    def lease(self, kind: str):
        """Crash-safe process lock; never delete the inode another process may hold."""
        handle = (self.directory / f"{self.key}.{kind}.lock").open("a+b")
        locked = False
        try:
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise DiscordFeedStopped(f"Discord feed {kind} already active in another worker.") from None
            self.check()
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
