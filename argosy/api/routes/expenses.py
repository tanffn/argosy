"""REST surface for the expenses subsystem (Wave EX1)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
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
    tags: list[str] = []                   # JSON-stored on row; '[]' when unset


class TransactionsResponse(BaseModel):
    transactions: list[TransactionOut]
    total: int


def _tx_to_out(
    r: "ExpenseTransaction",
    cat_by_id: dict[int, str],
) -> TransactionOut:
    """Marshal an ExpenseTransaction row into the TransactionOut DTO.

    Centralised so that adding new fields (e.g. tags from migration 0024)
    only needs to be done in one place.
    """
    return TransactionOut(
        id=r.id, occurred_on=r.occurred_on, merchant_raw=r.merchant_raw,
        amount_nis=float(r.amount_nis) if r.amount_nis is not None else None,
        amount_orig=float(r.amount_orig) if r.amount_orig is not None else None,
        currency_orig=r.currency_orig,
        direction=r.direction, tx_type=r.tx_type,
        category_slug=cat_by_id.get(r.category_id),
        category_source=r.category_source,
        is_card_payment=r.is_card_payment,
        source_id=r.source_id,
        tags=_parse_tags(getattr(r, "tags", None)),
    )



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
    tag: str | None = None,
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
    if tag:
        # Tags are JSON-stored as `'["a","b"]'`; LIKE pattern matches the
        # quoted member. Cheap at single-user scale (~1k rows).
        like = f'%"{tag}"%'
        q = q.filter(ExpenseTransaction.tags.like(like))

    total = q.count()
    rows = q.order_by(ExpenseTransaction.occurred_on.desc()) \
            .offset(offset).limit(limit).all()
    cat_by_id = {
        c.id: c.slug for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    return TransactionsResponse(
        transactions=[_tx_to_out(r, cat_by_id) for r in rows],
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
    """Per-month aggregate, split by currency.

    Foreign rows (amount_nis IS NULL after T12) contribute to their own
    currency bucket via amount_orig + currency_orig; native NIS rows
    populate the 'NIS' bucket via amount_nis.

    Used by /dashboard-overview as the "Monthly spend" chart series. As of
    EX4-fix-2026-05, the dashboard's `months` series is restricted to
    SPENDING ONLY — direction='debit' AND category.is_inflow=False AND
    category.is_excluded_from_spend=False. Inflows (salary, RSU vests,
    refunds, dividends), transfers, and investments are deliberately not
    in the chart: they are not "what you spent". /monthly-summary keeps
    the older, broader semantics for back-compat.
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


class YearlySummary(BaseModel):
    """12-month rollup (NIS-only; foreign rows excluded since their NIS
    conversion may be unavailable).

    Window selection: see ``DashboardOverview.window`` query param.

      * ``window=trailing_12`` (default): the 12 calendar months ending at
        the latest month with data. So if the latest month is May 2026,
        the window is Jun 2025 → May 2026.
      * ``window=calendar_year``: Jan 1 → Dec 31 of the YEAR of the latest
        month with data. So in May 2026 the window is Jan-Dec 2026 (which
        will only have 5 months reflected: Jan-May).

    months_covered is the actual count of distinct months in the rollup
    (≤ 12). avg_per_month_nis divides yearly_spending_total_nis by
    months_covered (or 0 if no months). current_vs_avg_pct =
    (current_month_spending_nis / avg_spending) - 1, expressed as a
    percentage; null when there's no average to compare against.

    yearly_spending_total_nis  — debits with is_inflow=False AND
                                  is_excluded_from_spend=False (real outflow).
    yearly_income_total_nis    — credits with is_inflow=True AND
                                  tx_type != 'refund' (real income: salary, RSU, dividends, ...).
    yearly_refunds_total_nis   — credits with is_inflow=True AND tx_type='refund'.
    yearly_inflow_total_nis    — DEPRECATED alias = income + refunds. Kept for
                                  back-compat with older UI/tests.
    total_nis                  — DEPRECATED alias for yearly_spending_total_nis;
                                  kept for backward compat with existing UI/tests.
    top_categories_12m         — ALL spending categories with a non-zero total
                                  in the window, sorted desc by total_nis. The
                                  frontend can paginate / show-all as desired.
                                  (Pre-2026-05 this was top-5 only.)
    window                     — 'trailing_12' or 'calendar_year'.
    window_label               — Pretty label for UI: 'Trailing 12 months' or
                                  '2026 (calendar year)'.
    window_start_month         — 'YYYY-MM' first month of the window.
    window_end_month           — 'YYYY-MM' last month of the window.
    """

    months_covered: int
    total_nis: float
    yearly_spending_total_nis: float
    yearly_income_total_nis: float
    yearly_refunds_total_nis: float
    yearly_inflow_total_nis: float
    avg_per_month_nis: float
    top_categories_12m: list[CategorySpend]
    current_vs_avg_pct: float | None
    window: str = "trailing_12"
    window_label: str = ""
    window_start_month: str = ""
    window_end_month: str = ""


class DividendsSummary(BaseModel):
    """Monthly dividends rollup. Detects rows by Hebrew Leumi wording
    (`נ"ע רבית/דו` / `דיב`) or English (`dividend` / `DIV`).

    `transactions` is the underlying USD-credit rows for the focal month so
    the user can scan what came in.
    """

    month: str                                # focal month (latest or query-param)
    current_month_total_usd: float            # USD dividends in focal month
    yearly_total_usd: float                   # 12-month rolling
    monthly_series: list[dict]                # [{month: 'YYYY-MM', total_usd: ...}, ...]
    transactions: list[TransactionOut]


class TaxesSummary(BaseModel):
    """Taxes paid rollup — NIS direct (income/property/SS) + USD RSU
    withholding (Schwab) when accessible.

    `by_kind` keys: ``income_tax_paid``, ``social_security_paid``,
    ``property_tax``, ``rsu_withholding_usd`` (USD). NIS-direct kinds map
    to NIS sums; ``rsu_withholding_usd`` is in USD (separate currency).
    """

    yearly_total_nis: float                   # NIS taxes (income+property+SS)
    yearly_total_usd: float                   # USD taxes (Schwab withholding)
    by_kind: dict[str, float]


