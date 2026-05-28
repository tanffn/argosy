"""REST surface for the user-files catalog (Wave A — provenance).

Endpoints:
  - GET  /api/files               — list catalog rows for a user, with filters
  - GET  /api/files/{id}/content  — stream the bytes of a single file
  - POST /api/files/upload        — generic catalog upload (NEW, 2026-05-29)

All endpoints respect the ``user_id`` ACL: a file is only visible to the
user who owns it. Soft-deleted rows are excluded by default; pass
``?include_deleted=true`` to opt in.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from argosy.logging import get_logger
from argosy.services.file_catalog import catalog_upload
from argosy.state import db as db_mod
from argosy.state.models import UserFile

log = get_logger("argosy.api.files")
router = APIRouter(prefix="/files", tags=["files"])


class UserFileItem(BaseModel):
    """One catalog row, serialized for the list endpoint."""

    id: int
    user_id: str
    sha256: str
    original_name: str
    sanitized_name: str
    mime_type: str
    kind: str
    size_bytes: int
    source: str
    turn_uuid: str | None
    intake_session_id: str | None
    plan_version_id: int | None
    decision_run_id: int | None
    created_at: datetime
    deleted_at: datetime | None


class FilesListResponse(BaseModel):
    """Page of catalog rows for one user."""

    items: list[UserFileItem]
    total: int
    limit: int
    offset: int


def _to_item(row: UserFile) -> UserFileItem:
    return UserFileItem(
        id=row.id,
        user_id=row.user_id,
        sha256=row.sha256,
        original_name=row.original_name,
        sanitized_name=row.sanitized_name,
        mime_type=row.mime_type,
        kind=row.kind,
        size_bytes=row.size_bytes,
        source=row.source,
        turn_uuid=row.turn_uuid,
        intake_session_id=row.intake_session_id,
        plan_version_id=row.plan_version_id,
        decision_run_id=row.decision_run_id,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


@router.get("", response_model=FilesListResponse)
async def list_files(
    user_id: str = Query("ariel"),
    kind: str | None = Query(None, description="Filter by kind: text|image|plan_markdown|broker_csv|other"),
    source: str | None = Query(None, description="Filter by source: chat_attachment|intake_upload|intake_file_to_text|cost_basis_import"),
    since: datetime | None = Query(None, description="Only return files created at-or-after this timestamp"),
    until: datetime | None = Query(None, description="Only return files created before this timestamp"),
    include_deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> FilesListResponse:
    """List a user's catalog rows, ordered newest first."""
    base = select(UserFile).where(UserFile.user_id == user_id)
    count_base = select(func.count(UserFile.id)).where(UserFile.user_id == user_id)
    if not include_deleted:
        base = base.where(UserFile.deleted_at.is_(None))
        count_base = count_base.where(UserFile.deleted_at.is_(None))
    if kind is not None:
        base = base.where(UserFile.kind == kind)
        count_base = count_base.where(UserFile.kind == kind)
    if source is not None:
        base = base.where(UserFile.source == source)
        count_base = count_base.where(UserFile.source == source)
    if since is not None:
        base = base.where(UserFile.created_at >= since)
        count_base = count_base.where(UserFile.created_at >= since)
    if until is not None:
        base = base.where(UserFile.created_at < until)
        count_base = count_base.where(UserFile.created_at < until)

    base = base.order_by(desc(UserFile.created_at)).limit(limit).offset(offset)

    async with db_mod.get_session() as session:
        rows = (await session.execute(base)).scalars().all()
        total = (await session.execute(count_base)).scalar_one()

    return FilesListResponse(
        items=[_to_item(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("/upload", response_model=UserFileItem)
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form("ariel"),
    kind: str = Form("other"),
) -> UserFileItem:
    """Generic catalog upload from the /files UI tile.

    Closes user-guide Hole #6 ("/files is read-only despite the name").
    Routes through the canonical ``catalog_upload`` funnel (SDD §17.1),
    same backend path the Advisor chat Attach button + the /expenses
    upload tile use. Source is stamped ``manual_upload`` so the row's
    provenance shows it came from the /files surface (not chat, not a
    statement-ingest pipeline).

    Allowed ``kind`` values: text | image | plan_markdown | broker_csv
    | other. UI defaults to "other" -- the user can pick a more
    specific kind via the UI, otherwise everything lands under "other".
    """
    contents = await file.read()
    user_file = await catalog_upload(
        user_id=user_id,
        raw_bytes=contents,
        original_name=file.filename or "unnamed",
        mime_type=file.content_type or "application/octet-stream",
        kind=kind,
        source="manual_upload",
    )
    # catalog_upload returns a UserFileDTO -- reshape into the catalog
    # row item the GET list endpoint already serves so the UI gets a
    # consistent shape across list + upload paths.
    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(UserFile).where(UserFile.id == user_file.id)
            )
        ).scalar_one()
    return _to_item(row)


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: int,
    user_id: str = Query("ariel"),
    include_deleted: bool = Query(False),
) -> FileResponse:
    """Stream the bytes of a single catalog row.

    ACL: the row's ``user_id`` must match the ``user_id`` query parameter.
    Soft-deleted rows return 404 unless ``include_deleted=true``.
    """
    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(UserFile).where(UserFile.id == file_id)
            )
        ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"file {file_id} not found")
    if row.user_id != user_id:
        # Don't leak existence — same 404 the wrong-user gets.
        raise HTTPException(status_code=404, detail=f"file {file_id} not found")
    if row.deleted_at is not None and not include_deleted:
        raise HTTPException(status_code=404, detail=f"file {file_id} not found")

    p = Path(row.storage_path)
    if not p.exists():
        log.error(
            "files.content.path_missing",
            file_id=file_id, user_id=user_id, storage_path=row.storage_path,
        )
        raise HTTPException(
            status_code=410,
            detail=f"file {file_id} catalog row exists but its bytes are missing on disk",
        )

    return FileResponse(
        path=str(p),
        media_type=row.mime_type,
        filename=row.original_name,
    )


__all__ = ["router"]
