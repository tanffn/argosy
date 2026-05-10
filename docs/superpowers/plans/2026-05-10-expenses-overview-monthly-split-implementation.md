# Expenses Overview / Monthly Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single `/expenses` Overview tab into two purpose-built tabs (yearly Overview + per-month Monthly), reshape the backend payload, and add five insight-level widgets per the EX6 spec.

**Architecture:** Extract the inline `dashboard_overview` aggregation logic from `argosy/api/routes/expenses.py` into a new `argosy/services/expense_dashboard.py` service module. Add a sibling `dashboard_monthly` endpoint. Reshape the existing endpoint to drop monthly fields. On the frontend, `HeroStats` and `MonthlySpendChart` gain `mode` props; five new chart/card components are added; a new `/expenses/monthly` route consumes the new endpoint.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Pydantic v2 (backend); Next.js 15 / React / Recharts 3.x / Tailwind (frontend); pytest (backend tests); existing `tests/test_dashboard_overview_endpoint.py` is the authoritative regression baseline.

**Spec:** `docs/superpowers/specs/2026-05-10-expenses-overview-monthly-split-design.md`

**Common conventions for this plan:**
- Python interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`
- Run backend tests: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
- Run lint+typecheck on UI: `cd ui ; npm run lint ; npm run typecheck`
- All commits should follow the existing convention: `<type>(<scope>): <subject>` (look at `git log --oneline -20` for examples).
- Working tree must be clean before starting; commit after each task.
- This plan does NOT introduce schema changes — no Alembic migrations.
- The existing dev DB has full corpus (6 sources, 56 statements, 2,179 transactions). All tests should pass against it without re-ingest.
- Before starting Task 1, verify the spec file exists at `docs/superpowers/specs/2026-05-10-expenses-overview-monthly-split-design.md` and read its API and aggregation sections (§5 and §7).

---

## Phase 1 — Backend foundation

### Task 1: Create `argosy/services/expense_dashboard.py` module skeleton

**Files:**
- Create: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

- [ ] **Step 1: Write a smoke test that imports the new module**

```python
# tests/test_expense_dashboard.py
"""Tests for argosy.services.expense_dashboard aggregation helpers."""
from argosy.services import expense_dashboard


def test_module_importable():
    assert hasattr(expense_dashboard, "__name__")
```

- [ ] **Step 2: Run test, expect ModuleNotFoundError**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_dashboard.py -v`
Expected: FAIL with "No module named 'argosy.services.expense_dashboard'"

- [ ] **Step 3: Create the module with a docstring**

```python
# argosy/services/expense_dashboard.py
"""Aggregation helpers for the /expenses dashboard endpoints.

Two endpoints share these helpers:

    GET /api/expenses/dashboard-overview  → "year-at-a-glance" tab
    GET /api/expenses/dashboard-monthly   → per-month detail tab

All helpers are sync, take a SQLAlchemy `Session`, and never call an LLM.
They return Pydantic models from `argosy.api.routes.expenses` (the route
module currently owns the schema; importing back from there is fine for
the v0 of this extraction).
"""
from __future__ import annotations
```

- [ ] **Step 4: Run test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): scaffold service module"
```

---

### Task 2: Add new Pydantic types to `argosy/api/routes/expenses.py`

**Files:**
- Modify: `argosy/api/routes/expenses.py` (add new types near existing `DashboardOverview` block, around line 573)

- [ ] **Step 1: Write a test that asserts each new type exists with the expected fields**

Append to `tests/test_expense_dashboard.py`:

```python
def test_new_pydantic_types_exist():
    from argosy.api.routes.expenses import (
        SavingsRatePoint,
        CategoryDelta,
        TopMovers,
        CurrencyMixPoint,
        ChartWindowBar,
        HeroMetric,
        HeroStatsMonthly,
        CategoryDeviation,
        DashboardMonthly,
    )
    sr = SavingsRatePoint(month="2026-04", income_nis=100.0, spending_nis=80.0, savings_rate=0.2)
    assert sr.savings_rate == 0.2
    cd = CategoryDelta(slug="food", label="Food", current_nis=100.0, prior_nis=80.0, delta_nis=20.0, delta_pct=0.25)
    assert cd.delta_pct == 0.25
    tm = TopMovers(grew=[cd], shrank=[], reason=None)
    assert tm.reason is None
    cmp_ = CurrencyMixPoint(month="2026-04", nis=1000.0, usd=200.0)
    assert cmp_.usd == 200.0
    cwb = ChartWindowBar(month="2026-04", total_nis=1000.0, total_usd=0.0, is_padding=False, is_selected=True)
    assert cwb.is_selected is True
    hm = HeroMetric(value_nis=1000.0, mom_delta_pct=0.1, vs_trailing12_pct=0.05)
    assert hm.value_nis == 1000.0
    hsm = HeroStatsMonthly(
        spent=hm, income=hm, refunds=hm,
        statements_reconciled=3, anomalies_count=1,
    )
    assert hsm.statements_reconciled == 3
    cdv = CategoryDeviation(
        slug="food", label="Food", this_month_nis=3800.0,
        typical_mean_nis=2800.0, typical_std_nis=400.0, z_score=2.5, delta_pct=0.357,
    )
    assert cdv.z_score == 2.5
    dm = DashboardMonthly(
        month="2026-04", available_months=["2026-04"],
        chart_window=[cwb] * 12,
        hero_stats=hsm,
        top_categories=[], categories_vs_typical=[],
        top_merchants=[], largest_transactions=[],
        anomalies=[],
    )
    assert dm.month == "2026-04"
```

- [ ] **Step 2: Run, expect ImportError**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_dashboard.py::test_new_pydantic_types_exist -v`
Expected: FAIL with `ImportError: cannot import name 'SavingsRatePoint' …`

- [ ] **Step 3: Add the types** to `argosy/api/routes/expenses.py` immediately after the existing `DashboardOverview` class (around line 590):

```python
# ---------------------------------------------------------------------------
# EX6 — new types for the Overview/Monthly split.
# ---------------------------------------------------------------------------

class SavingsRatePoint(BaseModel):
    """One month's savings-rate datum for the Overview tab trend chart."""

    month: str                      # 'YYYY-MM'
    income_nis: float
    spending_nis: float
    savings_rate: float             # (income - spending) / income; 0 if income == 0


class CategoryDelta(BaseModel):
    """One category's spending delta between current vs prior period."""

    slug: str
    label: str
    current_nis: float              # current-window total
    prior_nis: float                # prior-window total
    delta_nis: float                # current - prior
    delta_pct: float | None         # (current - prior)/prior; None when prior==0


class TopMovers(BaseModel):
    """Top-5 categories that grew vs top-5 that shrank, current vs prior period."""

    grew: list[CategoryDelta]
    shrank: list[CategoryDelta]
    reason: str | None = None       # 'insufficient_history' when prior is missing


class CurrencyMixPoint(BaseModel):
    """One month's NIS-vs-USD spending split for the Overview currency mix bar."""

    month: str
    nis: float
    usd: float


class ChartWindowBar(BaseModel):
    """One bar of the Monthly tab's 12-bar focal chart."""

    month: str
    total_nis: float
    total_usd: float
    is_padding: bool                # true when the bar is outside the user's data range
    is_selected: bool               # true when month == focal month


class HeroMetric(BaseModel):
    """Numeric hero card with MoM and vs-trailing-12 deltas."""

    value_nis: float
    mom_delta_pct: float | None     # vs prior month; None when prior is 0 or missing
    vs_trailing12_pct: float | None # vs trailing-12 avg; None when fewer than 3 priors


class HeroStatsMonthly(BaseModel):
    """Monthly tab's hero-stat bundle."""

    spent: HeroMetric
    income: HeroMetric
    refunds: HeroMetric
    statements_reconciled: int      # no delta semantics (count)
    anomalies_count: int            # no delta semantics (count)


class CategoryDeviation(BaseModel):
    """A category whose this-month total deviates from its typical (rolling-12) baseline."""

    slug: str
    label: str
    this_month_nis: float
    typical_mean_nis: float
    typical_std_nis: float          # floored at ₪50 to avoid blowups for sparse cats
    z_score: float                  # (this - mean)/std; signed
    delta_pct: float | None         # (this - mean)/mean; None when mean==0


class DashboardMonthly(BaseModel):
    """Response payload for GET /api/expenses/dashboard-monthly."""

    month: str                                        # focal month
    available_months: list[str]                       # for the MonthPicker
    chart_window: list[ChartWindowBar]                # always length 12
    hero_stats: HeroStatsMonthly
    top_categories: list[CategorySpend]               # selected-month top categories
    categories_vs_typical: list[CategoryDeviation]    # most-divergent categories
    top_merchants: list[MerchantSpend]
    largest_transactions: list[TransactionOut]        # top 5 by |amount_nis|
    anomalies: list[AnomalyCard]
    fx_mode: str = "per_currency"
```

