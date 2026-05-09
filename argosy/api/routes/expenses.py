"""REST surface for the expenses subsystem (Wave EX1)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db    # reuse the existing get_db dep
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.services.file_catalog import catalog_upload

router = APIRouter(prefix="/api/expenses", tags=["expenses"])


class UploadFileResult(BaseModel):
    filename: str
    status: str                                # 'parsed' | 'failed'
    statement_id: int | None = None
    transactions_inserted: int = 0
    correlations_made: int = 0
    categories_resolved: int = 0
    refunds_matched: int = 0
    parser_name: str | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    results: list[UploadFileResult]


@router.post("/upload", response_model=UploadResponse)
async def upload_statements(
    files: list[UploadFile] = File(...),
    user_id: str = Form(...),
    db: Annotated[Session, Depends(get_db)] = ...,
) -> UploadResponse:
    """Multi-file ingestion. Each file flows through catalog_upload then
    ingest_user_file; per-file outcome is reported back.

    Note: catalog_upload is async and manages its own DB session internally
    (it uses db_mod.get_session() directly). The route's sync ``db`` session
    is only used by the sync ingest_user_file call that follows.
    """
    results: list[UploadFileResult] = []
    for upload in files:
        contents = await upload.read()
        try:
            # catalog_upload is async, takes no session arg, uses raw_bytes=
            # (not contents=), and returns UserFileDTO.
            user_file = await catalog_upload(
                user_id=user_id,
                raw_bytes=contents,
                original_name=upload.filename,
                mime_type=upload.content_type or "application/octet-stream",
                kind="other",
                source="chat_attachment",
            )
        except Exception as e:
            results.append(UploadFileResult(
                filename=upload.filename, status="failed",
                error=f"catalog failure: {e}",
            ))
            continue

        try:
            ing = ingest_user_file(db, user_id, user_file.id)
            db.commit()
        except Exception as e:
            db.rollback()
            results.append(UploadFileResult(
                filename=upload.filename, status="failed",
                error=str(e),
            ))
            continue

        results.append(UploadFileResult(
            filename=upload.filename, status="parsed",
            statement_id=ing.statement_id,
            transactions_inserted=ing.transactions_inserted,
            correlations_made=ing.correlations_made,
            categories_resolved=ing.categories_resolved,
            refunds_matched=ing.refunds_matched,
            parser_name=ing.parser_name,
        ))

    return UploadResponse(results=results)
