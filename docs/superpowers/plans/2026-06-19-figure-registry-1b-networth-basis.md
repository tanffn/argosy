# Phase 1b — Canonical net-worth total basis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Expose the third canonical net-worth basis — total-incl-residence — as a single-sourced resolver figure owned by the Balance-Sheet role, so the Wealth Dashboard (₪14.05M) and the plan body (₪11.87M investable / ₪11.69M liquid) read ONE labeled set instead of contradicting (the live pv57 reader BLOCKer).

**Architecture:** DRY-extract the net-worth-incl-real-estate computation that currently lives in `wealth_dashboard._net_worth` into a shared pure helper `argosy/services/net_worth_bases.py`; `wealth_dashboard` calls it (behavior unchanged — proven by its existing tests), and `plan_numeric_resolver` calls it to publish `portfolio.total_net_worth_incl_residence_nis`. The figure registry already owns the liquid + investable bases; add an OWNER_MAP entry + `basis="total"` for the new key.

**Tech Stack:** Python 3.12, pytest. No new derivation math — the total-net-worth formula already exists in `wealth_dashboard._net_worth`; this extracts it to one place and exposes it as a resolver figure.

**Risk:** money-math + touches the heavily-used `wealth_dashboard`. Mitigation: extraction must be behavior-preserving (the existing `tests/test_*dashboard*`/`test_real_estate*` suite must stay green), and the new resolver figure is codex-reviewed before merge.

**Scope note:** Only the total-incl-residence basis. FI-crossing year, the retention split, and the NVDA pool/slice figures are separate plans (each needs its own derivation + codex money-math review). The render cutover (dashboard reads the resolver figure) is Phase 1c.

---

### Task 1: Extract the net-worth computation into a shared helper (behavior-preserving)

**Files:**
- Create: `argosy/services/net_worth_bases.py`
- Modify: `argosy/services/wealth_dashboard.py` (`_net_worth` delegates to the helper)
- Test: `tests/test_net_worth_bases.py`

- [ ] **Step 1: Read `argosy/services/wealth_dashboard.py::_net_worth` in full** (it spans ~`_net_worth` def to its return). Note its exact inputs (`snapshot`, `fx_usd_nis`, `session`, `user_id`), the real-estate-stub swap, the `loan_override`/`value_override` from `real_estate_ledger`, and the `(nw_nis, nw_usd)` return.

- [ ] **Step 2: Write the failing test** — assert the extracted helper reproduces `_net_worth` for a representative snapshot.

```python
# tests/test_net_worth_bases.py
from argosy.services.net_worth_bases import total_net_worth_incl_residence


def test_total_equals_investable_plus_real_estate_net_equity():
    # investable base (ex real estate stub) + a single property's net equity.
    class _Snap:
        totals_json = '{"total_usd_value_k": 4000.0}'   # $4.00M investable incl. RE stub
        positions_json = '[{"asset_type":"real estate","usd_value_k":69.0}]'  # legacy stub
        real_estate_json = '[{"location":"Home","currency":"USD","role":"Home","value_local":800.0},'\
                           ' {"location":"Home","currency":"USD","role":"Loan","value_local":300.0}]'
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=_Snap(), fx_usd_nis=3.0, session=None, user_id=None)
    # (4000 - 69 stub + (800-300) net equity) * 1000 * 3.0 NIS  ... in USD-k then NIS
    # investable_usd_k = 4000 - 69 = 3931; + net equity 500 = 4431 usd_k
    assert nw_usd == 4_431_000.0
    assert nw_nis == 4_431_000.0 * 3.0
```