- [ ] **Step 4: Run, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_dashboard.py::test_new_pydantic_types_exist -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): add EX6 Pydantic types"
```

---

## Phase 2 — Aggregation helpers (TDD, one helper per task)

For Phase 2 tasks, the test fixture is a small in-memory SQLite schema seeded via the existing `tests/conftest.py` fixtures. **Read `tests/test_dashboard_overview_endpoint.py` once before starting Task 3** — it shows the seeding patterns you'll reuse (categories, sources, statements, transactions). The simplest fixture utility is `_seed_user_with_data(session, user_id="test")` if such a helper exists in conftest; otherwise replicate the seeding inline.

### Task 3: Implement `compute_savings_rate_trend(session, user_id, months=12)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py` — add helper
- Test: `tests/test_expense_dashboard.py` — add test

**Behavior contract:**
- Returns 12 `SavingsRatePoint` entries, oldest-first.
- Income = sum of `amount_nis` over `direction=='credit' AND tx_type != 'refund'` filtered to inflow categories.
- Spending = sum over `direction=='debit' AND is_inflow=False AND is_excluded_from_spend=False`.
- `savings_rate = (income - spending) / income` if income > 0, else `0.0`.
- Months with zero data still appear (income=0, spending=0, savings_rate=0).
- Ordering: oldest→newest.

- [ ] **Step 1: Write the failing test**

```python
def test_compute_savings_rate_trend_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_savings_rate_trend
    points = compute_savings_rate_trend(db_session_with_seeded_user, "test", months=12)
    assert len(points) == 12
    # oldest-first
    assert points[0].month < points[-1].month
    # rate is bounded
    for p in points:
        assert -10.0 <= p.savings_rate <= 1.0
    # Months without income should have rate == 0.0
    for p in points:
        if p.income_nis == 0:
            assert p.savings_rate == 0.0
```

(Use the seeding utility you discover in `tests/conftest.py` or `tests/test_dashboard_overview_endpoint.py`. If no fixture exists, create one in `conftest.py` named `db_session_with_seeded_user` that seeds: 14 months of data, income alternating ₪10000/₪0, spending always ₪5000.)

- [ ] **Step 2: Run, expect FAIL** (ImportError or assertion mismatch)

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_expense_dashboard.py::test_compute_savings_rate_trend_basic -v`

- [ ] **Step 3: Implement the helper**

In `argosy/services/expense_dashboard.py`:

```python
from datetime import date
from typing import Sequence

from sqlalchemy import case, extract, func, select as sa_select
from sqlalchemy.orm import Session

from argosy.state.models import ExpenseCategory, ExpenseTransaction


def _trailing_months(latest: date, n: int) -> list[str]:
    """Return n trailing 'YYYY-MM' strings ending at `latest`, oldest-first."""
    out: list[str] = []
    y, m = latest.year, latest.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def _latest_tx_month(session: Session, user_id: str) -> date | None:
    """Return the first-of-month date for the latest tx month, or None if no data."""
    row = session.execute(
        sa_select(func.max(ExpenseTransaction.occurred_on))
        .where(ExpenseTransaction.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    return date(row.year, row.month, 1)


def compute_savings_rate_trend(
    session: Session, user_id: str, months: int = 12
):
    """One savings_rate point per month for the trailing window."""
    from argosy.api.routes.expenses import SavingsRatePoint

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return []
    month_keys = _trailing_months(latest, months)

    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            ExpenseTransaction.direction,
            ExpenseTransaction.tx_type,
            ExpenseCategory.is_inflow,
            ExpenseCategory.is_excluded_from_spend,
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(ExpenseTransaction.user_id == user_id)
        .group_by("y", "m", ExpenseTransaction.direction,
                  ExpenseTransaction.tx_type,
                  ExpenseCategory.is_inflow,
                  ExpenseCategory.is_excluded_from_spend)
    ).all()

    income: dict[str, float] = {k: 0.0 for k in month_keys}
    spending: dict[str, float] = {k: 0.0 for k in month_keys}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if key not in income:
            continue
        if r.direction == "credit" and r.tx_type != "refund" and r.is_inflow:
            income[key] += float(r.nis or 0.0)
        elif r.direction == "debit" and not (r.is_inflow or r.is_excluded_from_spend):
            spending[key] += float(r.nis or 0.0)

    out = []
    for key in month_keys:
        inc = income[key]
        spend = spending[key]
        rate = (inc - spend) / inc if inc > 0 else 0.0
        out.append(SavingsRatePoint(
            month=key, income_nis=inc, spending_nis=spend, savings_rate=rate,
        ))
    return out
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py tests/conftest.py
git commit -m "feat(expense-dashboard): savings-rate trend aggregation"
```

---

### Task 4: Implement `compute_top_movers(session, user_id, window)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:**
- `window='trailing_12'`: compare trailing-6 months vs prior-6 months (relative to latest tx month).
- `window='calendar_year'`: compare current-calendar-year-to-date vs prior-calendar-year-through-same-month-end.
- Returns `TopMovers` with `grew` (top 5 by positive delta_nis) and `shrank` (top 5 by negative delta_nis, most-negative first).
- If there's no prior period (e.g. user has < 12 months), return `TopMovers(grew=[], shrank=[], reason="insufficient_history")`.
- Spending-only filter (`direction=='debit' AND is_inflow=False AND is_excluded_from_spend=False`).
- Group by `category_id`, label = `ExpenseCategory.label_en`, slug = `ExpenseCategory.slug`.
- `delta_pct = (current - prior)/prior` when `prior > 0`; `None` otherwise.

- [ ] **Step 1: Write the failing test**

```python
def test_compute_top_movers_trailing(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_top_movers
    movers = compute_top_movers(db_session_with_seeded_user, "test", window="trailing_12")
    assert movers.reason in (None, "insufficient_history")
    if movers.reason is None:
        assert len(movers.grew) <= 5
        assert len(movers.shrank) <= 5
        # grew is sorted desc by delta_nis
        for a, b in zip(movers.grew, movers.grew[1:]):
            assert a.delta_nis >= b.delta_nis
        # shrank is sorted asc by delta_nis (most-negative first)
        for a, b in zip(movers.shrank, movers.shrank[1:]):
            assert a.delta_nis <= b.delta_nis


def test_compute_top_movers_insufficient_history(db_session_short_history):
    """User with only 4 months of data → reason='insufficient_history'."""
    from argosy.services.expense_dashboard import compute_top_movers
    movers = compute_top_movers(db_session_short_history, "test", window="trailing_12")
    assert movers.reason == "insufficient_history"
    assert movers.grew == []
    assert movers.shrank == []
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
def compute_top_movers(session: Session, user_id: str, window: str = "trailing_12"):
    """Top growing/shrinking categories, current vs prior period."""
    from argosy.api.routes.expenses import CategoryDelta, TopMovers

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")

    if window == "calendar_year":
        # current = Jan 1 .. latest_month_end of latest year
        # prior = Jan 1 .. latest_month_end of prior year
        end_month = latest.month
        cur_year = latest.year
        prior_year = cur_year - 1
        cur_keys = [f"{cur_year:04d}-{m:02d}" for m in range(1, end_month + 1)]
        prior_keys = [f"{prior_year:04d}-{m:02d}" for m in range(1, end_month + 1)]
    else:
        keys_24 = _trailing_months(latest, 12 + 6)  # need 12 prior + 6 current
        all_keys = _trailing_months(latest, 12)     # 12 trailing total: 6 prior + 6 current
        prior_keys = all_keys[:6]
        cur_keys = all_keys[6:]

    # Need at least one tx in prior_keys[0] month or earlier for "sufficient history".
    earliest = session.execute(
        sa_select(func.min(ExpenseTransaction.occurred_on))
        .where(ExpenseTransaction.user_id == user_id)
    ).scalar_one_or_none()
    if earliest is None:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")
    earliest_key = f"{earliest.year:04d}-{earliest.month:02d}"
    if earliest_key > prior_keys[0]:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")

    # Aggregate per (category, period).
    rows = session.execute(
        sa_select(
            ExpenseCategory.slug,
            ExpenseCategory.label_en,
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseCategory.is_inflow.is_(False),
            ExpenseCategory.is_excluded_from_spend.is_(False),
        )
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en, "y", "m")
    ).all()

    cur_totals: dict[str, dict] = {}  # slug -> {label, total}
    prior_totals: dict[str, dict] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        bucket = cur_totals if key in cur_keys else (prior_totals if key in prior_keys else None)
        if bucket is None:
            continue
        e = bucket.setdefault(r.slug, {"label": r.label_en, "total": 0.0})
        e["total"] += float(r.nis or 0.0)

    all_slugs = set(cur_totals) | set(prior_totals)
    deltas: list[CategoryDelta] = []
    for slug in all_slugs:
        cur = cur_totals.get(slug, {"label": "", "total": 0.0})
        prior = prior_totals.get(slug, {"label": "", "total": 0.0})
        label = cur["label"] or prior["label"] or slug
        delta_nis = cur["total"] - prior["total"]
        delta_pct = (delta_nis / prior["total"]) if prior["total"] > 0 else None
        deltas.append(CategoryDelta(
            slug=slug, label=label,
            current_nis=cur["total"], prior_nis=prior["total"],
            delta_nis=delta_nis, delta_pct=delta_pct,
        ))

    grew = sorted([d for d in deltas if d.delta_nis > 0],
                  key=lambda d: d.delta_nis, reverse=True)[:5]
    shrank = sorted([d for d in deltas if d.delta_nis < 0],
                    key=lambda d: d.delta_nis)[:5]
    return TopMovers(grew=grew, shrank=shrank, reason=None)
```

- [ ] **Step 4: Run, expect PASS** (both tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): top-movers categories aggregation"
```

---

### Task 5: Implement `compute_currency_mix(session, user_id, months=12)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:**
- Returns `list[CurrencyMixPoint]`, length `months`, oldest-first.
- For each month: NIS = sum(amount_nis where direction='debit', currency_orig='NIS' or NULL, spending filter); USD = sum(amount_orig where direction='debit', currency_orig='USD', spending filter).
- Spending filter: `is_inflow=False AND is_excluded_from_spend=False`.
- USD is the original USD amount, not converted.

- [ ] **Step 1: Write the failing test**

