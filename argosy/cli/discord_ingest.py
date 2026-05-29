"""`argosy discord-ingest` — one-shot smoke-test entry point.

DEPRECATED AS A CRON DAEMON. Production now runs the Discord listener
through :class:`argosy.services.jobs.discord_listener_job.DiscordListenerJob`,
which is registered with the :class:`argosy.services.jobs.registry.JobRegistry`
and supervised in-process by the registry's supervisor task (Sprint A
commit #6). There is no longer any external cron / systemd unit
expected.

Use ``argosy discord-ingest`` ONLY as a one-shot smoke test for
verifying creds + gateway reachability + a single end-to-end ingest
roundtrip. For long-lived production listening, start the Argosy API
server — ``uvicorn argosy.api.main:create_app --factory`` — and the
JobRegistry supervisor will keep the listener alive across crashes
with exponential-backoff restart.

Behavior contract (smoke-test mode)
-----------------------------------

* If ``~/.argosy/discord_creds.json`` is missing → exit 0 (the listener
  is "dormant until creds arrive"). The supervisor will fast-exit on
  the same condition.
* If creds are malformed → exit 2 with a clear stderr message; the
  supervisor sees that as a real config error.
* Otherwise → connect, listen, persist. On any other unhandled
  exception the process exits non-zero — there is no built-in restart
  here because this CLI is a one-shot.

See ``argosy/services/discord_listener.py`` for the protocol details
and credentials format, and
``argosy/services/jobs/discord_listener_job.py`` for the supervised job
shape.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings
from argosy.services.discord_listener import (
    load_creds,
    run_discord_listener,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="discord-ingest",
    help="Daemon: subscribe to Discord channel and ingest news signals.",
    add_completion=False,
)


@app.command()
def main(
    creds_path: Path | None = typer.Option(
        None,
        "--creds-path",
        help="Override the default creds path (~/.argosy/discord_creds.json).",
    ),
    max_message_age_minutes: int = typer.Option(
        60,
        "--max-age-minutes",
        help="Drop messages older than this many minutes (default 60).",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="Logging level (DEBUG/INFO/WARNING/ERROR)."
    ),
) -> None:
    """Connect to Discord and ingest messages until the gateway disconnects.

    Exits 0 cleanly if creds are missing — supervisor retries next tick.
    Exits 2 if creds are malformed. Other exceptions propagate.
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        creds = load_creds(creds_path)
    except ValueError as exc:
        typer.echo(f"discord-ingest: invalid creds: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if creds is None:
        # "Dormant until creds arrive" — clean exit so the supervisor
        # treats this tick as a no-op rather than a failure.
        typer.echo(
            "discord-ingest: creds file not found; bot is dormant. "
            "Drop ~/.argosy/discord_creds.json to activate."
        )
        raise typer.Exit(code=0)

    session_factory = _build_session_factory()
    logger.info(
        "discord-ingest: starting listener for channel %s",
        creds.channel_id,
    )
    asyncio.run(
        run_discord_listener(
            session_factory=session_factory,
            creds=creds,
            max_message_age_minutes=max_message_age_minutes,
        )
    )


def _build_session_factory():
    """Build a sync sessionmaker against the Argosy DB.

    Mirrors the pattern used in ``argosy.cli.ingest.ingest_schwab_lots_cmd``:
    convert the async URL to sync, enable WAL + busy_timeout for
    SQLite, return a callable that yields a Session per invocation.
    """
    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})

    if sync_url.startswith("sqlite") and ":memory:" not in sync_url:
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=60000")
            cur.close()

    return sessionmaker(bind=engine, expire_on_commit=False)


if __name__ == "__main__":  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
