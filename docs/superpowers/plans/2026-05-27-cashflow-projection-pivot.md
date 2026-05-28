# Cashflow Projection Pivot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Money math is risky — `Task 7` mandates a codex-tandem review of `cashflow_projection.py` BEFORE the route lands. Do not skip it.

**Goal:** Replace the `/plan` portfolio-value-over-time chart with a monthly-cashflow projection that answers two specific questions: (a) when can Ariel safely retire (i.e., when do projected monthly income streams ≥ inflated monthly expenses), and (b) what levers move that crossing earlier. Pension annuity (kupat_pensia + executive_insurance at age 67) and pension lump access (keren_hishtalmut + kupat_gemel at age 60) become first-class overlay lines; retirement-age becomes an interactive slider that controls when contributions stop.

**Architecture:** A new pure-Python service `argosy/services/cashflow_projection.py` extracts pension state from `identity_yaml`, projects per-month balances + portfolio value + expenses, and returns a series of `(months_out, age, portfolio_income_base/bear/bull, pension_annuity_monthly, expenses, surplus_base)` rows. A new route `GET /api/plan/draft/cashflow-projection` serves it. The UI rewrites `projection-chart.tsx` to render `$/mo` lines instead of `$` portfolio value, with checkbox overlays for bear/bull bands, pension annuity, pension lump, expenses, and a slider that re-fetches with a different `retirement_age`. The "retire-ready age" — first month where `portfolio_income_base + pension_annuity ≥ expenses` — is rendered as a vertical reference line. The existing portfolio-value model (lognormal bull/base/bear) is reused; the new layer is the cashflow transformation + pension state machine.

**Tech Stack:** Python 3.12, FastAPI, pydantic, SQLAlchemy 2 (sync session); TypeScript 5, Next.js 16 (Turbopack), recharts 3.8; pytest backend; codex-tandem (Layout A, Codex as reviewer) for the projection math.

---

## Filesystem layout

| Action | Path | Responsibility |
|---|---|---|
| Create | `argosy/services/cashflow_projection.py` | Pure math + data extraction. No I/O beyond SQLAlchemy reads. Returns dataclasses. |
| Create | `tests/test_cashflow_projection.py` | Unit tests of the math (~15-20 tests). |
| Modify | `argosy/api/routes/plan.py` | Add `/draft/cashflow-projection` route + pydantic DTOs. |
| Modify | `tests/test_plan_draft_api.py` | One integration test exercising the route end-to-end on seeded DB. |
| Create | `ui/src/components/plan/cashflow-projection-chart.tsx` | New chart component (cashflow lines, slider, checkboxes). |
| Modify | `ui/src/lib/api.ts` | Add `CashflowProjectionResponse` types + `planDraftCashflowProjection()` function. |
| Modify | `ui/src/app/plan/page.tsx` | Swap `<ProjectionChart>` for `<CashflowProjectionChart>`. Update fetch fan-out. |
| Delete | `ui/src/components/plan/projection-chart.tsx` | Retired — superseded by the new component. |

---

## Domain facts (verified against the current DB, 2026-05-27)

From `identity_yaml` for user `ariel`:

```yaml
date_of_birth: '1982-08-28'  # → current age ~43

pensions_ariel:
  pension_nis: 800147          # kupat_pensia balance
  executive_insurance_nis: 755907
  keren_hishtalmut_nis: 384000  # lump available at 60
  provident_fund_nis: 75000     # lump available at 60
  total_nis: 2015054
  data_date: '2025-12'

pensions:
  kupat_pensia:
    balance_nis: 800147
    contribution_rate_pct: 6.0       # employee
    employer_match_pct: 6.5
  keren_hishtalmut:
    balance_nis: 384000
    contribution_rate_pct: 2.5       # employee
    employer_match_pct: 7.5
  executive_insurance:
    balance_nis: 755907
    # No active contributions modelled (frozen legacy policy)
  kupat_gemel:
    balance_nis: 75000
    # No active contributions modelled
```

Plus (from `clal_pension_*` keys at the top level of `identity_yaml`):

```yaml
clal_pension_salary_basis_monthly_nis: 27101
clal_pension_employee_pct: 6.0
clal_pension_employer_pct: 6.5
clal_pension_severance_pct: 8.33
```

From `household_budget` agent_report (latest):

```json
{ "monthly_burn_nis": 23084, "monthly_income_nis": 54835 }
```

From `portfolio_snapshots` (latest): `fx_usd_nis: 2.94161`.

From `goals_yaml`:
- `retirement_target_year: '2031'` (≈ age 49) — used as default `retirement_age` for the slider.
- `retirement_drawdown_style: capital_preservation_returns_only` — drives the "real return × portfolio / 12" cashflow line.
- `target_annual_income: '360,000 NIS'` (reference; not directly used in the chart yet).

---

## Math model

All amounts internally in NIS to avoid FX drift; converted to USD at the response boundary using the snapshot `fx_usd_nis`.

### Constants (defaults; surfaced in `assumptions`)

| Symbol | Value | Meaning |
|---|---|---|
| `mu_nominal_annual` | 0.08 | S&P 500-like nominal portfolio drift |
| `sigma_annual` | 0.18 | Portfolio volatility |
| `inflation_annual` | 0.025 | Used for expense inflation AND for converting nominal→real return |
| `real_return_annual` | 0.055 | `mu_nominal − inflation` |
| `mekadem` | 200 | Annuity factor for kupat_pensia + executive_insurance at age 67 |
| `LUMP_PENSION_AGE` | 60 | Israeli statute: keren_hishtalmut + kupat_gemel withdrawable |
| `ANNUITY_AGE` | 67 | Israeli statute: kupat_pensia + executive_insurance start paying |

### Per-month state machine (t = 0 … horizon_months)

```
age_t = current_age + t/12

# Portfolio (existing lognormal math, untouched)
drift = mu_nominal - 0.5*sigma_annual**2
portfolio_base_nominal[t] = today_value_nis * exp(drift*t/12)
portfolio_bull_nominal[t] = portfolio_base_nominal[t] * exp(+sigma_annual*sqrt(t/12))
portfolio_bear_nominal[t] = portfolio_base_nominal[t] * exp(-sigma_annual*sqrt(t/12))

# Pension state — kupat_pensia
monthly_contribution_pensia = clal_pension_salary_basis_monthly_nis * (
    employee_pct + employer_match_pct + severance_pct
) / 100  # ≈ 5,646 NIS/mo at the seeded values

if age_t < retirement_age:
    pensia_balance[t] = pensia_balance[t-1] * (1 + real_return/12) + monthly_contribution_pensia
elif retirement_age <= age_t < ANNUITY_AGE:
    pensia_balance[t] = pensia_balance[t-1] * (1 + real_return/12)
else:  # age_t >= ANNUITY_AGE
    pensia_balance[t] = pensia_balance[t-1]  # frozen at start of annuity

# Pension state — keren_hishtalmut (analogous, but available at 60 as a lump)
monthly_contribution_hishtalmut = clal_pension_salary_basis_monthly_nis * (
    keren_hishtalmut.contribution_rate_pct + keren_hishtalmut.employer_match_pct
) / 100  # ≈ 2,710 NIS/mo

# Same accumulation rule; balance becomes "available" at age 60
# (For simplicity: at age 60, balance is added to portfolio in one shot)
# After age 60, no further accumulation modelled.

# Frozen funds (executive_insurance, kupat_gemel/provident_fund):
#   balance[t] = balance[t-1] * (1 + real_return/12)
#   no contributions

# Lump bump at age 60:
if age_t == LUMP_PENSION_AGE (in any month of that year, applied once):
    portfolio_*[t:] += keren_hishtalmut_balance[t] + kupat_gemel_balance[t]
    # Both buckets are then zeroed in their separate state.

# Monthly annuity from age 67:
if age_t >= ANNUITY_AGE:
    pension_annuity_monthly_nis = (
        pensia_balance[ANNUITY_AGE_MONTH] + executive_insurance_balance[ANNUITY_AGE_MONTH]
    ) / mekadem
else:
    pension_annuity_monthly_nis = 0

# Portfolio "safe" income (matches capital_preservation_returns_only):
#   monthly real-return income = portfolio * real_return / 12
portfolio_income_base_monthly_nis[t] = portfolio_base_nominal[t] * real_return / 12
# (bear/bull analogously)

# Expenses inflate at inflation_annual:
expenses_monthly_nis[t] = current_monthly_expenses_nis * (1+inflation_annual)**(t/12)

# Total monthly income at t:
total_income_monthly_nis[t] = portfolio_income_base_monthly_nis[t] + pension_annuity_monthly_nis[t]

# Surplus:
surplus_monthly_nis[t] = total_income_monthly_nis[t] - expenses_monthly_nis[t]
```

### Retire-ready age

