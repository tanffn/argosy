"""File catalog — single boundary helper for every user-supplied file (Wave A).

Background. Argosy ingests files via several distinct paths today:

  * Wave-5 chat attachments (``argosy/services/turn_attachments.py``)
  * Intake plan upload (``argosy/api/routes/intake.py::post_upload``)
  * Intake file-to-text conversion (``argosy/api/routes/intake.py``)
  * Broker cost-basis CSV import (``argosy/ingest/cost_basis.py``)

Each of those wrote to disk independently with no DB row, so there was no
way to ask "every file user X ever uploaded, ordered by date, with the
context of what triggered it." This module is the single boundary every
ingest path now flows through. New ingest paths are forced through it by
inspection — the helper is the contract.

Behavior:

  * **Content-addressed dedup per user** — sha256 + user_id form the dedup
    key. Re-uploading identical bytes for the same user collapses into the
    existing row instead of writing a second copy. Backed by the partial
    unique index ``ix_user_files_user_sha256_active`` (migration 0019)
    which excludes soft-deleted rows so a later re-upload after delete
    succeeds.
  * **Storage layout**:
    ``<ARGOSY_HOME>/uploads/<user_id>/<YYYY>/<YYYY-MM-DD>/<HHMMSS>__<sha8>__<sanitized>``.
    Old Wave-5 paths under ``<turn_uuid>/`` continue to work; the backfill
    CLI inserts catalog rows pointing at them.
  * **Audit trail** — each unique catalog write emits an
    ``audit_log`` row with ``event_type='provenance.upload.cataloged'``.
    Dedup hits do NOT emit (they would pollute the log with no-ops).
  * **Race recovery** — concurrent first-time uploads of the same bytes
    by the same user race on the partial unique index. The losing INSERT
    catches IntegrityError, re-SELECTs, and returns the survivor. Same
    pattern as ``argosy/orchestrator/flows/plan_amendment/dispatcher.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.services.turn_attachments import _sanitize_filename
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, UserFile

log = get_logger(__name__)


# Allowed values for the `source` column. Adding a new ingest path means
# adding a new entry here AND wiring it through `catalog_upload(...)`.
_ALLOWED_SOURCES = frozenset({
    "chat_attachment",
    "intake_upload",
    "intake_file_to_text",
    "cost_basis_import",
})

# Allowed values for the `kind` column. Cataloging-side classification
# (callers pass kind explicitly so the helper doesn't need to re-classify).
_ALLOWED_KINDS = frozenset({
    "text", "image", "plan_markdown", "broker_csv", "other",
})


class UserFileDTO(BaseModel):
    """Public DTO returned by ``catalog_upload`` and serialized by the
    REST surface. Mirrors the columns of ``UserFile`` (see
    ``argosy/state/models.py``).
    """

    id: int
    user_id: str
    sha256: str
    original_name: str
    sanitized_name: str
    mime_type: str
    kind: str
    size_bytes: int
    storage_path: str
    source: str
    turn_uuid: str | None = None
    intake_session_id: str | None = None
    plan_version_id: int | None = None
    decision_run_id: int | None = None
    created_at: datetime
    deleted_at: datetime | None = None

    @classmethod
    def from_orm_row(cls, row: UserFile) -> "UserFileDTO":
        return cls(
            id=row.id,
            user_id=row.user_id,
            sha256=row.sha256,
            original_name=row.original_name,
            sanitized_name=row.sanitized_name,
            mime_type=row.mime_type,
            kind=row.kind,
            size_bytes=row.size_bytes,
            storage_path=row.storage_path,
            source=row.source,
            turn_uuid=row.turn_uuid,
            intake_session_id=row.intake_session_id,
            plan_version_id=row.plan_version_id,
            decision_run_id=row.decision_run_id,
            created_at=row.created_at,
            deleted_at=row.deleted_at,
        )


def _storage_path_for(user_id: str, sha256: str, sanitized_name: str) -> Path:
    """Compute the new-layout absolute path for a fresh upload."""
    home = Path(get_settings().home)
    now = datetime.now(timezone.utc)
    year = now.strftime("%Y")
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H%M%S")
    short = sha256[:8]
    return (
        home / "uploads" / user_id / year / date
        / f"{time}__{short}__{sanitized_name}"
    )


async def catalog_upload(
    *,
    user_id: str,
    raw_bytes: bytes,
    original_name: str,
    mime_type: str,
    kind: str,
    source: str,
    turn_uuid: str | None = None,
    intake_session_id: str | None = None,
    plan_version_id: int | None = None,
    decision_run_id: int | None = None,
) -> UserFileDTO:
    """Catalog a single byte-blob. Idempotent on (user_id, sha256).

    Args:
        user_id: owner of the file (FK to ``users.id``).
        raw_bytes: full file content.
        original_name: filename as the user provided it.
        mime_type: MIME type (caller-provided; helper does not sniff).
        kind: one of ``_ALLOWED_KINDS``. Caller classifies.
        source: one of ``_ALLOWED_SOURCES``. The ingest channel.
        turn_uuid: chat-attachment context (when source='chat_attachment').
        intake_session_id: intake context (when source starts with 'intake_').
        plan_version_id: bridge to a baseline plan_versions row, if known.
        decision_run_id: bridge to a decision run, if known.

    Returns:
        UserFileDTO. Either the freshly-inserted row (a new file was written
        to disk + an audit_log row was emitted) OR the existing dedup-survivor
        row (no new disk write, no new audit emit).

    Raises:
        ValueError: ``kind`` or ``source`` not in the allowed set.
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {_ALLOWED_KINDS}, got {kind!r}")
    if source not in _ALLOWED_SOURCES:
        raise ValueError(
            f"source must be one of {_ALLOWED_SOURCES}, got {source!r}"
        )

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    sanitized = _sanitize_filename(original_name)
    size = len(raw_bytes)

    # Dedup pre-check. Fast path that avoids touching disk on repeat uploads.
    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(UserFile).where(
                    UserFile.user_id == user_id,
                    UserFile.sha256 == sha256,
                    UserFile.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.debug(
                "file_catalog.dedup_hit",
                user_id=user_id, sha256=sha256, file_id=existing.id,
            )
            return UserFileDTO.from_orm_row(existing)

    # First-time upload: write the file, then INSERT the row.
    storage_path = _storage_path_for(user_id, sha256, sanitized)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(raw_bytes)

    async with db_mod.get_session() as session:
        row = UserFile(
            user_id=user_id,
            sha256=sha256,
            original_name=original_name,
            sanitized_name=sanitized,
            mime_type=mime_type,
            kind=kind,
            size_bytes=size,
            storage_path=str(storage_path),
            source=source,
            turn_uuid=turn_uuid,
            intake_session_id=intake_session_id,
            plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Race: a concurrent first-time upload of identical bytes by
            # the same user beat us to the partial unique index. Our file
            # write was a no-op (same content), but we still need to clean
            # up our throwaway path and return the survivor row.
            await session.rollback()
            try:
                if storage_path.exists():
                    storage_path.unlink()
            except OSError:  # noqa: BLE001
                pass
            survivor = (
                await session.execute(
                    select(UserFile).where(
                        UserFile.user_id == user_id,
                        UserFile.sha256 == sha256,
                        UserFile.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            log.info(
                "file_catalog.dedup_race_recovered",
                user_id=user_id, sha256=sha256, file_id=survivor.id,
            )
            return UserFileDTO.from_orm_row(survivor)

        await session.refresh(row)

        # Emit a single audit_log row. Same session, same commit window.
        audit = AuditLog(
            user_id=user_id,
            event_type="provenance.upload.cataloged",
            entity_type="user_file",
            entity_id=str(row.id),
            payload_json=json.dumps({
                "sha256": sha256,
                "original_name": original_name,
                "sanitized_name": sanitized,
                "mime_type": mime_type,
                "kind": kind,
                "source": source,
                "size_bytes": size,
                "storage_path": str(storage_path),
                "turn_uuid": turn_uuid,
                "intake_session_id": intake_session_id,
                "plan_version_id": plan_version_id,
                "decision_run_id": decision_run_id,
            }),
        )
        session.add(audit)
        await session.commit()
        await session.refresh(row)

        dto = UserFileDTO.from_orm_row(row)

    log.info(
        "file_catalog.cataloged",
        user_id=user_id, sha256=sha256, file_id=dto.id, source=source, kind=kind,
    )
    return dto


__all__ = [
    "UserFileDTO",
    "catalog_upload",
]