```python
def test_compute_currency_mix_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_currency_mix
    points = compute_currency_mix(db_session_with_seeded_user, "test", months=12)
    assert len(points) == 12
    assert points[0].month < points[-1].month
    assert all(p.nis >= 0 for p in points)
    assert all(p.usd >= 0 for p in points)
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

- [ ] **Step 3: Implement**

```python
def compute_currency_mix(session: Session, user_id: str, months: int = 12):
    from argosy.api.routes.expenses import CurrencyMixPoint

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return []
    month_keys = _trailing_months(latest, months)

    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.coalesce(func.sum(case(
                (ExpenseTransaction.currency_orig == "USD",
                 ExpenseTransaction.amount_orig),
                else_=ExpenseTransaction.amount_nis,
            )), 0.0).label("amt"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
        )
        # spending filter — categorised rows must be non-inflow, non-excluded
        # (uncategorised rows pass the outer-join sieve already; treat as NIS spend)
        .group_by("y", "m", "ccy")
    ).all()

    nis: dict[str, float] = {k: 0.0 for k in month_keys}
    usd: dict[str, float] = {k: 0.0 for k in month_keys}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if key not in nis:
            continue
        if r.ccy == "USD":
            usd[key] += float(r.amt or 0.0)
        else:
            nis[key] += float(r.amt or 0.0)

    return [
        CurrencyMixPoint(month=k, nis=nis[k], usd=usd[k]) for k in month_keys
    ]
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): currency-mix aggregation"
```

---

### Task 6: Implement `compute_chart_window(session, user_id, focal_month)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:** implements §5.3 of the spec (the A-rule). Returns exactly 12 `ChartWindowBar` entries.

- [ ] **Step 1: Write failing tests covering all four edges**

```python
import pytest
from datetime import date


def _shift_month(s: str, delta: int) -> str:
    y, m = int(s[:4]), int(s[5:7])
    m += delta
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


def test_chart_window_centering_basic(db_session_long_history):
    """Selected month deep inside data range → 6 before + 1 selected + 5 after."""
    from argosy.services.expense_dashboard import compute_chart_window
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2025-06")
    assert len(bars) == 12
    months = [b.month for b in bars]
    assert months == [_shift_month("2025-06", -6 + i) for i in range(12)]
    assert sum(1 for b in bars if b.is_selected) == 1
    assert next(b for b in bars if b.is_selected).month == "2025-06"
    assert all(not b.is_padding for b in bars)


def test_chart_window_slide_at_future_edge(db_session_long_history):
    """Selected month is the latest month → window slides left so newest is the rightmost bar."""
    from argosy.services.expense_dashboard import compute_chart_window
    # Assume db_session_long_history's newest month is "2026-04"
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2026-04")
    assert bars[-1].month == "2026-04"
    assert bars[-1].is_selected
    assert all(not b.is_padding for b in bars)


def test_chart_window_slide_at_past_edge(db_session_long_history):
    """Selected month is the earliest → window slides right so oldest is leftmost bar."""
    from argosy.services.expense_dashboard import compute_chart_window
    # Assume db_session_long_history's oldest month is "2024-12"
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2024-12")
    assert bars[0].month == "2024-12"
    assert bars[0].is_selected
    assert all(not b.is_padding for b in bars)


def test_chart_window_short_history_pads_to_12(db_session_short_history):
    """User with 4 months of data → 12 bars, 8 with is_padding=True."""
    from argosy.services.expense_dashboard import compute_chart_window
    bars = compute_chart_window(db_session_short_history, "test", focal_month="2026-03")
    assert len(bars) == 12
    pad_count = sum(1 for b in bars if b.is_padding)
    assert pad_count == 8
```

(Two new fixtures expected here: `db_session_long_history` (≥18 months, oldest 2024-12, newest 2026-04) and `db_session_short_history` (4 months, e.g. 2026-01..2026-04). Add to `tests/conftest.py`.)

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
def compute_chart_window(session: Session, user_id: str, focal_month: str):
    """Return 12 ChartWindowBar entries per the A-rule (spec §5.3)."""
    from argosy.api.routes.expenses import ChartWindowBar

    # Find oldest and newest months with data.
    bounds = session.execute(
        sa_select(
            func.min(ExpenseTransaction.occurred_on).label("oldest"),
            func.max(ExpenseTransaction.occurred_on).label("newest"),
        ).where(ExpenseTransaction.user_id == user_id)
    ).one()
    if bounds.oldest is None:
        return []
    oldest_key = f"{bounds.oldest.year:04d}-{bounds.oldest.month:02d}"
    newest_key = f"{bounds.newest.year:04d}-{bounds.newest.month:02d}"

    # Compute ideal window centred on focal.
    ideal = [_shift_month_key(focal_month, -6 + i) for i in range(12)]
    left, right = ideal[0], ideal[-1]

    # Slide right at past edge.
    if left < oldest_key:
        # shift = how many months to add
        shift = _months_between(left, oldest_key)
        ideal = [_shift_month_key(k, shift) for k in ideal]
        left, right = ideal[0], ideal[-1]

    # Slide left at future edge — but never push left past oldest.
    if right > newest_key:
        shift = -_months_between(newest_key, right)
        ideal = [_shift_month_key(k, shift) for k in ideal]
        # Re-clamp: don't go past oldest.
        if ideal[0] < oldest_key:
            shift_back = _months_between(ideal[0], oldest_key)
            ideal = [_shift_month_key(k, shift_back) for k in ideal]

    # Aggregate per month from DB.
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.coalesce(func.sum(case(
                (ExpenseTransaction.currency_orig == "USD",
                 ExpenseTransaction.amount_orig),
                else_=ExpenseTransaction.amount_nis,
            )), 0.0).label("amt"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
        )
        .group_by("y", "m", "ccy")
    ).all()
    nis: dict[str, float] = {}
    usd: dict[str, float] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if r.ccy == "USD":
            usd[key] = usd.get(key, 0.0) + float(r.amt or 0.0)
        else:
            nis[key] = nis.get(key, 0.0) + float(r.amt or 0.0)

    out = []
    for key in ideal:
        is_padding = key < oldest_key or key > newest_key
        out.append(ChartWindowBar(
            month=key,
            total_nis=0.0 if is_padding else nis.get(key, 0.0),
            total_usd=0.0 if is_padding else usd.get(key, 0.0),
            is_padding=is_padding,
            is_selected=(key == focal_month),
        ))
    return out


def _shift_month_key(s: str, delta: int) -> str:
    y, m = int(s[:4]), int(s[5:7])
    m += delta
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


def _months_between(a: str, b: str) -> int:
    """Return signed count of months from a→b. b later than a → positive."""
    ay, am = int(a[:4]), int(a[5:7])
    by, bm = int(b[:4]), int(b[5:7])
    return (by - ay) * 12 + (bm - am)
```

- [ ] **Step 4: Run, expect PASS** (all four tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py tests/conftest.py
git commit -m "feat(expense-dashboard): chart-window A-rule sliding aggregation"
```

---

### Task 7: Implement `compute_hero_stats_monthly(session, user_id, month)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:**
- Returns `HeroStatsMonthly`.
- `spent`, `income`, `refunds` are each a `HeroMetric`.
  - `value_nis` is the focal-month total (mirrors existing `current_month_*` semantics).
  - `mom_delta_pct = (curr - prev) / prev` if `prev > 0`, else `None`.
  - `vs_trailing12_pct = (curr - avg12) / avg12` where `avg12` is the average of the 12 months immediately preceding (and excluding) `month`. Returns `None` if fewer than 3 prior months had any data OR avg12 == 0.
- `statements_reconciled` = count of statements with `status='green'` whose period overlaps `month`.
- `anomalies_count` = number of anomaly cards relevant to `month` (look at how `anomalies` is computed in the existing route — reuse the function if extractable).

- [ ] **Step 1: Failing test**

```python
def test_compute_hero_stats_monthly_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_hero_stats_monthly
    h = compute_hero_stats_monthly(db_session_with_seeded_user, "test", month="2026-03")
    assert h.spent.value_nis >= 0
    # When no prior month, mom_delta is None
    h_first = compute_hero_stats_monthly(db_session_with_seeded_user, "test", month="2024-12")
    assert h_first.spent.mom_delta_pct is None
    assert h_first.spent.vs_trailing12_pct is None


def test_compute_hero_stats_monthly_zero_prior(db_session_with_zero_prior):
    """If prior month had zero spending → mom_delta_pct is None (not infinity)."""
    from argosy.services.expense_dashboard import compute_hero_stats_monthly
    h = compute_hero_stats_monthly(db_session_with_zero_prior, "test", month="2026-04")
    assert h.spent.mom_delta_pct is None
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** (delegates to existing `_dashboard_compute_anomalies` and `_dashboard_compute_statements_reconciled` from `argosy/api/routes/expenses.py` — extract these as small helpers if they're inline; otherwise call them directly).

```python
def compute_hero_stats_monthly(session: Session, user_id: str, month: str):
    from argosy.api.routes.expenses import HeroMetric, HeroStatsMonthly

    spending_by_month = _spending_by_month_dict(session, user_id)  # see helper below
    income_by_month = _income_by_month_dict(session, user_id)
    refunds_by_month = _refunds_by_month_dict(session, user_id)

    def metric(by_month: dict[str, float], key: str) -> HeroMetric:
        cur = by_month.get(key, 0.0)
        prev_key = _shift_month_key(key, -1)
        prev = by_month.get(prev_key)
        mom = (cur - prev) / prev if (prev is not None and prev > 0) else None

        # trailing-12 = 12 months immediately before `key`
        trailing_keys = [_shift_month_key(key, -i) for i in range(1, 13)]
        prior_vals = [by_month[k] for k in trailing_keys if k in by_month]
        if len(prior_vals) >= 3 and sum(prior_vals) > 0:
            avg = sum(prior_vals) / len(prior_vals)
            vs12 = (cur - avg) / avg if avg > 0 else None
        else:
            vs12 = None

        return HeroMetric(value_nis=cur, mom_delta_pct=mom, vs_trailing12_pct=vs12)

    # Reuse the route module's existing reconciliation + anomaly computation.
    # If they're hard to import, inline simple counts here.
    statements_reconciled = _count_reconciled_statements_for_month(session, user_id, month)
    anomalies_count = _count_anomalies_for_month(session, user_id, month)

    return HeroStatsMonthly(
        spent=metric(spending_by_month, month),
        income=metric(income_by_month, month),
        refunds=metric(refunds_by_month, month),
        statements_reconciled=statements_reconciled,
        anomalies_count=anomalies_count,
    )