Earliest month t ≥ retirement_age_month such that `total_income_monthly_nis[t] >= expenses_monthly_nis[t]`. May be `None` if never crosses within the horizon.

### Notes on this model

1. **Severance contribution** (8.33%) traditionally goes to a separate `pizurim` account; modelling it into kupat_pensia overstates that balance ≈ 67%. The math is documented; the user accepted this simplification ("mekadem 200 is ok"). Codex-tandem (Task 7) should flag if a more realistic split is recommended.
2. **Inflation indexation of the annuity:** real-world Israeli pensions adjust partially. We model it as flat NIS from age 67. Documented in `assumptions`.
3. **Hishtalmut tax-free window:** withdrawal at 60 is tax-free if held ≥6 years. Both buckets are well past that. No tax adjustment needed.
4. **kupat_pensia + executive_insurance growth post-retirement:** both keep growing at `real_return` between `retirement_age` and `ANNUITY_AGE` (no contributions). Simple and conservative.
5. **Today's portfolio value:** read via the same TSV-discovery path as the existing `get_draft_projection` route. Convert to NIS via FX for the math, back to USD for the response.

---

## Task 1: Pure-math primitives + pension extraction

**Files:**
- Create: `argosy/services/cashflow_projection.py`
- Create: `tests/test_cashflow_projection.py`

**Approach:** TDD. Write the math primitives first, then the extraction layer, then the top-level `project_cashflow` that orchestrates. Math primitives are pure functions of typed inputs — no DB, no I/O. The extraction layer reads from DB.

- [ ] **Step 1.1: Define the dataclasses (no logic yet)**

```python
# argosy/services/cashflow_projection.py
"""Monthly-cashflow projection for the /plan retirement view.

Answers two questions:
  a. When can Ariel safely retire — earliest month where projected total
     monthly income (portfolio real-return + pension annuity) covers
     inflated expenses.
  b. How sensitive is that crossing to (retirement_age, mu, sigma,
     inflation, mekadem).

Pure-Python module: DB reads in ``extract_pension_state`` /
``extract_household_state``; everything below is pure math. The route
layer in ``argosy.api.routes.plan`` wraps this in a FastAPI endpoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.orm import Session

# Defaults — surfaced in the response ``assumptions`` block.
DEFAULT_MU_NOMINAL_ANNUAL = 0.08
DEFAULT_SIGMA_ANNUAL = 0.18
DEFAULT_INFLATION_ANNUAL = 0.025
DEFAULT_MEKADEM = 200.0
LUMP_PENSION_AGE = 60
ANNUITY_AGE = 67


@dataclass(frozen=True)
class PensionState:
    """Snapshot of all pension-bucket balances + monthly contribution
    rates extracted from ``identity_yaml`` at the start of projection.

    All amounts in NIS. ``contribution_monthly_nis`` is zero when the
    bucket is frozen (executive_insurance, kupat_gemel)."""
    kupat_pensia_balance_nis: float
    kupat_pensia_contribution_monthly_nis: float
    executive_insurance_balance_nis: float
    keren_hishtalmut_balance_nis: float
    keren_hishtalmut_contribution_monthly_nis: float
    kupat_gemel_balance_nis: float


@dataclass(frozen=True)
class HouseholdState:
    """Per-month financial state at projection start. NIS."""
    monthly_expenses_nis: float
    portfolio_value_nis: float
    fx_usd_nis: float
    current_age_years: float


@dataclass(frozen=True)
class CashflowPoint:
    """One projected month."""
    months_out: int
    age_years: float
    date_yyyy_mm: str
    portfolio_value_base_nis: float
    portfolio_value_bear_nis: float
    portfolio_value_bull_nis: float
    portfolio_income_base_monthly_nis: float
    portfolio_income_bear_monthly_nis: float
    portfolio_income_bull_monthly_nis: float
    pension_annuity_monthly_nis: float
    pension_lump_available_nis: float  # cumulative lump unlocked (0 until age 60)
    expenses_monthly_nis: float
    surplus_base_monthly_nis: float  # base income - expenses


@dataclass(frozen=True)
class CashflowProjection:
    """Top-level result returned to the route."""
    series: list[CashflowPoint]
    retire_ready_age: float | None  # earliest age where base income covers expenses
    retire_ready_months_out: int | None
    pension_state_at_start: PensionState
    household_state_at_start: HouseholdState
    retirement_age_assumed: float
    assumptions: dict
```

- [ ] **Step 1.2: Write tests for pure-math primitives — accumulation + lump bump**

```python
# tests/test_cashflow_projection.py
"""Unit tests for the cashflow projection math.

Covers:
  - Pure pension-balance accumulation (contribution path, frozen path)
  - Annuity computation at age 67 (mekadem 200, sum of two buckets)
  - Lump bump at age 60 (portfolio gets the cash, source buckets zeroed)
  - Real-return income (portfolio * real_return / 12)
  - Inflation indexing of expenses
  - Retire-ready detection (crossing logic)
  - Extraction from a seeded UserContext + AgentReport + PortfolioSnapshot
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from argosy.services.cashflow_projection import (
    ANNUITY_AGE,
    DEFAULT_INFLATION_ANNUAL,
    DEFAULT_MEKADEM,
    DEFAULT_MU_NOMINAL_ANNUAL,
    DEFAULT_SIGMA_ANNUAL,
    LUMP_PENSION_AGE,
    CashflowPoint,
    HouseholdState,
    PensionState,
    accumulate_pension_balance,
    compute_pension_annuity,
    detect_retire_ready,
    inflate_expenses,
    portfolio_real_return_monthly,
    project_cashflow,
)


class TestAccumulatePensionBalance:
    def test_with_contributions_grows_above_compound_interest(self):
        # 100k starting, 5k/mo contribution, 5.5% real return, 12 months
        b = accumulate_pension_balance(
            starting_balance_nis=100_000.0,
            monthly_contribution_nis=5_000.0,
            real_return_annual=0.055,
            months=12,
        )
        # Pure compound (no contributions): 100k * 1.055 = 105,500
        # With 60k of contributions roughly evenly placed: ~167,000
        assert 165_000 < b < 170_000

    def test_frozen_bucket_grows_by_real_return_only(self):
        b = accumulate_pension_balance(
            starting_balance_nis=100_000.0,
            monthly_contribution_nis=0.0,
            real_return_annual=0.055,
            months=12,
        )
        assert b == pytest.approx(100_000.0 * 1.055, rel=1e-3)

    def test_zero_months_returns_starting_balance(self):
        assert accumulate_pension_balance(
            starting_balance_nis=42_000.0,
            monthly_contribution_nis=0.0,
            real_return_annual=0.055,
            months=0,
        ) == pytest.approx(42_000.0)


class TestComputePensionAnnuity:
    def test_mekadem_200_divides_sum_of_buckets(self):
        # Sum 1.5M / 200 = 7,500 NIS/mo
        a = compute_pension_annuity(
            kupat_pensia_balance_nis=750_000.0,
            executive_insurance_balance_nis=750_000.0,
            mekadem=200.0,
        )
        assert a == pytest.approx(7_500.0)

    def test_zero_balances_zero_annuity(self):
        assert compute_pension_annuity(
            kupat_pensia_balance_nis=0.0,
            executive_insurance_balance_nis=0.0,
            mekadem=200.0,
        ) == 0.0


class TestPortfolioRealReturnMonthly:
    def test_basic_formula(self):
        # 1M * 0.055 / 12 ≈ 4,583
        assert portfolio_real_return_monthly(
            portfolio_value_nis=1_000_000.0,
            real_return_annual=0.055,
        ) == pytest.approx(1_000_000.0 * 0.055 / 12, rel=1e-9)


class TestInflateExpenses:
    def test_one_year_inflation(self):
        e = inflate_expenses(
            base_monthly_nis=20_000.0,
            inflation_annual=0.025,
            months_out=12,
        )
        assert e == pytest.approx(20_000.0 * 1.025, rel=1e-9)

    def test_t_zero_no_inflation(self):
        assert inflate_expenses(20_000.0, 0.025, 0) == pytest.approx(20_000.0)


class TestDetectRetireReady:
    def test_returns_first_crossing_month(self):
        series = [
            CashflowPoint(
                months_out=i, age_years=43+i/12,
                date_yyyy_mm="2026-05",
                portfolio_value_base_nis=0,
                portfolio_value_bear_nis=0,
                portfolio_value_bull_nis=0,
                portfolio_income_base_monthly_nis=(15_000 + i*100),
                portfolio_income_bear_monthly_nis=0,
                portfolio_income_bull_monthly_nis=0,
                pension_annuity_monthly_nis=0,
                pension_lump_available_nis=0,
                expenses_monthly_nis=20_000,
                surplus_base_monthly_nis=(15_000 + i*100) - 20_000,
            )
            for i in range(120)
        ]
        # 15000 + i*100 = 20000 at i=50
        out = detect_retire_ready(series)
        assert out is not None
        assert out[0] == 50  # months_out
        # age_years at i=50 = 43 + 50/12 ≈ 47.17
        assert 47.0 < out[1] < 47.5

    def test_returns_none_when_never_crosses(self):
        series = [
            CashflowPoint(
                months_out=i, age_years=43+i/12, date_yyyy_mm="2026-05",
                portfolio_value_base_nis=0, portfolio_value_bear_nis=0,
                portfolio_value_bull_nis=0,
                portfolio_income_base_monthly_nis=10_000,
                portfolio_income_bear_monthly_nis=0,
                portfolio_income_bull_monthly_nis=0,
                pension_annuity_monthly_nis=0, pension_lump_available_nis=0,
                expenses_monthly_nis=20_000,
                surplus_base_monthly_nis=-10_000,
            )
            for i in range(60)
        ]
        assert detect_retire_ready(series) is None
```

