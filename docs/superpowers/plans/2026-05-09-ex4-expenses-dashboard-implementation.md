# Wave EX4 — Expenses Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a substantive `/expenses` dashboard in the existing Next.js 16.2 app — overview page with charts (monthly spend, category donut, top-merchants, anomalies, sources health), transactions browser with filters + inline category edit, per-source drilldown, FX toggle. Add 3 lean backend endpoints + a hotfix to support it.

**Architecture:** Backend-first hotfix + 2 new aggregation endpoints, then UI scaffolding (route + NavBar + FX toggle + API helpers), then per-component build (hero → charts → tables) for the overview page, then transactions + sources pages. Each component is a typed pure-render unit (no internal fetching) so they're independently testable. Recharts 3.8 for visualizations (already in deps).

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 (backend); Next.js 16.2.4 / React 19.2 / Tailwind 4 / Recharts 3.8 / shadcn-style components (frontend).

**Spec:** `docs/superpowers/specs/2026-05-09-ex4-expenses-dashboard-design.md`. Read first.

**Predecessors:** EX1 (ingest core, landed) + EX1.1 (stabilization, landing in same session).

**IMPORTANT — Next.js 16 caveat (from `ui/AGENTS.md`):**
> "This is NOT the Next.js you know. This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices."

When in doubt, mirror conventions from `ui/src/app/portfolio/page.tsx` and `ui/src/app/plan/page.tsx`.

---

## Files this wave creates or modifies

**Backend (create):**
- `tests/test_dashboard_overview_endpoint.py`
- `tests/test_source_detail_endpoint.py`

**Backend (modify):**
- `argosy/api/routes/expenses.py` — hotfix `TransactionOut.amount_nis: float | None` + add 2 endpoints (`/dashboard-overview`, `/source-detail/{source_id}`).
- `tests/test_expense_routes.py` — extend to assert NULL `amount_nis` survives the response.

**Frontend (create):**
- `ui/src/app/expenses/layout.tsx`
- `ui/src/app/expenses/page.tsx`
- `ui/src/app/expenses/transactions/page.tsx`
- `ui/src/app/expenses/sources/page.tsx`
- `ui/src/components/expenses/monthly-spend-chart.tsx`
- `ui/src/components/expenses/category-donut.tsx`
- `ui/src/components/expenses/top-merchants-card.tsx`
- `ui/src/components/expenses/anomaly-highlights.tsx`
- `ui/src/components/expenses/sources-health-table.tsx`
- `ui/src/components/expenses/source-statement-timeline.tsx`
- `ui/src/components/expenses/transactions-table.tsx`
- `ui/src/components/expenses/category-edit-popover.tsx`
- `ui/src/components/expenses/fx-toggle.tsx`
- `ui/src/components/expenses/hero-stats.tsx`
- `ui/src/lib/expenses/api.ts`
- `ui/src/lib/expenses/format.ts`
- `ui/src/lib/expenses/fx-mode.ts`

**Frontend (modify):**
- `ui/src/components/nav.tsx` — add `Expenses` tab.

---

## Conventions worth knowing

- Run python via `D:/Projects/financial-advisor/.venv/Scripts/python.exe`
- Run tests via `pytest -m "not llm_eval" -q`
- Frontend dev: `cd ui ; npm run dev` (port 1337). Build: `npm run build`. Lint: `npm run lint`.
- PowerShell: `;` not `&&`.
- All UI components use `"use client"` at top.
- Existing API client: `import { api } from "@/lib/api"`. Add new types via `ui/src/lib/expenses/api.ts` that re-exports from there.
- Tailwind class merging: `cn(...)` from `@/lib/utils`.
- Card / Button / Badge / Tabs already in `@/components/ui/*` (shadcn-style).
- Charts: `import { ... } from "recharts"`. Always wrap in `<ResponsiveContainer width="100%" height={H}>`.

---

## Task list

### Phase A — Backend prep (Tasks 1-3)

### Task 1: Hotfix `TransactionOut.amount_nis` nullable + add `amount_orig` / `currency_orig`

**Files:**
- Modify: `argosy/api/routes/expenses.py` — `TransactionOut` model + the response builder.
- Modify: `tests/test_expense_routes.py` — extend an existing test (or add one) to seed a foreign-currency tx and assert response shape.

- [ ] **Step 1: Read the current `TransactionOut` and `list_transactions` response builder**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "
from pathlib import Path
src = Path('argosy/api/routes/expenses.py').read_text()
i = src.find('class TransactionOut')
print(src[i:i+1200])
"
```

- [ ] **Step 2: Write a failing test for foreign-row passthrough**

Append to `tests/test_expense_routes.py`:

```python
def test_list_transactions_passes_through_foreign_amount_nis_null(client_with_db):
    """Post-EX1.1, foreign rows have amount_nis=None. The endpoint must surface
    that without TypeError'ing through float()."""
    from datetime import date
    from decimal import Decimal
    from sqlalchemy.orm import Session
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    SessionFactory = client_with_db.app.state.session_factory
    with Session(SessionFactory.bind) as s:
        s.add(User(id="u_fx", plan="free")); s.flush()
        f = UserFile(
            user_id="u_fx", sha256="f"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id="u_fx", kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="u_fx", source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="isracard",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        # Foreign row with amount_nis NULL.
        s.add(ExpenseTransaction(
            user_id="u_fx", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 5),
            merchant_raw="NETFLIX", merchant_normalized="netflix",
            amount_nis=None,
            amount_orig=Decimal("12.18"), currency_orig="USD",
            direction="debit", tx_type="regular", raw_row_json="{}",
        ))
        s.commit()

    r = client_with_db.get("/api/expenses/transactions?user_id=u_fx")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    tx = body["transactions"][0]
    assert tx["amount_nis"] is None
    assert tx["amount_orig"] == 12.18
    assert tx["currency_orig"] == "USD"
```

- [ ] **Step 3: Run test, observe TypeError**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_routes.py::test_list_transactions_passes_through_foreign_amount_nis_null -v
```

Expected: FAIL — pydantic ValidationError or TypeError on `float(None)`.

- [ ] **Step 4: Update `TransactionOut` and the response builder**

In `argosy/api/routes/expenses.py`:

```python
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
```

In the `list_transactions` builder (around line 226-238):

```python
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
```

