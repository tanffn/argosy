"""REST surface for the expenses subsystem (Wave EX1)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import case, extract, func, select as sa_select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db    # reuse the existing get_db dep
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.services.file_catalog import catalog_upload
from argosy.state.models import (
    ExpenseCategory,
    ExpenseSource,
    ExpenseStatement,
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
    card_last4: str | None = Form(None),
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

        # Max issuer requires card_last4 (the file itself only carries the
        # bank-account it bills to, not the card last-4). Detect format here
        # so we can fail-fast with a clear message before ingest tries to run.
        try:
            from argosy.services.expense_ingest.sniff import detect_format
            from argosy.services.expense_ingest.types import ParserName
            from pathlib import Path as _P
            detected = detect_format(_P(user_file.storage_path))
            if detected == ParserName.MAX and not card_last4:
                results.append(UploadFileResult(
                    filename=upload.filename, status="failed",
                    error="card_last4 required for Max uploads",
                ))
                continue
        except Exception:
            # Sniff failures will be re-raised by ingest_user_file with a
            # clearer error; don't swallow them silently here.
            pass

        try:
            ing = ingest_user_file(db, user_id, user_file.id, last4_hint=card_last4)
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
    amount_nis: float | None              # was: float — NULL for foreign rows post-EX1.1
    amount_orig: float | None = None
    currency_orig: str | None = None
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
                amount_nis=float(r.amount_nis) if r.amount_nis is not None else None,
                amount_orig=float(r.amount_orig) if r.amount_orig is not None else None,
                currency_orig=r.currency_orig,
                direction=r.direction,
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

class MonthlyTotalEntry(BaseModel):
    """Per-month aggregate, with totals split by currency.

    Foreign rows (amount_nis IS NULL after T12) contribute to their own
    currency bucket via amount_orig + currency_orig; native NIS rows
    populate the 'NIS' bucket via amount_nis.
    """

    month: str                            # 'YYYY-MM'
    totals_by_currency: dict[str, float]  # {'NIS': 12345.67, 'USD': 25.0}
    transaction_count: int


@router.get("/monthly-summary", response_model=list[MonthlyTotalEntry])
def monthly_summary(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = Query(default=12, ge=1, le=120),
) -> list[MonthlyTotalEntry]:
    """Per-(month, currency) totals. Excludes is_card_payment rows so card
    settlements don't double-count against per-card transaction totals.

    The amount used per row is `amount_nis` when present, else `amount_orig`
    (foreign rows where amount_nis was set to NULL by the parser per Bug 2.1).
    Currency is `currency_orig` for foreign rows, 'NIS' otherwise.
    """

    rows = db.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.sum(case(
                (ExpenseTransaction.amount_nis.is_not(None),
                 ExpenseTransaction.amount_nis),
                else_=ExpenseTransaction.amount_orig,
            )).label("total"),
            func.count().label("n"),
        )
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
        )
        .group_by("y", "m", "ccy")
        .order_by("y", "m")
    ).all()

    out: dict[str, MonthlyTotalEntry] = {}
    for y, m, ccy, total, n in rows:
        key = f"{int(y):04d}-{int(m):02d}"
        entry = out.get(key)
        if entry is None:
            entry = MonthlyTotalEntry(
                month=key, totals_by_currency={}, transaction_count=0,
            )
            out[key] = entry
        entry.totals_by_currency[ccy] = float(total or 0)
        entry.transaction_count += int(n)

    sorted_entries = sorted(out.values(), key=lambda e: e.month)
    return sorted_entries[-months:]


# ---------------------------------------------------------------------------
# GET /dashboard-overview
# ---------------------------------------------------------------------------

class CategorySpend(BaseModel):
    slug: str
    label_en: str
    total_nis: float
    transaction_count: int
    percent: float


class MerchantSpend(BaseModel):
    merchant_normalized: str
    merchant_display: str
    total_nis: float
    transaction_count: int
    category_slug: str | None


class AnomalyCard(BaseModel):
    kind: str                     # uncategorized | novel_merchant | large_outlier | fee_waiver_missed | conservation_gap
    severity: str                 # red | yellow | info
    message: str
    detail: str | None = None
    link: str | None = None


class SourceHealthEntry(BaseModel):
    source_id: int
    display_name: str
    issuer: str
    external_id: str
    last_period: date | None
    parsed_total_nis: float | None
    declared_total_nis: float | None
    gap: float | None
    status: str                   # green | yellow | red | unknown
    statement_count: int
    correlated_card_payments: int


class DashboardOverview(BaseModel):
    months: list[MonthlyTotalEntry]
    current_month_top_categories: list[CategorySpend]
    top_merchants_current_month: list[MerchantSpend]
    anomalies: list[AnomalyCard]
    sources_health: list[SourceHealthEntry]
    fx_mode: str


def _gap_status(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    a = abs(gap)
    if a < 0.5:
        return "green"
    if a < 5.0:
        return "yellow"
    return "red"


@router.get("/dashboard-overview", response_model=DashboardOverview)
def dashboard_overview(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = Query(default=12, ge=1, le=60),
    fx: str = Query(default="per_currency", regex="^(per_currency|nis)$"),
) -> DashboardOverview:
    # 1. Months — re-use the same SQL as /monthly-summary
    rows = db.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.sum(case(
                (ExpenseTransaction.amount_nis.is_not(None),
                 ExpenseTransaction.amount_nis),
                else_=ExpenseTransaction.amount_orig,
            )).label("total"),
            func.count().label("n"),
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .group_by("y", "m", "ccy")
        .order_by("y", "m")
    ).all()
    month_acc: dict[str, MonthlyTotalEntry] = {}
    for y, m, ccy, total, n in rows:
        if y is None or m is None:
            continue
        key = f"{int(y):04d}-{int(m):02d}"
        e = month_acc.setdefault(key, MonthlyTotalEntry(
            month=key, totals_by_currency={}, transaction_count=0,
        ))
        e.totals_by_currency[ccy or "NIS"] = float(total or 0)
        e.transaction_count += int(n)
    months_list = sorted(month_acc.values(), key=lambda e: e.month)[-months:]

    # 2. Current-month top categories (NIS, descending)
    if months_list:
        cur = months_list[-1].month  # 'YYYY-MM'
        cur_y, cur_m = (int(p) for p in cur.split("-"))
        cat_rows = db.execute(
            sa_select(
                ExpenseCategory.slug, ExpenseCategory.label_en,
                func.sum(ExpenseTransaction.amount_nis).label("total"),
                func.count().label("n"),
            )
            .join(ExpenseTransaction,
                  ExpenseTransaction.category_id == ExpenseCategory.id)
            .where(ExpenseTransaction.user_id == user_id)
            .where(ExpenseTransaction.is_card_payment.is_(False))
            .where(ExpenseTransaction.amount_nis.is_not(None))
            .where(extract("year", ExpenseTransaction.occurred_on) == cur_y)
            .where(extract("month", ExpenseTransaction.occurred_on) == cur_m)
            .where(ExpenseCategory.is_excluded_from_spend.is_(False))
            .group_by(ExpenseCategory.slug, ExpenseCategory.label_en)
            .order_by(func.sum(ExpenseTransaction.amount_nis).desc())
            .limit(10)
        ).all()
        total_month = sum(float(r.total or 0) for r in cat_rows) or 1.0
        top_cats = [
            CategorySpend(
                slug=r.slug, label_en=r.label_en,
                total_nis=float(r.total or 0),
                transaction_count=int(r.n or 0),
                percent=float(r.total or 0) / total_month * 100.0,
            )
            for r in cat_rows
        ]
    else:
        top_cats = []

    # 3. Top merchants (current month)
    if months_list:
        cur = months_list[-1].month
        cur_y, cur_m = (int(p) for p in cur.split("-"))
        mer_rows = db.execute(
            sa_select(
                ExpenseTransaction.merchant_normalized,
                func.max(ExpenseTransaction.merchant_raw).label("display"),
                func.sum(ExpenseTransaction.amount_nis).label("total"),
                func.count().label("n"),
                func.max(ExpenseCategory.slug).label("cat"),
            )
            .outerjoin(ExpenseCategory,
                       ExpenseCategory.id == ExpenseTransaction.category_id)
            .where(ExpenseTransaction.user_id == user_id)
            .where(ExpenseTransaction.is_card_payment.is_(False))
            .where(ExpenseTransaction.amount_nis.is_not(None))
            .where(ExpenseTransaction.direction == "debit")
            .where(extract("year", ExpenseTransaction.occurred_on) == cur_y)
            .where(extract("month", ExpenseTransaction.occurred_on) == cur_m)
            .group_by(ExpenseTransaction.merchant_normalized)
            .order_by(func.sum(ExpenseTransaction.amount_nis).desc())
            .limit(10)
        ).all()
        top_merchants = [
            MerchantSpend(
                merchant_normalized=r.merchant_normalized,
                merchant_display=r.display or r.merchant_normalized,
                total_nis=float(r.total or 0),
                transaction_count=int(r.n or 0),
                category_slug=r.cat,
            )
            for r in mer_rows
        ]
    else:
        top_merchants = []

    # 4. Anomalies
    anomalies: list[AnomalyCard] = []
    # 4a. Uncategorized count
    uncat_n = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.is_card_payment.is_(False),
    ).join(ExpenseCategory,
           ExpenseCategory.id == ExpenseTransaction.category_id).filter(
        ExpenseCategory.slug == "uncategorized",
    ).count()
    if uncat_n > 0:
        anomalies.append(AnomalyCard(
            kind="uncategorized", severity="yellow" if uncat_n < 50 else "red",
            message=f"{uncat_n} transactions are uncategorized",
            link="/expenses/transactions?category=uncategorized",
        ))
    # 4b. Conservation gaps (latest statement per source)
    for src_row in db.query(ExpenseSource).filter_by(user_id=user_id).all():
        latest = db.query(ExpenseStatement).filter_by(
            source_id=src_row.id, user_id=user_id,
        ).order_by(ExpenseStatement.period_end.desc()).first()
        if latest is None or latest.declared_total_nis is None:
            continue
        gap = float(latest.parsed_total_nis or 0) - float(latest.declared_total_nis)
        if abs(gap) >= 5.0:
            anomalies.append(AnomalyCard(
                kind="conservation_gap", severity="red",
                message=f"{src_row.display_name}: latest gap ₪{gap:+.2f}",
                detail=f"parsed={latest.parsed_total_nis} declared={latest.declared_total_nis}",
            ))
    # 4c. Card 2923 fee-waiver: if discount card has any standing-order fee row
    #     in latest statement but NO matching credit/refund row → flag.
    discount = db.query(ExpenseSource).filter_by(
        user_id=user_id, issuer="discount", external_id="2923",
    ).one_or_none()
    if discount is not None:
        latest = db.query(ExpenseStatement).filter_by(
            source_id=discount.id, user_id=user_id,
        ).order_by(ExpenseStatement.period_end.desc()).first()
        if latest is not None:
            stmt_txs = db.query(ExpenseTransaction).filter_by(
                statement_id=latest.id, user_id=user_id,
            ).all()
            fees = [t for t in stmt_txs
                    if t.direction == "debit"
                    and "כרטיס" in (t.merchant_raw or "")
                    and t.amount_nis and float(t.amount_nis) > 5]
            credits = [t for t in stmt_txs if t.direction == "credit"]
            if fees and not credits:
                anomalies.append(AnomalyCard(
                    kind="fee_waiver_missed", severity="red",
                    message="Discount Card 2923: card-fee charged with NO matching discount credit",
                    detail="Verify the fee-waiver promotion is still active",
                ))

    # 5. Sources health
    sources_health: list[SourceHealthEntry] = []
    for src_row in db.query(ExpenseSource).filter_by(
        user_id=user_id, active=True,
    ).order_by(ExpenseSource.created_at).all():
        latest = db.query(ExpenseStatement).filter_by(
            source_id=src_row.id, user_id=user_id,
        ).order_by(ExpenseStatement.period_end.desc()).first()
        stmt_n = db.query(ExpenseStatement).filter_by(
            source_id=src_row.id, user_id=user_id,
        ).count()
        gap = (
            float(latest.parsed_total_nis or 0) - float(latest.declared_total_nis)
            if latest and latest.declared_total_nis is not None
            else None
        )
        corr_n = db.query(ExpenseTransaction).filter_by(
            source_id=src_row.id, user_id=user_id, is_card_payment=True,
        ).count()
        sources_health.append(SourceHealthEntry(
            source_id=src_row.id, display_name=src_row.display_name,
            issuer=src_row.issuer, external_id=src_row.external_id,
            last_period=latest.period_end if latest else None,
            parsed_total_nis=float(latest.parsed_total_nis) if latest and latest.parsed_total_nis is not None else None,
            declared_total_nis=float(latest.declared_total_nis) if latest and latest.declared_total_nis is not None else None,
            gap=gap, status=_gap_status(gap),
            statement_count=stmt_n,
            correlated_card_payments=corr_n,
        ))

    return DashboardOverview(
        months=months_list,
        current_month_top_categories=top_cats,
        top_merchants_current_month=top_merchants,
        anomalies=anomalies,
        sources_health=sources_health,
        fx_mode=fx,
    )
