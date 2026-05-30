"""Manual ingest of pre-downloaded alpha-report .txt files.

Bridge for the case where the Discord bot can't read message history
(channel admin hasn't granted Read Message History). User downloads
the past N daily reports manually from Discord and points this CLI at
the folder. Each .txt file lands in `news_signals` + (if parseable)
`predictions` using the same pipeline as the live listener.

Filename convention (relaxed): the script looks for a date in the
filename via a few common patterns:

  Alpha Report 5-29-2026.txt   ->  2026-05-29
  Alpha Report 2026-05-29.txt  ->  2026-05-29
  alpha_report_2026_05_29.txt  ->  2026-05-29
  Alpha-Report-2026-05-29.txt  ->  2026-05-29

If no date matches, the file's mtime is used (with a WARNING in the
report).

Usage:
    python -m argosy.scripts.manual_alpha_report_ingest \\
        --dir "C:/Users/ariel/Downloads/AlphaReports" \\
        [--user-id ariel] [--channel-id 1324786557936472198] [--dry-run]

Re-runs on the same folder are safe — the (source='discord',
source_ref=<deterministic>) UNIQUE on news_signals dedups silently.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import typer

from argosy.config import get_settings
from argosy.services.discord_listener import (
    _handle_message,
    Attachment,
    MessageEvent,
)
from argosy.state import db as db_mod
from argosy.state.models import NewsSignal, Prediction
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger("argosy.scripts.manual_alpha_report_ingest")


# ---------------------------------------------------------------------------
# Filename → date parsing
# ---------------------------------------------------------------------------

_DATE_PATTERNS: tuple[tuple[str, str], ...] = (
    # Alpha Report 5-29-2026.txt  /  Alpha-Report-5-29-2026.txt
    (r"(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})", "MDY"),
    # Alpha Report 2026-05-29.txt
    (r"(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})", "YMD"),
)


def parse_date_from_filename(name: str) -> datetime | None:
    """Pull a date out of common alpha-report filename shapes.

    Returns a UTC datetime at 00:00 on the parsed date, or None if no
    pattern matched. Caller falls back to file mtime when None.
    """
    for pattern, order in _DATE_PATTERNS:
        m = re.search(pattern, name)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if order == "MDY":
                month, day, year = int(a), int(b), int(c)
            else:  # YMD
                year, month, day = int(a), int(b), int(c)
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Synthetic event construction
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read file with UTF-8 → latin-1 fallback (same chain as the
    listener's attachment fetcher)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _synthetic_event(path: Path, *, channel_id: int) -> MessageEvent:
    """Build a MessageEvent that looks enough like a live Discord post
    that _handle_message will process it idempotently."""
    parsed = parse_date_from_filename(path.name)
    received_at = parsed or datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    # message_id is a deterministic string derived from the filename
    # stem. Source_ref becomes f"msg-manual-<stem>" inside the listener.
    message_id = f"manual-{re.sub(r'[^A-Za-z0-9_-]+', '_', path.stem)}"
    return MessageEvent(
        message_id=message_id,
        channel_id=channel_id,
        content=_read_text(path),
        timestamp=received_at,
        attachments=[],   # text is already in `content`
    )


# ---------------------------------------------------------------------------
# Main async driver
# ---------------------------------------------------------------------------


async def _ingest_dir(
    folder: Path,
    *,
    user_id: str,
    channel_id: int,
    dry_run: bool,
) -> dict[str, int]:
    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, future=True)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, future=True
    )

    files = sorted(
        [p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() == ".txt"],
    )
    if not files:
        logger.warning("no .txt files found in %s", folder)
        return {"files": 0, "ingested": 0, "skipped": 0, "errors": 0}

    pre = _count_news_signal_rows(session_factory)
    logger.info("found %d files; %d news_signals rows in DB pre-run",
                len(files), pre)

    counts = {
        "files": len(files), "ingested": 0,
        "skipped_dedup": 0, "errors": 0,
    }
    for path in files:
        event = _synthetic_event(path, channel_id=channel_id)
        if dry_run:
            logger.info(
                "[dry-run] %s -> message_id=%s received_at=%s bytes=%d",
                path.name, event.message_id, event.timestamp,
                len(event.content),
            )
            continue
        try:
            # _handle_message handles dedup + extract + write
            # NewsSignal + write Prediction (if parseable).
            # http_client is unused since attachments=[]. max_age
            # generous enough that backdated reports (filename date
            # may be days/weeks ago) aren't dropped as "too old".
            await _handle_message(
                event=event,
                session_factory=session_factory,
                http_client=None,
                known_tickers=None,
                channel_id=channel_id,
                max_age=timedelta(days=365),
                now_fn=lambda: datetime.now(timezone.utc),
            )
            counts["ingested"] += 1
            logger.info("ingested %s", path.name)
        except Exception:  # noqa: BLE001 - script-level reporting
            counts["errors"] += 1
            logger.exception("error ingesting %s", path.name)

    post = _count_news_signal_rows(session_factory)
    pred_total = _count_prediction_rows(session_factory)
    logger.info(
        "done — news_signals rows pre=%d post=%d (delta=%d), "
        "predictions=%d, ingested=%d, errors=%d",
        pre, post, post - pre, pred_total,
        counts["ingested"], counts["errors"],
    )
    return counts


def _count_news_signal_rows(session_factory) -> int:
    with session_factory() as s:
        return s.execute(select(func.count(NewsSignal.id))).scalar() or 0


def _count_prediction_rows(session_factory) -> int:
    with session_factory() as s:
        return s.execute(select(func.count(Prediction.id))).scalar() or 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


app = typer.Typer(
    add_completion=False,
    help="Ingest pre-downloaded alpha-report .txt files into news_signals.",
)


@app.command()
def main(
    dir: Path = typer.Option(
        ..., "--dir",
        help="Folder containing the .txt alpha reports.",
        exists=True, file_okay=False, dir_okay=True,
    ),
    user_id: str = typer.Option(
        "ariel", "--user-id",
        help="User the ingest is attributed to. Default: ariel.",
    ),
    channel_id: int = typer.Option(
        1324786557936472198, "--channel-id",
        help="Synthetic Discord channel id for the source_ref. "
             "Default matches the alpha-report channel for traceability.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Parse + report without writing any rows.",
    ),
) -> None:
    """Ingest every .txt file in --dir."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    counts = asyncio.run(
        _ingest_dir(
            dir,
            user_id=user_id,
            channel_id=channel_id,
            dry_run=dry_run,
        )
    )
    typer.echo(json.dumps(counts, indent=2))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    app()
