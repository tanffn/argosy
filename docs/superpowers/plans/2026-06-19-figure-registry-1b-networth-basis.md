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

- [ ] **Step 2: Write the failing test.** CODEX FIX: `compute_real_estate_equity` treats `value_local` as LOCAL CURRENCY UNITS (not k); the existing `_net_worth` converts via `/1000` to USD-k internally. Do NOT hardcode an arithmetic guess — assert the helper equals what the current `_net_worth` logic produces. Test the real edge behaviors codex flagged: stub swap, ledger loan/value overrides, snapshot-FX vs caller-FX for RE conversion, malformed JSON fallback, and `(None,None)` on missing/nonpositive total.

```python
# tests/test_net_worth_bases.py
from argosy.services.net_worth_bases import total_net_worth_incl_residence


class _Snap:
    def __init__(self, totals, positions="[]", real_estate="[]", fx=3.0):
        self.totals_json = totals
        self.positions_json = positions
        self.real_estate_json = real_estate
        self.fx_usd_nis = fx


def test_total_swaps_legacy_re_stub_for_full_net_equity():
    # value_local is LOCAL units. Home 800,000 USD - Loan 300,000 USD = 500,000 net equity.
    snap = _Snap(
        totals='{"total_usd_value_k": 4000.0}',                       # $4.00M incl. legacy stub
        positions='[{"asset_type":"real estate","currency":"USD","usd_value_k":69.0}]',
        real_estate='[{"location":"Home","currency":"USD","role":"Home","value_local":800000.0},'
                    ' {"location":"Home","currency":"USD","role":"Loan","value_local":300000.0}]',
    )
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=snap, fx_usd_nis=3.0, session=None, user_id=None)
    # base 4,000,000 USD - 69,000 stub + 500,000 net equity = 4,431,000 USD; ×3.0 = NIS.
    assert nw_usd == 4_431_000.0
    assert nw_nis == 4_431_000.0 * 3.0


def test_missing_total_returns_none():
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=_Snap(totals="{}"), fx_usd_nis=3.0, session=None, user_id=None)
    assert nw_nis is None and nw_usd is None


def test_malformed_real_estate_json_falls_back_to_base():
    snap = _Snap(totals='{"total_usd_value_k": 1000.0}', real_estate="not json")
    nw_nis, nw_usd = total_net_worth_incl_residence(
        snapshot=snap, fx_usd_nis=3.0, session=None, user_id=None)
    assert nw_usd == 1_000_000.0  # no RE applied; base preserved
```

CONFIRM the expected numbers against the real `_net_worth` body in Step 4 (the helper IS that body); if the current logic differs (e.g. `usd_value_k` units, the `/1000` placement), adjust the EXPECTED values to match the existing behavior — the contract is "helper == current `_net_worth`", proven additionally by Step 5 running the dashboard's own suite.

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

CODEX FIX: the existing `_seed_all` seeds only `totals_json` + `fx_usd_nis` — NO `positions_json`/`real_estate_json` — so a test relying on it could pass without exercising residence inclusion. Seed a snapshot WITH a real_estate row explicitly, and assert PARITY with the shared helper on the same snapshot (not a `>=` inequality, which codex correctly rejected — RE net equity can be below the legacy stub or negative).

```python
# append to tests/test_plan_numeric_resolver.py
def test_total_net_worth_incl_residence_matches_helper(session):
    """The total-incl-residence basis is resolved and EQUALS the shared
    net_worth_bases helper on the same snapshot (single-source parity — the
    dashboard reads the same helper, so they cannot diverge)."""
    from datetime import date as _date
    from decimal import Decimal as _Dec
    import json as _json
    from argosy.state.models import PortfolioSnapshotRow, FxRate
    from argosy.services.net_worth_bases import total_net_worth_incl_residence

    session.add(FxRate(date=_date.today(), currency="USD", rate=_Dec("3.0"), source="boi"))
    snap = PortfolioSnapshotRow(
        user_id="ariel", imported_at=__import__("datetime").datetime(2026, 6, 2),
        snapshot_date=_date.today(), fx_usd_nis=3.0,
        totals_json=_json.dumps({"total_usd_value_k": 4000.0}),
        positions_json=_json.dumps([
            {"symbol": "VOO", "currency": "USD", "usd_value_k": 3931.0, "asset_type": "ETF"},
            {"asset_type": "real estate", "currency": "USD", "usd_value_k": 69.0},
        ]),
        real_estate_json=_json.dumps([
            {"location": "Home", "currency": "USD", "role": "Home", "value_local": 800000.0},
            {"location": "Home", "currency": "USD", "role": "Loan", "value_local": 300000.0},
        ]),
    )
    session.add(snap)
    session.commit()

    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    total = resolved.get("portfolio.total_net_worth_incl_residence_nis")
    assert total.status == "resolved" and total.unit == "nis"
    # exact parity with the shared helper on the SAME snapshot (BOI rate 3.0).
    expect_nis, _ = total_net_worth_incl_residence(
        snapshot=snap, fx_usd_nis=3.0, session=session, user_id="ariel")
    assert total.value == pytest.approx(expect_nis)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_numeric_resolver.py::test_total_net_worth_incl_residence_matches_helper -v`