- [ ] **Step 5: Run test, expect PASS + full route suite green**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_routes.py -q
```

Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```pwsh
git add argosy/api/routes/expenses.py tests/test_expense_routes.py
git commit -m "fix(expenses-api): TransactionOut.amount_nis nullable + amount_orig/currency_orig fields"
```

### Task 2: New `/dashboard-overview` endpoint

**Files:**
- Modify: `argosy/api/routes/expenses.py` — new model + endpoint.
- Create: `tests/test_dashboard_overview_endpoint.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_overview_endpoint.py`:

```python
"""Dashboard-overview endpoint — returns one bundle for the overview page."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed_minimal(client_with_db, *, user_id: str = "u1"):
    SF = client_with_db.app.state.session_factory
    with Session(SF.bind) as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        # Seed system + user categories
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        # Source + statement + 5 transactions in current month
        f = UserFile(
            user_id=user_id, sha256="a"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("250"), declared_total_nis=Decimal("250"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(user_id=user_id, slug="dining").one()
        for i in range(5):
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, 5, i + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("50"), direction="debit", tx_type="regular",
                category_id=cat.id, category_source="user",
                category_confidence=Decimal("1.0"),
                raw_row_json="{}",
            ))
        s.commit()


def test_dashboard_overview_returns_full_payload(client_with_db):
    _seed_minimal(client_with_db)
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u1&months=6")
    assert r.status_code == 200
    body = r.json()
    assert "months" in body
    assert "current_month_top_categories" in body
    assert "top_merchants_current_month" in body
    assert "anomalies" in body
    assert "sources_health" in body
    assert "fx_mode" in body


def test_dashboard_overview_empty_corpus_returns_empty_lists(client_with_db):
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_empty&months=6")
    assert r.status_code == 200
    body = r.json()
    assert body["months"] == []
    assert body["sources_health"] == []
    assert body["anomalies"] == []


def test_dashboard_overview_top_categories_ordered_by_spend(client_with_db):
    _seed_minimal(client_with_db, user_id="u2")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u2&months=6")
    body = r.json()
    cats = body["current_month_top_categories"]
    # Each entry: {slug, label_en, total_nis, percent}
    assert all("slug" in c and "total_nis" in c for c in cats)
    if len(cats) >= 2:
        assert cats[0]["total_nis"] >= cats[1]["total_nis"]


def test_dashboard_overview_sources_health_includes_status(client_with_db):
    _seed_minimal(client_with_db, user_id="u3")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u3&months=6")
    body = r.json()
    assert len(body["sources_health"]) == 1
    src = body["sources_health"][0]
    assert src["display_name"] == "test"
    # gap < 0.5 → green
    assert src["status"] == "green"
    assert src["gap"] is not None and abs(src["gap"]) < 0.5
```

- [ ] **Step 2: Run, observe FAIL (404 endpoint missing)**

- [ ] **Step 3: Implement the endpoint**

In `argosy/api/routes/expenses.py`, append:

```python
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
    from sqlalchemy import case, extract, select as sa_select

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
```

(Note: `fx=nis` server-side conversion is intentionally NOT implemented in v1 — the dashboard renders whatever currencies are in `months[].totals_by_currency`. If the user toggles to NIS-only mode, the frontend can sum NIS-only and skip foreign rows. Defer real BoI conversion to a follow-up if the user asks.)

- [ ] **Step 4: Run tests, expect PASS**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_dashboard_overview_endpoint.py -v
```

- [ ] **Step 5: Run full route suite**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_routes.py tests/test_dashboard_overview_endpoint.py -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```pwsh
git add argosy/api/routes/expenses.py tests/test_dashboard_overview_endpoint.py
git commit -m "feat(expenses-api): /dashboard-overview endpoint for the EX4 dashboard"
```

### Task 3: New `/source-detail/{source_id}` endpoint

**Files:**
- Modify: `argosy/api/routes/expenses.py`.
- Create: `tests/test_source_detail_endpoint.py`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_source_detail_endpoint.py`:

```python
"""Source-detail endpoint — per-source statement timeline + reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed(client_with_db, user_id="u1", source_id_out=None):
    SF = client_with_db.app.state.session_factory
    ids = {}
    with Session(SF.bind) as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        f = UserFile(
            user_id=user_id, sha256="b"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0000", display_name="Test Card",
        )
        s.add(src); s.flush()
        ids["source_id"] = src.id
        # Two statements
        for month in (3, 4):
            stmt = ExpenseStatement(
                user_id=user_id, source_id=src.id, file_id=f.id,
                period_start=date(2026, month, 1),
                period_end=date(2026, month, 28),
                parsed_total_nis=Decimal("100"),
                declared_total_nis=Decimal("100"),
                parser_name="isracard", parser_version="0.1.0", status="parsed",
            )
            s.add(stmt); s.flush()
            for i in range(3):
                s.add(ExpenseTransaction(
                    user_id=user_id, source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(2026, month, i + 5),
                    merchant_raw=f"M{month}-{i}",
                    merchant_normalized=f"m{month}{i}",
                    amount_nis=Decimal("33"), direction="debit", tx_type="regular",
                    raw_row_json="{}",
                ))
        s.commit()
    return ids


def test_source_detail_returns_timeline(client_with_db):
    ids = _seed(client_with_db)
    sid = ids["source_id"]
    r = client_with_db.get(f"/api/expenses/source-detail/{sid}?user_id=u1")
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["id"] == sid
    assert body["source"]["display_name"] == "Test Card"
    assert len(body["statements"]) == 2
    # Sorted oldest-first
    assert body["statements"][0]["period_start"] < body["statements"][1]["period_start"]
    for s in body["statements"]:
        assert s["transaction_count"] == 3


def test_source_detail_unknown_source_returns_404(client_with_db):
    r = client_with_db.get("/api/expenses/source-detail/99999?user_id=u_anyone")
    assert r.status_code == 404
```

- [ ] **Step 2: Run, observe 404 (endpoint missing)**

- [ ] **Step 3: Implement the endpoint**

In `argosy/api/routes/expenses.py`, append:

```python
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


class SourceDetailResponse(BaseModel):
    source: SourceOut
    statements: list[StatementSummary]


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
    return SourceDetailResponse(
        source=SourceOut(
            id=src.id, kind=src.kind, issuer=src.issuer,
            external_id=src.external_id, display_name=src.display_name,
            cardholder_name=src.cardholder_name, active=src.active,
        ),
        statements=out_stmts,
    )
```

- [ ] **Step 4: Run tests, expect PASS**

- [ ] **Step 5: Commit**

```pwsh
git add argosy/api/routes/expenses.py tests/test_source_detail_endpoint.py
git commit -m "feat(expenses-api): /source-detail/{id} endpoint"
```

---

### Phase B — Frontend scaffolding (Tasks 4-6)

### Task 4: API helpers + types in `ui/src/lib/expenses/`

**Files:**
- Create: `ui/src/lib/expenses/api.ts`, `format.ts`, `fx-mode.ts`.

- [ ] **Step 1: Create `ui/src/lib/expenses/api.ts`**

```ts
/**
 * Expenses API client. Extends @/lib/api with the EX4 endpoints.
 */

