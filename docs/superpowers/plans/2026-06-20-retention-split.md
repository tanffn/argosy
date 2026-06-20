# Phase 1b — canonical RSU retention split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Checkbox steps.

**Goal:** Publish the TWO canonical, distinctly-labeled RSU net-retention rates — at-vest ordinary (~47%) and capital-track Section-102 long-term (~72%) — as owned resolver figures, so prose can never present them as one conflated "retention" that contradicts itself (the recurring pv55/pv56/pv57 reader AMBER/BLOCKer).

**Architecture:**
- `tax.retention_at_vest_pct` — sourced from `equity_comp_analyst` base scenario `net_retention_pct` (the after marginal+surtax rate on vest income). Resolver convention is a FRACTION (0-1), but equity_comp stores `net_retention_pct` as 0-100 → divide by 100.
- `tax.retention_capital_track_pct` — derived from the Israeli Section-102 capital-track long-term rate (statutory 25% CGT + 3% surtax in the high-income zone) as a DOCUMENTED policy constant `SECTION_102_LT_CGT_RATE = 0.28` → retention `1 - 0.28 = 0.72`. Sourced to `domain_knowledge/tax/israel/` (a statutory parameter, like the structural ages — not a magic number).
- Registry owns both (TAX owner), each a distinct labeled figure.

**Tech Stack:** Python 3.12, pytest. No MC; deterministic.

**Methodology (codex-review BEFORE build — tax-rate correctness):** confirm the Section-102 capital-track long-term effective rate (25% base + 3% surtax = 28% for high earners; verify against `domain_knowledge/tax/israel/`), and that at-vest `net_retention_pct` (0-100) maps to a 0-1 fraction.

**Scope:** only the two retention figures. The prose/ledger render cutover (sections read these figures + label them) is Phase 1c.

---

### Task 1: `tax.retention_at_vest_pct` from equity_comp

**Files:**
- Modify: `argosy/services/plan_numeric_resolver.py` (extend `_resolve_equity_comp_analyst` to ALSO emit the at-vest retention; add key to `_KEY_UNITS`)
- Test: `tests/test_plan_numeric_resolver.py`

**Context:** `_resolve_equity_comp_analyst` already parses `EquityCompAnalystOutput` and reads `scenarios[known_grants_only]`. Its years carry `net_retention_pct` (0-100, e.g. 47.0). Emit `tax.retention_at_vest_pct = net_retention_pct/100` from the base scenario's representative (first) year. The existing `_RESOLVERS` registration for `equity_comp_analyst` lists the keys it owns — add the new key there too.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_plan_numeric_resolver.py
def test_retention_at_vest_pct_from_equity_comp(session):
    """At-vest ordinary retention is sourced from equity_comp net_retention_pct
    (0-100) as a 0-1 fraction."""
    _seed_all(session)   # _equity_comp_json seeds net_retention_pct=47.0
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    r = resolved.get("tax.retention_at_vest_pct")
    assert r.status == "resolved"
    assert r.unit == "pct"
    assert r.value == pytest.approx(0.47)   # 47.0 / 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_retention_at_vest_pct_from_equity_comp -v`
Expected: FAIL — `status == "pending"` (key not produced).

- [ ] **Step 3: Implement.** In `_resolve_equity_comp_analyst`, after computing the existing `savings.annual_net_nis`, also append:

```python
    # At-vest ORDINARY-income net retention (after marginal IL + surtax), from the
    # base scenario's representative year. Stored 0-100 in the model -> resolver
    # FRACTION convention (0-1). Distinct from the capital-track rate (Task 2).
    ret_key = "tax.retention_at_vest_pct"
    ret_val = None
    years = getattr(base, "years", None) or []
    if years:
        nrp = _to_float(getattr(years[0], "net_retention_pct", None))
        if nrp is not None:
            ret_val = nrp / 100.0
    out_values.append(
        ResolvedValue(key=ret_key, value=ret_val, unit="pct",
                      status="resolved" if ret_val is not None else "pending",
                      source_locator="equity_comp_analyst.scenarios[known_grants_only].years[0].net_retention_pct",
                      agent_report_id=report_id, confidence="MEDIUM",
                      formula="at-vest ordinary-income net retention (1 - marginal - surtax)")
        if ret_val is not None else
        ResolvedValue.pending(ret_key, "pct", "equity_comp net_retention_pct missing", agent_report_id=report_id)
    )
```

