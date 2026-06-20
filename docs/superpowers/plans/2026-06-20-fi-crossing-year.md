# Phase 1b — canonical FI-crossing year Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Checkbox (`- [ ]`) steps.

**Goal:** Publish ONE canonical `retirement.fi_crossing_year` figure, derived deterministically from the resolver's own numbers and **reconciled with the FI-margin verdict by construction** — so no surface (esp. the trajectory table) can say "FI crossed in 2026" while the plan says FI is not yet reached (the live pv56/pv57 reader BLOCKer).

**Architecture:** A pure money-math function `fi_crossing_year(*, liquid_now, fi_total, real_return, annual_savings, current_year)` in `argosy/services/fi_crossing.py` that returns the first calendar year the future value of current liquid net worth plus a real-savings annuity reaches the FI total-capital target. The resolver calls it from already-resolved figures and publishes the year; the registry owns it (Retirement). Reconciliation invariant: margin ≥ 0 → crossing = current year ("reached"); margin < 0 → crossing strictly in the future.

**Tech Stack:** Python 3.12, pytest. Deterministic future-value-of-annuity in REAL terms (the resolver's `required_real_yield`/`return_assumption` and FI target are real, so the projection is real — no inflation double-count).

**Methodology (to be codex-reviewed BEFORE build):** real future value after `n` years =
`liquid_now*(1+r)^n + annual_savings * ((1+r)^n - 1)/r` (r = real return; r=0 handled as `liquid_now + annual_savings*n`). `fi_crossing_year` = `current_year + n` for the smallest integer `n ≥ 0` with FV ≥ `fi_total`. Capped at a horizon (e.g. current_year+60); beyond → `None` (never reached on this trajectory). This is a TRAJECTORY crossing reconciled with the margin — NOT "derived from the margin alone" (codex's earlier caution).

**Scope:** only `retirement.fi_crossing_year`. The trajectory-table render cutover (table reads this figure) is Phase 1c. Scenario bands (bear/base/bull crossing) are a later enhancement; this ships the base-trajectory crossing.

---

### Task 1: pure `fi_crossing_year` money-math

**Files:**
- Create: `argosy/services/fi_crossing.py`
- Test: `tests/test_fi_crossing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fi_crossing.py
import pytest
from argosy.services.fi_crossing import fi_crossing_year


def test_already_reached_is_current_year():
    # liquid already >= target -> crosses now.
    assert fi_crossing_year(liquid_now=12_000_000, fi_total=11_836_133,
                            real_return=0.03, annual_savings=300_000,
                            current_year=2026) == 2026


def test_future_crossing_with_growth_and_savings():
    # short of target now -> future year; grows via return + savings.
    yr = fi_crossing_year(liquid_now=11_668_397, fi_total=11_836_133,
                          real_return=0.03, annual_savings=300_000,
                          current_year=2026)
    assert yr is not None and yr > 2026   # MUST be future (margin negative now)
    # one year of 3% growth + 300k savings on 11.67M clears 11.84M -> 2027.
    assert yr == 2027


def test_zero_return_uses_linear_savings():
    yr = fi_crossing_year(liquid_now=11_000_000, fi_total=11_900_000,
                          real_return=0.0, annual_savings=300_000,
                          current_year=2026)
    # need 900k / 300k = 3 years -> 2029.
    assert yr == 2029


def test_never_reached_within_horizon_returns_none():
    # tiny base, no savings, target unreachable -> None within horizon.
    assert fi_crossing_year(liquid_now=1_000, fi_total=10_000_000,
                            real_return=0.0, annual_savings=0.0,
                            current_year=2026, horizon_years=60) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fi_crossing.py -v`
Expected: FAIL — `ModuleNotFoundError: argosy.services.fi_crossing`.

- [ ] **Step 3: Implement**

```python
# argosy/services/fi_crossing.py
"""Deterministic FI-capital crossing year.

The first calendar year the FUTURE VALUE of current liquid net worth plus a
real-savings annuity reaches the FI total-capital target. All inputs are REAL
(the resolver's return + FI target are real), so the projection is real — no
inflation double-count. Reconciled with the FI margin by construction: if liquid
already clears the target the crossing is the current year; otherwise it is
strictly in the future. Pure: no DB, no LLM.
"""
from __future__ import annotations


def _future_value(liquid_now: float, real_return: float,
                  annual_savings: float, n: int) -> float:
    if n <= 0:
        return liquid_now
    if real_return == 0.0:
        return liquid_now + annual_savings * n
    growth = (1.0 + real_return) ** n
    return liquid_now * growth + annual_savings * (growth - 1.0) / real_return


def fi_crossing_year(
    *, liquid_now: float, fi_total: float, real_return: float,
    annual_savings: float, current_year: int, horizon_years: int = 60,
) -> int | None:
    """Smallest year >= current_year whose projected real net worth >= fi_total.
    None when the target is not reached within ``horizon_years`` on this
    trajectory. Already-at-or-above-target -> current_year."""
    for n in range(0, horizon_years + 1):
        if _future_value(liquid_now, real_return, annual_savings, n) >= fi_total:
            return current_year + n
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fi_crossing.py -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add argosy/services/fi_crossing.py tests/test_fi_crossing.py
git commit -m "feat(retirement): deterministic FI-capital crossing-year money-math"
```

---

### Task 2: resolver publishes `retirement.fi_crossing_year`

**Files:**
- Modify: `argosy/services/plan_numeric_resolver.py`
- Test: `tests/test_plan_numeric_resolver.py`

**Context:** publish from already-resolved figures: `portfolio.liquid_net_worth_nis`, `retirement.fi_total_capital_nis`, `retirement.return_assumption_pct` (real), `savings.annual_net_nis`. Compute via `fi_crossing_year` with `current_year = date.today().year`. Reconciliation invariant enforced by the math: if liquid >= fi_total it returns current year. Pending when any input is pending.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_plan_numeric_resolver.py
def test_fi_crossing_year_is_future_when_margin_negative(session):
    """The crossing year must be a FUTURE year whenever the FI margin is
    negative (the live pv56/pv57 reader BLOCKer: a table said 'crossed 2026'
    while the plan said not-yet-reached)."""
    from datetime import date as _date
    _seed_all(session)   # seeds liquid net worth short of the FI total target
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    margin = resolved.get("retirement.fi_margin_signed_nis")
    crossing = resolved.get("retirement.fi_crossing_year")
    assert crossing.status == "resolved"
    assert crossing.unit == "year"
    if margin.status == "resolved" and margin.value is not None and margin.value < 0:
        assert crossing.value > _date.today().year, (
            "FI not reached -> crossing must be a future year, never the current year")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_fi_crossing_year_is_future_when_margin_negative -v`
Expected: FAIL — `crossing.status == "pending"`.

- [ ] **Step 3: Implement `_apply_fi_crossing_year`** in `plan_numeric_resolver.py`, called in `resolve_plan_numbers` AFTER `_apply_fi_margin` (so the inputs exist):

```python
def _apply_fi_crossing_year(values):
    """Publish retirement.fi_crossing_year from already-resolved figures.
    Reconciled with the FI margin by construction (the money-math returns the
    current year only when liquid already clears the target). Pending when any
    input is missing — never a guess."""
    from datetime import date as _date
    from argosy.services.fi_crossing import fi_crossing_year
    key = "retirement.fi_crossing_year"

    def _r(k):
        rv = values.get(k)
        return rv.value if (rv and rv.status == "resolved" and rv.value is not None) else None

    liquid = _r("portfolio.liquid_net_worth_nis")
    fi_total = _r("retirement.fi_total_capital_nis")
    real_return = _r("retirement.return_assumption_pct")
    savings = _r("savings.annual_net_nis")
    if None in (liquid, fi_total, real_return, savings):
        values[key] = ResolvedValue.pending(key, "year", "fi_crossing inputs pending")
        return
    yr = fi_crossing_year(
        liquid_now=float(liquid), fi_total=float(fi_total),
        real_return=float(real_return), annual_savings=float(savings),
        current_year=_date.today().year)
    if yr is None:
        values[key] = ResolvedValue.pending(key, "year", "FI target not reached within horizon")
        return
    values[key] = ResolvedValue(
        key=key, value=float(yr), unit="year", status="resolved",
        source_locator="fi_crossing.fi_crossing_year",
        confidence="HIGH",
        formula="first year FV(liquid, real return, savings annuity) >= FI total capital")
```

Add `"retirement.fi_crossing_year": "year"` to `_KEY_UNITS`; call `_apply_fi_crossing_year(values)` after `_apply_fi_margin(values)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_fi_crossing_year_is_future_when_margin_negative -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_numeric_resolver.py tests/test_plan_numeric_resolver.py
git commit -m "feat(resolver): publish reconciled FI-crossing year"
```

---

### Task 3: registry owns `retirement.fi_crossing_year`

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context:** `year` is a new unit. Add an explicit OWNER_MAP entry (Retirement, formula_result, HIGH). The `retirement.` prefix rule already covers it, but an explicit entry sets the right kind/materiality.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
def test_fi_crossing_year_owned_by_retirement():
    spec = owner_for("retirement.fi_crossing_year")
    assert spec.owner is OwnerRole.RETIREMENT_FI
    assert spec.kind is FigureKind.FORMULA_RESULT
    assert spec.uncategorized is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_fi_crossing_year_owned_by_retirement -v`
Expected: FAIL — the prefix rule gives MEDIUM, but kind FORMULA_RESULT already; the explicit-entry assertion drives adding it (and confirms HIGH materiality below).

- [ ] **Step 3: Add the OWNER_MAP entry**

```python
# beside the other retirement.* entries in OWNER_MAP:
    "retirement.fi_crossing_year": OwnerSpec(_R, _FR, _HI),
```

- [ ] **Step 4: Run test + full registry file (incl. live smoke)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -v`
Expected: PASS (the live smoke now includes fi_crossing_year, owned + resolved).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): own retirement.fi_crossing_year"
```

---

## Self-Review

- **Spec coverage:** implements `retirement.fi_crossing_year` (spec Phase-1 item 3) — reconciled with the FI margin by construction (codex's earlier caution that "derived from margin alone" is a false invariant is addressed: this is a trajectory crossing — FV of liquid + savings annuity — that the margin sign bounds).
- **Money-math to codex-review BEFORE build:** the FV-annuity model, real-vs-nominal consistency, the r=0 branch, and the horizon cap.
- **No magic numbers:** every input is an existing resolver figure; horizon cap (60y) matches the existing projection horizon convention.
- **Placeholder scan:** complete code + commands + expected output in every step.
- **Type consistency:** `fi_crossing_year` signature identical across Tasks 1-2; new unit `"year"`; OWNER_MAP `_R/_FR/_HI` aliases as defined in figure_registry.