(Adjust the exact arithmetic in Step 4 to match `_net_worth`'s real formula once read; the test asserts the helper == the formula.)

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_net_worth_bases.py -v`
Expected: FAIL — `ModuleNotFoundError: argosy.services.net_worth_bases`.

- [ ] **Step 4: Move the body of `_net_worth` into the new helper**, signature `total_net_worth_incl_residence(*, snapshot, fx_usd_nis, session=None, user_id=None) -> tuple[float|None, float|None]`. Copy the logic VERBATIM (stub swap, ledger overrides, equity computation). Then in `wealth_dashboard.py`, replace `_net_worth`'s body with a one-line delegation `return total_net_worth_incl_residence(snapshot=snapshot, fx_usd_nis=fx_usd_nis, session=session, user_id=user_id)`. Adjust the Step-2 test's expected numbers to the real formula.

- [ ] **Step 5: Run the new test AND the existing dashboard/real-estate suite (behavior preserved)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_net_worth_bases.py tests/ -k "dashboard or real_estate or wealth" -v`
Expected: PASS (the dashboard's existing tests prove the extraction changed nothing).

- [ ] **Step 6: Commit**

```bash
git add argosy/services/net_worth_bases.py argosy/services/wealth_dashboard.py tests/test_net_worth_bases.py
git commit -m "refactor(net-worth): extract total-incl-residence into a shared helper"
```

---

### Task 2: Resolver publishes `portfolio.total_net_worth_incl_residence_nis`

**Files:**
- Modify: `argosy/services/plan_numeric_resolver.py` (add `_apply_total_net_worth`, register the key in `_KEY_UNITS`)
- Test: `tests/test_plan_numeric_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_plan_numeric_resolver.py
def test_total_net_worth_incl_residence_resolved(session):
    """The total-incl-residence basis is a resolved nis figure, DISTINCT from the
    investable + liquid bases (the three labeled net-worth bases)."""
    # _seed_all already seeds a snapshot with positions + real estate.
    _seed_all(session)
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    total = resolved.get("portfolio.total_net_worth_incl_residence_nis")
    investable = resolved.get("portfolio.net_worth_nis")
    assert total.status == "resolved"
    assert total.unit == "nis"
    # total (incl. primary residence net equity) >= investable (which excludes it)
    assert total.value >= investable.value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_total_net_worth_incl_residence_resolved -v`
Expected: FAIL — `total.status == "pending"` (key not produced).

- [ ] **Step 3: Implement `_apply_total_net_worth`** in `plan_numeric_resolver.py`, called from the core `resolve_plan_numbers` flow alongside `_resolve_net_worth`. It loads the latest snapshot + BOI FX (same as `_resolve_net_worth`), calls `total_net_worth_incl_residence`, and registers:

```python
def _apply_total_net_worth(session, user_id, values):
    """Register the total net worth INCL. primary-residence equity — the third
    canonical basis (alongside investable portfolio.net_worth_nis and liquid
    portfolio.liquid_net_worth_nis). Single-sourced from the shared
    net_worth_bases helper the Wealth Dashboard also uses, so the dashboard and
    the plan body cannot diverge. Pending (never a guess) when the snapshot is absent."""
    from argosy.services.net_worth_bases import total_net_worth_incl_residence
    key = "portfolio.total_net_worth_incl_residence_nis"
    try:
        snap = _latest_snapshot(session, user_id)   # same helper _resolve_net_worth uses
        fx = _boi_fx(session)                        # same BOI rate _resolve_net_worth uses
        nw_nis, _ = total_net_worth_incl_residence(
            snapshot=snap, fx_usd_nis=fx, session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — never break the resolver
        log.warning("plan_numeric_resolver.total_net_worth_failed err=%s", exc)
        nw_nis = None
    if nw_nis is None:
        values[key] = ResolvedValue.pending(key, "nis", "no snapshot for total net worth")
        return
    values[key] = ResolvedValue(
        key=key, value=float(nw_nis), unit="nis", status="resolved",
        source_locator="net_worth_bases.total_net_worth_incl_residence",
        confidence="HIGH",
        formula="investable net worth + real-estate net equity (incl. primary residence)")
```

(Use the actual snapshot/FX accessors `_resolve_net_worth` uses — read it to copy the exact calls; do not invent new ones.) Add `"portfolio.total_net_worth_incl_residence_nis": "nis"` to `_KEY_UNITS`, and call `_apply_total_net_worth(session, user_id, values)` in `resolve_plan_numbers` near the other `_apply_*` net-worth calls.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_total_net_worth_incl_residence_resolved -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_numeric_resolver.py tests/test_plan_numeric_resolver.py
git commit -m "feat(resolver): publish total-incl-residence net-worth basis"
```

---

### Task 3: Registry owns the new basis (labeled)

**Files:**
- Modify: `argosy/quality/figure_registry.py` (`OWNER_MAP`)
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
def test_total_net_worth_basis_owned_and_labeled():
    spec = owner_for("portfolio.total_net_worth_incl_residence_nis")
    assert spec.owner is OwnerRole.BALANCE_SHEET
    assert spec.basis == "total"
    assert spec.uncategorized is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_total_net_worth_basis_owned_and_labeled -v`
Expected: FAIL — falls to the `portfolio.` prefix rule (basis is None).

- [ ] **Step 3: Add the explicit OWNER_MAP entry**

```python
# in OWNER_MAP, beside the other portfolio.* entries:
    "portfolio.total_net_worth_incl_residence_nis": OwnerSpec(_B, _FR, _HI, basis="total"),
```

- [ ] **Step 4: Run test to verify it passes + the live smoke still clean**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -v`
Expected: PASS (incl. the live-resolver coverage test — the new key is now owned + resolves).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): own the total-incl-residence net-worth basis"
```

---

## Self-Review

- **Spec coverage:** implements the `net_worth.total_incl_residence_nis` figure from spec Phase-1 item 3 (the three labeled net-worth bases) + its ownership. The other item-3 figures (FI-crossing, retention split, pool/slice) and the render cutover (1c) are explicitly deferred — stated in Scope.
- **DRY:** the net-worth-incl-RE formula now lives in ONE helper; both the dashboard and the resolver call it — so the ₪14.05M (dashboard) and the resolver figure are the SAME number by construction.
- **Behavior-preserving:** Task 1 keeps the dashboard's existing tests green (the extraction changes no behavior).
- **No invented math:** the formula is copied verbatim from `_net_worth`; the resolver reuses `_resolve_net_worth`'s snapshot/FX accessors.
- **Placeholder scan:** Task 1 Step 2/4 note the arithmetic must be matched to the real `_net_worth` formula once read — this is a deliberate "verify against the real code" instruction, not a placeholder; the helper is asserted equal to the existing formula.