class DashboardOverview(BaseModel):
    months: list[MonthlyTotalEntry]
    current_month: str | None                       # 'YYYY-MM' the headline scopes to
    current_month_spending_nis: float               # NIS-only, spending-only
    current_month_income_nis: float                 # credits, tx_type != 'refund'
    current_month_refunds_nis: float                # credits, tx_type == 'refund'
    current_month_inflow_nis: float                 # DEPRECATED alias = income + refunds
    current_month_top_categories: list[CategorySpend]
    current_month_income: list[CategorySpend]       # income (non-refund) by category
    current_month_inflow: list[CategorySpend]       # DEPRECATED alias for current_month_income+refunds
    top_merchants_current_month: list[MerchantSpend]
    anomalies: list[AnomalyCard]
    sources_health: list[SourceHealthEntry]
    yearly_summary: YearlySummary
    dividends: DividendsSummary
    taxes: TaxesSummary
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


# Dividend detection: Leumi USD account rows wear Hebrew descriptors
# ('נ"ע רבית/דו' / 'דיב'); Schwab/etoro brokers use English ('dividend',
# 'DIV'). We accept either pattern. Direction must be 'credit' and currency
# 'USD' (the Leumi USD pmach account is the only place these land for now;
# generalising to other USD accounts is forward-compatible).
_DIVIDEND_HE = ("דיב", "רבית/דו", "רבית/דב")
_DIVIDEND_EN = ("dividend", "div ")    # 'div ' to avoid 'divan'/'divine'/etc

def _is_dividend_row(merchant_norm: str | None,
                      merchant_raw: str | None,
                      currency_orig: str | None) -> bool:
    if currency_orig != "USD":
        return False
    text = (merchant_norm or "").lower() + " " + (merchant_raw or "").lower()
    if any(token in text for token in _DIVIDEND_EN):
        return True
    raw_he = (merchant_raw or "") + " " + (merchant_norm or "")
    if any(token in raw_he for token in _DIVIDEND_HE):
        return True
    return False


# Tax detection: category-driven so we use the user's taxonomy rather than
# guessing from merchant text.
_TAX_CATEGORY_SLUGS = {
    "taxes",
    "taxes.income_tax_paid",
    "taxes.social_security_paid",
    "housing.property_tax",
}
# Slug -> by_kind label.
_TAX_KIND_LABEL = {
    "taxes": "other_taxes",
    "taxes.income_tax_paid": "income_tax_paid",
    "taxes.social_security_paid": "social_security_paid",
    "housing.property_tax": "property_tax",
}