- [ ] **Step 1.3: Implement the pure-math primitives**

```python
# argosy/services/cashflow_projection.py — append below the dataclasses

def accumulate_pension_balance(
    *,
    starting_balance_nis: float,
    monthly_contribution_nis: float,
    real_return_annual: float,
    months: int,
) -> float:
    """Apply N months of (growth at real_return/12) + monthly contribution.

    Contribution is applied AFTER growth each month so the contribution
    starts earning at month t+1. Matches the convention in
    ``argosy.services.wealth_dashboard.project_wealth_curve``.
    """
    b = float(starting_balance_nis)
    monthly_rate = real_return_annual / 12.0
    for _ in range(months):
        b = b * (1.0 + monthly_rate) + monthly_contribution_nis
    return b


def compute_pension_annuity(
    *,
    kupat_pensia_balance_nis: float,
    executive_insurance_balance_nis: float,
    mekadem: float,
) -> float:
    """``(sum_of_balances) / mekadem`` — the standard Israeli
    monthly-stipend formula. ``mekadem`` typically 190-220; 200 is the
    common conservative default."""
    if mekadem <= 0:
        return 0.0
    return (kupat_pensia_balance_nis + executive_insurance_balance_nis) / mekadem


def portfolio_real_return_monthly(
    *,
    portfolio_value_nis: float,
    real_return_annual: float,
) -> float:
    """Monthly portfolio income under the ``capital_preservation_returns_only``
    drawdown style: take this month's share of the annual real return."""
    return portfolio_value_nis * real_return_annual / 12.0


def inflate_expenses(
    base_monthly_nis: float,
    inflation_annual: float,
    months_out: int,
) -> float:
    return base_monthly_nis * ((1.0 + inflation_annual) ** (months_out / 12.0))


def detect_retire_ready(
    series: Sequence[CashflowPoint],
) -> tuple[int, float] | None:
    """Return ``(months_out, age_years)`` of the first row where
    ``portfolio_income_base + pension_annuity >= expenses``. ``None``
    if no such row in the supplied window."""
    for p in series:
        total = p.portfolio_income_base_monthly_nis + p.pension_annuity_monthly_nis
        if total >= p.expenses_monthly_nis:
            return p.months_out, p.age_years
    return None
```

- [ ] **Step 1.4: Run primitive tests**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" \
  tests/test_cashflow_projection.py::TestAccumulatePensionBalance \
  tests/test_cashflow_projection.py::TestComputePensionAnnuity \
  tests/test_cashflow_projection.py::TestPortfolioRealReturnMonthly \
  tests/test_cashflow_projection.py::TestInflateExpenses \
  tests/test_cashflow_projection.py::TestDetectRetireReady \
  -v
```
Expected: 8 passed.

- [ ] **Step 1.5: Commit primitives**

```bash
git add argosy/services/cashflow_projection.py tests/test_cashflow_projection.py
git commit -m "feat(cashflow): pure-math primitives for monthly cashflow projection"
```

---

## Task 2: Pension + household state extraction from DB

**Files:**
- Modify: `argosy/services/cashflow_projection.py`
- Modify: `tests/test_cashflow_projection.py`

- [ ] **Step 2.1: Add extraction-test fixtures**

Reuse the seed helpers from `tests/test_wealth_dashboard.py` (`_seed_user`, `_seed_user_context`, `_seed_snapshot`, `_seed_household_budget_report`). Import them or copy the pattern. For the pension YAML, extend `_seed_user_context` (locally in this test file) to inject a `pensions_ariel` + `pensions` + `clal_pension_*` block.

```python
# Append to tests/test_cashflow_projection.py

import yaml
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
)
from datetime import datetime, timezone


