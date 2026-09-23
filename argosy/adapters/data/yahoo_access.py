"""Shared Yahoo backpressure. No price, freshness, or investment judgments.

The sidecar is separate from the financial DB: callers can hold a financial
write transaction. OS locking is crash-safe and spans backend/CLI processes.
Only cache misses enter here; valid cached responses remain usable.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class YahooUnavailable(RuntimeError):
    """Temporary provider backpressure, never a missing/invalid instrument."""


class YahooCooldown(YahooUnavailable):
    def __init__(self, until: float):
        self.retry_at = until
        stamp = datetime.fromtimestamp(until, UTC).isoformat()
        super().__init__(f"Yahoo rate limited; automatic retry eligible after {stamp}")


class YahooRequestCancelled(YahooUnavailable):
    """Caller cancelled before network admission."""


class YahooAccess:
    def __init__(self, directory: Path, *, clock=time.time, wait_seconds=30.0):
        self.directory = directory
        self.clock = clock
        self.wait_seconds = wait_seconds
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "access.sqlite3"
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY, until REAL NOT NULL, failures INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, last_attempt REAL NOT NULL)")

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
            row = db.execute("SELECT until FROM cooldown WHERE id=1").fetchone()
        if row and row[0] > self.clock():
            raise YahooCooldown(row[0])

    def status(self):
        with self._db() as db:
            block = db.execute("SELECT until,failures FROM cooldown WHERE id=1").fetchone()
            activity = db.execute("SELECT attempts,last_attempt FROM activity WHERE id=1").fetchone()
        return {"retry_at": block[0] if block else None, "failures": block[1] if block else 0,
                "sdk_operations": activity[0] if activity else 0,
                "last_attempt": activity[1] if activity else None}

    @contextmanager
    def _lease(self, cancelled=lambda: False):
        handle = (self.directory / "request.lock").open("a+b")
        locked = False
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + self.wait_seconds
            while not locked:
                if cancelled():
                    raise YahooRequestCancelled("Yahoo request cancelled before admission")
                self.check()
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
                    if time.monotonic() >= deadline:
                        raise YahooUnavailable("Yahoo request already in progress; retry on the next refresh") from None
                    time.sleep(0.05)
            self.check()  # Another process may have set cooldown before release.
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def run(self, fetch, *, recheck=None, cancelled=lambda: False):
        from yfinance.exceptions import YFRateLimitError

        with self._lease(cancelled):
            if recheck:
                hit = recheck()
                if hit is not None:
                    return hit
            if cancelled():
                raise YahooRequestCancelled("Yahoo request cancelled before network I/O")
            started = False
            try:
                # This check is the admission boundary, with no DB wait between
                # cancellation observation and SDK initiation.
                if cancelled():
                    raise YahooRequestCancelled("Yahoo request cancelled before network I/O")
                started = True
                value = fetch()
            except YFRateLimitError as exc:
                with self._db() as db:
                    old = db.execute("SELECT failures FROM cooldown WHERE id=1").fetchone()
                    failures = min((old[0] if old else 0) + 1, 3)
                    until = self.clock() + min(900 * 2 ** (failures - 1), 3600)
                    db.execute("INSERT OR REPLACE INTO cooldown VALUES (1,?,?)", (until, failures))
                raise YahooCooldown(until) from exc
            else:
                with self._db() as db:
                    db.execute("DELETE FROM cooldown WHERE id=1")
                return value
            finally:
                # Count SDK operations after completion, not admission waiters.
                # A cancelled active operation still owns its lease until here.
                if started:
                    with self._db() as db:
                        db.execute("INSERT INTO activity VALUES (1,1,?) ON CONFLICT(id) DO UPDATE SET attempts=attempts+1,last_attempt=excluded.last_attempt", (self.clock(),))


def default_access() -> YahooAccess:
    from argosy.config import get_settings

    return YahooAccess(get_settings().db_file.parent / "yahoo-access")