# ----------------- shared per-month accumulators -----------------

def _spending_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            (ExpenseCategory.is_inflow.is_(False) | ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}


def _income_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.tx_type != "refund",
            ExpenseCategory.is_inflow.is_(True),
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}


def _refunds_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.tx_type == "refund",
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}
```

For `_count_reconciled_statements_for_month` and `_count_anomalies_for_month`: inline simple counts. The reconciled-count uses `ExpenseStatement.status == 'green'` and statement period overlaps the month. For `_count_anomalies_for_month`, simplest impl is to just count the same anomalies the existing route would have computed for the focal month — it's fine to leave it as a placeholder constant 0 if extraction is hard, since the field is non-load-bearing for hero rendering. **Do extract the existing anomaly computation if it's a clean helper; otherwise inline a count of `expense_review_queue` rows for that month or just return 0.** Document the choice in a one-line comment.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): hero-stats with MoM and trailing-12 deltas"
```

---

### Task 8: Implement `compute_categories_vs_typical(session, user_id, month)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:**
- For each spending-only category, compute trailing-12-month mean+std of NIS spending (the 12 months strictly before `month`).
- Std floored at ₪50.
- Categories with fewer than 3 prior-month observations are excluded.
- z-score = `(this_month_nis - mean) / std`.
- Return top 3 by `|z_score|`, sorted by z_score desc (most-positive first).

- [ ] **Step 1: Failing test**

```python
def test_compute_categories_vs_typical_z_sort(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_categories_vs_typical
    out = compute_categories_vs_typical(db_session_with_seeded_user, "test", month="2026-03")
    assert len(out) <= 3
    for a, b in zip(out, out[1:]):
        assert abs(a.z_score) >= abs(b.z_score)
    for r in out:
        assert r.typical_std_nis >= 50.0
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
def compute_categories_vs_typical(session: Session, user_id: str, month: str):
    from argosy.api.routes.expenses import CategoryDeviation
    import statistics

    # All spending-only rows grouped by (slug, month).
    rows = session.execute(
        sa_select(
            ExpenseCategory.slug,
            ExpenseCategory.label_en,
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory, ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseCategory.is_inflow.is_(False),
            ExpenseCategory.is_excluded_from_spend.is_(False),
        )
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en, "y", "m")
    ).all()

    # Build {slug: {month_key: total}}.
    by_slug: dict[str, dict[str, float]] = {}
    labels: dict[str, str] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        by_slug.setdefault(r.slug, {})[key] = float(r.nis or 0.0)
        labels[r.slug] = r.label_en

    trailing_keys = [_shift_month_key(month, -i) for i in range(1, 13)]
    out: list[CategoryDeviation] = []
    for slug, monthly in by_slug.items():
        prior = [monthly[k] for k in trailing_keys if k in monthly]
        if len(prior) < 3:
            continue
        cur = monthly.get(month, 0.0)
        mean = sum(prior) / len(prior)
        std = statistics.pstdev(prior) if len(prior) >= 2 else 0.0
        std = max(std, 50.0)
        z = (cur - mean) / std if std > 0 else 0.0
        delta_pct = (cur - mean) / mean if mean > 0 else None
        out.append(CategoryDeviation(
            slug=slug, label=labels[slug], this_month_nis=cur,
            typical_mean_nis=mean, typical_std_nis=std,
            z_score=z, delta_pct=delta_pct,
        ))

    out.sort(key=lambda d: abs(d.z_score), reverse=True)
    return out[:3]
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): categories-vs-typical z-score aggregation"
```

---

### Task 9: Implement `compute_largest_transactions(session, user_id, month, limit=5)`

**Files:**
- Modify: `argosy/services/expense_dashboard.py`
- Test: `tests/test_expense_dashboard.py`

**Behavior contract:**
- Returns up to `limit` `TransactionOut` entries.
- Filter: `direction='debit'` AND spending-only filter AND `tx.occurred_on` in the focal month.
- Order: by `ABS(amount_nis)` desc, ties broken by `occurred_on` desc.
- Use the same row→`TransactionOut` mapping as the existing `transactions` endpoint to avoid divergence (consider extracting the mapping helper if not already extracted).

- [ ] **Step 1: Failing test**

```python
def test_compute_largest_transactions_top5_sorted(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_largest_transactions
    txs = compute_largest_transactions(db_session_with_seeded_user, "test", month="2026-03", limit=5)
    assert len(txs) <= 5
    for a, b in zip(txs, txs[1:]):
        assert abs(a.amount_nis or 0) >= abs(b.amount_nis or 0)
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
def compute_largest_transactions(
    session: Session, user_id: str, month: str, limit: int = 5
):
    """Top `limit` transactions by |amount_nis| in the focal month, debits + spending-only."""
    from argosy.api.routes.expenses import TransactionOut, _row_to_transaction_out

    y, m = int(month[:4]), int(month[5:7])
    # First/last day of month
    first = date(y, m, 1)
    if m == 12:
        last_excl = date(y + 1, 1, 1)
    else:
        last_excl = date(y, m + 1, 1)

    rows = session.execute(
        sa_select(ExpenseTransaction)
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= first,
            ExpenseTransaction.occurred_on < last_excl,
            (ExpenseCategory.is_inflow.is_(False) | ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .order_by(func.abs(ExpenseTransaction.amount_nis).desc(),
                  ExpenseTransaction.occurred_on.desc())
        .limit(limit)
    ).scalars().all()

    return [_row_to_transaction_out(r) for r in rows]
```