const BASE =
  typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
    ? process.env.NEXT_PUBLIC_API_URL
    : "http://localhost:8000";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

export interface MonthlyTotalEntry {
  month: string;
  totals_by_currency: Record<string, number>;
  transaction_count: number;
}

export interface CategorySpend {
  slug: string;
  label_en: string;
  total_nis: number;
  transaction_count: number;
  percent: number;
}

export interface MerchantSpend {
  merchant_normalized: string;
  merchant_display: string;
  total_nis: number;
  transaction_count: number;
  category_slug: string | null;
}

export interface AnomalyCard {
  kind:
    | "uncategorized"
    | "novel_merchant"
    | "large_outlier"
    | "fee_waiver_missed"
    | "conservation_gap";
  severity: "red" | "yellow" | "info";
  message: string;
  detail?: string | null;
  link?: string | null;
}

export interface SourceHealthEntry {
  source_id: number;
  display_name: string;
  issuer: string;
  external_id: string;
  last_period: string | null;
  parsed_total_nis: number | null;
  declared_total_nis: number | null;
  gap: number | null;
  status: "green" | "yellow" | "red" | "unknown";
  statement_count: number;
  correlated_card_payments: number;
}

export interface DashboardOverview {
  months: MonthlyTotalEntry[];
  current_month_top_categories: CategorySpend[];
  top_merchants_current_month: MerchantSpend[];
  anomalies: AnomalyCard[];
  sources_health: SourceHealthEntry[];
  fx_mode: string;
}

export interface SourceOut {
  id: number;
  kind: string;
  issuer: string;
  external_id: string;
  display_name: string;
  cardholder_name: string | null;
  active: boolean;
}

export interface StatementSummary {
  id: number;
  period_start: string;
  period_end: string;
  parsed_total_nis: number | null;
  declared_total_nis: number | null;
  gap: number | null;
  status: "green" | "yellow" | "red" | "unknown";
  parser_name: string;
  parser_version: string;
  transaction_count: number;
  correlated_count: number;
}

export interface SourceDetailResponse {
  source: SourceOut;
  statements: StatementSummary[];
}

export interface TransactionOut {
  id: number;
  occurred_on: string;
  merchant_raw: string;
  amount_nis: number | null;
  amount_orig: number | null;
  currency_orig: string | null;
  direction: "debit" | "credit";
  tx_type: string;
  category_slug: string | null;
  category_source: string | null;
  is_card_payment: boolean;
  source_id: number;
}

export interface TransactionsResponse {
  transactions: TransactionOut[];
  total: number;
}

export interface CategoryOut {
  id: number;
  slug: string;
  label_en: string;
  label_he: string;
  parent_slug: string | null;
  is_excluded_from_spend: boolean;
  is_inflow: boolean;
}

export interface CategoriesResponse {
  categories: CategoryOut[];
}

export interface SourcesResponse {
  sources: SourceOut[];
}

export const expensesApi = {
  dashboardOverview: (userId: string, months = 12, fx: "per_currency" | "nis" = "per_currency") =>
    getJSON<DashboardOverview>(
      `/api/expenses/dashboard-overview?user_id=${encodeURIComponent(userId)}&months=${months}&fx=${fx}`,
    ),
  sources: (userId: string) =>
    getJSON<SourcesResponse>(
      `/api/expenses/sources?user_id=${encodeURIComponent(userId)}`,
    ),
  sourceDetail: (sourceId: number, userId: string) =>
    getJSON<SourceDetailResponse>(
      `/api/expenses/source-detail/${sourceId}?user_id=${encodeURIComponent(userId)}`,
    ),
  transactions: (userId: string, params: Partial<{
    from_date: string;
    to_date: string;
    category: string;
    source_id: number;
    direction: "debit" | "credit";
    include_card_payments: boolean;
    search: string;
    limit: number;
    offset: number;
  }> = {}) => {
    const qs = new URLSearchParams({ user_id: userId });
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return getJSON<TransactionsResponse>(`/api/expenses/transactions?${qs.toString()}`);
  },
  categories: (userId: string) =>
    getJSON<CategoriesResponse>(
      `/api/expenses/categories?user_id=${encodeURIComponent(userId)}`,
    ),
  patchTransactionCategory: (txId: number, userId: string, slug: string) =>
    patchJSON<{
      transaction_id: number;
      category_slug: string;
      category_source: string;
      affected_count: number;
    }>(`/api/expenses/transactions/${txId}`, {
      user_id: userId,
      category_slug: slug,
    }),
};
```

- [ ] **Step 2: Create `ui/src/lib/expenses/format.ts`**

```ts
/**
 * Formatting helpers for the expenses UI.
 */

const NIS_FORMATTER = new Intl.NumberFormat("en-IL", {
  style: "currency",
  currency: "ILS",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const NIS_FORMATTER_2DP = new Intl.NumberFormat("en-IL", {
  style: "currency",
  currency: "ILS",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const FOREIGN_FORMATTERS: Record<string, Intl.NumberFormat> = {
  USD: new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }),
  EUR: new Intl.NumberFormat("en-EU", { style: "currency", currency: "EUR" }),
  GBP: new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }),
};

export function formatNIS(amount: number, opts: { precise?: boolean } = {}): string {
  return (opts.precise ? NIS_FORMATTER_2DP : NIS_FORMATTER).format(amount);
}

export function formatCurrency(amount: number, currency: string): string {
  if (currency === "NIS" || currency === "ILS") return formatNIS(amount);
  const fmt = FOREIGN_FORMATTERS[currency];
  if (fmt) return fmt.format(amount);
  return `${amount.toFixed(2)} ${currency}`;
}

export function formatPercent(p: number, decimals = 1): string {
  return `${p.toFixed(decimals)}%`;
}