def _seed_full_state(session, *, user_id="ariel"):
    """Seed a complete user with all the pension + budget + snapshot data
    the cashflow projection needs. Mirrors the real DB shape for ``ariel``."""
    session.add(User(user_id=user_id, name="Ariel", email="a@e"))
    identity = {
        "date_of_birth": "1982-08-28",
        "clal_pension_salary_basis_monthly_nis": 27101,
        "clal_pension_employee_pct": 6.0,
        "clal_pension_employer_pct": 6.5,
        "clal_pension_severance_pct": 8.33,
        "pensions_ariel": {
            "pension_nis": 800_147,
            "executive_insurance_nis": 755_907,
            "keren_hishtalmut_nis": 384_000,
            "provident_fund_nis": 75_000,
            "total_nis": 2_015_054,
            "data_date": "2025-12",
        },
        "pensions": {
            "kupat_pensia": {
                "balance_nis": 800_147,
                "contribution_rate_pct": 6.0,
                "employer_match_pct": 6.5,
            },
            "keren_hishtalmut": {
                "balance_nis": 384_000,
                "contribution_rate_pct": 2.5,
                "employer_match_pct": 7.5,
            },
            "executive_insurance": {"balance_nis": 755_907},
            "kupat_gemel": {"balance_nis": 75_000},
        },
        "fx_rate": {"usd_nis": 2.94},
    }
    session.add(UserContext(
        user_id=user_id,
        identity_yaml=yaml.safe_dump(identity),
        goals_yaml="",
        constraints_yaml="",
        current_stage="complete",
    ))
    session.add(PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/seed.tsv",
        positions_json="[]",
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({"total_usd_value_k": 1500.0}),
        fx_usd_nis=2.94,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    ))
    body = {
        "runway_class": "comfortable",
        "monthly_burn_nis": 23_084.0,
        "monthly_income_nis": 54_835.0,
        "monthly_net_nis": 31_751.0,
        "safe_withdrawal_monthly_usd": 11_800.0,
        "headroom_summary": "seeded",
        "key_concerns": [],
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    session.add(AgentReport(
        user_id=user_id, agent_role="household_budget", decision_id=None,
        prompt_hash="x", response_text=f"```json\n{json.dumps(body)}\n```",
        tokens_in=0, tokens_out=0, cost_usd=0, model="seed",
    ))
    session.commit()


class TestExtractPensionState:
    def test_reads_all_four_buckets_and_contribution_rates(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import extract_pension_state
            state = extract_pension_state(s, "ariel")
        assert state.kupat_pensia_balance_nis == 800_147
        assert state.executive_insurance_balance_nis == 755_907
        assert state.keren_hishtalmut_balance_nis == 384_000
        assert state.kupat_gemel_balance_nis == 75_000
        # kupat_pensia monthly contribution = 27101 * (6 + 6.5 + 8.33)/100 = 5,646
        expected_pensia_contrib = 27101 * (6.0 + 6.5 + 8.33) / 100.0
        assert state.kupat_pensia_contribution_monthly_nis == pytest.approx(
            expected_pensia_contrib, rel=1e-3
        )
        # hishtalmut contribution = 27101 * (2.5 + 7.5)/100 = 2,710.1
        expected_hishtalmut_contrib = 27101 * (2.5 + 7.5) / 100.0
        assert state.keren_hishtalmut_contribution_monthly_nis == pytest.approx(
            expected_hishtalmut_contrib, rel=1e-3
        )

    def test_returns_zeros_when_no_identity(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            from argosy.services.cashflow_projection import extract_pension_state
            state = extract_pension_state(s, "missing-user")
        assert state.kupat_pensia_balance_nis == 0.0
        assert state.kupat_pensia_contribution_monthly_nis == 0.0


class TestExtractHouseholdState:
    def test_reads_burn_portfolio_fx_age(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import extract_household_state
            state = extract_household_state(s, "ariel", today=date(2026, 5, 27))
        assert state.monthly_expenses_nis == pytest.approx(23_084.0)
        # 1500k USD * 2.94 = 4,410,000 NIS
        assert state.portfolio_value_nis == pytest.approx(4_410_000.0, rel=1e-3)
        assert state.fx_usd_nis == pytest.approx(2.94)
        # 1982-08-28 → 2026-05-27 ≈ 43.74 years
        assert 43.6 < state.current_age_years < 43.9
```

- [ ] **Step 2.2: Implement extraction**

```python
# Append to argosy/services/cashflow_projection.py

from datetime import date

from argosy.services.wealth_dashboard import (
    _latest_household_budget_report,
    _latest_snapshot,
    _load_user_context_yaml,
    _resolve_fx_usd_nis,
)


def _safe_float(d: dict, *keys, default: float = 0.0) -> float:
    """Walk ``d[keys[0]][keys[1]]...`` and return as float; default on
    missing/None/un-coerceable."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def extract_pension_state(session: Session, user_id: str) -> PensionState:
    """Pull pension balances + active contribution rates from identity_yaml.

    Returns a PensionState with zeros for any missing keys (so the math
    layer can run unconditionally on under-populated users)."""
    ctx = _load_user_context_yaml(session, user_id)

    salary = _safe_float(ctx, "clal_pension_salary_basis_monthly_nis")
    emp_pct = _safe_float(ctx, "clal_pension_employee_pct")
    er_pct = _safe_float(ctx, "clal_pension_employer_pct")
    sev_pct = _safe_float(ctx, "clal_pension_severance_pct")
    pensia_contrib = salary * (emp_pct + er_pct + sev_pct) / 100.0

    hishtalmut_emp = _safe_float(ctx, "pensions", "keren_hishtalmut", "contribution_rate_pct")
    hishtalmut_er = _safe_float(ctx, "pensions", "keren_hishtalmut", "employer_match_pct")
    hishtalmut_contrib = salary * (hishtalmut_emp + hishtalmut_er) / 100.0

    return PensionState(
        kupat_pensia_balance_nis=_safe_float(ctx, "pensions_ariel", "pension_nis"),
        kupat_pensia_contribution_monthly_nis=pensia_contrib,
        executive_insurance_balance_nis=_safe_float(
            ctx, "pensions_ariel", "executive_insurance_nis"
        ),
        keren_hishtalmut_balance_nis=_safe_float(
            ctx, "pensions_ariel", "keren_hishtalmut_nis"
        ),
        keren_hishtalmut_contribution_monthly_nis=hishtalmut_contrib,
        kupat_gemel_balance_nis=_safe_float(
            ctx, "pensions_ariel", "provident_fund_nis"
        ),
    )


def _compute_age_years(date_of_birth_iso: str | None, today: date) -> float:
    if not date_of_birth_iso:
        return 43.0  # safe default
    try:
        dob = date.fromisoformat(date_of_birth_iso)
    except ValueError:
        return 43.0
    days = (today - dob).days
    return days / 365.25


def extract_household_state(
    session: Session,
    user_id: str,
    *,
    today: date | None = None,
) -> HouseholdState:
    """Pull (monthly_burn_nis, portfolio_value_nis, fx, age) for the
    projection root. Falls back gracefully when any field is absent."""
    today = today or date.today()
    ctx = _load_user_context_yaml(session, user_id)
    budget = _latest_household_budget_report(session, user_id) or {}
    snapshot = _latest_snapshot(session, user_id)
    fx_usd_nis, _ = _resolve_fx_usd_nis(snapshot=snapshot, user_ctx=ctx)

    monthly_expenses_nis = _safe_float(budget, "monthly_burn_nis")

    # Portfolio value: prefer the explicit ``total_usd_value_k`` on the
    # latest snapshot row (matches the existing get_draft_projection
    # path), convert to NIS via the resolved FX.
    portfolio_value_nis = 0.0
    if snapshot is not None:
        try:
            import json as _json
            totals = _json.loads(snapshot.totals_json or "{}")
            usd_k = float(totals.get("total_usd_value_k") or 0.0)
            portfolio_value_nis = usd_k * 1000.0 * fx_usd_nis
        except (ValueError, TypeError):
            portfolio_value_nis = 0.0

    age = _compute_age_years(
        ctx.get("date_of_birth") if isinstance(ctx, dict) else None,
        today,
    )
    return HouseholdState(
        monthly_expenses_nis=monthly_expenses_nis,
        portfolio_value_nis=portfolio_value_nis,
        fx_usd_nis=fx_usd_nis,
        current_age_years=age,
    )
```

- [ ] **Step 2.3: Run extraction tests**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" \
  tests/test_cashflow_projection.py::TestExtractPensionState \
  tests/test_cashflow_projection.py::TestExtractHouseholdState \
  -v
```
Expected: 3 passed.

- [ ] **Step 2.4: Commit**

```bash
git add argosy/services/cashflow_projection.py tests/test_cashflow_projection.py
git commit -m "feat(cashflow): extract PensionState + HouseholdState from DB"
```

---

## Task 3: Top-level `project_cashflow` orchestrator

**Files:**
- Modify: `argosy/services/cashflow_projection.py`
- Modify: `tests/test_cashflow_projection.py`

The orchestrator walks `t=0..horizon_months`, applies the per-bucket state machine each tick, builds the `CashflowPoint` series, and detects retire-ready.

- [ ] **Step 3.1: Write the orchestrator test**

```python
class TestProjectCashflow:
    def test_full_projection_at_seeded_state(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh,
            pensions=pen,
            retirement_age=49.0,
            years=30,
            mu_nominal_annual=DEFAULT_MU_NOMINAL_ANNUAL,
            sigma_annual=DEFAULT_SIGMA_ANNUAL,
            inflation_annual=DEFAULT_INFLATION_ANNUAL,
            mekadem=DEFAULT_MEKADEM,
            today=date(2026, 5, 27),
        )
        # Series length = 30 years * 12 + 1 (t=0 included)
        assert len(proj.series) == 30 * 12 + 1

        first = proj.series[0]
        assert first.months_out == 0
        # At t=0: portfolio_income_base = 4.41M * 0.055 / 12 ≈ 20,212 NIS
        assert first.portfolio_income_base_monthly_nis == pytest.approx(
            4_410_000.0 * 0.055 / 12.0, rel=1e-3
        )
        # At t=0: no pension annuity, no lump, expenses ≈ 23,084
        assert first.pension_annuity_monthly_nis == 0
        assert first.pension_lump_available_nis == 0
        assert first.expenses_monthly_nis == pytest.approx(23_084.0, rel=1e-6)

    def test_lump_unlocks_at_age_60(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen,
            retirement_age=49.0, years=30,
            mu_nominal_annual=DEFAULT_MU_NOMINAL_ANNUAL,
            sigma_annual=DEFAULT_SIGMA_ANNUAL,
            inflation_annual=DEFAULT_INFLATION_ANNUAL,
            mekadem=DEFAULT_MEKADEM,
            today=date(2026, 5, 27),
        )
        # At age 60 (≈ 16.26 years out = month 195), lump unlocks
        lump_idx = next(
            i for i, p in enumerate(proj.series) if p.age_years >= 60.0
        )
        # Before lump: pension_lump_available_nis == 0
        assert proj.series[lump_idx - 1].pension_lump_available_nis == 0
        # At/after lump: > 0 (sum of keren_hishtalmut + kupat_gemel grown)
        assert proj.series[lump_idx].pension_lump_available_nis > 0
        # Should be at least the original 459,000 NIS (frozen kupat_gemel
        # + accumulated keren_hishtalmut)
        assert proj.series[lump_idx].pension_lump_available_nis >= 459_000

    def test_annuity_kicks_in_at_age_67(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen,
            retirement_age=49.0, years=30,
            mu_nominal_annual=DEFAULT_MU_NOMINAL_ANNUAL,
            sigma_annual=DEFAULT_SIGMA_ANNUAL,
            inflation_annual=DEFAULT_INFLATION_ANNUAL,
            mekadem=DEFAULT_MEKADEM,
            today=date(2026, 5, 27),
        )
        annuity_idx = next(
            i for i, p in enumerate(proj.series) if p.age_years >= 67.0
        )
        # Before age 67: annuity == 0
        assert proj.series[annuity_idx - 1].pension_annuity_monthly_nis == 0
        # At/after age 67: > 0
        assert proj.series[annuity_idx].pension_annuity_monthly_nis > 0
        # Should be at least balance / 200 of original 1,556,054
        assert (
            proj.series[annuity_idx].pension_annuity_monthly_nis >= 1_556_054 / 200
        )

    def test_contributions_stop_at_retirement_age(self, client_with_db):
        """When retirement_age = 50, kupat_pensia balance at age 50 should
        be GREATER than at age 49, but contributions stop, so growth
        after 50 is slower than before."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        # Two projections, only retirement_age differs.
        proj_retire_49 = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=20,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        proj_retire_60 = project_cashflow(
            household=hh, pensions=pen, retirement_age=60.0, years=20,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        # At age 67 (228 months from now), the later-retirement projection
        # has paid into kupat_pensia for ~11 more years; annuity should
        # be higher.
        age_67_idx_49 = next(i for i, p in enumerate(proj_retire_49.series) if p.age_years >= 67)
        age_67_idx_60 = next(i for i, p in enumerate(proj_retire_60.series) if p.age_years >= 67)
        assert (
            proj_retire_60.series[age_67_idx_60].pension_annuity_monthly_nis
            > proj_retire_49.series[age_67_idx_49].pension_annuity_monthly_nis
        )

    def test_retire_ready_detected_when_crossing_exists(self, client_with_db):
        """With Ariel's seeded numbers (large portfolio + healthy pensions),
        the retire-ready crossing should land within 30 years."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        # At today's portfolio (4.41M NIS), real-return income alone is
        # 20,212/mo vs expenses 23,084/mo — short by 2,872. Should cross
        # within 30y as portfolio grows.
        assert proj.retire_ready_months_out is not None
        assert proj.retire_ready_age is not None
        assert proj.retire_ready_age >= hh.current_age_years
```

- [ ] **Step 3.2: Implement `project_cashflow` (iterative monthly compounding)**

The portfolio path is iterative (not closed-form) so the mid-projection lump bump composes correctly with subsequent compounding. The lognormal bands are tracked as multiplicative factors `exp(±sigma*sqrt(t/12))` applied each month to the running `portfolio_base_nis`.

```python
# Append to argosy/services/cashflow_projection.py

import calendar


def _add_months(today: date, n: int) -> date:
    """Add n calendar months to ``today``. Day clamps to end-of-month."""
    y = today.year + (today.month - 1 + n) // 12
    m = (today.month - 1 + n) % 12 + 1
    d = min(today.day, calendar.monthrange(y, m)[1])
    return date(y, m, d)


def project_cashflow(
    *,
    household: HouseholdState,
    pensions: PensionState,
    retirement_age: float,
    years: int,
    mu_nominal_annual: float = DEFAULT_MU_NOMINAL_ANNUAL,
    sigma_annual: float = DEFAULT_SIGMA_ANNUAL,
    inflation_annual: float = DEFAULT_INFLATION_ANNUAL,
    mekadem: float = DEFAULT_MEKADEM,
    today: date | None = None,
) -> CashflowProjection:
    """Project ``years * 12 + 1`` monthly cashflow points and detect
    retire-ready age. See module docstring for the math model.

    Portfolio path: iterative monthly compounding at ``mu_nominal/12``;
    bull/bear = base × exp(±sigma × sqrt(t/12)) re-derived from the running
    base at each tick so the lump-bump at age 60 composes correctly.

    Pension buckets: per-month accumulate-and-grow. Lump unlock at age 60
    transfers (keren_hishtalmut + kupat_gemel) into ``portfolio_base_nis``.
    Annuity lock at age 67: ``(pensia + exec_ins) / mekadem``; thereafter
    pensia + exec_ins are treated as consumed (frozen, not compounded).
    """
    today = today or date.today()
    months = max(1, min(years, 50)) * 12
    real_return = mu_nominal_annual - inflation_annual
    monthly_growth = 1.0 + mu_nominal_annual / 12.0  # nominal monthly compounding

    # Per-bucket running NIS state
    portfolio_base_nis = household.portfolio_value_nis
    pensia_balance = pensions.kupat_pensia_balance_nis
    exec_ins_balance = pensions.executive_insurance_balance_nis
    hishtalmut_balance = pensions.keren_hishtalmut_balance_nis
    kupat_gemel_balance = pensions.kupat_gemel_balance_nis

    lump_unlocked = False
    lump_amount_nis = 0.0
    annuity_monthly_nis = 0.0
    annuity_locked = False

    out: list[CashflowPoint] = []

    for t in range(months + 1):
        age_t = household.current_age_years + t / 12.0
        t_years = t / 12.0

        # ---- Step 1: advance portfolio_base by one month of growth
        # (skipped at t=0 — we emit today's actual state first).
        if t > 0:
            portfolio_base_nis *= monthly_growth

        # ---- Step 2: advance pension bucket balances by one month.
        # Contributions stop at ``retirement_age``. Both ``executive_insurance``
        # and ``kupat_gemel`` are frozen (no contributions) regardless of age.
        if t > 0:
            real_monthly = 1.0 + real_return / 12.0
            # kupat_pensia: accumulate until annuity lock
            if not annuity_locked:
                contrib_pensia = (
                    pensions.kupat_pensia_contribution_monthly_nis
                    if age_t < retirement_age else 0.0
                )
                pensia_balance = pensia_balance * real_monthly + contrib_pensia
                exec_ins_balance = exec_ins_balance * real_monthly
            # hishtalmut + kupat_gemel: accumulate until lump unlock
            if not lump_unlocked:
                contrib_hisht = (
                    pensions.keren_hishtalmut_contribution_monthly_nis
                    if age_t < retirement_age else 0.0
                )
                hishtalmut_balance = (
                    hishtalmut_balance * real_monthly + contrib_hisht
                )
                kupat_gemel_balance = kupat_gemel_balance * real_monthly

        # ---- Step 3: lump unlock at age 60 (first month age_t >= 60).
        # Add the combined balance to portfolio_base_nis; zero the sources.
        # Subsequent ticks will compound the lump along with the portfolio.
        if age_t >= LUMP_PENSION_AGE and not lump_unlocked:
            lump_amount_nis = hishtalmut_balance + kupat_gemel_balance
            portfolio_base_nis += lump_amount_nis
            hishtalmut_balance = 0.0
            kupat_gemel_balance = 0.0
            lump_unlocked = True

        # ---- Step 4: annuity lock at age 67.
        if age_t >= ANNUITY_AGE and not annuity_locked:
            annuity_monthly_nis = compute_pension_annuity(
                kupat_pensia_balance_nis=pensia_balance,
                executive_insurance_balance_nis=exec_ins_balance,
                mekadem=mekadem,
            )
            annuity_locked = True

        # ---- Step 5: derive bull/bear from base via lognormal ±1σ band.
        # At t=0 the band collapses to base (no uncertainty yet).
        log_std = sigma_annual * math.sqrt(t_years)
        portfolio_bull_nis = portfolio_base_nis * math.exp(log_std)
        portfolio_bear_nis = portfolio_base_nis * math.exp(-log_std)

        # ---- Step 6: derived series — incomes, expenses, surplus.
        portfolio_income_base = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_base_nis, real_return_annual=real_return
        )
        portfolio_income_bull = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_bull_nis, real_return_annual=real_return
        )
        portfolio_income_bear = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_bear_nis, real_return_annual=real_return
        )
        expenses_t = inflate_expenses(
            household.monthly_expenses_nis, inflation_annual, t
        )
        surplus_base = portfolio_income_base + annuity_monthly_nis - expenses_t

        d = _add_months(today, t)
        out.append(CashflowPoint(
            months_out=t,
            age_years=age_t,
            date_yyyy_mm=d.strftime("%Y-%m"),
            portfolio_value_base_nis=portfolio_base_nis,
            portfolio_value_bear_nis=portfolio_bear_nis,
            portfolio_value_bull_nis=portfolio_bull_nis,
            portfolio_income_base_monthly_nis=portfolio_income_base,
            portfolio_income_bear_monthly_nis=portfolio_income_bear,
            portfolio_income_bull_monthly_nis=portfolio_income_bull,
            pension_annuity_monthly_nis=annuity_monthly_nis,
            pension_lump_available_nis=lump_amount_nis if lump_unlocked else 0.0,
            expenses_monthly_nis=expenses_t,
            surplus_base_monthly_nis=surplus_base,
        ))

    retire_ready = detect_retire_ready(out)
    return CashflowProjection(
        series=out,
        retire_ready_months_out=retire_ready[0] if retire_ready else None,
        retire_ready_age=retire_ready[1] if retire_ready else None,
        pension_state_at_start=pensions,
        household_state_at_start=household,
        retirement_age_assumed=retirement_age,
        assumptions={
            "mu_nominal_annual": mu_nominal_annual,
            "sigma_annual": sigma_annual,
            "real_return_annual": real_return,
            "inflation_annual": inflation_annual,
            "mekadem": mekadem,
            "lump_pension_age": LUMP_PENSION_AGE,
            "annuity_age": ANNUITY_AGE,
            "model_notes": (
                "Iterative monthly compounding at mu_nominal/12 for the "
                "portfolio base; bull/bear = base × exp(±sigma × sqrt(t/12)). "
                "Real-return drawdown income = portfolio × (mu - inflation) / 12. "
                "Lump (keren_hishtalmut + kupat_gemel) unlocks at age 60, added "
                "to portfolio. Annuity (kupat_pensia + executive_insurance) "
                "locked at age 67 via balance / mekadem; balances frozen "
                "thereafter. Executive insurance modelled as frozen (no "
                "contributions). Severance (8.33%) modelled as kupat_pensia "
                "contribution — flagged for codex-tandem review."
            ),
        },
    )
```

> **Key invariant the implementer must preserve:** after the lump unlock at age 60, `portfolio_value_base_nis` at month-of-unlock + 12 should equal `(portfolio_value_base_nis at month-of-unlock) × (1 + mu_nominal_annual/12)^12`. The test in Step 3.1's `test_lump_unlocks_at_age_60` is augmented (see 3.1.5 below) to enforce this.

- [ ] **Step 3.1.5: Add an invariant test for post-lump compounding**

Append to `TestProjectCashflow` in `tests/test_cashflow_projection.py`:

```python
    def test_portfolio_keeps_compounding_after_lump_unlock(self, client_with_db):
        """After the lump bump at age 60, the portfolio base should
        continue compounding at mu_nominal/12 per month — i.e., the
        value one year post-unlock should equal value-at-unlock × (1+mu/12)^12.
        Guards against a regression where the lump-bump path resets
        the portfolio's compound state."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        unlock_idx = next(
            i for i, p in enumerate(proj.series) if p.pension_lump_available_nis > 0
        )
        # Compounding 12 months past the unlock month, no further lumps:
        v_at_unlock = proj.series[unlock_idx].portfolio_value_base_nis
        v_plus_12 = proj.series[unlock_idx + 12].portfolio_value_base_nis
        expected = v_at_unlock * ((1.0 + 0.08 / 12.0) ** 12)
        assert v_plus_12 == pytest.approx(expected, rel=1e-6)
```

- [ ] **Step 3.3: Run orchestrator tests**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" \
  tests/test_cashflow_projection.py::TestProjectCashflow -v
```
Expected: 6 passed.

- [ ] **Step 3.4: Commit**

```bash
git add argosy/services/cashflow_projection.py tests/test_cashflow_projection.py
git commit -m "feat(cashflow): top-level project_cashflow orchestrator + tests"
```

---

## Task 4: API route + DTO + integration test

**Files:**
- Modify: `argosy/api/routes/plan.py`
- Modify: `tests/test_plan_draft_api.py`

- [ ] **Step 4.1: Add the route + pydantic DTOs**

```python
# Append to argosy/api/routes/plan.py near the ProjectionResponse block:

class CashflowPointDTO(BaseModel):
    months_out: int
    age_years: float
    date: str  # YYYY-MM
    portfolio_value_base_usd: float
    portfolio_value_bear_usd: float
    portfolio_value_bull_usd: float
    portfolio_income_base_monthly_usd: float
    portfolio_income_bear_monthly_usd: float
    portfolio_income_bull_monthly_usd: float
    pension_annuity_monthly_usd: float
    pension_lump_available_usd: float
    expenses_monthly_usd: float
    surplus_base_monthly_usd: float


class CashflowProjectionResponse(BaseModel):
    today_date: str
    today_age_years: float
    fx_usd_nis: float
    retirement_age_assumed: float
    retire_ready_age: float | None
    retire_ready_months_out: int | None
    series: list[CashflowPointDTO]
    assumptions: dict


@router.get(
    "/draft/cashflow-projection", response_model=CashflowProjectionResponse
)
def get_draft_cashflow_projection(
    user_id: str = Query("ariel"),
    years: int = Query(30, ge=1, le=50),
    retirement_age: float = Query(49.0, ge=30.0, le=80.0),
    db: Session = Depends(get_db),
) -> CashflowProjectionResponse:
    """Return a per-month cashflow projection for the /plan retirement view.

    Pure-math endpoint — no LLM, no external HTTP, just three DB reads
    + the projection loop. <30 ms for a 30-year horizon."""
    from argosy.services.cashflow_projection import (
        extract_household_state,
        extract_pension_state,
        project_cashflow,
    )

    hh = extract_household_state(db, user_id)
    pen = extract_pension_state(db, user_id)
    proj = project_cashflow(
        household=hh,
        pensions=pen,
        retirement_age=retirement_age,
        years=years,
    )

    fx = hh.fx_usd_nis if hh.fx_usd_nis > 0 else 1.0
    def to_usd(nis: float) -> float:
        return round(nis / fx, 2)

    series_dto = [
        CashflowPointDTO(
            months_out=p.months_out,
            age_years=round(p.age_years, 3),
            date=p.date_yyyy_mm,
            portfolio_value_base_usd=to_usd(p.portfolio_value_base_nis),
            portfolio_value_bear_usd=to_usd(p.portfolio_value_bear_nis),
            portfolio_value_bull_usd=to_usd(p.portfolio_value_bull_nis),
            portfolio_income_base_monthly_usd=to_usd(p.portfolio_income_base_monthly_nis),
            portfolio_income_bear_monthly_usd=to_usd(p.portfolio_income_bear_monthly_nis),
            portfolio_income_bull_monthly_usd=to_usd(p.portfolio_income_bull_monthly_nis),
            pension_annuity_monthly_usd=to_usd(p.pension_annuity_monthly_nis),
            pension_lump_available_usd=to_usd(p.pension_lump_available_nis),
            expenses_monthly_usd=to_usd(p.expenses_monthly_nis),
            surplus_base_monthly_usd=to_usd(p.surplus_base_monthly_nis),
        )
        for p in proj.series
    ]
    return CashflowProjectionResponse(
        today_date=datetime.now(timezone.utc).date().isoformat(),
        today_age_years=round(hh.current_age_years, 3),
        fx_usd_nis=fx,
        retirement_age_assumed=round(proj.retirement_age_assumed, 1),
        retire_ready_age=(round(proj.retire_ready_age, 2)
                          if proj.retire_ready_age is not None else None),
        retire_ready_months_out=proj.retire_ready_months_out,
        series=series_dto,
        assumptions=proj.assumptions,
    )
```

- [ ] **Step 4.2: Add it to the module `__all__` list if one exists** (search for `"TargetProgressResponse",` near the bottom and add `"CashflowProjectionResponse",` adjacent.)

- [ ] **Step 4.3: Write integration test**

```python
# Append to tests/test_plan_draft_api.py

def test_cashflow_projection_route_returns_series(client_with_db):
    """Smoke: full route returns a 30-year monthly series + retire-ready age."""
    SF = client_with_db.app.state.session_factory
    # Reuse the cashflow projection's _seed_full_state helper:
    from tests.test_cashflow_projection import _seed_full_state
    with SF() as s:
        _seed_full_state(s)
    r = client_with_db.get("/api/plan/draft/cashflow-projection?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert "series" in body
    assert len(body["series"]) == 30 * 12 + 1
    assert body["fx_usd_nis"] == pytest.approx(2.94)
    # First point: t=0, expenses ≈ 23,084 NIS / 2.94 ≈ 7,851 USD
    first = body["series"][0]
    assert first["months_out"] == 0
    assert first["expenses_monthly_usd"] == pytest.approx(7_851.0, rel=1e-2)


def test_cashflow_projection_retirement_age_param(client_with_db):
    """Different ``retirement_age`` → different pension annuity at 67."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    r1 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&retirement_age=49"
    )
    r2 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&retirement_age=60"
    )
    assert r1.status_code == 200 and r2.status_code == 200
    # Annuity at age 67 should be larger when retirement_age=60 (more contributions)
    # Find first point with age >= 67 in each series
    def first_at_67(body):
        for p in body["series"]:
            if p["age_years"] >= 67.0:
                return p
        return None
    p49 = first_at_67(r1.json())
    p60 = first_at_67(r2.json())
    assert p49 is not None and p60 is not None
    assert p60["pension_annuity_monthly_usd"] > p49["pension_annuity_monthly_usd"]
```

- [ ] **Step 4.4: Run the route tests**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" \
  tests/test_plan_draft_api.py::test_cashflow_projection_route_returns_series \
  tests/test_plan_draft_api.py::test_cashflow_projection_retirement_age_param \
  -v
```
Expected: 2 passed.

- [ ] **Step 4.5: Run the full plan-draft test module to confirm nothing else broke**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" \
  tests/test_plan_draft_api.py -q
```
Expected: all green.

- [ ] **Step 4.6: Commit**

```bash
git add argosy/api/routes/plan.py tests/test_plan_draft_api.py
git commit -m "feat(plan): /draft/cashflow-projection route — monthly cashflow + retire-ready"
```

---

## Task 5: TS types + API client

**Files:**
- Modify: `ui/src/lib/api.ts`

- [ ] **Step 5.1: Add the response interfaces near `ProjectionResponse`**

```typescript
// Near the bottom of ui/src/lib/api.ts, after ProjectionResponse:

export interface CashflowPoint {
  months_out: number;
  age_years: number;
  date: string; // YYYY-MM
  portfolio_value_base_usd: number;
  portfolio_value_bear_usd: number;
  portfolio_value_bull_usd: number;
  portfolio_income_base_monthly_usd: number;
  portfolio_income_bear_monthly_usd: number;
  portfolio_income_bull_monthly_usd: number;
  pension_annuity_monthly_usd: number;
  pension_lump_available_usd: number;
  expenses_monthly_usd: number;
  surplus_base_monthly_usd: number;
}

export interface CashflowProjectionResponse {
  today_date: string;
  today_age_years: number;
  fx_usd_nis: number;
  retirement_age_assumed: number;
  retire_ready_age: number | null;
  retire_ready_months_out: number | null;
  series: CashflowPoint[];
  assumptions: {
    mu_nominal_annual: number;
    sigma_annual: number;
    real_return_annual: number;
    inflation_annual: number;
    mekadem: number;
    lump_pension_age: number;
    annuity_age: number;
    model_notes: string;
  };
}
```

- [ ] **Step 5.2: Add the client function in the `api` object**

```typescript
// Inside the api = { ... } block, near planDraftProjection:

  planDraftCashflowProjection: (
    userId: string,
    years = 30,
    retirementAge = 49,
  ) =>
    getJSON<CashflowProjectionResponse>(
      `/api/plan/draft/cashflow-projection?user_id=${encodeURIComponent(
        userId,
      )}&years=${years}&retirement_age=${retirementAge}`,
    ),
```

- [ ] **Step 5.3: Verify with tsc + lint**

```powershell
# From ui/:
npx tsc --noEmit
npx eslint src/lib/api.ts
```
Expected: both clean.

- [ ] **Step 5.4: Commit**

```bash
git add ui/src/lib/api.ts
git commit -m "feat(ui): add CashflowProjectionResponse types + client function"
```

---

## Task 6: UI — `CashflowProjectionChart` component + page integration

**Files:**
- Create: `ui/src/components/plan/cashflow-projection-chart.tsx`
- Modify: `ui/src/app/plan/page.tsx`
- Delete: `ui/src/components/plan/projection-chart.tsx`

- [ ] **Step 6.1: Write the new component**

```typescript
// ui/src/components/plan/cashflow-projection-chart.tsx
"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type CashflowProjectionResponse } from "@/lib/api";

interface CashflowProjectionChartProps {
  userId: string;
}

interface ChartRow {
  months_out: number;
  age_years: number;
  date: string;
  portfolio_base: number;
  portfolio_band: [number, number]; // [bear, bull] for the area
  pension_annuity: number;
  total_income: number; // portfolio_base + pension_annuity
  expenses: number;
}

function fmtUsd(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => fmtUsd(x)).join(" – ");
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtSignedUsd(n: number): string {
  const sign = n >= 0 ? "+" : "−";
  return `${sign}${fmtUsd(Math.abs(n))}`;
}

function fmtXTickAge(age: number): string {
  // age is decimal years; show only integer ages at every January equivalent.
  // We'll re-tick by year boundaries on the actual chart data.
  if (Number.isInteger(Math.round(age * 100) / 100)) {
    return `${Math.round(age)}`;
  }
  return "";
}

export function CashflowProjectionChart({ userId }: CashflowProjectionChartProps) {
  const [data, setData] = useState<CashflowProjectionResponse | null>(null);
  const [retirementAge, setRetirementAge] = useState<number>(49);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Overlay toggles
  const [showBand, setShowBand] = useState(true);
  const [showAnnuity, setShowAnnuity] = useState(true);
  const [showLumpMarker, setShowLumpMarker] = useState(true);
  const [showRetireReady, setShowRetireReady] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .planDraftCashflowProjection(userId, 30, retirementAge)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, retirementAge]);

  const rows = useMemo<ChartRow[]>(() => {
    if (!data) return [];
    return data.series.map((p) => ({
      months_out: p.months_out,
      age_years: p.age_years,
      date: p.date,
      portfolio_base: p.portfolio_income_base_monthly_usd,
      portfolio_band: [
        p.portfolio_income_bear_monthly_usd,
        p.portfolio_income_bull_monthly_usd,
      ],
      pension_annuity: p.pension_annuity_monthly_usd,
      total_income:
        p.portfolio_income_base_monthly_usd + p.pension_annuity_monthly_usd,
      expenses: p.expenses_monthly_usd,
    }));
  }, [data]);

  // The age at which the lump unlocks (always 60 per assumptions).
  const lumpAge = data?.assumptions.lump_pension_age ?? 60;
  const annuityAge = data?.assumptions.annuity_age ?? 67;

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as ChartRow | undefined;
    if (!row) return null;
    const delta = row.total_income - row.expenses;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)} · {row.date}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">portfolio income (base)</span>
          <span className="font-mono">{fmtUsd(row.portfolio_base)}/mo</span>
          {showAnnuity && (
            <>
              <span className="text-muted-foreground">pension annuity</span>
              <span className="font-mono">{fmtUsd(row.pension_annuity)}/mo</span>
            </>
          )}
          <span className="text-muted-foreground font-medium">total income</span>
          <span className="font-mono font-medium">{fmtUsd(row.total_income)}/mo</span>
          <span className="text-muted-foreground">expenses (inflated)</span>
          <span className="font-mono">{fmtUsd(row.expenses)}/mo</span>
          <span className={delta >= 0 ? "text-success font-medium" : "text-error font-medium"}>
            {delta >= 0 ? "surplus" : "shortfall"}
          </span>
          <span
            className={`font-mono font-medium ${
              delta >= 0 ? "text-success" : "text-error"
            }`}
          >
            {fmtSignedUsd(delta)}/mo
          </span>
        </div>
      </div>
    );
  };

  if (loading && !data) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle className="text-base">Monthly cashflow projection</CardTitle>
          <CardDescription>Loading…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (error || !data) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle className="text-base">Monthly cashflow projection</CardTitle>
          <CardDescription>{error ?? "No projection available."}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle className="text-base">Monthly cashflow projection · 30y</CardTitle>
        <CardDescription>
          When does projected monthly income cover expenses? Today: portfolio income{" "}
          <span className="font-mono">
            {fmtUsd(rows[0]?.portfolio_base ?? 0)}
          </span>
          /mo · expenses{" "}
          <span className="font-mono">{fmtUsd(rows[0]?.expenses ?? 0)}</span>
          /mo.{" "}
          {data.retire_ready_age != null ? (
            <>
              <span className="text-success font-medium">
                Retire-ready at age {data.retire_ready_age.toFixed(1)}
              </span>{" "}
              (assumed retirement age:{" "}
              <span className="font-mono">{retirementAge}</span>).
            </>
          ) : (
            <>
              <span className="text-error font-medium">
                No crossing in 30y at assumed retirement age{" "}
                {retirementAge}.
              </span>
            </>
          )}
          <br />
          <span className="text-[10px] font-mono opacity-70">
            Real-return drawdown (mu={data.assumptions.mu_nominal_annual},
            inflation={data.assumptions.inflation_annual},
            real={data.assumptions.real_return_annual.toFixed(3)}). Annuity at{" "}
            {annuityAge} via mekadem={data.assumptions.mekadem}. Lump unlock at{" "}
            {lumpAge}.
          </span>
        </CardDescription>
        {/* Retirement-age slider + overlay checkboxes */}
        <div className="mt-3 flex flex-wrap items-center gap-4 text-xs">
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">retirement age</span>
            <input
              type="range"
              min={data.today_age_years}
              max={70}
              step={1}
              value={retirementAge}
              onChange={(e) => setRetirementAge(Number(e.target.value))}
              className="w-40"
              aria-label="Retirement age"
            />
            <span className="font-mono w-8 text-right">{retirementAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showBand}
              onChange={(e) => setShowBand(e.target.checked)}
            />
            <span>±1σ band</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showAnnuity}
              onChange={(e) => setShowAnnuity(e.target.checked)}
            />
            <span>pension annuity @ {annuityAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showLumpMarker}
              onChange={(e) => setShowLumpMarker(e.target.checked)}
            />
            <span>lump marker @ {lumpAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showRetireReady}
              onChange={(e) => setShowRetireReady(e.target.checked)}
            />
            <span>retire-ready marker</span>
          </label>
        </div>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={380}>
          <ComposedChart
            data={rows}
            margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis
              dataKey="age_years"
              fontSize={11}
              tickFormatter={fmtXTickAge}
              domain={["dataMin", "dataMax"]}
              type="number"
              ticks={(() => {
                // Integer-age ticks at every 5 years, plus key ages 60 + 67.
                if (rows.length === 0) return [];
                const minAge = Math.floor(rows[0].age_years);
                const maxAge = Math.ceil(rows[rows.length - 1].age_years);
                const out: number[] = [];
                for (let a = minAge; a <= maxAge; a += 5) out.push(a);
                if (!out.includes(lumpAge)) out.push(lumpAge);
                if (!out.includes(annuityAge)) out.push(annuityAge);
                return out.sort((a, b) => a - b);
              })()}
            />
            <YAxis
              fontSize={10}
              tickFormatter={(v) => fmtUsd(v)}
              width={64}
            />
            <Tooltip content={renderTooltip} />

            {showBand && (
              <Area
                type="monotone"
                dataKey="portfolio_band"
                stroke="none"
                fill="#6366f1"
                fillOpacity={0.15}
                isAnimationActive={false}
                name="±1σ portfolio band"
              />
            )}
            <Line
              type="monotone"
              dataKey="portfolio_base"
              stroke="#6366f1"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="portfolio income (base)"
            />
            {showAnnuity && (
              <Line
                type="monotone"
                dataKey="pension_annuity"
                stroke="#10b981"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
                name="pension annuity"
              />
            )}
            <Line
              type="monotone"
              dataKey="total_income"
              stroke="#f59e0b"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              name="total income"
            />
            <Line
              type="monotone"
              dataKey="expenses"
              stroke="#f43f5e"
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
              name="expenses (inflating)"
            />
            {showLumpMarker && (
              <ReferenceLine
                x={lumpAge}
                stroke="#a3a3a3"
                strokeDasharray="3 3"
                label={{
                  value: `lump @ ${lumpAge}`,
                  position: "top",
                  fill: "#a3a3a3",
                  fontSize: 10,
                }}
              />
            )}
            {showAnnuity && (
              <ReferenceLine
                x={annuityAge}
                stroke="#10b981"
                strokeDasharray="3 3"
                label={{
                  value: `annuity @ ${annuityAge}`,
                  position: "top",
                  fill: "#10b981",
                  fontSize: 10,
                }}
              />
            )}
            {showRetireReady && data.retire_ready_age != null && (
              <ReferenceLine
                x={data.retire_ready_age}
                stroke="#f59e0b"
                strokeWidth={2}
                label={{
                  value: `retire-ready ${data.retire_ready_age.toFixed(1)}`,
                  position: "insideTopRight",
                  fill: "#f59e0b",
                  fontSize: 11,
                  fontWeight: 600,
                }}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 6.2: Wire it into the `/plan` page**

In `ui/src/app/plan/page.tsx`:

Find the import block (lines 7-19 region) and replace:
```typescript
import { ProjectionChart } from "@/components/plan/projection-chart";
```
with:
```typescript
import { CashflowProjectionChart } from "@/components/plan/cashflow-projection-chart";
```

Find the visualizations grid and replace:
```typescript
<ProjectionChart data={projection} />
```
with:
```typescript
<CashflowProjectionChart userId={USER_ID} />
```

Remove the `projP` line + the `projection` state + everything related:
- Remove `const projP = api.planDraftProjection(USER_ID, 10).catch(...)` from `refresh()`.
- Remove the corresponding await + `setProjection` call.
- Remove the `projection` useState declaration.
- Remove the `import { type ProjectionResponse, ... }` type import (search for `ProjectionResponse` and clean up).

> **Why this break is intentional:** the new chart owns its own data-fetching because `retirementAge` is internal state that re-fetches. Hoisting that to the page would couple unrelated state.

- [ ] **Step 6.3: Delete the old projection chart**

```bash
rm ui/src/components/plan/projection-chart.tsx
```

- [ ] **Step 6.4: Verify tsc + lint clean**

```powershell
# From ui/:
npx tsc --noEmit
npx eslint src/app/plan/page.tsx src/components/plan/cashflow-projection-chart.tsx src/lib/api.ts
```
Expected: both clean. If `tsc` complains about `projection` / `ProjectionResponse` unused, the cleanup in 6.2 missed a reference — find and remove.

- [ ] **Step 6.5: Visual smoke**

Restart the dev server if needed, then:
1. Open http://localhost:1337/plan
2. Confirm the new chart renders with X-axis labelled by age (43, 45, 50, …, 73), lump @ 60 and annuity @ 67 markers visible.
3. Drag the retirement-age slider — confirm the network shows debounced re-fetches and the chart updates.
4. Toggle each checkbox — confirm the corresponding line / band / marker shows/hides.
5. Hover at age 60 — tooltip should show the lump+portfolio+annuity breakdown.
6. Hover at age 67 — pension annuity should appear in the tooltip.

If no retire-ready age in 30y at the default retirement_age=49, the orange marker is absent and the header shows "No crossing in 30y" in red. Try dragging the slider higher to find a crossing.

- [ ] **Step 6.6: Commit**

```bash
git add ui/src/app/plan/page.tsx ui/src/components/plan/cashflow-projection-chart.tsx
git rm ui/src/components/plan/projection-chart.tsx
git commit -m "feat(plan): pivot projection chart to monthly cashflow with retirement-age slider"
```

---

## Task 7: Codex-tandem review of the projection math

**Files:**
- `argosy/services/cashflow_projection.py` (target of review)

**Why:** money math + retirement decisions = high blast-radius. The codex-tandem kit lives at `D:/Projects/financial-advisor/tools/codex-tandem/`. Layout A (Claude leads, Codex reviews).

- [ ] **Step 7.1: Dispatch Codex as reviewer on `cashflow_projection.py`**

From the repo root, dispatch via the single-codex pattern:

```python
import sys
sys.path.insert(0, "D:/Projects/financial-advisor/tools/codex-tandem/scripts")
from pathlib import Path
from engine_codex import run_codex

r = run_codex(
    node_dir=Path("D:/Projects/financial-advisor/argosy/services"),
    prompt="""Review cashflow_projection.py for an Israeli household retirement-cashflow projection. Specifically:

1. The kupat_pensia state machine: contributions stop at retirement_age, balance grows at real_return until age 67, then becomes annuity = balance / mekadem (default 200). Is this consistent with how kupat_pensia annuities actually work?

2. The severance (8.33%) is folded into kupat_pensia monthly contributions. Is this conservative or aggressive?

3. The lump bump at age 60: keren_hishtalmut + kupat_gemel balances are added to the portfolio at the lump unlock month. Does the portfolio compounding handle this correctly across the bull/base/bear bands?

4. The "real return = mu_nominal - inflation" assumption for portfolio drawdown income (matches the user's capital_preservation_returns_only style). Any subtle issues?

5. Numerical stability: 30-year monthly loop with compound interest + lognormal — any overflow / precision risk?

6. Edge cases: retirement_age before current age; retirement_age after age 67; zero balances.

Return findings as: FINDING [SEVERITY] — TOPIC: detail. Severity in {BLOCK, AMBER, YELLOW, NIT}.
""",
    agent_name="cashflow_math_review",
    role="reviewer",
)
print("verdict:", r.verdict_text[:2000])
print("cost:", r.cost, "tokens:", r.tokens)
```

- [ ] **Step 7.2: Triage findings**

For each finding:
- **BLOCK** — must fix before merging. Add a task to the plan + implement.
- **AMBER** — fix if the change is < 30 min; else document in `assumptions.model_notes` and move on.
- **YELLOW / NIT** — note in commit message, defer to follow-up.

- [ ] **Step 7.3: If fixes are needed, implement them in a follow-up commit**

```bash
git commit -m "fix(cashflow): address codex-tandem review findings — <topics>"
```

- [ ] **Step 7.4: Run the full backend test suite to confirm no regressions**

```bash
D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q
```
Expected: all green.

---

## Self-review summary

- **Goal/Architecture/Tech Stack:** present at top.
- **No placeholders:** every test has real code; every implementation has real code; every command is exact.
- **Type consistency:** `PensionState`, `HouseholdState`, `CashflowPoint`, `CashflowProjection`, `CashflowProjectionResponse` are used consistently across tasks 1-6.
- **Spec coverage:**
  - "Pivot to monthly cashflow" → Tasks 1-6.
  - "Pension lump at 60 + annuity at 67" → Task 1 (math) + Task 3 (orchestrator) + Task 6 (UI markers).
  - "Mekadem 200" → Task 1 + Task 3 default + Task 6 surfacing.
  - "Retirement-age slider" → Task 6.
  - "Codex-tandem on money math" → Task 7.
  - "Real-return drawdown style" → Task 1 (`portfolio_real_return_monthly`) + Task 3.
  - "Checkbox overlays" → Task 6.
  - "Retire-ready marker" → Task 1 (`detect_retire_ready`) + Task 6 (`ReferenceLine`).
- **Frequent commits:** seven commits across tasks 1-7.
- **TDD:** primitives and orchestrator both written test-first.
- **Risks called out:** the closed-form-vs-iterative portfolio path question in Task 3, flagged for codex-tandem in Task 7.

---

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-05-27-cashflow-projection-pivot.md`.