(If `_row_to_transaction_out` doesn't exist in `argosy/api/routes/expenses.py`, extract the row→`TransactionOut` logic from the `/transactions` endpoint into a small helper there and call it.)

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add argosy/services/expense_dashboard.py argosy/api/routes/expenses.py tests/test_expense_dashboard.py
git commit -m "feat(expense-dashboard): largest-transactions per month aggregation"
```

---

## Phase 3 — Reshape `/dashboard-overview` endpoint

### Task 10: Update `DashboardOverview` schema (drop monthly fields, add new yearly fields)

**Files:**
- Modify: `argosy/api/routes/expenses.py:573` (the `DashboardOverview` class)
- Modify: `argosy/api/routes/expenses.py` `DividendsSummary` and `TaxesSummary` (add trend_12mo)
- Test: `tests/test_dashboard_overview_endpoint.py` and `tests/test_expense_dashboard.py`

- [ ] **Step 1: Update `DividendsSummary` and `TaxesSummary` schemas to add `trend_12mo: list[dict]`** (or reuse `MonthlyTotalEntry` shape — define a small `TrendPoint` model if cleaner).

```python
class TrendPoint(BaseModel):
    month: str
    total_nis: float = 0.0
    total_usd: float = 0.0


# Add field to DividendsSummary:
    trend_12mo: list[TrendPoint] = Field(default_factory=list)

# Add field to TaxesSummary:
    trend_12mo: list[TrendPoint] = Field(default_factory=list)
```

- [ ] **Step 2: Reshape `DashboardOverview`**

Replace the existing `DashboardOverview` class with:

```python
class DashboardOverview(BaseModel):
    """Yearly tab payload — 'how is the year going?'.

    Removed in EX6 (now lives on /dashboard-monthly):
      current_month, current_month_*, current_month_top_categories,
      current_month_income, current_month_inflow,
      top_merchants_current_month, anomalies.
    """

    months: list[MonthlyTotalEntry]                    # trailing-N totals (chart)
    yearly_summary: YearlySummary
    savings_rate_trend: list[SavingsRatePoint] = Field(default_factory=list)
    top_movers: TopMovers = Field(default_factory=lambda: TopMovers(grew=[], shrank=[]))
    currency_mix: list[CurrencyMixPoint] = Field(default_factory=list)
    dividends: DividendsSummary | None = None
    taxes: TaxesSummary | None = None
    sources_health: list[SourceHealthEntry]
    fx_mode: str
```

- [ ] **Step 3: Run existing test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_dashboard_overview_endpoint.py -v`
Expected: **MOST tests fail** because the endpoint hasn't been updated yet to populate the new fields and stop populating the old ones. That's OK — Task 11 fixes them.

- [ ] **Step 4: DO NOT COMMIT YET** — reshape endpoint in Task 11; commit at end of Phase 3.

---

### Task 11: Update `dashboard_overview()` endpoint to populate new shape

**Files:**
- Modify: `argosy/api/routes/expenses.py:642` (the `dashboard_overview` route handler)
- Modify: `tests/test_dashboard_overview_endpoint.py` — update assertions for new shape.

- [ ] **Step 1: Update the handler signature** — drop `month` query param; the endpoint no longer scopes to a focal month.

```python
@router.get("/dashboard-overview", response_model=DashboardOverview)
def dashboard_overview(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    months: int = Query(default=12, ge=1, le=60),
    fx: str = Query(default="per_currency", pattern="^(per_currency|nis)$"),
    window: str = Query(
        default="trailing_12",
        pattern="^(trailing_12|calendar_year)$",
    ),
) -> DashboardOverview:
    ...
```

- [ ] **Step 2: Inside the handler**:
  - Keep the existing computation for `months` (chart series), `yearly_summary`, `sources_health`.
  - **Remove** all `current_month_*`, `current_month_top_categories`, `current_month_income`, `current_month_inflow`, `top_merchants_current_month`, `anomalies` computation.
  - Call new helpers from `argosy.services.expense_dashboard`: `compute_savings_rate_trend`, `compute_top_movers`, `compute_currency_mix`.
  - For `dividends.trend_12mo` and `taxes.trend_12mo`: extract a small helper inside `expense_dashboard.py` that builds a 12-month trend list from the existing dividends/taxes computation. The simplest thing is to add `compute_dividends_trend_12mo(session, user_id)` and `compute_taxes_trend_12mo(session, user_id)` that return `list[TrendPoint]`. Implement them as straightforward group-by-month versions of the existing inline logic.
  - Return the new shape.

- [ ] **Step 3: Update `tests/test_dashboard_overview_endpoint.py`** — every assertion that touched `current_month_*` or `top_merchants_current_month` or `anomalies` needs to be deleted or moved to the Task 13 monthly-endpoint test. Add new assertions for `savings_rate_trend`, `top_movers`, `currency_mix`, `dividends.trend_12mo`, `taxes.trend_12mo`.

- [ ] **Step 4: Run existing-suite tests**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_dashboard_overview_endpoint.py tests/test_expense_dashboard.py -v`
Expected: PASS

- [ ] **Step 5: Run full backend suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q`
Expected: PASS (1,020+ tests). If anything else fails, those tests were depending on now-removed fields — update them. Most likely candidates: any test that decoded the dashboard-overview response and checked `current_month_*` keys.

- [ ] **Step 6: Commit**

```bash
git add argosy/api/routes/expenses.py argosy/services/expense_dashboard.py tests/test_dashboard_overview_endpoint.py tests/test_expense_dashboard.py
git commit -m "refactor(expenses-api): reshape /dashboard-overview to yearly-only payload"
```

---

## Phase 4 — Add `/dashboard-monthly` endpoint

### Task 12: Implement `dashboard_monthly()` route handler

**Files:**
- Modify: `argosy/api/routes/expenses.py` — add the route just below `dashboard_overview`.

- [ ] **Step 1: Failing test (in next task) — for now, just add the route.**

- [ ] **Step 2: Implementation**

```python
@router.get("/dashboard-monthly", response_model=DashboardMonthly)
def dashboard_monthly(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    month: str = Query(
        ...,
        pattern=r"^\d{4}-\d{2}$",
        description="Focal month, 'YYYY-MM'.",
    ),
    fx: str = Query(default="per_currency", pattern="^(per_currency|nis)$"),
) -> DashboardMonthly:
    """Per-month detail bundle.

    All aggregations scoped to the focal month, with hero-stat MoM + trailing-12
    deltas and a 12-bar sliding chart window per the A-rule (spec §5.3).
    """
    from argosy.services.expense_dashboard import (
        compute_chart_window,
        compute_hero_stats_monthly,
        compute_categories_vs_typical,
        compute_largest_transactions,
    )

    # available_months: distinct YYYY-MM strings from tx_occurred, asc.
    rows = db.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
        )
        .where(ExpenseTransaction.user_id == user_id)
        .group_by("y", "m")
        .order_by("y", "m")
    ).all()
    available = [f"{int(r.y):04d}-{int(r.m):02d}" for r in rows]

    chart_window = compute_chart_window(db, user_id, focal_month=month)
    hero_stats = compute_hero_stats_monthly(db, user_id, month=month)
    categories_vs_typical = compute_categories_vs_typical(db, user_id, month=month)
    largest_transactions = compute_largest_transactions(db, user_id, month=month, limit=5)

    # Reuse existing helpers for top_categories, top_merchants, anomalies.
    # These exist in dashboard_overview today; extract them into private helpers
    # in this module if they aren't already.
    top_categories = _dashboard_top_categories_for_month(db, user_id, month)
    top_merchants = _dashboard_top_merchants_for_month(db, user_id, month)
    anomalies = _dashboard_anomalies_for_month(db, user_id, month)

    return DashboardMonthly(
        month=month,
        available_months=available,
        chart_window=chart_window,
        hero_stats=hero_stats,
        top_categories=top_categories,
        categories_vs_typical=categories_vs_typical,
        top_merchants=top_merchants,
        largest_transactions=largest_transactions,
        anomalies=anomalies,
        fx_mode=fx,
    )
```

You'll need to extract the existing `current_month_top_categories`, `top_merchants_current_month`, and `anomalies` computations from the old `dashboard_overview` body (which Task 11 just removed) into named helpers — `_dashboard_top_categories_for_month`, `_dashboard_top_merchants_for_month`, `_dashboard_anomalies_for_month`. Place them in `argosy/api/routes/expenses.py` (private to the module) or in `expense_dashboard.py` if cleaner.

- [ ] **Step 3: Sanity-check route**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -c "from argosy.api.main import create_app; app = create_app(); print([r.path for r in app.routes if 'dashboard' in r.path])"`
Expected output should include both `/api/expenses/dashboard-overview` and `/api/expenses/dashboard-monthly`.

- [ ] **Step 4: Commit (held until Task 13 adds the test)**

---

### Task 13: API tests for `/dashboard-monthly`

**Files:**
- Create: `tests/test_dashboard_monthly_endpoint.py`

- [ ] **Step 1: Write the test**

```python
"""Tests for GET /api/expenses/dashboard-monthly."""

from fastapi.testclient import TestClient


def test_dashboard_monthly_basic_shape(client_with_seeded_data):
    client, user_id = client_with_seeded_data
    r = client.get(f"/api/expenses/dashboard-monthly?user_id={user_id}&month=2026-03")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["month"] == "2026-03"
    assert isinstance(body["available_months"], list)
    assert len(body["chart_window"]) == 12
    assert any(b["is_selected"] for b in body["chart_window"])
    assert "spent" in body["hero_stats"]
    assert "income" in body["hero_stats"]
    assert "refunds" in body["hero_stats"]
    assert isinstance(body["top_categories"], list)
    assert isinstance(body["categories_vs_typical"], list)
    assert len(body["categories_vs_typical"]) <= 3
    assert isinstance(body["largest_transactions"], list)
    assert len(body["largest_transactions"]) <= 5
    assert isinstance(body["anomalies"], list)


def test_dashboard_monthly_padding_for_short_history(client_with_short_history):
    client, user_id = client_with_short_history
    r = client.get(f"/api/expenses/dashboard-monthly?user_id={user_id}&month=2026-03")
    assert r.status_code == 200
    body = r.json()
    pad_count = sum(1 for b in body["chart_window"] if b["is_padding"])
    assert pad_count > 0
    assert len(body["chart_window"]) == 12


def test_dashboard_monthly_missing_month_param(client_with_seeded_data):
    client, user_id = client_with_seeded_data
    r = client.get(f"/api/expenses/dashboard-monthly?user_id={user_id}")
    assert r.status_code == 422  # FastAPI validation error
```

(Add fixtures `client_with_seeded_data` and `client_with_short_history` to `tests/conftest.py` if not already present. They should yield `(TestClient, user_id)` tuples.)

- [ ] **Step 2: Run, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_dashboard_monthly_endpoint.py -v`

- [ ] **Step 3: Run full backend suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_dashboard_monthly_endpoint.py tests/conftest.py
git commit -m "feat(expenses-api): GET /dashboard-monthly endpoint"
```

---

## Phase 5 — Frontend types and API client

### Task 14: Update `ui/src/lib/expenses/api.ts` types and add `dashboardMonthly`

**Files:**
- Modify: `ui/src/lib/expenses/api.ts`

- [ ] **Step 1: Update `DashboardOverview` interface** to drop monthly fields and add new ones:

```typescript
export interface SavingsRatePoint {
  month: string;
  income_nis: number;
  spending_nis: number;
  savings_rate: number;
}

export interface CategoryDelta {
  slug: string;
  label: string;
  current_nis: number;
  prior_nis: number;
  delta_nis: number;
  delta_pct: number | null;
}

export interface TopMovers {
  grew: CategoryDelta[];
  shrank: CategoryDelta[];
  reason: string | null;
}

export interface CurrencyMixPoint {
  month: string;
  nis: number;
  usd: number;
}

export interface TrendPoint {
  month: string;
  total_nis: number;
  total_usd: number;
}

export interface DividendsSummary {
  // existing fields …
  trend_12mo: TrendPoint[];
}

export interface TaxesSummary {
  // existing fields …
  trend_12mo: TrendPoint[];
}

export interface DashboardOverview {
  months: MonthlyTotalEntry[];
  yearly_summary: YearlySummary;
  savings_rate_trend: SavingsRatePoint[];
  top_movers: TopMovers;
  currency_mix: CurrencyMixPoint[];
  dividends: DividendsSummary | null;
  taxes: TaxesSummary | null;
  sources_health: SourceHealthEntry[];
  fx_mode: string;
}
```

Add new types and the new client method:

```typescript
export interface ChartWindowBar {
  month: string;
  total_nis: number;
  total_usd: number;
  is_padding: boolean;
  is_selected: boolean;
}

export interface HeroMetric {
  value_nis: number;
  mom_delta_pct: number | null;
  vs_trailing12_pct: number | null;
}

export interface HeroStatsMonthly {
  spent: HeroMetric;
  income: HeroMetric;
  refunds: HeroMetric;
  statements_reconciled: number;
  anomalies_count: number;
}

export interface CategoryDeviation {
  slug: string;
  label: string;
  this_month_nis: number;
  typical_mean_nis: number;
  typical_std_nis: number;
  z_score: number;
  delta_pct: number | null;
}

export interface DashboardMonthly {
  month: string;
  available_months: string[];
  chart_window: ChartWindowBar[];
  hero_stats: HeroStatsMonthly;
  top_categories: CategorySpend[];
  categories_vs_typical: CategoryDeviation[];
  top_merchants: MerchantSpend[];
  largest_transactions: TransactionOut[];
  anomalies: AnomalyCard[];
  fx_mode: string;
}
```

Update `expensesApi.dashboardOverview` to drop the `month` arg, and add `dashboardMonthly`:

```typescript
  dashboardOverview: (
    userId: string,
    months = 12,
    fx: "per_currency" | "nis" = "per_currency",
    window?: YearlyWindow | null,
  ) => {
    const qs = new URLSearchParams({
      user_id: userId,
      months: String(months),
      fx,
    });
    if (window) qs.set("window", window);
    return getJSON<DashboardOverview>(
      `/api/expenses/dashboard-overview?${qs.toString()}`,
    );
  },
  dashboardMonthly: (
    userId: string,
    month: string,
    fx: "per_currency" | "nis" = "per_currency",
  ) => {
    const qs = new URLSearchParams({ user_id: userId, month, fx });
    return getJSON<DashboardMonthly>(
      `/api/expenses/dashboard-monthly?${qs.toString()}`,
    );
  },
```

- [ ] **Step 2: Run typecheck**

Run: `cd ui ; npm run typecheck`
Expected: TYPE ERRORS in pages/components that reference removed fields. We'll fix those in subsequent tasks. **Do NOT commit yet.**

- [ ] **Step 3: Hold the commit until Phase 6+ stabilizes the UI.**

---

## Phase 6 — New frontend components

Each component lives in `ui/src/components/expenses/` and follows the existing component conventions (reads from props, no internal API calls, plain Tailwind, Recharts where chart-y).

**Important for Phase 6:** Read `ui/AGENTS.md` and `node_modules/next/dist/docs/` snippets before writing UI code — Next.js 15 has API differences from training data. Existing components like `monthly-spend-chart.tsx` and `category-donut.tsx` are good references.

### Task 15: `SavingsRateTrend` component

**Files:**
- Create: `ui/src/components/expenses/savings-rate-trend.tsx`

- [ ] **Step 1: Implementation**

```tsx
"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { SavingsRatePoint } from "@/lib/expenses/api";

interface Props {
  data: SavingsRatePoint[];
}

export function SavingsRateTrend({ data }: Props) {
  if (data.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Savings rate</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not enough data yet.
        </CardContent>
      </Card>
    );
  }
  const series = data.map((p) => ({
    month: p.month,
    rate: Math.round(p.savings_rate * 1000) / 10, // percent
  }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Savings rate <span className="text-muted-foreground text-sm font-normal">(income − spending) / income</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={series}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" tickFormatter={(m: string) => m.slice(2)} />
            <YAxis tickFormatter={(v: number) => `${v}%`} />
            <Tooltip
              formatter={
                ((v: number) => [`${v}%`, "Savings rate"]) as unknown as never
              }
            />
            <Area type="monotone" dataKey="rate" stroke="#16a34a" fill="#16a34a33" />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd ui ; npm run lint -- --max-warnings=999 ; npm run typecheck`
Expected: no errors specific to this component.

- [ ] **Step 3: Commit (held with Task 21 reshaped Overview page)**

---

### Task 16: `TopMoversCard` component

**Files:**
- Create: `ui/src/components/expenses/top-movers-card.tsx`

- [ ] **Step 1: Implementation**

```tsx
"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TopMovers } from "@/lib/expenses/api";

interface Props {
  data: TopMovers;
}

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

function MoverRow({ slug, label, delta_nis, delta_pct }: {
  slug: string; label: string; delta_nis: number; delta_pct: number | null;
}) {
  const positive = delta_nis > 0;
  return (
    <Link
      href={`/expenses/transactions?category=${encodeURIComponent(slug)}`}
      className="flex items-center justify-between gap-2 py-1.5 hover:bg-secondary/40 rounded-sm px-2 -mx-2"
    >
      <span className="text-sm">{label}</span>
      <span className={`text-sm tabular-nums ${positive ? "text-emerald-600" : "text-rose-600"}`}>
        {positive ? "+" : ""}{fmt(delta_nis)}
        {delta_pct !== null && (
          <span className="text-xs text-muted-foreground ml-1.5">
            ({Math.round(delta_pct * 100)}%)
          </span>
        )}
      </span>
    </Link>
  );
}

export function TopMoversCard({ data }: Props) {
  if (data.reason === "insufficient_history") {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Top movers</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Need at least 12 months of data to compare.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Top movers <span className="text-muted-foreground text-sm font-normal">last 6 months vs prior 6</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <div className="text-xs uppercase text-muted-foreground mb-1">Grew</div>
            {data.grew.length === 0 ? (
              <div className="text-xs text-muted-foreground">None.</div>
            ) : data.grew.map((d) => <MoverRow key={d.slug} {...d} />)}
          </div>
          <div>
            <div className="text-xs uppercase text-muted-foreground mb-1">Shrank</div>
            {data.shrank.length === 0 ? (
              <div className="text-xs text-muted-foreground">None.</div>
            ) : data.shrank.map((d) => <MoverRow key={d.slug} {...d} />)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 17: `CurrencyMixCard` component

**Files:**
- Create: `ui/src/components/expenses/currency-mix-card.tsx`

- [ ] **Step 1: Implementation**

```tsx
"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CurrencyMixPoint } from "@/lib/expenses/api";

interface Props {
  data: CurrencyMixPoint[];
}

export function CurrencyMixCard({ data }: Props) {
  if (data.length === 0) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Currency mix</CardTitle></CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No data.
        </CardContent>
      </Card>
    );
  }
  const series = data.map((p) => ({
    month: p.month.slice(2),
    NIS: Math.round(p.nis),
    USD: Math.round(p.usd),
  }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Currency mix <span className="text-muted-foreground text-sm font-normal">trailing 12 months</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" />
            <YAxis />
            <Tooltip />
            <Legend />
            <Bar dataKey="NIS" stackId="a" fill="#3b82f6" />
            <Bar dataKey="USD" stackId="a" fill="#a855f7" />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 18: `CategoriesVsTypicalCard` component

**Files:**
- Create: `ui/src/components/expenses/categories-vs-typical-card.tsx`

- [ ] **Step 1: Implementation**

```tsx
"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CategoryDeviation } from "@/lib/expenses/api";

interface Props {
  data: CategoryDeviation[];
  month: string | null;
}

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

export function CategoriesVsTypicalCard({ data, month }: Props) {
  if (data.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Categories vs typical</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not enough trailing-12 history yet.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Categories vs typical{" "}
          <span className="text-muted-foreground text-sm font-normal">vs your trailing-12 baseline</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {data.map((d) => {
          const over = d.this_month_nis > d.typical_mean_nis;
          return (
            <Link
              key={d.slug}
              href={`/expenses/transactions?category=${encodeURIComponent(d.slug)}${month ? `&month=${month}` : ""}`}
              className="flex flex-col gap-0.5 hover:bg-secondary/40 px-2 -mx-2 py-1.5 rounded-sm"
            >
              <div className="flex items-center justify-between text-sm">
                <span>{d.label}</span>
                <span className={`tabular-nums ${over ? "text-rose-600" : "text-emerald-600"}`}>
                  {fmt(d.this_month_nis)}
                  {d.delta_pct !== null && (
                    <span className="text-xs ml-1.5">
                      ({over ? "+" : ""}{Math.round(d.delta_pct * 100)}%)
                    </span>
                  )}
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                Usually {fmt(d.typical_mean_nis)} — z = {d.z_score.toFixed(1)}
              </div>
            </Link>
          );
        })}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 19: `LargestTransactionsCard` component

**Files:**
- Create: `ui/src/components/expenses/largest-transactions-card.tsx`

- [ ] **Step 1: Implementation**

```tsx
"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TransactionOut } from "@/lib/expenses/api";

interface Props {
  transactions: TransactionOut[];
  month: string | null;
}

function fmt(amt: number | null) {
  if (amt === null) return "—";
  return `₪${Math.round(Math.abs(amt)).toLocaleString("en-IL")}`;
}

export function LargestTransactionsCard({ transactions, month }: Props) {
  if (transactions.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Largest transactions</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No spending transactions in this month.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Largest transactions{" "}
          {month && (
            <span className="text-muted-foreground text-sm font-normal">— {month}</span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col">
        {transactions.map((t) => (
          <Link
            key={t.id}
            href={`/expenses/transactions?search=${encodeURIComponent(t.merchant_raw)}`}
            className="flex items-center justify-between text-sm py-1.5 px-2 -mx-2 hover:bg-secondary/40 rounded-sm"
          >
            <span className="truncate max-w-[60%]">{t.merchant_raw}</span>
            <span className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground">{t.occurred_on}</span>
              <span className="tabular-nums">{fmt(t.amount_nis)}</span>
            </span>
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

## Phase 7 — Modified frontend components

### Task 20: Update `HeroStats` for two modes (`yearly` + `monthly+MoM`)

**Files:**
- Modify: `ui/src/components/expenses/hero-stats.tsx`

- [ ] **Step 1: Read the existing file** to see how it currently builds stat tiles. The existing component reads `current_month_*` fields from `DashboardOverview`. We're adding two modes.

- [ ] **Step 2: Refactor to accept either an `overview` (yearly mode) or `monthly` (with MoM deltas) shape**

```tsx
"use client";

import type { DashboardOverview, DashboardMonthly } from "@/lib/expenses/api";

type YearlyProps = {
  mode: "yearly";
  overview: DashboardOverview;
};
type MonthlyProps = {
  mode: "monthly";
  data: DashboardMonthly;
};
type Props = YearlyProps | MonthlyProps;

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

function pct(n: number | null) {
  if (n === null) return null;
  const sign = n >= 0 ? "+" : "";
  return `${sign}${Math.round(n * 100)}%`;
}

function DeltaPill({ delta }: { delta: number | null }) {
  if (delta === null) return <span className="text-xs text-muted-foreground">—</span>;
  const positive = delta >= 0;
  return (
    <span
      className={`text-xs tabular-nums px-1.5 py-0.5 rounded-sm ${
        positive ? "text-emerald-700 bg-emerald-100" : "text-rose-700 bg-rose-100"
      }`}
    >
      {pct(delta)}
    </span>
  );
}

function StatTile({ label, value, mom, vs12 }: {
  label: string; value: string; mom?: number | null; vs12?: number | null;
}) {
  return (
    <div className="rounded-md border border-border p-3 flex flex-col gap-1">
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {(mom !== undefined || vs12 !== undefined) && (
        <div className="flex items-center gap-2 mt-1">
          {mom !== undefined && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              vs prior <DeltaPill delta={mom} />
            </span>
          )}
          {vs12 !== undefined && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              vs avg <DeltaPill delta={vs12} />
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function HeroStats(props: Props) {
  if (props.mode === "yearly") {
    const y = props.overview.yearly_summary;
    return (
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <StatTile label="Spent (yr)" value={fmt(y.yearly_spending_total_nis)} />
        <StatTile label="Income (yr)" value={fmt(y.yearly_income_total_nis)} />
        <StatTile label="Refunds (yr)" value={fmt(y.yearly_refunds_total_nis)} />
        <StatTile label="Avg/mo" value={fmt(y.avg_per_month_nis)} />
        <StatTile
          label="Sources"
          value={String(props.overview.sources_health.length)}
        />
      </div>
    );
  }
  const h = props.data.hero_stats;
  return (
    <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
      <StatTile
        label="Spent"
        value={fmt(h.spent.value_nis)}
        mom={h.spent.mom_delta_pct}
        vs12={h.spent.vs_trailing12_pct}
      />
      <StatTile
        label="Income"
        value={fmt(h.income.value_nis)}
        mom={h.income.mom_delta_pct}
        vs12={h.income.vs_trailing12_pct}
      />
      <StatTile
        label="Refunds"
        value={fmt(h.refunds.value_nis)}
        mom={h.refunds.mom_delta_pct}
        vs12={h.refunds.vs_trailing12_pct}
      />
      <StatTile label="Reconciled" value={String(h.statements_reconciled)} />
      <StatTile label="Anomalies" value={String(h.anomalies_count)} />
    </div>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 21: Update `MonthlySpendChart` for `mode: 'small' | 'focal'`

**Files:**
- Modify: `ui/src/components/expenses/monthly-spend-chart.tsx`

- [ ] **Step 1: Read the existing file.** It currently takes `data, fxMode, selectedMonth, onMonthSelected`. We add `mode`. In `small` mode it accepts `data: MonthlyTotalEntry[]` (12 trailing) and click navigates with `useRouter().push(...)`. In `focal` mode it accepts `chartWindow: ChartWindowBar[]` (12 bars per A-rule), highlights `is_selected`, fades `is_padding`, and calls `onMonthSelected` for in-place rescope.

- [ ] **Step 2: Update prop types and renderer**

```tsx
"use client";

import { useRouter } from "next/navigation";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  ChartWindowBar,
  MonthlyTotalEntry,
} from "@/lib/expenses/api";

type SmallProps = {
  mode: "small";
  data: MonthlyTotalEntry[];
};
type FocalProps = {
  mode: "focal";
  chartWindow: ChartWindowBar[];
  onMonthSelected: (month: string) => void;
};
type Props = SmallProps | FocalProps;

function totalNisOf(entry: MonthlyTotalEntry): number {
  // Spending series — entry.totals_by_currency may have 'NIS' and 'USD' keys.
  // For the small read-only chart we show NIS totals only.
  return entry.totals_by_currency?.NIS ?? 0;
}

export function MonthlySpendChart(props: Props) {
  const router = useRouter();

  if (props.mode === "small") {
    const series = props.data.map((e) => ({
      month: e.month.slice(2),
      key: e.month,
      total: Math.round(totalNisOf(e)),
    }));
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Monthly spend{" "}
            <span className="text-muted-foreground text-sm font-normal">click a bar to drill in</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="h-32">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={series}
              onClick={(e) => {
                const datum = e?.activePayload?.[0]?.payload as { key?: string };
                if (datum?.key)
                  router.push(`/expenses/monthly?month=${datum.key}`);
              }}
            >
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="month" />
              <YAxis hide />
              <Tooltip />
              <Bar dataKey="total" fill="#3b82f6" cursor="pointer" />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    );
  }

  // focal
  const series = props.chartWindow.map((b) => ({
    month: b.month.slice(2),
    key: b.month,
    total: Math.round(b.total_nis + b.total_usd * 3.7), // crude: USD shown alongside as approx. NIS-equivalent
    is_padding: b.is_padding,
    is_selected: b.is_selected,
  }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Monthly spend <span className="text-muted-foreground text-sm font-normal">±6 months</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={series}
            onClick={(e) => {
              const datum = e?.activePayload?.[0]?.payload as { key?: string; is_padding?: boolean };
              if (datum?.key && !datum.is_padding) props.onMonthSelected(datum.key);
            }}
          >
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" />
            <YAxis />
            <Tooltip />
            <Bar dataKey="total" cursor="pointer">
              {series.map((d, i) => (
                <Cell
                  key={i}
                  fill={
                    d.is_padding ? "#cbd5e1" :
                    d.is_selected ? "#1d4ed8" :
                    "#3b82f6"
                  }
                  fillOpacity={d.is_padding ? 0.3 : 1}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
```

(Note: the rough USD→NIS multiplication is a placeholder for visual integration only — server-side FX is still TBD per spec §10. In `fx_mode == 'nis'` USD is already 0, so this is a no-op for that mode.)

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 22: Augment `DividendsCard` and `TaxesCard` with inline 12-bar mini-charts

**Files:**
- Modify: `ui/src/components/expenses/dividends-card.tsx`
- Modify: `ui/src/components/expenses/taxes-card.tsx`

- [ ] **Step 1: Read the existing files.** They render single-number summaries today.

- [ ] **Step 2: For each, accept `trend12mo: TrendPoint[]` from props (default `[]`) and render an inline 60×24 SVG sparkline below the headline number when present**

Example for `dividends-card.tsx` (apply analogous changes to `taxes-card.tsx`):

```tsx
import type { DividendsSummary } from "@/lib/expenses/api";

interface Props {
  data: DividendsSummary;
}

function MiniBars({ values, height = 28 }: { values: number[]; height?: number }) {
  const max = Math.max(1, ...values);
  return (
    <svg width={values.length * 6} height={height} className="block">
      {values.map((v, i) => {
        const h = Math.max(1, Math.round((v / max) * (height - 4)));
        return (
          <rect
            key={i}
            x={i * 6}
            y={height - h - 2}
            width={4}
            height={h}
            fill="currentColor"
            className="text-emerald-600"
          />
        );
      })}
    </svg>
  );
}

export function DividendsCard({ data }: Props) {
  const trend = (data.trend_12mo ?? []).map((p) => p.total_usd);
  // ... existing JSX ...
  // Below the headline total, render:
  // {trend.length > 0 && <MiniBars values={trend} />}
}
```

(The existing card layout's inner JSX should be left intact; just add the `MiniBars` block.)

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

## Phase 8 — Routes and pages

### Task 23: Update `ui/src/app/expenses/layout.tsx` (add Monthly tab)

**Files:**
- Modify: `ui/src/app/expenses/layout.tsx`

- [ ] **Step 1: Update TABS array**

```tsx
const TABS = [
  { href: "/expenses", label: "Overview" },
  { href: "/expenses/monthly", label: "Monthly" },
  { href: "/expenses/transactions", label: "Transactions" },
  { href: "/expenses/sources", label: "Sources" },
  { href: "/expenses/trips", label: "Trips" },
  { href: "/expenses/rsu", label: "RSU" },
];
```

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 24: Reshape `/expenses` (Overview) page

**Files:**
- Modify: `ui/src/app/expenses/page.tsx`

- [ ] **Step 1: Replace the existing implementation**

```tsx
"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { CategoriesVsTypicalCard } from "@/components/expenses/categories-vs-typical-card";
import { CurrencyMixCard } from "@/components/expenses/currency-mix-card";
import { DividendsCard } from "@/components/expenses/dividends-card";
import { HeroStats } from "@/components/expenses/hero-stats";
import { LargestTransactionsCard } from "@/components/expenses/largest-transactions-card";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { SavingsRateTrend } from "@/components/expenses/savings-rate-trend";
import { SourcesHealthTable } from "@/components/expenses/sources-health-table";
import { TaxesCard } from "@/components/expenses/taxes-card";
import { TopMoversCard } from "@/components/expenses/top-movers-card";
import { YearlySummaryCard } from "@/components/expenses/yearly-summary-card";
import { Card, CardContent } from "@/components/ui/card";
import {
  expensesApi,
  type DashboardOverview,
  type YearlyWindow,
} from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

function ExpensesOverviewInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();
  const windowParam = params.get("window");
  const selectedWindow: YearlyWindow =
    windowParam === "calendar_year" ? "calendar_year" : "trailing_12";

  useEffect(() => {
    setLoading(true);
    expensesApi
      .dashboardOverview(USER_ID, 12, fxMode, selectedWindow)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode, selectedWindow]);

  function setWindow(w: YearlyWindow) {
    const next = new URLSearchParams(params.toString());
    if (w === "trailing_12") next.delete("window");
    else next.set("window", w);
    const qs = next.toString();
    router.replace(qs ? `/expenses?${qs}` : "/expenses");
  }

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
      <HeroStats mode="yearly" overview={data} />
      <YearlySummaryCard data={data.yearly_summary} onWindowChange={setWindow} />
      <SavingsRateTrend data={data.savings_rate_trend} />
      <TopMoversCard data={data.top_movers} />
      <CurrencyMixCard data={data.currency_mix} />
      {(data.dividends || data.taxes) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.dividends && <DividendsCard data={data.dividends} />}
          {data.taxes && <TaxesCard data={data.taxes} />}
        </div>
      )}
      <MonthlySpendChart mode="small" data={data.months} />
      <SourcesHealthTable data={data.sources_health} />
    </div>
  );
}

export default function ExpensesOverviewPage() {
  return (
    <Suspense fallback={
      <Card><CardContent className="py-8 text-center text-muted-foreground text-sm">Loading…</CardContent></Card>
    }>
      <ExpensesOverviewInner />
    </Suspense>
  );
}
```

(Note that we still import `CategoriesVsTypicalCard` and `LargestTransactionsCard` even though they're not yet rendered on Overview — this is a holdover; remove them. Actually: only import what's used. So drop those two imports here — they go on Monthly only.)

Re-check imports — keep only: `CurrencyMixCard`, `DividendsCard`, `HeroStats`, `MonthlySpendChart`, `SavingsRateTrend`, `SourcesHealthTable`, `TaxesCard`, `TopMoversCard`, `YearlySummaryCard`, plus `Card`/`CardContent` and the API client.

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 25: Create `/expenses/monthly/page.tsx`

**Files:**
- Create: `ui/src/app/expenses/monthly/page.tsx`

- [ ] **Step 1: Write the page**

```tsx
"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { AnomalyHighlights } from "@/components/expenses/anomaly-highlights";
import { CategoriesVsTypicalCard } from "@/components/expenses/categories-vs-typical-card";
import { CategoryDonut } from "@/components/expenses/category-donut";
import { HeroStats } from "@/components/expenses/hero-stats";
import { LargestTransactionsCard } from "@/components/expenses/largest-transactions-card";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { MonthPicker } from "@/components/expenses/month-picker";
import { TopMerchantsCard } from "@/components/expenses/top-merchants-card";
import { Card, CardContent } from "@/components/ui/card";
import { expensesApi, type DashboardMonthly } from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

function ExpensesMonthlyInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<DashboardMonthly | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();

  const monthParam = params.get("month");
  // null until first fetch resolves; then auto-select latest available.
  const selectedMonth = monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : null;

  useEffect(() => {
    if (!selectedMonth) {
      // Fetch a "latest" month: hit dashboard-overview just for available_months.
      // Cheaper: hit dashboard-monthly with a guess (today) and adjust if 422 hits.
      // Pragmatic: fetch latest by navigating to overview's first available month.
      // We'll fall back to fetching with today's month string and seeing what comes back.
      const today = new Date();
      const ym = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`;
      expensesApi.dashboardMonthly(USER_ID, ym, fxMode)
        .then((d) => {
          // available_months last entry is the actual latest
          const latest = d.available_months[d.available_months.length - 1];
          if (latest && latest !== ym) {
            router.replace(`/expenses/monthly?month=${latest}`);
            return;
          }
          setData(d);
        })
        .catch((e: unknown) => setError(String(e)))
        .finally(() => setLoading(false));
      return;
    }
    setLoading(true);
    expensesApi.dashboardMonthly(USER_ID, selectedMonth, fxMode)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode, selectedMonth, router]);

  function setMonth(m: string) {
    const next = new URLSearchParams(params.toString());
    next.set("month", m);
    router.replace(`/expenses/monthly?${next.toString()}`);
  }

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
          Loading…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <MonthPicker
          months={data.available_months.map((m) => ({
            month: m,
            totals_by_currency: {},
            transaction_count: 0,
          }))}
          value={data.month}
          onChange={(m) => m && setMonth(m)}
        />
      </div>
      <HeroStats mode="monthly" data={data} />
      <MonthlySpendChart
        mode="focal"
        chartWindow={data.chart_window}
        onMonthSelected={setMonth}
      />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CategoryDonut data={data.top_categories} month={data.month} />
        <CategoriesVsTypicalCard data={data.categories_vs_typical} month={data.month} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopMerchantsCard data={data.top_merchants} month={data.month} />
        <LargestTransactionsCard transactions={data.largest_transactions} month={data.month} />
      </div>
      <AnomalyHighlights anomalies={data.anomalies} />
    </div>
  );
}

export default function ExpensesMonthlyPage() {
  return (
    <Suspense fallback={
      <Card><CardContent className="py-8 text-center text-muted-foreground text-sm">Loading…</CardContent></Card>
    }>
      <ExpensesMonthlyInner />
    </Suspense>
  );
}
```

(Note: `MonthPicker` currently expects a `MonthlyTotalEntry[]`; we adapt by mapping the string list. If `MonthPicker`'s shape is too rigid, accept a `string[]` + `value` instead — adapt the component to accept either.)

- [ ] **Step 2: Lint + typecheck**

- [ ] **Step 3: Commit held**

---

### Task 26: Sweep — make sure no other UI consumer relies on removed dashboardOverview fields

**Files:**
- Search via Grep: any `current_month_*` / `top_merchants_current_month` / `current_month_top_categories` / `current_month_inflow` references in `ui/src/`.

- [ ] **Step 1: Grep**

Run via the Grep tool: pattern `current_month_top_categories|top_merchants_current_month|current_month_inflow|current_month_income(?!_breakdown)|current_month_spending|current_month_refunds`, scope `ui/src/`.

- [ ] **Step 2: Update each reference** to either pull from `DashboardMonthly.hero_stats` (if rendered on the new monthly tab) or remove (if rendered on the old overview).

- [ ] **Step 3: Lint + typecheck the entire ui project**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: clean.

- [ ] **Step 4: Big commit — UI rework lands together**

```bash
git add ui/src/
git commit -m "feat(expenses-ui): split Overview into Overview + Monthly tabs"
```

---

## Phase 9 — End-to-end verification

### Task 27: Smoke the dev server

**Files:** none (manual verification step).

- [ ] **Step 1: Restart uvicorn**

The dev DB needs `ARGOSY_EXPENSE_SAMPLES_ROOT` set for any RSU work but not for these endpoints. Start:

```bash
ARGOSY_EXPENSE_SAMPLES_ROOT="D:/Google Drive/Family/Finances/Portfolio/Resources" \
  D:/Projects/financial-advisor/.venv/Scripts/python.exe \
  -m uvicorn argosy.api.main:create_app --factory --host 127.0.0.1 --port 8000
```

(Run in background; the user typically uses `run_in_background=True`.)

- [ ] **Step 2: Hit each endpoint with curl**

```bash
curl -s "http://127.0.0.1:8000/api/expenses/dashboard-overview?user_id=ariel&fx=per_currency" | python -m json.tool | head -50
curl -s "http://127.0.0.1:8000/api/expenses/dashboard-monthly?user_id=ariel&month=2026-04&fx=per_currency" | python -m json.tool | head -50
```

Expected: 200, JSON shapes match the new schemas.

- [ ] **Step 3: Restart Next.js dev server**

```bash
cd ui ; npm run dev
```

- [ ] **Step 4: Eyeball each tab** — visit `http://127.0.0.1:1337/expenses` and `http://127.0.0.1:1337/expenses/monthly`. Confirm both render without console errors and all widgets fetch data.

(Manual smokes deliberately skipped is the official policy, but for a UI-heavy split we do an informal eyeball.)

- [ ] **Step 5: Final full backend suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q`
Expected: PASS.

- [ ] **Step 6: SDD handover update**

Edit `docs/design/SDD.md`:
- Update the "Last updated" cell at the top with today's date (2026-05-10) noting EX6 landed.
- Add a new subsection in §16 "Wave EX6 — Overview/Monthly split" with a 4-bullet summary: routes added, endpoints reshaped, components added, schema changes (none).

Commit:

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): EX6 — overview/monthly split landed"
```

---

## Self-review checklist (post-execution)

Before declaring done, run through this list:

- [ ] All 27 tasks committed; `git log --oneline | head -30` shows the trail.
- [ ] `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q` is green.
- [ ] `cd ui ; npm run lint ; npm run typecheck` is green.
- [ ] `/expenses` renders with: HeroStats(yearly), YearlySummaryCard, SavingsRateTrend, TopMoversCard, CurrencyMixCard, DividendsCard+TaxesCard with mini-charts, MonthlySpendChart(small), SourcesHealthTable.
- [ ] `/expenses/monthly` renders with: MonthPicker, HeroStats(monthly+MoM), MonthlySpendChart(focal sliding), CategoryDonut, CategoriesVsTypicalCard, TopMerchantsCard, LargestTransactionsCard, AnomalyHighlights.
- [ ] Click a bar on Overview's small chart → URL becomes `/expenses/monthly?month=YYYY-MM`.
- [ ] Click a bar on Monthly's focal chart → URL is updated in place to `?month=YYYY-MM`, no navigation.
- [ ] Padding bars in chart_window render faded (when applicable; user has 17+ months so no padding will appear in production for them).
- [ ] SDD §16 lists Wave EX6.