@router.get("/dashboard-overview", response_model=DashboardOverview)
def dashboard_overview(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = Query(default=12, ge=1, le=60),
    fx: str = Query(default="per_currency", pattern="^(per_currency|nis)$"),
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-\d{2}$",
        description=(
            "Optional 'YYYY-MM' to scope current_month_* fields and the "
            "current-vs-avg comparison to a specific month instead of the "
            "latest month with data. Format: YYYY-MM (e.g. '2026-04')."
        ),
    ),
    window: str = Query(
        default="trailing_12",
        pattern="^(trailing_12|calendar_year)$",
        description=(
            "Yearly-summary window. 'trailing_12' (default): the 12 months "
            "ending at the latest month with data. 'calendar_year': Jan 1 "
            "to Dec 31 of the year of the latest month with data (so a "
            "partial year shows the months it has)."
        ),
    ),
) -> DashboardOverview:
    """Dashboard overview bundle.

    Spending vs inflow are kept strictly separate across this endpoint:

    - ``current_month_top_categories``, ``top_merchants_current_month``,
      ``yearly_summary.top_categories_12m``,
      ``yearly_summary.yearly_spending_total_nis``,
      ``yearly_summary.avg_per_month_nis`` and
      ``yearly_summary.current_vs_avg_pct`` exclude inflow categories
      (``is_inflow=True``) AND categories explicitly excluded from spend
      (``is_excluded_from_spend=True``). Together that's "real outflow".
    - ``current_month_inflow`` and ``yearly_summary.yearly_inflow_total_nis``
      cover only ``is_inflow=True`` categories — salary, RSU vest proceeds,
      bonus, dividends, refunds, etc.
    - ``months`` (the chart series) keeps both legs combined so the user can
      still see overall activity per month — that's a known compromise; it's
      a chart, not a "you spent X" headline.

    The ``month`` query parameter scopes ``current_month_*`` and
    ``current_vs_avg_pct`` to a chosen month (default: latest month with
    data).
    """
    # 1. Months — SPENDING-ONLY chart series.
    #    The user surfaced this bug: the previous query summed "all activity",
    #    so an Apr 2026 month with $40K of investment buys + $151K of RSU
    #    disbursement showed a huge USD bar that meant "money MOVED" — not
    #    money spent. Now: direction='debit', category.is_inflow=False,
    #    category.is_excluded_from_spend=False (mirrors the same filter as
    #    current_month_top_categories). Uncategorised debits are still
    #    included via outer-join (they're real outflow, just unlabelled).
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
        .outerjoin(ExpenseCategory,
                   ExpenseCategory.id == ExpenseTransaction.category_id)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .where(ExpenseTransaction.direction == "debit")
        .where((ExpenseCategory.id.is_(None)) |
               ((ExpenseCategory.is_excluded_from_spend.is_(False)) &
                (ExpenseCategory.is_inflow.is_(False))))
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

    # Pick the focal month: explicit ``month=`` param if given, else the
    # latest month with data. If the param doesn't match any month with
    # data we still honour it (the queries below will return empty rows,
    # which is honest behaviour).
    if month is not None:
        focal_month = month
    elif months_list:
        focal_month = months_list[-1].month
    else:
        focal_month = None

    # 2. Current-month top categories — SPENDING only (excludes inflows AND
    #    excluded-from-spend like transfers/investments).
    if focal_month is not None:
        cur_y, cur_m = (int(p) for p in focal_month.split("-"))
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
            .where(ExpenseCategory.is_inflow.is_(False))
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
        cur_month_spending_nis = sum(float(r.total or 0) for r in cat_rows)

        # 2b. Current-month INCOME (real income — credits, NOT refunds).
        income_rows = db.execute(
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
            .where(ExpenseTransaction.direction == "credit")
            .where(ExpenseTransaction.tx_type != "refund")
            .group_by(ExpenseCategory.slug, ExpenseCategory.label_en)
            .order_by(func.sum(ExpenseTransaction.amount_nis).desc())
            .limit(10)
        ).all()
        cur_month_income_nis = sum(float(r.total or 0) for r in income_rows)
        income_pct_base = cur_month_income_nis or 1.0
        income_cats = [
            CategorySpend(
                slug=r.slug, label_en=r.label_en,
                total_nis=float(r.total or 0),
                transaction_count=int(r.n or 0),
                percent=float(r.total or 0) / income_pct_base * 100.0,
            )
            for r in income_rows
        ]

        # 2c. Current-month REFUNDS (credits with tx_type='refund').
        cur_month_refunds_nis = float(db.execute(
            sa_select(
                func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0),
            )
            .where(ExpenseTransaction.user_id == user_id)
            .where(ExpenseTransaction.is_card_payment.is_(False))
            .where(ExpenseTransaction.amount_nis.is_not(None))
            .where(extract("year", ExpenseTransaction.occurred_on) == cur_y)
            .where(extract("month", ExpenseTransaction.occurred_on) == cur_m)
            .where(ExpenseTransaction.direction == "credit")
            .where(ExpenseTransaction.tx_type == "refund")
        ).scalar() or 0)
        # Deprecated alias = income + refunds (= what the old field meant).
        cur_month_inflow_nis = cur_month_income_nis + cur_month_refunds_nis
        # current_month_inflow stays as the alias view: income breakdown
        # mirrors income_cats since we want one canonical "real income"
        # category list. This preserves callers that read `current_month_inflow`.
        inflow_cats = income_cats
    else:
        top_cats = []
        income_cats = []
        inflow_cats = []
        cur_month_spending_nis = 0.0
        cur_month_income_nis = 0.0
        cur_month_refunds_nis = 0.0
        cur_month_inflow_nis = 0.0

    # 3. Top merchants (focal month) — SPENDING only.
    if focal_month is not None:
        cur_y, cur_m = (int(p) for p in focal_month.split("-"))
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
            .where((ExpenseCategory.id.is_(None)) |
                   ((ExpenseCategory.is_excluded_from_spend.is_(False)) &
                    (ExpenseCategory.is_inflow.is_(False))))
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

    # 4. Anomalies — base set + oddity detectors below.
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
                link="/expenses/sources",
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
                    link=f"/expenses/transactions?source_id={discount.id}&include_card_payments=1",
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

    # 6. Yearly summary — windowed rollup, NIS-only.
    #    Anchor the window to the LATEST MONTH WITH DATA (not today) so
    #    partial corpora render sensible numbers, and so the "current month"
    #    headline is actually inside the rollup.
    #
    #    Two windows supported:
    #
    #      trailing_12 (default): the 12 months ending at the latest month
    #        with data. So if latest month is 2026-05, window is
    #        2025-06 → 2026-05.
    #
    #      calendar_year: Jan 1 → Dec 31 of the YEAR of the latest month
    #        with data. So if latest month is 2026-05, window is
    #        2026-01 → 2026-05 in practice (months actually present in the
    #        data — months_covered counts distinct months with rows).
    if months_list:
        last_y, last_m = (int(p) for p in months_list[-1].month.split("-"))
        if last_m == 12:
            anchor = date(last_y, 12, 31)
        else:
            anchor = date(last_y, last_m + 1, 1) - timedelta(days=1)
        if window == "calendar_year":
            window_start = date(last_y, 1, 1)
            window_label = f"{last_y} (calendar year)"
            window_start_month = f"{last_y:04d}-01"
            window_end_month = f"{last_y:04d}-{last_m:02d}"
        else:
            # trailing_12: window starts at the first month of the last_12
            # slice for clean month boundaries.
            last_12 = months_list[-12:]
            first_y, first_m = (int(p) for p in last_12[0].month.split("-"))
            window_start = date(first_y, first_m, 1)
            window_label = "Trailing 12 months"
            window_start_month = f"{first_y:04d}-{first_m:02d}"
            window_end_month = f"{last_y:04d}-{last_m:02d}"
        # months_covered: distinct months in months_list that fall within
        # [window_start, anchor]. For trailing_12 that's the len of the slice;
        # for calendar_year it's the count of months >= window_start.
        ws_key = f"{window_start.year:04d}-{window_start.month:02d}"
        in_window = [m for m in months_list
                     if ws_key <= m.month <= window_end_month]
        months_covered = len(in_window)
    else:
        anchor = date.today()
        window_start = anchor - timedelta(days=365)
        window_label = (
            f"{anchor.year} (calendar year)" if window == "calendar_year"
            else "Trailing 12 months"
        )
        window_start_month = ""
        window_end_month = ""
        months_covered = 0

    # Yearly SPENDING total + top categories — exclude inflow & excluded.
    spending_12m_rows = db.execute(
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
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= anchor)
        .where(ExpenseCategory.is_excluded_from_spend.is_(False))
        .where(ExpenseCategory.is_inflow.is_(False))
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en)
        .order_by(func.sum(ExpenseTransaction.amount_nis).desc())
    ).all()
    yearly_spending_total_nis = sum(
        float(r.total or 0) for r in spending_12m_rows
    )
    spending_pct_base = yearly_spending_total_nis or 1.0
    # ALL spending categories with a non-zero total — sorted desc by total_nis
    # (the underlying SQL already ORDER BY desc). Frontend may paginate /
    # show-all as desired. Filter zero-totals defensively.
    top_cats_12m = [
        CategorySpend(
            slug=r.slug, label_en=r.label_en,
            total_nis=float(r.total or 0),
            transaction_count=int(r.n or 0),
            percent=float(r.total or 0) / spending_pct_base * 100.0,
        )
        for r in spending_12m_rows
        if float(r.total or 0) > 0
    ]
    avg_per_month_nis = (
        (yearly_spending_total_nis / months_covered) if months_covered else 0.0
    )

    # Yearly INCOME total (credits, non-refund). Split from refunds.
    yearly_income_total_nis = float(db.execute(
        sa_select(
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0),
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .where(ExpenseTransaction.amount_nis.is_not(None))
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= anchor)
        .where(ExpenseTransaction.direction == "credit")
        .where(ExpenseTransaction.tx_type != "refund")
    ).scalar() or 0)
    # Yearly REFUNDS total (credits, refund tx_type).
    yearly_refunds_total_nis = float(db.execute(
        sa_select(
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0),
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .where(ExpenseTransaction.amount_nis.is_not(None))
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= anchor)
        .where(ExpenseTransaction.direction == "credit")
        .where(ExpenseTransaction.tx_type == "refund")
    ).scalar() or 0)
    # Deprecated alias kept for back-compat.
    yearly_inflow_total_nis = yearly_income_total_nis + yearly_refunds_total_nis

    # current_vs_avg_pct: spending-vs-spending. Use the focal month's
    # spending (computed above) so the trend reflects the headline.
    current_vs_avg_pct: float | None
    if avg_per_month_nis > 0 and focal_month is not None:
        current_vs_avg_pct = (cur_month_spending_nis / avg_per_month_nis - 1.0) * 100.0
    else:
        current_vs_avg_pct = None

    yearly = YearlySummary(
        months_covered=months_covered,
        total_nis=yearly_spending_total_nis,
        yearly_spending_total_nis=yearly_spending_total_nis,
        yearly_income_total_nis=yearly_income_total_nis,
        yearly_refunds_total_nis=yearly_refunds_total_nis,
        yearly_inflow_total_nis=yearly_inflow_total_nis,
        avg_per_month_nis=avg_per_month_nis,
        top_categories_12m=top_cats_12m,
        current_vs_avg_pct=current_vs_avg_pct,
        window=window,
        window_label=window_label,
        window_start_month=window_start_month,
        window_end_month=window_end_month,
    )

    # 6b. Anomaly oddities — sensible defaults the user authorised:
    #     - merchant_spike: a single tx > 5x the merchant's 12-month average.
    #     - new_high_value_merchant: a tx >= ₪500 from a merchant unseen in the
    #       prior 12 months.
    #     Cap each at top 5 (most recent first). Only consider focal-month tx
    #     so this fires on what the user is currently looking at.
    if focal_month is not None:
        cur_y, cur_m = (int(p) for p in focal_month.split("-"))
        focal_first = date(cur_y, cur_m, 1)
        from calendar import monthrange as _mr
        focal_last_day = _mr(cur_y, cur_m)[1]
        focal_last = date(cur_y, cur_m, focal_last_day)
        focal_txs = db.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseTransaction.amount_nis.is_not(None),
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= focal_first,
            ExpenseTransaction.occurred_on <= focal_last,
        ).all()

        spikes: list[tuple[float, AnomalyCard]] = []
        new_high: list[tuple[float, AnomalyCard]] = []
        # Pre-compute prior-12mo per-merchant stats once per merchant we hit
        # in the focal month — cheap because we only iterate distinct merchants
        # the user actually has activity for this month.
        merchant_cache: dict[str, dict[str, float | int]] = {}
        prior_window_end = focal_first - timedelta(days=1)
        prior_window_start = max(window_start, prior_window_end - timedelta(days=365))
        for tx in focal_txs:
            mn = tx.merchant_normalized or ""
            if not mn:
                continue
            stats = merchant_cache.get(mn)
            if stats is None:
                rows = db.execute(
                    sa_select(
                        func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0),
                        func.count(),
                    )
                    .where(ExpenseTransaction.user_id == user_id)
                    .where(ExpenseTransaction.is_card_payment.is_(False))
                    .where(ExpenseTransaction.amount_nis.is_not(None))
                    .where(ExpenseTransaction.direction == "debit")
                    .where(ExpenseTransaction.merchant_normalized == mn)
                    .where(ExpenseTransaction.occurred_on >= prior_window_start)
                    .where(ExpenseTransaction.occurred_on <= prior_window_end)
                ).one()
                total_prior, n_prior = float(rows[0] or 0), int(rows[1] or 0)
                stats = {
                    "avg": (total_prior / n_prior) if n_prior else 0.0,
                    "n": n_prior,
                }
                merchant_cache[mn] = stats
            tx_amount = float(tx.amount_nis or 0)
            display = (tx.merchant_raw or mn).strip()[:40]
            link = (
                f"/expenses/transactions?search="
                + (display.replace(" ", "%20") if display else "")
            )
            avg_prior = float(stats["avg"])
            n_prior = int(stats["n"])
            if avg_prior > 0 and tx_amount > 5 * avg_prior:
                ratio = tx_amount / avg_prior
                spikes.append((tx_amount, AnomalyCard(
                    kind="merchant_spike", severity="yellow",
                    message=f"{display}: ₪{tx_amount:,.0f} ({ratio:.1f}× usual)",
                    detail=f"avg over prior 12mo: ₪{avg_prior:,.2f} (n={n_prior})",
                    link=link,
                )))
            elif n_prior == 0 and tx_amount >= 500:
                new_high.append((tx_amount, AnomalyCard(
                    kind="new_high_value_merchant", severity="yellow",
                    message=f"{display}: ₪{tx_amount:,.0f} from a new merchant",
                    detail="No prior activity from this merchant in the last 12 months",
                    link=link,
                )))
        # Top 5 of each kind, biggest first.
        spikes.sort(key=lambda t: t[0], reverse=True)
        new_high.sort(key=lambda t: t[0], reverse=True)
        for _, card in spikes[:5]:
            anomalies.append(card)
        for _, card in new_high[:5]:
            anomalies.append(card)

    # 7. Dividends — USD credits whose merchant matches dividend wording.
    div_candidates = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.is_card_payment.is_(False),
        ExpenseTransaction.direction == "credit",
        ExpenseTransaction.currency_orig == "USD",
        ExpenseTransaction.occurred_on >= window_start,
        ExpenseTransaction.occurred_on <= anchor,
    ).all()
    div_rows = [
        r for r in div_candidates
        if _is_dividend_row(r.merchant_normalized, r.merchant_raw, r.currency_orig)
    ]
    yearly_div_usd = sum(float(r.amount_orig or 0) for r in div_rows)
    div_monthly: dict[str, float] = {}
    for r in div_rows:
        key = f"{r.occurred_on.year:04d}-{r.occurred_on.month:02d}"
        div_monthly[key] = div_monthly.get(key, 0.0) + float(r.amount_orig or 0)
    div_series = [
        {"month": k, "total_usd": v}
        for k, v in sorted(div_monthly.items())
    ]
    cur_month_div_usd = (
        div_monthly.get(focal_month, 0.0) if focal_month is not None else 0.0
    )
    cat_by_id_slug = {
        c.id: c.slug for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id,
        ).all()
    }
    if focal_month is not None:
        cur_y, cur_m = (int(p) for p in focal_month.split("-"))
        focal_div_rows = [
            r for r in div_rows
            if r.occurred_on.year == cur_y and r.occurred_on.month == cur_m
        ]
    else:
        focal_div_rows = []
    div_txs = [_tx_to_out(r, cat_by_id_slug) for r in focal_div_rows]
    dividends = DividendsSummary(
        month=focal_month or "",
        current_month_total_usd=cur_month_div_usd,
        yearly_total_usd=yearly_div_usd,
        monthly_series=div_series,
        transactions=div_txs,
    )

    # 8. Taxes — by-kind NIS rollup + Schwab USD withholding when present.
    tax_rows = db.execute(
        sa_select(
            ExpenseCategory.slug,
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0).label("total"),
        )
        .join(ExpenseTransaction,
              ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .where(ExpenseTransaction.amount_nis.is_not(None))
        .where(ExpenseTransaction.direction == "debit")
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= anchor)
        .where(ExpenseCategory.slug.in_(_TAX_CATEGORY_SLUGS))
        .group_by(ExpenseCategory.slug)
    ).all()
    by_kind: dict[str, float] = {}
    yearly_tax_nis = 0.0
    for slug, total in tax_rows:
        kind = _TAX_KIND_LABEL.get(slug, slug)
        by_kind[kind] = float(by_kind.get(kind, 0.0) + float(total or 0))
        yearly_tax_nis += float(total or 0)

    # Schwab USD withholding — best-effort. The CSV path is configurable
    # via env (ARGOSY_SCHWAB_CSV_PATH) and not always present locally; if
    # parsing fails we report 0 USD rather than erroring out the dashboard.
    yearly_tax_usd = 0.0
    try:
        import os
        from pathlib import Path as _P
        from argosy.services.rsu_reconciliation.schwab_csv import parse_csv
        schwab_path = os.environ.get("ARGOSY_SCHWAB_CSV_PATH")
        if schwab_path:
            p = _P(schwab_path)
            if p.exists():
                report = parse_csv(p)
                for sale in report.sales:
                    if (sale.date and sale.date >= window_start
                            and sale.date <= anchor):
                        for lot in sale.lots:
                            yearly_tax_usd += float(lot.taxes_usd or 0)
    except Exception:
        yearly_tax_usd = 0.0
    if yearly_tax_usd > 0:
        by_kind["rsu_withholding_usd"] = yearly_tax_usd

    taxes = TaxesSummary(
        yearly_total_nis=yearly_tax_nis,
        yearly_total_usd=yearly_tax_usd,
        by_kind=by_kind,
    )

    return DashboardOverview(
        months=months_list,
        current_month=focal_month,
        current_month_spending_nis=cur_month_spending_nis,
        current_month_income_nis=cur_month_income_nis,
        current_month_refunds_nis=cur_month_refunds_nis,
        current_month_inflow_nis=cur_month_inflow_nis,
        current_month_top_categories=top_cats,
        current_month_income=income_cats,
        current_month_inflow=inflow_cats,
        top_merchants_current_month=top_merchants,
        anomalies=anomalies,
        sources_health=sources_health,
        yearly_summary=yearly,
        dividends=dividends,
        taxes=taxes,
        fx_mode=fx,
    )