export function formatMonth(yyyymm: string): string {
  // '2026-05' -> 'May 2026'
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return yyyymm;
  const dt = new Date(y, m - 1, 1);
  return dt.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

export function formatRelativeMonth(yyyymm: string): string {
  const now = new Date();
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return yyyymm;
  const target = new Date(y, m - 1, 1);
  const cur = new Date(now.getFullYear(), now.getMonth(), 1);
  const diff = (cur.getFullYear() - target.getFullYear()) * 12 + (cur.getMonth() - target.getMonth());
  if (diff === 0) return "This month";
  if (diff === 1) return "Last month";
  if (diff < 12) return `${diff} months ago`;
  return formatMonth(yyyymm);
}

/**
 * Stable HSL color from a category slug. Same slug → same color across renders.
 */
export function colorForSlug(slug: string): string {
  let h = 0;
  for (let i = 0; i < slug.length; i++) {
    h = (h * 31 + slug.charCodeAt(i)) % 360;
  }
  return `hsl(${h}, 65%, 55%)`;
}
```

- [ ] **Step 3: Create `ui/src/lib/expenses/fx-mode.ts`**

```ts
"use client";

import { useEffect, useState } from "react";

export type FxMode = "per_currency" | "nis";

const STORAGE_KEY = "argosy.expenses.fxMode";

export function useFxMode(): [FxMode, (m: FxMode) => void] {
  const [mode, setMode] = useState<FxMode>("per_currency");
  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "per_currency" || stored === "nis") setMode(stored);
  }, []);
  const update = (m: FxMode) => {
    setMode(m);
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY, m);
    }
  };
  return [mode, update];
}
```

- [ ] **Step 4: Verify TypeScript compiles**

```pwsh
cd ui ; npm run lint
```

(Note: `npm run lint` runs eslint. There's no separate `tsc --noEmit`; lint includes type-aware rules.)

- [ ] **Step 5: Commit**

```pwsh
git add ui/src/lib/expenses
git commit -m "feat(ui-expenses): API client + format helpers + fx-mode hook"
```

### Task 5: Add `/expenses` to NavBar

**Files:**
- Modify: `ui/src/components/nav.tsx`.

- [ ] **Step 1: Insert tab between Portfolio and Plan**

In `ui/src/components/nav.tsx`, find:

```ts
  { href: "/portfolio", label: "Portfolio" },
  { href: "/plan", label: "Plan" },
```

Change to:

```ts
  { href: "/portfolio", label: "Portfolio" },
  { href: "/expenses", label: "Expenses" },
  { href: "/plan", label: "Plan" },
```

- [ ] **Step 2: Lint**

```pwsh
cd ui ; npm run lint
```

- [ ] **Step 3: Commit**

```pwsh
git add ui/src/components/nav.tsx
git commit -m "feat(ui): add Expenses tab to NavBar"
```

### Task 6: `/expenses` route layout + FX toggle component

**Files:**
- Create: `ui/src/app/expenses/layout.tsx`
- Create: `ui/src/components/expenses/fx-toggle.tsx`

- [ ] **Step 1: Create `fx-toggle.tsx`**

```tsx
"use client";

import { useFxMode, type FxMode } from "@/lib/expenses/fx-mode";
import { cn } from "@/lib/utils";