Expected: FAIL — `total.status == "pending"` (key not produced).

- [ ] **Step 3: Implement `_apply_total_net_worth`** in `plan_numeric_resolver.py`. CODEX FIX: there is NO `_latest_snapshot`/`_boi_fx`. Use the EXACT pattern `_resolve_net_worth` uses (`plan_numeric_resolver.py:486`): an inline `select(PortfolioSnapshotRow)…desc().limit(1)` query, then `snap_fx = _to_float(snap.fx_usd_nis) or 0.0`, then `fx, _src = _current_boi_usd_nis(session, snap_fx)`. Pass the SAME `fx` (current BOI) as the caller FX to the helper, matching how the dashboard converts to NIS.

```python
def _apply_total_net_worth(session, user_id, values):
    """Register total net worth INCL. primary-residence equity — the third
    canonical basis (alongside investable portfolio.net_worth_nis and liquid
    portfolio.liquid_net_worth_nis). Single-sourced from the shared
    net_worth_bases helper the Wealth Dashboard also uses, so the two cannot
    diverge. Pending (never a guess) when no snapshot/FX exists."""
    from argosy.services.net_worth_bases import total_net_worth_incl_residence
    key = "portfolio.total_net_worth_incl_residence_nis"
    try:
        snap = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.id.desc()).limit(1)
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.total_net_worth_query_failed err=%s", exc)
        snap = None
    if snap is None:
        values[key] = ResolvedValue.pending(key, "nis", "portfolio_snapshot (none)")
        return
    snap_fx = _to_float(snap.fx_usd_nis) or 0.0
    fx, _src = _current_boi_usd_nis(session, snap_fx)
    if not fx or fx <= 0:
        values[key] = ResolvedValue.pending(key, "nis", "no FX available")
        return
    try:
        nw_nis, _ = total_net_worth_incl_residence(
            snapshot=snap, fx_usd_nis=fx, session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — never break the resolver
        log.warning("plan_numeric_resolver.total_net_worth_failed err=%s", exc)
        nw_nis = None
    if nw_nis is None:
        values[key] = ResolvedValue.pending(key, "nis", "total net worth unavailable")
        return
    values[key] = ResolvedValue(
        key=key, value=float(nw_nis), unit="nis", status="resolved",
        source_locator="net_worth_bases.total_net_worth_incl_residence",
        confidence="HIGH",
        formula="investable net worth + real-estate net equity (incl. primary residence)")
```

Add `"portfolio.total_net_worth_incl_residence_nis": "nis"` to `_KEY_UNITS`, and call `_apply_total_net_worth(session, user_id, values)` in `resolve_plan_numbers` right after `nw = _resolve_net_worth(...)` is applied.

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
- **Placeholder scan:** Task 1 Step 2/4 note the arithmetic must be matched to the real `_net_worth` formula once read — a deliberate "verify against the real code" instruction, not a placeholder.
- **Codex plan-review (CHANGES NEEDED) incorporated:** (1) resolver uses the real inline-snapshot + `_current_boi_usd_nis(session, snap_fx)` pattern, not the non-existent `_latest_snapshot`/`_boi_fx`; (2) test arithmetic uses LOCAL `value_local` units (800000/300000), not k; (3) edge-case tests added (missing total → None, malformed RE JSON → base); (4) the resolver test seeds `positions_json`+`real_estate_json` explicitly (the existing `_seed_all` does not) and asserts PARITY with the shared helper; (5) the invalid `total >= investable` invariant removed (RE equity can be below the legacy stub or negative).