# ---------------------------------------------------------------------------
# GET /income-breakdown
# ---------------------------------------------------------------------------


class IncomeBreakdown(BaseModel):
    """Per-month income drilldown.

    `total_nis` is the sum of NIS-credited income (credits with
    tx_type != 'refund'). `by_category` is a per-category aggregation;
    `transactions` is the flat list of the actual income rows so the user
    can scan what came in.
    """

    month: str
    total_nis: float
    by_category: list[CategorySpend]
    transactions: list[TransactionOut]


@router.get("/income-breakdown", response_model=IncomeBreakdown)
def income_breakdown(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    month: str = Query(
        ..., pattern=r"^\d{4}-\d{2}$",
        description="'YYYY-MM' month to drill into.",
    ),
) -> IncomeBreakdown:
    y, m = (int(p) for p in month.split("-"))

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
        .where(extract("year", ExpenseTransaction.occurred_on) == y)
        .where(extract("month", ExpenseTransaction.occurred_on) == m)
        .where(ExpenseTransaction.direction == "credit")
        .where(ExpenseTransaction.tx_type != "refund")
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en)
        .order_by(func.sum(ExpenseTransaction.amount_nis).desc())
    ).all()
    total = sum(float(r.total or 0) for r in cat_rows)
    base = total or 1.0
    by_category = [
        CategorySpend(
            slug=r.slug, label_en=r.label_en,
            total_nis=float(r.total or 0),
            transaction_count=int(r.n or 0),
            percent=float(r.total or 0) / base * 100.0,
        )
        for r in cat_rows
    ]

    # Flat list of the income rows themselves.
    tx_rows = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.is_card_payment.is_(False),
        ExpenseTransaction.direction == "credit",
        ExpenseTransaction.tx_type != "refund",
        extract("year", ExpenseTransaction.occurred_on) == y,
        extract("month", ExpenseTransaction.occurred_on) == m,
    ).order_by(ExpenseTransaction.occurred_on.desc()).all()
    cat_by_id = {
        c.id: c.slug for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id,
        ).all()
    }
    transactions = [
        _tx_to_out(r, cat_by_id) for r in tx_rows
    ]
    return IncomeBreakdown(
        month=month, total_nis=total,
        by_category=by_category, transactions=transactions,
    )