export function FxToggle({ className }: { className?: string }) {
  const [mode, setMode] = useFxMode();
  const opts: { value: FxMode; label: string }[] = [
    { value: "per_currency", label: "Per currency" },
    { value: "nis", label: "NIS-converted" },
  ];
  return (
    <div className={cn(
      "inline-flex items-center rounded-md border border-border bg-background p-0.5 text-xs",
      className,
    )}>
      {opts.map((o) => (
        <button
          key={o.value}
          onClick={() => setMode(o.value)}
          className={cn(
            "px-2.5 py-1 rounded transition-colors",
            mode === o.value
              ? "bg-secondary text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Create `layout.tsx`**

```tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { FxToggle } from "@/components/expenses/fx-toggle";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/expenses", label: "Overview" },
  { href: "/expenses/transactions", label: "Transactions" },
  { href: "/expenses/sources", label: "Sources" },
];

export default function ExpensesLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="max-w-6xl mx-auto p-6 flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Expenses</h1>
        <FxToggle />
      </div>
      <nav className="border-b border-border -mx-1">
        <ul className="flex items-center gap-1">
          {TABS.map((t) => {
            const active = pathname === t.href;
            return (
              <li key={t.href}>
                <Link
                  href={t.href}
                  className={cn(
                    "inline-block px-3 py-2 text-sm rounded-t-md transition-colors",
                    active
                      ? "bg-secondary text-foreground border-b-2 border-primary -mb-[2px]"
                      : "text-muted-foreground hover:text-foreground hover:bg-secondary/40",
                  )}
                >
                  {t.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      {children}
    </div>
  );
}
```

- [ ] **Step 3: Lint**

```pwsh
cd ui ; npm run lint
```

- [ ] **Step 4: Commit**

```pwsh
git add ui/src/app/expenses/layout.tsx ui/src/components/expenses/fx-toggle.tsx
git commit -m "feat(ui-expenses): /expenses layout with FX toggle + sub-tabs"
```

---

### Phase C — Overview page components (Tasks 7-12)

### Task 7: Hero stats card row

**Files:**
- Create: `ui/src/components/expenses/hero-stats.tsx`

- [ ] **Step 1: Create the component**

```tsx
"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type DashboardOverview,
  type CategorySpend,
} from "@/lib/expenses/api";
import { formatNIS, formatPercent, formatRelativeMonth } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface HeroStatsProps {
  overview: DashboardOverview;
  fxMode: FxMode;
}

function monthSpend(month: { totals_by_currency: Record<string, number> }, fxMode: FxMode): number {
  if (fxMode === "nis") {
    // Best-effort: sum only NIS for now; v1 doesn't FX-convert client-side.
    return month.totals_by_currency.NIS ?? 0;
  }
  // Per-currency mode: NIS-only "primary" total + foreign rendered separately elsewhere.
  return month.totals_by_currency.NIS ?? 0;
}

export function HeroStats({ overview, fxMode }: HeroStatsProps) {
  const months = overview.months;
  const cur = months.at(-1);
  const prev = months.at(-2);
  const curNis = cur ? monthSpend(cur, fxMode) : 0;
  const prevNis = prev ? monthSpend(prev, fxMode) : 0;
  const trend = prevNis > 0 ? ((curNis - prevNis) / prevNis) * 100 : 0;
  const top: CategorySpend | undefined = overview.current_month_top_categories[0];
  const sources = overview.sources_health;
  const refundsCount = 0; // TODO: surface in API; for v1, hide if 0.

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {cur ? formatRelativeMonth(cur.month) : "This month"}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">{formatNIS(curNis)}</div>
          {prev && (
            <div className={trend > 0 ? "text-xs text-rose-600" : "text-xs text-emerald-600"}>
              {trend > 0 ? "+" : ""}{trend.toFixed(1)}% vs last month
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Top category
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold capitalize">{top?.label_en ?? "—"}</div>
          {top && (
            <div className="text-xs text-muted-foreground">
              {formatNIS(top.total_nis)} · {formatPercent(top.percent)}
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Sources
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">{sources.length}</div>
          <div className="text-xs text-muted-foreground">
            {sources.filter((s) => s.status === "green").length} reconciled
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Anomalies
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">
            {overview.anomalies.length}
          </div>
          <div className="text-xs text-muted-foreground">
            {overview.anomalies.filter((a) => a.severity === "red").length} red ·{" "}
            {overview.anomalies.filter((a) => a.severity === "yellow").length} yellow
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```pwsh
git add ui/src/components/expenses/hero-stats.tsx
git commit -m "feat(ui-expenses): hero stats card row"
```

### Task 8: Monthly spend chart (Recharts BarChart)

**Files:**
- Create: `ui/src/components/expenses/monthly-spend-chart.tsx`

- [ ] **Step 1: Create the chart**

```tsx
"use client";

import {
  Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type MonthlyTotalEntry } from "@/lib/expenses/api";
import { formatMonth, formatNIS } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface MonthlySpendChartProps {
  data: MonthlyTotalEntry[];
  fxMode: FxMode;
  height?: number;
}

const CURRENCY_COLORS: Record<string, string> = {
  NIS: "hsl(220, 70%, 55%)",
  USD: "hsl(160, 65%, 50%)",
  EUR: "hsl(280, 65%, 60%)",
  GBP: "hsl(30, 80%, 55%)",
};

export function MonthlySpendChart({ data, fxMode, height = 280 }: MonthlySpendChartProps) {
  // Build chart data: one row per month, columns per currency.
  const currencies = new Set<string>();
  for (const m of data) for (const c of Object.keys(m.totals_by_currency)) currencies.add(c);
  const ccyOrder = ["NIS", "USD", "EUR", "GBP"].filter((c) => currencies.has(c));
  for (const c of currencies) if (!ccyOrder.includes(c)) ccyOrder.push(c);

  const rows = data.map((m) => {
    const row: Record<string, number | string> = { month: formatMonth(m.month) };
    for (const c of ccyOrder) row[c] = m.totals_by_currency[c] ?? 0;
    return row;
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Monthly spend</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis dataKey="month" fontSize={11} />
            <YAxis fontSize={11} tickFormatter={(v: number) => formatNIS(v)} />
            <Tooltip
              formatter={(value: number, name: string) => [formatNIS(value), name]}
              cursor={{ fill: "var(--secondary)", opacity: 0.4 }}
            />
            {ccyOrder.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
            {(fxMode === "nis" ? ["NIS"] : ccyOrder).map((c) => (
              <Bar
                key={c}
                dataKey={c}
                stackId="ccy"
                fill={CURRENCY_COLORS[c] ?? "hsl(0, 0%, 60%)"}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Commit**

```pwsh
git add ui/src/components/expenses/monthly-spend-chart.tsx
git commit -m "feat(ui-expenses): monthly spend bar chart with per-currency stacks"
```

### Task 9: Category donut

**Files:**
- Create: `ui/src/components/expenses/category-donut.tsx`

- [ ] **Step 1: Create the component**

```tsx
"use client";

import Link from "next/link";
import {
  Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type CategorySpend } from "@/lib/expenses/api";
import { colorForSlug, formatNIS, formatPercent } from "@/lib/expenses/format";

interface CategoryDonutProps {
  data: CategorySpend[];
  height?: number;
}

export function CategoryDonut({ data, height = 280 }: CategoryDonutProps) {
  const total = data.reduce((s, c) => s + c.total_nis, 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Categories — current month</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-12 text-center">
            No data this month yet.
          </div>
        ) : (
          <div className="flex flex-col lg:flex-row gap-4 items-center">
            <ResponsiveContainer width="100%" height={height}>
              <PieChart>
                <Pie
                  data={data}
                  dataKey="total_nis"
                  nameKey="label_en"
                  innerRadius={60}
                  outerRadius={100}
                  paddingAngle={2}
                  isAnimationActive={false}
                >
                  {data.map((c) => (
                    <Cell key={c.slug} fill={colorForSlug(c.slug)} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value: number, _: string, item: { payload: CategorySpend }) => [
                    `${formatNIS(value)} (${formatPercent(item.payload.percent)})`,
                    item.payload.label_en,
                  ]}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="flex-1 flex flex-col gap-1 text-sm">
              <div className="text-xs text-muted-foreground mb-1">
                Total: {formatNIS(total)}
              </div>
              {data.slice(0, 8).map((c) => (
                <Link
                  key={c.slug}
                  href={`/expenses/transactions?category=${encodeURIComponent(c.slug)}`}
                  className="flex items-center gap-2 hover:bg-secondary/40 px-2 py-1 rounded"
                >
                  <span
                    className="w-3 h-3 rounded-sm shrink-0"
                    style={{ background: colorForSlug(c.slug) }}
                  />
                  <span className="capitalize flex-1 truncate">{c.label_en}</span>
                  <span className="text-muted-foreground tabular-nums">
                    {formatNIS(c.total_nis)}
                  </span>
                  <span className="text-xs text-muted-foreground w-12 text-right tabular-nums">
                    {formatPercent(c.percent)}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Commit**

```pwsh
git add ui/src/components/expenses/category-donut.tsx
git commit -m "feat(ui-expenses): category donut + per-row legend with drill links"
```

### Task 10: Top merchants card

**Files:**
- Create: `ui/src/components/expenses/top-merchants-card.tsx`

- [ ] **Step 1: Create the component**

```tsx
"use client";

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type MerchantSpend } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

interface TopMerchantsCardProps {
  data: MerchantSpend[];
}

export function TopMerchantsCard({ data }: TopMerchantsCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Top merchants — current month</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-6 text-center">
            No merchants yet.
          </div>
        ) : (
          <ol className="divide-y divide-border">
            {data.map((m, i) => (
              <li key={m.merchant_normalized} className="flex items-center gap-3 py-2">
                <span className="text-xs text-muted-foreground tabular-nums w-5">
                  {i + 1}.
                </span>
                <Link
                  href={`/expenses/transactions?search=${encodeURIComponent(m.merchant_display)}`}
                  className="flex-1 min-w-0 truncate hover:underline"
                  title={m.merchant_display}
                >
                  {m.merchant_display}
                </Link>
                {m.category_slug && (
                  <Badge variant="secondary" className="text-xs capitalize">
                    {m.category_slug.replace(/_/g, " ")}
                  </Badge>
                )}
                <span className="text-sm tabular-nums text-right w-20">
                  {formatNIS(m.total_nis)}
                </span>
                <span className="text-xs text-muted-foreground tabular-nums w-8 text-right">
                  ×{m.transaction_count}
                </span>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Commit**

```pwsh
git add ui/src/components/expenses/top-merchants-card.tsx
git commit -m "feat(ui-expenses): top merchants card"
```

### Task 11: Anomaly highlights card

**Files:**
- Create: `ui/src/components/expenses/anomaly-highlights.tsx`

- [ ] **Step 1: Create the component**

```tsx
"use client";

import { AlertTriangle, AlertCircle, Info } from "lucide-react";
import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type AnomalyCard } from "@/lib/expenses/api";
import { cn } from "@/lib/utils";

interface AnomalyHighlightsProps {
  anomalies: AnomalyCard[];
}

const ICON_BY_SEVERITY = {
  red: AlertCircle,
  yellow: AlertTriangle,
  info: Info,
} as const;

const COLOR_BY_SEVERITY = {
  red: "text-rose-500",
  yellow: "text-amber-500",
  info: "text-sky-500",
} as const;

export function AnomalyHighlights({ anomalies }: AnomalyHighlightsProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Anomalies & alerts</CardTitle>
      </CardHeader>
      <CardContent>
        {anomalies.length === 0 ? (
          <div className="text-sm text-emerald-600 py-4 text-center">
            ✓ All looks normal.
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {anomalies.map((a, i) => {
              const Icon = ICON_BY_SEVERITY[a.severity];
              const inner = (
                <div className="flex gap-2 items-start p-2 rounded hover:bg-secondary/40">
                  <Icon className={cn("h-4 w-4 mt-0.5 shrink-0", COLOR_BY_SEVERITY[a.severity])} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{a.message}</div>
                    {a.detail && (
                      <div className="text-xs text-muted-foreground mt-0.5">{a.detail}</div>
                    )}
                  </div>
                </div>
              );
              return (
                <li key={i}>
                  {a.link ? <Link href={a.link}>{inner}</Link> : inner}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Commit**

```pwsh
git add ui/src/components/expenses/anomaly-highlights.tsx
git commit -m "feat(ui-expenses): anomaly highlights card"
```

### Task 12: Sources health table + Overview page assembly

**Files:**
- Create: `ui/src/components/expenses/sources-health-table.tsx`
- Create: `ui/src/app/expenses/page.tsx`

- [ ] **Step 1: Create `sources-health-table.tsx`**

```tsx
"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type SourceHealthEntry } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";
import { cn } from "@/lib/utils";

const STATUS_DOT = {
  green: "bg-emerald-500",
  yellow: "bg-amber-500",
  red: "bg-rose-500",
  unknown: "bg-muted-foreground/40",
} as const;

export function SourcesHealthTable({ data }: { data: SourceHealthEntry[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Sources & reconciliation</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-6 text-center">
            No sources registered.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-muted-foreground border-b border-border">
                <th className="text-left py-2 pr-2">Source</th>
                <th className="text-left py-2 px-2">Latest period</th>
                <th className="text-right py-2 px-2">Parsed</th>
                <th className="text-right py-2 px-2">Declared</th>
                <th className="text-right py-2 px-2">Gap</th>
                <th className="text-right py-2 px-2">Stmts</th>
                <th className="text-right py-2 pl-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.source_id} className="border-b border-border/60 hover:bg-secondary/40">
                  <td className="py-2 pr-2">
                    <Link
                      href={`/expenses/sources?source_id=${s.source_id}`}
                      className="hover:underline"
                    >
                      {s.display_name}
                    </Link>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {s.issuer} {s.external_id}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-muted-foreground tabular-nums">
                    {s.last_period ?? "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.parsed_total_nis !== null ? formatNIS(s.parsed_total_nis) : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.declared_total_nis !== null ? formatNIS(s.declared_total_nis) : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.gap !== null
                      ? <span className={s.status === "red" ? "text-rose-600" : ""}>
                          {s.gap >= 0 ? "+" : ""}{s.gap.toFixed(2)}
                        </span>
                      : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">{s.statement_count}</td>
                  <td className="py-2 pl-2 text-right">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[s.status])} />
                      <span className="text-xs capitalize">{s.status}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Create `app/expenses/page.tsx`**

```tsx
"use client";

import { useEffect, useState } from "react";

import { AnomalyHighlights } from "@/components/expenses/anomaly-highlights";
import { CategoryDonut } from "@/components/expenses/category-donut";
import { HeroStats } from "@/components/expenses/hero-stats";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { SourcesHealthTable } from "@/components/expenses/sources-health-table";
import { TopMerchantsCard } from "@/components/expenses/top-merchants-card";
import { Card, CardContent } from "@/components/ui/card";
import { expensesApi, type DashboardOverview } from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

export default function ExpensesOverviewPage() {
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();

  useEffect(() => {
    setLoading(true);
    expensesApi
      .dashboardOverview(USER_ID, 12, fxMode)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode]);

  if (error) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-rose-600 text-sm">
          Failed to load: {error}
        </CardContent>
      </Card>
    );
  }
  if (loading || !data) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading dashboard…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <HeroStats overview={data} fxMode={fxMode} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MonthlySpendChart data={data.months} fxMode={fxMode} />
        <CategoryDonut data={data.current_month_top_categories} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopMerchantsCard data={data.top_merchants_current_month} />
        <AnomalyHighlights anomalies={data.anomalies} />
      </div>
      <SourcesHealthTable data={data.sources_health} />
    </div>
  );
}
```

- [ ] **Step 3: Lint**

```pwsh
cd ui ; npm run lint
```

- [ ] **Step 4: Commit**

```pwsh
git add ui/src/components/expenses/sources-health-table.tsx ui/src/app/expenses/page.tsx
git commit -m "feat(ui-expenses): sources-health table + overview page assembly"
```

---

### Phase D — Transactions page (Tasks 13-14)

### Task 13: Category-edit popover + transactions table component

**Files:**
- Create: `ui/src/components/expenses/category-edit-popover.tsx`
- Create: `ui/src/components/expenses/transactions-table.tsx`

- [ ] **Step 1: Create `category-edit-popover.tsx`**

```tsx
"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  expensesApi, type CategoryOut,
} from "@/lib/expenses/api";

interface CategoryEditPopoverProps {
  txId: number;
  userId: string;
  currentSlug: string | null;
  categories: CategoryOut[];
  onChanged?: (newSlug: string) => void;
}

export function CategoryEditPopover({
  txId, userId, currentSlug, categories, onChanged,
}: CategoryEditPopoverProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [saving, setSaving] = useState<string | null>(null);

  const filtered = categories.filter((c) =>
    c.slug.includes(filter.toLowerCase()) || c.label_en.toLowerCase().includes(filter.toLowerCase())
  );

  async function pick(slug: string) {
    setSaving(slug);
    try {
      await expensesApi.patchTransactionCategory(txId, userId, slug);
      onChanged?.(slug);
      setOpen(false);
      setFilter("");
    } catch (e) {
      alert(`Failed to save: ${e}`);
    } finally {
      setSaving(null);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Badge variant="secondary" className="cursor-pointer hover:bg-secondary/80 capitalize">
          {currentSlug?.replace(/_/g, " ") ?? "uncategorized"}
        </Badge>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Re-categorize</DialogTitle>
        </DialogHeader>
        <Input
          placeholder="Filter categories…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoFocus
        />
        <div className="max-h-72 overflow-y-auto flex flex-col gap-1">
          {filtered.map((c) => (
            <Button
              key={c.slug}
              variant={c.slug === currentSlug ? "secondary" : "ghost"}
              size="sm"
              disabled={saving !== null}
              onClick={() => pick(c.slug)}
              className="justify-start capitalize"
            >
              {saving === c.slug ? "Saving…" : c.label_en}
            </Button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Create `transactions-table.tsx`**

```tsx
"use client";

import { CategoryEditPopover } from "@/components/expenses/category-edit-popover";
import { Badge } from "@/components/ui/badge";
import {
  type CategoryOut,
  type SourceOut,
  type TransactionOut,
} from "@/lib/expenses/api";
import { formatCurrency, formatNIS } from "@/lib/expenses/format";

const USER_ID = "ariel";

interface TransactionsTableProps {
  transactions: TransactionOut[];
  categories: CategoryOut[];
  sources: SourceOut[];
  onCategoryChanged?: () => void;
}

export function TransactionsTable({
  transactions, categories, sources, onCategoryChanged,
}: TransactionsTableProps) {
  const sourceById = new Map(sources.map((s) => [s.id, s]));

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs text-muted-foreground border-b border-border">
          <th className="text-left py-2 pr-2">Date</th>
          <th className="text-left py-2 px-2">Merchant</th>
          <th className="text-left py-2 px-2">Category</th>
          <th className="text-left py-2 px-2">Source</th>
          <th className="text-right py-2 pl-2">Amount</th>
        </tr>
      </thead>
      <tbody>
        {transactions.map((t) => {
          const src = sourceById.get(t.source_id);
          const isRefund = t.direction === "credit" || t.tx_type === "refund";
          const amountText = t.amount_nis !== null
            ? formatNIS(t.amount_nis)
            : (t.amount_orig !== null && t.currency_orig !== null
              ? formatCurrency(t.amount_orig, t.currency_orig)
              : "—");
          return (
            <tr key={t.id} className="border-b border-border/60 hover:bg-secondary/40">
              <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-muted-foreground">
                {t.occurred_on}
              </td>
              <td className="py-2 px-2 truncate max-w-xs">{t.merchant_raw}</td>
              <td className="py-2 px-2">
                <CategoryEditPopover
                  txId={t.id}
                  userId={USER_ID}
                  currentSlug={t.category_slug}
                  categories={categories}
                  onChanged={() => onCategoryChanged?.()}
                />
              </td>
              <td className="py-2 px-2 text-xs text-muted-foreground">
                {src?.display_name ?? `#${t.source_id}`}
              </td>
              <td className="py-2 pl-2 text-right tabular-nums whitespace-nowrap">
                <span className={isRefund ? "text-emerald-600" : ""}>
                  {isRefund ? "+" : ""}{amountText}
                </span>
                {t.tx_type !== "regular" && (
                  <Badge variant="secondary" className="ml-2 text-xs">
                    {t.tx_type}
                  </Badge>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 3: Commit**

```pwsh
git add ui/src/components/expenses/category-edit-popover.tsx ui/src/components/expenses/transactions-table.tsx
git commit -m "feat(ui-expenses): transactions table + inline category edit popover"
```

### Task 14: `/expenses/transactions` page with filters

**Files:**
- Create: `ui/src/app/expenses/transactions/page.tsx`

- [ ] **Step 1: Create the page**

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { TransactionsTable } from "@/components/expenses/transactions-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  expensesApi,
  type CategoryOut,
  type SourceOut,
  type TransactionsResponse,
} from "@/lib/expenses/api";

const USER_ID = "ariel";
const PAGE_SIZE = 100;

export default function TransactionsPage() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<TransactionsResponse | null>(null);
  const [categories, setCategories] = useState<CategoryOut[]>([]);
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);

  const filterParams = {
    category: params.get("category") ?? undefined,
    source_id: params.get("source_id") ? Number(params.get("source_id")) : undefined,
    direction: params.get("direction") as "debit" | "credit" | undefined,
    search: params.get("search") ?? undefined,
    from_date: params.get("from_date") ?? undefined,
    to_date: params.get("to_date") ?? undefined,
    include_card_payments: params.get("include_card_payments") === "1",
  };

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [tx, cats, srcs] = await Promise.all([
        expensesApi.transactions(USER_ID, {
          ...filterParams,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        }),
        expensesApi.categories(USER_ID),
        expensesApi.sources(USER_ID),
      ]);
      setData(tx);
      setCategories(cats.categories);
      setSources(srcs.sources);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filterParams), page]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(params.toString());
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
    router.replace(`/expenses/transactions?${next.toString()}`);
    setPage(0);
  }

  const total = data?.total ?? 0;
  const showing = data?.transactions.length ?? 0;

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardContent className="p-4 flex flex-wrap gap-2 items-end">
          <Input
            placeholder="Search merchant…"
            defaultValue={filterParams.search ?? ""}
            onBlur={(e) => setParam("search", e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setParam("search", e.currentTarget.value);
            }}
            className="max-w-xs"
          />
          <select
            value={filterParams.category ?? ""}
            onChange={(e) => setParam("category", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
          >
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c.slug} value={c.slug}>{c.label_en}</option>
            ))}
          </select>
          <select
            value={filterParams.source_id ?? ""}
            onChange={(e) => setParam("source_id", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
          >
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s.id} value={s.id}>{s.display_name}</option>
            ))}
          </select>
          <select
            value={filterParams.direction ?? ""}
            onChange={(e) => setParam("direction", e.target.value || null)}
            className="bg-background border border-border rounded px-2 py-1.5 text-sm"
          >
            <option value="">Both</option>
            <option value="debit">Debits</option>
            <option value="credit">Credits</option>
          </select>
          <label className="text-xs text-muted-foreground inline-flex items-center gap-1">
            <input
              type="checkbox"
              checked={filterParams.include_card_payments}
              onChange={(e) => setParam("include_card_payments", e.target.checked ? "1" : null)}
            />
            include card-payments
          </label>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-4 overflow-x-auto">
          {loading && !data ? (
            <div className="text-sm text-muted-foreground py-6 text-center">
              Loading transactions…
            </div>
          ) : (
            <>
              <div className="text-xs text-muted-foreground mb-2">
                {showing} of {total} transactions
              </div>
              <TransactionsTable
                transactions={data?.transactions ?? []}
                categories={categories}
                sources={sources}
                onCategoryChanged={refresh}
              />
              <div className="flex items-center justify-between mt-3 text-sm">
                <Button
                  variant="outline" size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  ← Prev
                </Button>
                <span className="text-muted-foreground text-xs">
                  Page {page + 1} of {Math.max(1, Math.ceil(total / PAGE_SIZE))}
                </span>
                <Button
                  variant="outline" size="sm"
                  disabled={(page + 1) * PAGE_SIZE >= total}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next →
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Lint**

- [ ] **Step 3: Commit**

```pwsh
git add ui/src/app/expenses/transactions/page.tsx
git commit -m "feat(ui-expenses): /transactions page with filters + pagination"
```

---

### Phase E — Sources page (Task 15)

### Task 15: Per-source detail page

**Files:**
- Create: `ui/src/app/expenses/sources/page.tsx`
- Create: `ui/src/components/expenses/source-statement-timeline.tsx`

- [ ] **Step 1: Create the timeline component**

```tsx
"use client";

import {
  Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { type StatementSummary } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

export function SourceStatementTimeline({ data }: { data: StatementSummary[] }) {
  const rows = data.map((s) => ({
    period: s.period_start.slice(0, 7),
    parsed: s.parsed_total_nis ?? 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={rows} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
        <XAxis dataKey="period" fontSize={10} />
        <YAxis fontSize={10} tickFormatter={(v: number) => formatNIS(v)} width={70} />
        <Tooltip formatter={(v: number) => formatNIS(v)} />
        <Bar dataKey="parsed" fill="hsl(220, 70%, 55%)" isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
```

- [ ] **Step 2: Create `app/expenses/sources/page.tsx`**

```tsx
"use client";

import { useEffect, useState } from "react";

import { SourceStatementTimeline } from "@/components/expenses/source-statement-timeline";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  expensesApi,
  type SourceDetailResponse,
  type SourceOut,
} from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

const STATUS_DOT = {
  green: "bg-emerald-500",
  yellow: "bg-amber-500",
  red: "bg-rose-500",
  unknown: "bg-muted-foreground/40",
} as const;

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [details, setDetails] = useState<Record<number, SourceDetailResponse>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const list = await expensesApi.sources(USER_ID);
        if (cancelled) return;
        setSources(list.sources);
        const detailEntries = await Promise.all(
          list.sources.map((s) =>
            expensesApi.sourceDetail(s.id, USER_ID)
              .then((d) => [s.id, d] as const)
              .catch(() => [s.id, null] as const),
          ),
        );
        if (cancelled) return;
        const map: Record<number, SourceDetailResponse> = {};
        for (const [id, d] of detailEntries) {
          if (d) map[id] = d;
        }
        setDetails(map);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loading && sources.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading sources…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {sources.map((s) => {
        const d = details[s.id];
        return (
          <Card key={s.id}>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <span>{s.display_name}</span>
                <span className="text-xs text-muted-foreground font-normal">
                  {s.issuer} {s.external_id}
                  {s.cardholder_name ? ` · ${s.cardholder_name}` : ""}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {d ? (
                <>
                  <SourceStatementTimeline data={d.statements} />
                  <table className="w-full text-sm mt-3">
                    <thead>
                      <tr className="text-xs text-muted-foreground border-b border-border">
                        <th className="text-left py-2 pr-2">Period</th>
                        <th className="text-right py-2 px-2">Parsed</th>
                        <th className="text-right py-2 px-2">Declared</th>
                        <th className="text-right py-2 px-2">Gap</th>
                        <th className="text-right py-2 px-2">Tx</th>
                        <th className="text-right py-2 px-2">Card-paid</th>
                        <th className="text-right py-2 pl-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.statements.map((st) => (
                        <tr key={st.id} className="border-b border-border/60">
                          <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-xs">
                            {st.period_start} → {st.period_end}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.parsed_total_nis !== null ? formatNIS(st.parsed_total_nis) : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.declared_total_nis !== null ? formatNIS(st.declared_total_nis) : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.gap !== null ? `${st.gap >= 0 ? "+" : ""}${st.gap.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">{st.transaction_count}</td>
                          <td className="py-2 px-2 text-right tabular-nums">{st.correlated_count}</td>
                          <td className="py-2 pl-2 text-right">
                            <span className="inline-flex items-center gap-1.5">
                              <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[st.status])} />
                              <span className="text-xs capitalize">{st.status}</span>
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              ) : (
                <div className="text-sm text-muted-foreground py-6 text-center">
                  Could not load detail for {s.display_name}.
                </div>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 3: Lint**

- [ ] **Step 4: Commit**

```pwsh
git add ui/src/components/expenses/source-statement-timeline.tsx ui/src/app/expenses/sources/page.tsx
git commit -m "feat(ui-expenses): /sources page with per-source statement timeline"
```

---

### Phase F — Polish + smoke (Task 16)

### Task 16: Build, smoke, fix any issues

- [ ] **Step 1: Build the UI**

```pwsh
cd ui ; npm run build
```

If build fails, inspect the error, fix, retry. Typical issues:
- Recharts default-export quirks → use named imports per component above.
- TypeScript strict-mode null guards → ensure all the `?? null` and `!== null` guards are in place.
- Next.js 16 may warn about `useSearchParams` outside a Suspense boundary — wrap the transactions page export in `<Suspense>` if needed.

- [ ] **Step 2: Run full backend test suite**

```pwsh
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q
```

Expected: 950+ PASS (was ~948 + the new tests).

- [ ] **Step 3: Manually smoke each route in dev**

```pwsh
cd ui ; npm run dev
```

(Note: the user's uvicorn at port 8000 must be running with the new endpoints loaded. Either restart uvicorn or run a quick `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --port 8000` in another terminal.)

Open `http://localhost:1337/expenses` — verify:
- Hero stats render (4 cards).
- Monthly spend chart loads.
- Category donut populates.
- Top merchants table loads.
- Anomalies section either shows green-✓ or lists items.
- Sources health table lists 5 sources (post-T16 backfill).

Then `/expenses/transactions`:
- Table loads.
- Filters work (category dropdown, source dropdown, search, direction).
- Pagination works.
- Inline category edit dialog opens, save persists.

Then `/expenses/sources`:
- Each source has a card with a timeline + statement table.

- [ ] **Step 4: Commit any fixes**

If any issues surfaced, commit them with `fix(ui-expenses): ...` messages.

- [ ] **Step 5: Final commit — wave 3 close**

If clean, no further commits needed. Update SDD §16 handover note in a separate task.

---

## End-state acceptance

- [ ] All 3 backend endpoints exist (`/dashboard-overview`, `/source-detail/{id}`, hotfix to `/transactions`); ~6 new tests pass.
- [ ] `pytest -m "not llm_eval" -q` shows 950+ PASS, 0 FAIL.
- [ ] `npm run build` succeeds in `ui/`.
- [ ] `npm run lint` clean in `ui/`.
- [ ] NavBar has "Expenses" tab.
- [ ] `/expenses` overview renders without console errors.
- [ ] `/expenses/transactions` filters + inline category edit work.
- [ ] `/expenses/sources` renders per-source timeline.
- [ ] FX toggle changes display (per-currency stack vs NIS-only).
- [ ] No regressions in existing routes (Portfolio, Plan, Proposals, etc.).