(Match the function's actual return mechanism — read it: it returns a `list[ResolvedValue]`; append to that list, named `out_values` here as a placeholder for whatever the function accumulates.) Add `tax.retention_at_vest_pct` to the `equity_comp_analyst` key tuple in `_RESOLVERS` and to `_KEY_UNITS` (`"pct"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_retention_at_vest_pct_from_equity_comp -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_numeric_resolver.py tests/test_plan_numeric_resolver.py
git commit -m "feat(resolver): publish at-vest ordinary RSU retention rate"
```

---

### Task 2: `tax.retention_capital_track_pct` from the statutory Section-102 rate

**Files:**
- Modify: `argosy/services/plan_numeric_resolver.py` (policy constant + `_apply_capital_track_retention`; `_KEY_UNITS`)
- Test: `tests/test_plan_numeric_resolver.py`

**Context:** the Section-102 capital-track long-term rate is statutory (25% base CGT + 3% high-income surtax = 28% in the surtax zone). Define it as a single-sourced policy constant (sourced to domain knowledge), retention = 1 - rate.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_plan_numeric_resolver.py
def test_retention_capital_track_pct_from_statutory_rate(session):
    _seed_all(session)
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    r = resolved.get("tax.retention_capital_track_pct")
    assert r.status == "resolved"
    assert r.unit == "pct"
    assert r.value == pytest.approx(0.72)   # 1 - 0.28 (25% CGT + 3% surtax)
    # and it is DISTINCT from the at-vest rate (the whole point)
    at_vest = resolved.get("tax.retention_at_vest_pct")
    assert abs(r.value - at_vest.value) > 0.1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_retention_capital_track_pct_from_statutory_rate -v`
Expected: FAIL — pending.

- [ ] **Step 3: Implement.**

```python
# near the other policy constants (e.g. PENSION_UNLOCK_AGE):
# Israeli Section-102 capital-track long-term effective rate in the high-income
# surtax zone: 25% base CGT + 3% surtax. Statutory policy parameter (domain
# knowledge: domain_knowledge/tax/israel/), NOT a derived/guessed number.
SECTION_102_LT_CGT_RATE = 0.28


def _apply_capital_track_retention(values):
    """Publish tax.retention_capital_track_pct = 1 - Section-102 long-term rate.
    Distinct from the at-vest ordinary rate — two legitimate treatments."""
    key = "tax.retention_capital_track_pct"
    values[key] = ResolvedValue(
        key=key, value=1.0 - SECTION_102_LT_CGT_RATE, unit="pct", status="resolved",
        source_locator="plan_numeric_resolver.SECTION_102_LT_CGT_RATE (domain_knowledge/tax/israel)",
        confidence="HIGH",
        formula="1 - Section-102 capital-track long-term rate (25% CGT + 3% surtax)")
```

Add `"tax.retention_capital_track_pct": "pct"` to `_KEY_UNITS`; call `_apply_capital_track_retention(values)` in `resolve_plan_numbers` near the other `_apply_*` constants.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_retention_capital_track_pct_from_statutory_rate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_numeric_resolver.py tests/test_plan_numeric_resolver.py
git commit -m "feat(resolver): publish capital-track Section-102 retention rate"
```

---

### Task 3: registry owns both retention figures (TAX)

**Files:**
- Modify: `argosy/quality/figure_registry.py` (OWNER_MAP)
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
def test_retention_rates_owned_by_tax_and_distinct():
    at_vest = owner_for("tax.retention_at_vest_pct")
    cap = owner_for("tax.retention_capital_track_pct")
    assert at_vest.owner is OwnerRole.TAX and cap.owner is OwnerRole.TAX
    assert at_vest.uncategorized is False and cap.uncategorized is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_retention_rates_owned_by_tax_and_distinct -v`
Expected: FAIL — caught by the `tax.` prefix rule (TAX owner) but without explicit kind/materiality; the assertion on explicit entries drives adding them.

- [ ] **Step 3: Add OWNER_MAP entries**

```python
    "tax.retention_at_vest_pct": OwnerSpec(_T, _FR, _HI),
    "tax.retention_capital_track_pct": OwnerSpec(_T, _AS, _HI),
```

- [ ] **Step 4: Run test + full registry file (incl. live smoke)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -v`
Expected: PASS (both retention keys owned + resolved in the live smoke).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): own the two RSU retention rates (TAX)"
```

---

## Self-Review

- **Spec coverage:** implements the retention split (spec Phase-1 item 3) — two distinctly-labeled rates so prose cannot conflate them.
- **No magic number:** the capital-track rate is a STATUTORY policy constant sourced to domain knowledge (like the structural ages), not a guess; the at-vest rate is sourced from the equity_comp analyst output.
- **Unit convention:** at-vest divides the analyst's 0-100 `net_retention_pct` to the resolver's 0-1 fraction (a real source of bugs — explicitly tested == 0.47).
- **To codex-review BEFORE build:** the 28% Section-102 rate value (verify vs `domain_knowledge/tax/israel/`) and the 0-100→0-1 conversion.
- **Placeholder scan:** Task 1 Step 3 notes the exact return-mechanism must match the real `_resolve_equity_comp_analyst` (it returns a list) — a "verify against real code" instruction, not a placeholder.