# ---------------------------------------------------------------------------
# GET /source-detail/{source_id}
# ---------------------------------------------------------------------------

class StatementSummary(BaseModel):
    id: int
    period_start: date
    period_end: date
    parsed_total_nis: float | None
    declared_total_nis: float | None
    gap: float | None
    status: str
    parser_name: str
    parser_version: str
    transaction_count: int
    correlated_count: int


class MonthBucket(BaseModel):
    """Per-month aggregate of a source's transactions, derived from
    tx.occurred_on (NOT statement period). This is the right granularity
    regardless of how the issuer chunks statements — Discount Bank exports
    one large file per period; Isracard exports monthly. The chart uses
    months across sources for consistency.
    """

    month: str                       # 'YYYY-MM'
    debit_nis: float                 # outflow (debits, NIS-only)
    credit_nis: float                # inflow (credits, NIS-only)
    transaction_count: int
    correlated_count: int            # is_card_payment rows in this month


class SourceDetailResponse(BaseModel):
    source: SourceOut
    statements: list[StatementSummary]
    months: list[MonthBucket]


@router.get("/source-detail/{source_id}", response_model=SourceDetailResponse)
def source_detail(
    source_id: int,
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> SourceDetailResponse:
    src = db.query(ExpenseSource).filter_by(
        id=source_id, user_id=user_id,
    ).one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="source not found")
    stmts = db.query(ExpenseStatement).filter_by(
        source_id=src.id, user_id=user_id,
    ).order_by(ExpenseStatement.period_start).all()
    out_stmts: list[StatementSummary] = []
    for st in stmts:
        tx_n = db.query(ExpenseTransaction).filter_by(
            statement_id=st.id, user_id=user_id,
        ).count()
        corr_n = db.query(ExpenseTransaction).filter_by(
            statement_id=st.id, user_id=user_id, is_card_payment=True,
        ).count()
        gap = (
            float(st.parsed_total_nis or 0) - float(st.declared_total_nis)
            if st.declared_total_nis is not None
            else None
        )
        out_stmts.append(StatementSummary(
            id=st.id,
            period_start=st.period_start, period_end=st.period_end,
            parsed_total_nis=float(st.parsed_total_nis) if st.parsed_total_nis is not None else None,
            declared_total_nis=float(st.declared_total_nis) if st.declared_total_nis is not None else None,
            gap=gap, status=_gap_status(gap),
            parser_name=st.parser_name, parser_version=st.parser_version,
            transaction_count=tx_n, correlated_count=corr_n,
        ))

    # Per-month buckets — group all the source's transactions by
    # YYYY-MM(occurred_on) regardless of statement boundaries. This is the
    # consistent granularity across issuers (Discount exports per-period
    # files, Isracard per month — both render the same number of bars here).
    month_rows = db.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(
                func.sum(case(
                    (ExpenseTransaction.direction == "debit",
                     ExpenseTransaction.amount_nis),
                    else_=0,
                )), 0,
            ).label("debit"),
            func.coalesce(
                func.sum(case(
                    (ExpenseTransaction.direction == "credit",
                     ExpenseTransaction.amount_nis),
                    else_=0,
                )), 0,
            ).label("credit"),
            func.count().label("n"),
            func.coalesce(
                func.sum(case(
                    (ExpenseTransaction.is_card_payment.is_(True), 1),
                    else_=0,
                )), 0,
            ).label("corr"),
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.source_id == src.id)
        .group_by("y", "m")
        .order_by("y", "m")
    ).all()
    months_buckets: list[MonthBucket] = []
    for y, m, debit, credit, n, corr in month_rows:
        if y is None or m is None:
            continue
        months_buckets.append(MonthBucket(
            month=f"{int(y):04d}-{int(m):02d}",
            debit_nis=float(debit or 0),
            credit_nis=float(credit or 0),
            transaction_count=int(n or 0),
            correlated_count=int(corr or 0),
        ))

    return SourceDetailResponse(
        source=SourceOut(
            id=src.id, kind=src.kind, issuer=src.issuer,
            external_id=src.external_id, display_name=src.display_name,
            cardholder_name=src.cardholder_name, active=src.active,
        ),
        statements=out_stmts,
        months=months_buckets,
    )


