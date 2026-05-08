"""`argosy admin catalog-backfill` — one-shot backfill of the user_files table.

After Wave A lands, every NEW upload is cataloged at the boundary helper.
But files written before this wave shipped — Wave 5 chat attachments
under ``<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/`` and intake plan
markdown referenced by ``plan_versions.source_path`` — have no catalog
rows. This command walks those locations, hashes each file, and inserts
a ``user_files`` row pointing at the legacy path so they appear in the
Files UI alongside new uploads.

The command is **idempotent**: it skips files whose sha256 is already
cataloged for the given user (the partial unique index would reject the
INSERT anyway). Files are NOT relocated — old paths are kept so any
agent_reports response_text that references them stays valid.

Usage::

    argosy admin catalog-backfill                    # backfill ARGOSY_HOME
    argosy admin catalog-backfill --user-id ariel    # one user only
    argosy admin catalog-backfill --dry-run          # report, don't write
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
import typer

from argosy.config import get_settings
from argosy.logging import configure_logging, get_logger
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, PlanVersion, User, UserFile

_log = get_logger(__name__)


def _classify_kind(path: Path) -> str:
    """Pick a `kind` based on extension. Conservative; defaults to 'other'."""
    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        return "plan_markdown"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if ext in {".csv"}:
        return "broker_csv"
    if ext in {".txt", ".text", ".yaml", ".yml", ".json"}:
        return "text"
    return "other"


def _classify_source(path: Path, user_id: str) -> tuple[str, str | None]:
    """Pick a `source` and optional turn_uuid based on the legacy path."""
    # Wave 5 chat layout: <home>/uploads/<user_id>/<turn_uuid>/<file>
    parts = path.parts
    if "uploads" in parts:
        i = parts.index("uploads")
        # parts[i+1] is user_id; parts[i+2] is the segment (turn_uuid OR YYYY)
        if i + 2 < len(parts):
            segment = parts[i + 2]
            if len(segment) == 32 and all(c in "0123456789abcdef" for c in segment):
                return "chat_attachment", segment
    return "chat_attachment", None


async def _backfill_uploads_dir(
    user_filter: str | None, dry_run: bool,
) -> tuple[int, int]:
    """Walk <ARGOSY_HOME>/uploads/<user_id>/ and catalog every file."""
    home = Path(get_settings().home)
    uploads = home / "uploads"
    if not uploads.exists():
        typer.echo(f"(uploads dir not found at {uploads}; skipping)")
        return 0, 0

    inserted = 0
    skipped = 0
    for user_dir in sorted(uploads.iterdir()):
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        if user_filter is not None and user_id != user_filter:
            continue
        for path in user_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                typer.echo(f"  skip (read error): {path} — {exc}")
                continue
            sha = hashlib.sha256(raw).hexdigest()
            kind = _classify_kind(path)
            source, turn_uuid = _classify_source(path, user_id)
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            sanitized = path.name

            async with db_mod.get_session() as session:
                existing = (
                    await session.execute(
                        sa.select(UserFile).where(
                            UserFile.user_id == user_id,
                            UserFile.sha256 == sha,
                            UserFile.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    skipped += 1
                    continue

                # Ensure user row exists before FK insert.
                user = (
                    await session.execute(sa.select(User).where(User.id == user_id))
                ).scalar_one_or_none()
                if user is None:
                    typer.echo(
                        f"  warning: user_id={user_id!r} has no users row — "
                        "skipping (FK would fail)"
                    )
                    skipped += 1
                    continue

                if dry_run:
                    typer.echo(
                        f"  would insert: user={user_id} sha={sha[:8]} "
                        f"path={path}"
                    )
                    inserted += 1
                    continue

                row = UserFile(
                    user_id=user_id,
                    sha256=sha,
                    original_name=path.name,
                    sanitized_name=sanitized,
                    mime_type="application/octet-stream",
                    kind=kind,
                    size_bytes=len(raw),
                    storage_path=str(path),
                    source=source,
                    turn_uuid=turn_uuid,
                    created_at=mtime,
                )
                session.add(row)
                try:
                    await session.commit()
                except sa.exc.IntegrityError:
                    # Concurrent backfill or already cataloged via another path.
                    await session.rollback()
                    skipped += 1
                    continue
                await session.refresh(row)
                # Backfill audit event so the audit_log shows where each
                # legacy file came from.
                async with db_mod.get_session() as audit_session:
                    audit_session.add(AuditLog(
                        user_id=user_id,
                        event_type="provenance.upload.cataloged",
                        entity_type="user_file",
                        entity_id=str(row.id),
                        payload_json=json.dumps({
                            "sha256": sha,
                            "original_name": path.name,
                            "size_bytes": len(raw),
                            "storage_path": str(path),
                            "source": source,
                            "kind": kind,
                            "backfilled": True,
                        }),
                    ))
                    await audit_session.commit()
                inserted += 1
    return inserted, skipped


async def _backfill_plan_versions(
    user_filter: str | None, dry_run: bool,
) -> tuple[int, int]:
    """For each baseline plan_versions row whose source_path resolves to a
    real file, ensure a catalog row exists and set plan_versions.source_file_id.
    """
    matched = 0
    skipped = 0
    home = Path(get_settings().home)

    async with db_mod.get_session() as session:
        q = sa.select(PlanVersion).where(PlanVersion.source_file_id.is_(None))
        if user_filter is not None:
            q = q.where(PlanVersion.user_id == user_filter)
        rows = (await session.execute(q)).scalars().all()

        for pv in rows:
            # plan_versions.source_path is a filename (often just "x.md"),
            # not a full path. Search for a user_files row matching the
            # user_id + sanitized basename. If exactly one match, link it.
            candidates = (
                await session.execute(
                    sa.select(UserFile).where(
                        UserFile.user_id == pv.user_id,
                        UserFile.original_name == pv.source_path,
                        UserFile.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            if len(candidates) != 1:
                skipped += 1
                continue
            if dry_run:
                typer.echo(
                    f"  would link plan_version {pv.id} -> "
                    f"user_files.{candidates[0].id}"
                )
                matched += 1
                continue
            pv.source_file_id = candidates[0].id
            matched += 1

        if not dry_run:
            await session.commit()

    _ = home  # currently unused; reserved for future absolute-path resolution
    return matched, skipped


def catalog_backfill(
    user_id: str | None = typer.Option(
        None, "--user-id", help="Backfill only this user's files."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be inserted; don't write."
    ),
) -> None:
    """One-shot backfill of the user_files catalog from legacy filesystem state."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> None:
        typer.echo("== Pass 1: walking uploads/ ==")
        ins1, skip1 = await _backfill_uploads_dir(user_id, dry_run)
        typer.echo(f"   inserted: {ins1}   skipped: {skip1}")
        typer.echo("== Pass 2: linking plan_versions.source_file_id ==")
        ins2, skip2 = await _backfill_plan_versions(user_id, dry_run)
        typer.echo(f"   linked: {ins2}   skipped: {skip2}")
        if dry_run:
            typer.echo("(dry run — no rows were written)")

    asyncio.run(_main())


__all__ = ["catalog_backfill"]
