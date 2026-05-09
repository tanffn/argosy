"""REST surface for the expenses subsystem (Wave EX1)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db    # reuse the existing get_db dep
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.services.file_catalog import catalog_upload
from argosy.state.models import (
    ExpenseCategory,
    ExpenseSource,
    ExpenseTransaction,
    MerchantCategoryCache,
)

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
def upload_statements(
    files: list[UploadFile] = File(...),
    user_id: str = Form(...),
    db: Annotated[Session, Depends(get_db)] = ...,
) -> UploadResponse:
    """Multi-file ingestion. Each file flows through catalog_upload then
    ingest_user_file; per-file outcome is reported back.

    Sync (not async) on purpose: ``ingest_user_file`` → ``resolve_categories_for_user``
    → ``HouseholdCategorizerAgent._invoke_llm`` calls ``asyncio.run()``, which
    raises ``RuntimeError`` from inside an already-running event loop. FastAPI
    runs sync routes in a worker thread, so the inner ``asyncio.run()`` works
    correctly. ``catalog_upload`` is async, so we drive it from the worker
    thread via ``asyncio.run()`` here too.
    """
    results: list[UploadFileResult] = []
    for upload in files:
        # Sync read — `upload.read()` is async; in a sync route we read the
        # underlying SpooledTemporaryFile directly.
        contents = upload.file.read()
        try:
            # catalog_upload is async, takes no session arg, uses raw_bytes=
            # (not contents=), and returns UserFileDTO. Run on this thread's
            # own event loop so our outer event loop is undisturbed.
            user_file = asyncio.run(catalog_upload(
                user_id=user_id,
                raw_bytes=contents,
                original_name=upload.filename,
                mime_type=upload.content_type or "application/octet-stream",
                kind="other",
                source="chat_attachment",
            ))
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


# ---------------------------------------------------------------------------
# GET /sources
# ---------------------------------------------------------------------------

class SourceOut(BaseModel):
    id: int
    kind: str
    issuer: str
    external_id: str
    display_name: str
    cardholder_name: str | None
    active: bool


class SourcesResponse(BaseModel):
    sources: list[SourceOut]


@router.get("/sources", response_model=SourcesResponse)
def list_sources(user_id: str,
                 db: Annotated[Session, Depends(get_db)]) -> SourcesResponse:
    rows = db.query(ExpenseSource).filter_by(
        user_id=user_id, active=True
    ).order_by(ExpenseSource.created_at).all()
    return SourcesResponse(sources=[
        SourceOut(id=r.id, kind=r.kind, issuer=r.issuer,
                  external_id=r.external_id, display_name=r.display_name,
                  cardholder_name=r.cardholder_name, active=r.active)
        for r in rows
    ])


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------

class TransactionOut(BaseModel):
    id: int
    occurred_on: date
    merchant_raw: str
    amount_nis: float
    direction: str
    tx_type: str
    category_slug: str | None
    category_source: str | None
    is_card_payment: bool
    source_id: int


class TransactionsResponse(BaseModel):
    transactions: list[TransactionOut]
    total: int


@router.get("/transactions", response_model=TransactionsResponse)
def list_transactions(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    from_date: date | None = None,
    to_date: date | None = None,
    category: str | None = None,
    source_id: int | None = None,
    direction: str | None = None,
    include_card_payments: bool = False,
    search: str | None = None,
    limit: int = Query(default=200, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
) -> TransactionsResponse:
    q = db.query(ExpenseTransaction).filter_by(user_id=user_id)
    if not include_card_payments:
        q = q.filter(ExpenseTransaction.is_card_payment.is_(False))
    if from_date:
        q = q.filter(ExpenseTransaction.occurred_on >= from_date)
    if to_date:
        q = q.filter(ExpenseTransaction.occurred_on <= to_date)
    if category:
        cat = db.query(ExpenseCategory).filter_by(
            user_id=user_id, slug=category).one_or_none()
        if cat is None:
            return TransactionsResponse(transactions=[], total=0)
        q = q.filter(ExpenseTransaction.category_id == cat.id)
    if source_id:
        q = q.filter(ExpenseTransaction.source_id == source_id)
    if direction:
        q = q.filter(ExpenseTransaction.direction == direction)
    if search:
        like = f"%{search}%"
        q = q.filter(ExpenseTransaction.merchant_raw.ilike(like))

    total = q.count()
    rows = q.order_by(ExpenseTransaction.occurred_on.desc()) \
            .offset(offset).limit(limit).all()
    cat_by_id = {
        c.id: c.slug for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    return TransactionsResponse(
        transactions=[
            TransactionOut(
                id=r.id, occurred_on=r.occurred_on, merchant_raw=r.merchant_raw,
                amount_nis=float(r.amount_nis), direction=r.direction,
                tx_type=r.tx_type,
                category_slug=cat_by_id.get(r.category_id),
                category_source=r.category_source,
                is_card_payment=r.is_card_payment,
                source_id=r.source_id,
            )
            for r in rows
        ],
        total=total,
    )


# ---------------------------------------------------------------------------
# PATCH /transactions/{transaction_id}
# ---------------------------------------------------------------------------

class PatchCategoryRequest(BaseModel):
    user_id: str
    category_slug: str


class PatchCategoryResponse(BaseModel):
    transaction_id: int
    category_slug: str
    category_source: str
    affected_count: int


@router.patch("/transactions/{transaction_id}",
               response_model=PatchCategoryResponse)
def patch_transaction_category(
    transaction_id: int,
    body: PatchCategoryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PatchCategoryResponse:
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    cat = db.query(ExpenseCategory).filter_by(
        user_id=body.user_id, slug=body.category_slug
    ).one_or_none()
    if cat is None:
        raise HTTPException(status_code=400,
                             detail=f"unknown category {body.category_slug}")
    tx.category_id = cat.id
    tx.category_source = "user"
    tx.category_confidence = Decimal("1.00")

    pattern = tx.merchant_normalized
    cache = db.query(MerchantCategoryCache).filter_by(
        user_id=body.user_id, merchant_pattern=pattern, is_regex=False,
    ).one_or_none()
    if cache is None:
        db.add(MerchantCategoryCache(
            user_id=body.user_id, merchant_pattern=pattern,
            category_id=cat.id, source="user", confidence=Decimal("1.00"),
            hit_count=1, last_hit_at=datetime.now(timezone.utc),
        ))
    else:
        cache.category_id = cat.id
        cache.source = "user"
        cache.confidence = Decimal("1.00")
        cache.hit_count += 1
        cache.last_hit_at = datetime.now(timezone.utc)

    siblings = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == body.user_id,
        ExpenseTransaction.merchant_normalized == pattern,
        ExpenseTransaction.id != tx.id,
    ).all()
    for sib in siblings:
        sib.category_id = cat.id
        sib.category_source = "user"
        sib.category_confidence = Decimal("1.00")

    db.commit()
    return PatchCategoryResponse(
        transaction_id=tx.id, category_slug=body.category_slug,
        category_source="user", affected_count=1 + len(siblings),
    )


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------

class CategoryOut(BaseModel):
    id: int
    slug: str
    label_en: str
    label_he: str
    parent_slug: str | None
    is_excluded_from_spend: bool
    is_inflow: bool


class CategoriesResponse(BaseModel):
    categories: list[CategoryOut]


@router.get("/categories", response_model=CategoriesResponse)
def list_categories(user_id: str,
                    db: Annotated[Session, Depends(get_db)]) -> CategoriesResponse:
    rows = db.query(ExpenseCategory).filter_by(user_id=user_id) \
             .order_by(ExpenseCategory.display_order).all()
    by_id = {r.id: r.slug for r in rows}
    return CategoriesResponse(categories=[
        CategoryOut(
            id=r.id, slug=r.slug, label_en=r.label_en, label_he=r.label_he,
            parent_slug=by_id.get(r.parent_id),
            is_excluded_from_spend=r.is_excluded_from_spend,
            is_inflow=r.is_inflow,
        )
        for r in rows
    ])


# ---------------------------------------------------------------------------
# GET /monthly-summary
# ---------------------------------------------------------------------------

class MonthlySummaryRow(BaseModel):
    month: str                     # 'YYYY-MM'
    total_real_spend_nis: float
    total_real_income_nis: float
    by_category: dict[str, float]


class MonthlySummaryResponse(BaseModel):
    by_month: list[MonthlySummaryRow]


@router.get("/monthly-summary", response_model=MonthlySummaryResponse)
def monthly_summary(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = Query(default=12, ge=1, le=120),
) -> MonthlySummaryResponse:
    """Aggregate by (month, category). Excludes is_card_payment rows."""

    cats = {c.id: c for c in db.query(ExpenseCategory).filter_by(
        user_id=user_id
    ).all()}
    rows = db.query(
        func.strftime("%Y-%m", ExpenseTransaction.occurred_on).label("ym"),
        ExpenseTransaction.category_id,
        ExpenseTransaction.direction,
        func.sum(ExpenseTransaction.amount_nis).label("total"),
    ).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.is_card_payment.is_(False),
    ).group_by("ym", ExpenseTransaction.category_id,
                ExpenseTransaction.direction).all()

    by_month: dict[str, MonthlySummaryRow] = {}
    for ym, cat_id, direction, total in rows:
        if ym not in by_month:
            by_month[ym] = MonthlySummaryRow(
                month=ym, total_real_spend_nis=0.0,
                total_real_income_nis=0.0, by_category={},
            )
        cat = cats.get(cat_id)
        slug = cat.slug if cat else "uncategorized"
        by_month[ym].by_category[slug] = (
            by_month[ym].by_category.get(slug, 0.0) + float(total)
        )
        if cat is None:
            continue
        if cat.is_inflow and direction == "credit":
            by_month[ym].total_real_income_nis += float(total)
        elif (not cat.is_inflow and not cat.is_excluded_from_spend
              and direction == "debit"):
            by_month[ym].total_real_spend_nis += float(total)

    sorted_months = sorted(by_month.keys(), reverse=True)[:months]
    return MonthlySummaryResponse(
        by_month=[by_month[m] for m in sorted(sorted_months)]
    )