# ---------------------------------------------------------------------------
# Tags (Feature 3 — overlay on top of category)
# ---------------------------------------------------------------------------


def _parse_tags(raw: str | None) -> list[str]:
    """Decode the JSON tag list. Tolerant: bad JSON / None / empty all
    return [] so callers don't need to guard.
    """
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(t) for t in v if isinstance(t, (str,))]
    except (ValueError, TypeError):
        pass
    return []


def _serialize_tags(tags: list[str]) -> str:
    """JSON-encode a list of tags, deduped + sorted for stable storage.
    Empty list -> '[]' so the column never holds NULL.
    """
    seen: list[str] = []
    for t in tags:
        s = str(t).strip()
        if s and s not in seen:
            seen.append(s)
    return json.dumps(sorted(seen), ensure_ascii=False)


class TagsRequest(BaseModel):
    user_id: str
    tags: list[str]


class TagRequest(BaseModel):
    user_id: str
    tag: str


class TagsResponse(BaseModel):
    transaction_id: int
    tags: list[str]


@router.patch("/transactions/{transaction_id}/tags", response_model=TagsResponse)
def patch_transaction_tags(
    transaction_id: int,
    body: TagsRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TagsResponse:
    """Replace the entire tag list on a transaction."""
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id,
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    tx.tags = _serialize_tags(body.tags)
    db.commit()
    return TagsResponse(transaction_id=tx.id, tags=_parse_tags(tx.tags))


@router.post("/transactions/{transaction_id}/tags/add", response_model=TagsResponse)
def add_transaction_tag(
    transaction_id: int,
    body: TagRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TagsResponse:
    """Idempotent — adds the tag if not already present."""
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id,
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    cur = _parse_tags(tx.tags)
    if body.tag not in cur:
        cur.append(body.tag)
    tx.tags = _serialize_tags(cur)
    db.commit()
    return TagsResponse(transaction_id=tx.id, tags=_parse_tags(tx.tags))


@router.post("/transactions/{transaction_id}/tags/remove", response_model=TagsResponse)
def remove_transaction_tag(
    transaction_id: int,
    body: TagRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TagsResponse:
    """Idempotent — removes the tag if present."""
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id,
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    cur = [t for t in _parse_tags(tx.tags) if t != body.tag]
    tx.tags = _serialize_tags(cur)
    db.commit()
    return TagsResponse(transaction_id=tx.id, tags=_parse_tags(tx.tags))


class TagsListResponse(BaseModel):
    tags: list[str]


@router.get("/tags", response_model=TagsListResponse)
def list_tags(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    prefix: str | None = None,
) -> TagsListResponse:
    """Distinct tags across the user's transactions, optionally filtered
    by prefix (e.g. 'trip:'). Built by scanning the JSON tag column —
    fine at single-user scale; if this ever needs to scale we'll
    materialize a tag-table.
    """
    rows = db.query(ExpenseTransaction.tags).filter_by(user_id=user_id).all()
    seen: set[str] = set()
    for (raw,) in rows:
        for t in _parse_tags(raw):
            if prefix is None or t.startswith(prefix):
                seen.add(t)
    return TagsListResponse(tags=sorted(seen))


class CurrencyAmount(BaseModel):
    currency: str
    total: float


class TripSummary(BaseModel):
    """Aggregate spend tagged with one tag.

    Use case: ``tag=trip:greece-2026-aug`` returns the union of flights,
    hotels, restaurants, etc. that the user grouped under that one tag.
    """

    tag: str
    transaction_count: int
    total_nis: float
    currency_breakdown: list[CurrencyAmount]
    by_category: list[CategorySpend]
    transactions: list[TransactionOut]
    period_start: date | None
    period_end: date | None


@router.get("/trip-summary", response_model=TripSummary)
def trip_summary(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    tag: str = Query(..., min_length=1),
) -> TripSummary:
    """Aggregate every transaction tagged with ``tag`` into one summary."""
    like = f'%"{tag}"%'
    rows = db.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.tags.like(like),
    ).order_by(ExpenseTransaction.occurred_on).all()

    cat_by_id = {
        c.id: (c.slug, c.label_en) for c in db.query(ExpenseCategory).filter_by(
            user_id=user_id,
        ).all()
    }
    # Per-category aggregate (NIS).
    cat_acc: dict[str, dict[str, float | int | str]] = {}
    total_nis = 0.0
    ccy_acc: dict[str, float] = {}
    period_start: date | None = None
    period_end: date | None = None
    for r in rows:
        if r.amount_nis is not None:
            total_nis += float(r.amount_nis)
        # Currency breakdown — NIS for native, original currency for foreign.
        if r.currency_orig:
            ccy_acc[r.currency_orig] = ccy_acc.get(r.currency_orig, 0.0) + float(
                r.amount_orig or 0,
            )
        elif r.amount_nis is not None:
            ccy_acc["NIS"] = ccy_acc.get("NIS", 0.0) + float(r.amount_nis)
        if r.category_id and r.category_id in cat_by_id:
            slug, label_en = cat_by_id[r.category_id]
            entry = cat_acc.setdefault(slug, {
                "slug": slug, "label_en": label_en,
                "total_nis": 0.0, "transaction_count": 0,
            })
            entry["total_nis"] = float(entry["total_nis"]) + float(r.amount_nis or 0)
            entry["transaction_count"] = int(entry["transaction_count"]) + 1
        if period_start is None or r.occurred_on < period_start:
            period_start = r.occurred_on
        if period_end is None or r.occurred_on > period_end:
            period_end = r.occurred_on

    cat_base = total_nis or 1.0
    by_category = [
        CategorySpend(
            slug=str(e["slug"]), label_en=str(e["label_en"]),
            total_nis=float(e["total_nis"]),
            transaction_count=int(e["transaction_count"]),
            percent=float(e["total_nis"]) / cat_base * 100.0,
        )
        for e in sorted(
            cat_acc.values(), key=lambda v: float(v["total_nis"]), reverse=True,
        )
    ]
    cat_by_id_slug = {cid: slug for cid, (slug, _) in cat_by_id.items()}
    transactions = [_tx_to_out(r, cat_by_id_slug) for r in rows]

    return TripSummary(
        tag=tag,
        transaction_count=len(rows),
        total_nis=total_nis,
        currency_breakdown=[
            CurrencyAmount(currency=k, total=v) for k, v in sorted(ccy_acc.items())
        ],
        by_category=by_category,
        transactions=transactions,
        period_start=period_start,
        period_end=period_end,
    )


# ---------------------------------------------------------------------------
# GET /rsu-reconciliation
# ---------------------------------------------------------------------------
#
# Visual surface for ``argosy expenses verify-rsu``: parses Schwab Equity
# Awards Center CSVs from disk (under ARGOSY_EXPENSE_SAMPLES_ROOT) and
# pairs each disbursement against Leumi USD account credits in the DB.
# Read-only — never writes. Mirrors the CLI logic in
# ``argosy.cli.expenses_admin.verify_rsu``.

class RsuSaleLot(BaseModel):
    shares: int
    sale_price_usd: float
    vest_date: date | None
    gross_proceeds_usd: float | None
    cost_basis_usd: float | None
    realized_gain_usd: float | None
    taxes_usd: float | None
    holding_period: str | None             # 'LONG TERM' | 'SHORT TERM' | None


class RsuSale(BaseModel):
    date: date
    symbol: str
    quantity_shares: int
    gross_usd: float
    fees_usd: float
    net_usd: float
    total_taxes_usd: float
    lots: list[RsuSaleLot]


class RsuDisbursement(BaseModel):
    date: date
    amount_usd: float
    matched_leumi_credit_id: int | None     # NULL = no match
    days_diff: int | None
    amount_diff_usd: float | None


class RsuLeumiCredit(BaseModel):
    tx_id: int
    date: date
    amount_usd: float
    merchant_raw: str
    reference: str | None
    matched_disbursement_index: int | None  # NULL = unmatched


class RsuSummary(BaseModel):
    sales_count: int
    sales_total_gross_usd: float
    sales_total_fees_usd: float
    sales_total_net_usd: float
    sales_total_taxes_usd: float
    disbursements_count: int
    disbursements_matched_count: int
    disbursements_total_usd: float
    leumi_credits_count: int
    leumi_credits_unmatched_count: int
    leumi_credits_unmatched_total_usd: float


class RsuReconciliationResponse(BaseModel):
    sales: list[RsuSale]
    disbursements: list[RsuDisbursement]
    leumi_credits: list[RsuLeumiCredit]
    summary: RsuSummary
    schwab_csv_paths: list[str]
    warning: str | None = None


@router.get("/rsu-reconciliation", response_model=RsuReconciliationResponse)
def rsu_reconciliation(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    tolerance_usd: float = Query(1.0, ge=0.0),
    tolerance_days: int = Query(7, ge=0, le=90),
) -> RsuReconciliationResponse:
    """Schwab → Leumi USD reconciliation, surfaced for the dashboard.

    Walks ``$ARGOSY_EXPENSE_SAMPLES_ROOT/<year>/Schwab/*.csv`` (any year),
    parses each via ``rsu_reconciliation.parse_csv``, dedups sales and
    disbursements, then pairs disbursements against Leumi USD account
    44745200 credits using the same greedy ``reconcile`` matcher as the
    ``verify-rsu`` CLI.

    Graceful degradation: if the env var is unset OR no CSVs are found,
    returns 200 with empty lists and a ``warning`` string so the UI can
    render an empty-state help card.
    """
    import os
    from pathlib import Path

    from argosy.services.rsu_reconciliation import (
        LeumiCredit,
        SchwabReport,
        parse_csv,
        reconcile,
    )

    root_str = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    csv_paths: list[Path] = []
    warning: str | None = None
    if not root_str:
        warning = "ARGOSY_EXPENSE_SAMPLES_ROOT not set"
    else:
        root = Path(root_str)
        if not root.exists():
            warning = f"ARGOSY_EXPENSE_SAMPLES_ROOT does not exist: {root}"
        else:
            # Walk <root>/<year>/Schwab/*.csv. We accept any folder name for
            # the year level so ad-hoc names like 'archive' still work.
            for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                schwab_dir = year_dir / "Schwab"
                if not schwab_dir.exists() or not schwab_dir.is_dir():
                    continue
                for csv in sorted(schwab_dir.glob("*.csv")):
                    csv_paths.append(csv)
            if not csv_paths:
                warning = (
                    f"No Schwab CSVs found under {root}/<year>/Schwab/. "
                    "Drop the EquityAwardsCenter export there to enable "
                    "reconciliation."
                )

    # Merge reports across CSVs while de-duping sales and disbursements
    # on (date, key fields). Same shape as the CLI's _MergedReport.
    merged = SchwabReport()
    seen_sale: set[tuple] = set()
    seen_disb: set[tuple] = set()
    for p in csv_paths:
        try:
            r = parse_csv(p)
        except Exception:
            # Skip unreadable CSVs but keep going — don't 500 the dashboard.
            continue
        for sale in r.sales:
            key = (sale.date, sale.symbol, sale.quantity_shares,
                   round(sale.gross_usd, 2), round(sale.fees_usd, 2))
            if key in seen_sale:
                continue
            seen_sale.add(key)
            merged.sales.append(sale)
        for disb in r.disbursements:
            key = (disb.date, disb.action, round(disb.amount_usd, 2))
            if key in seen_disb:
                continue
            seen_disb.add(key)
            merged.disbursements.append(disb)

    # Pull Leumi USD credits from DB for the user (account 44745200).
    leumi_credits: list[LeumiCredit] = []
    rows = (
        db.query(ExpenseTransaction)
        .join(ExpenseSource, ExpenseTransaction.source_id == ExpenseSource.id)
        .filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseSource.issuer == "leumi",
            ExpenseSource.external_id == "44745200",
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.currency_orig == "USD",
        )
        .order_by(ExpenseTransaction.occurred_on)
        .all()
    )
    for tx in rows:
        if tx.amount_orig is None:
            continue
        leumi_credits.append(LeumiCredit(
            date=tx.occurred_on,
            amount_usd=float(tx.amount_orig),
            merchant_raw=tx.merchant_raw,
            reference=tx.reference,
            tx_id=tx.id,
        ))

    rec = reconcile(
        merged,
        leumi_credits,
        tolerance_usd=tolerance_usd,
        tolerance_days=tolerance_days,
    )

    # ---- Build response ----
    # Disbursements sorted desc; assign each a stable index for cross-linking.
    disbs_sorted = sorted(
        merged.disbursements, key=lambda d: d.date, reverse=True,
    )
    # Map disbursement identity → its position in disbs_sorted so
    # leumi credits can reference back to the matching disbursement.
    disb_index: dict[int, int] = {id(d): i for i, d in enumerate(disbs_sorted)}
    # Map disbursement identity → matching credit (from rec.matches).
    disb_to_match = {id(m.disbursement): m for m in rec.matches}
    # Map credit tx_id → matching disbursement identity (from rec.matches).
    credit_to_disb_id: dict[int, int] = {
        m.credit.tx_id: id(m.disbursement) for m in rec.matches
    }

    out_disbs: list[RsuDisbursement] = []
    for disb in disbs_sorted:
        m = disb_to_match.get(id(disb))
        out_disbs.append(RsuDisbursement(
            date=disb.date,
            amount_usd=round(disb.amount_usd, 2),
            matched_leumi_credit_id=(m.credit.tx_id if m else None),
            days_diff=(m.days_diff if m else None),
            amount_diff_usd=(m.amount_diff_usd if m else None),
        ))

    # Restrict Leumi credits to a window around the disbursements: ± 30 days
    # of any disbursement, OR the entire credit list if there are no
    # disbursements. This keeps the right-column noise low.
    if disbs_sorted:
        min_d = min(d.date for d in disbs_sorted) - timedelta(days=30)
        max_d = max(d.date for d in disbs_sorted) + timedelta(days=30)
        windowed_credits = [
            c for c in leumi_credits if min_d <= c.date <= max_d
        ]
    else:
        windowed_credits = list(leumi_credits)

    windowed_credits.sort(key=lambda c: c.date, reverse=True)
    out_credits: list[RsuLeumiCredit] = []
    for c in windowed_credits:
        matched_disb_obj_id = credit_to_disb_id.get(c.tx_id)
        matched_idx: int | None = (
            disb_index.get(matched_disb_obj_id)
            if matched_disb_obj_id is not None else None
        )
        out_credits.append(RsuLeumiCredit(
            tx_id=c.tx_id,
            date=c.date,
            amount_usd=round(c.amount_usd, 2),
            merchant_raw=c.merchant_raw,
            reference=c.reference,
            matched_disbursement_index=matched_idx,
        ))

    # Sales sorted desc.
    sales_sorted = sorted(merged.sales, key=lambda s: s.date, reverse=True)
    out_sales: list[RsuSale] = []
    for s in sales_sorted:
        out_sales.append(RsuSale(
            date=s.date,
            symbol=s.symbol,
            quantity_shares=s.quantity_shares,
            gross_usd=round(s.gross_usd, 2),
            fees_usd=round(s.fees_usd, 2),
            net_usd=round(s.net_usd, 2),
            total_taxes_usd=round(s.total_taxes_usd, 2),
            lots=[
                RsuSaleLot(
                    shares=lot.shares,
                    sale_price_usd=round(lot.sale_price_usd, 4),
                    vest_date=lot.vest_date,
                    gross_proceeds_usd=(
                        round(lot.gross_proceeds_usd, 2)
                        if lot.gross_proceeds_usd is not None else None
                    ),
                    cost_basis_usd=(
                        round(lot.cost_basis_usd, 2)
                        if lot.cost_basis_usd is not None else None
                    ),
                    realized_gain_usd=(
                        round(lot.realized_gain_usd, 2)
                        if lot.realized_gain_usd is not None else None
                    ),
                    taxes_usd=round(lot.taxes_usd, 2),
                    holding_period=lot.holding_period,
                )
                for lot in s.lots
            ],
        ))

    # Summary numbers — lightweight aggregates the UI shows in hero cards.
    sales_gross = sum(s.gross_usd for s in merged.sales)
    sales_fees = sum(s.fees_usd for s in merged.sales)
    sales_net = sum(s.net_usd for s in merged.sales)
    sales_taxes = sum(s.total_taxes_usd for s in merged.sales)
    disb_total = sum(d.amount_usd for d in merged.disbursements)
    unmatched_credits_in_window = [
        c for c in windowed_credits if c.tx_id not in credit_to_disb_id
    ]
    unmatched_credit_total = sum(c.amount_usd for c in unmatched_credits_in_window)

    summary = RsuSummary(
        sales_count=len(merged.sales),
        sales_total_gross_usd=round(sales_gross, 2),
        sales_total_fees_usd=round(sales_fees, 2),
        sales_total_net_usd=round(sales_net, 2),
        sales_total_taxes_usd=round(sales_taxes, 2),
        disbursements_count=len(merged.disbursements),
        disbursements_matched_count=len(rec.matches),
        disbursements_total_usd=round(disb_total, 2),
        leumi_credits_count=len(windowed_credits),
        leumi_credits_unmatched_count=len(unmatched_credits_in_window),
        leumi_credits_unmatched_total_usd=round(unmatched_credit_total, 2),
    )

    return RsuReconciliationResponse(
        sales=out_sales,
        disbursements=out_disbs,
        leumi_credits=out_credits,
        summary=summary,
        schwab_csv_paths=[str(p) for p in csv_paths],
        warning=warning,
    )
