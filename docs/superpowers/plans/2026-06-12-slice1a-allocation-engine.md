# Slice 1a — Deterministic Allocation Engine + Rebind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce executable buy/trim/swap allocation candidates from the canonical, glide-aware Argosy plan (not the TSV) — including a correct cash-constrained "deploy $X" mode — with all amounts deterministic.

**Architecture:** A new pure-Python service `argosy/services/allocation_engine.py` reads the canonical `TargetAllocationDoc` (glide-aware) + a normalized holdings/cash view, and emits `AllocationCandidate[]` (structured legs) under three explicit modes. It reuses the existing `diff_plan_vs_holdings` closed-book primitive for rebalance and adds a buy-only water-fill for cash deployment. A read endpoint surfaces the candidates; the windfall allocator is rebound to the canonical doc.

**Tech Stack:** Python 3.12, dataclasses, pydantic (existing doc models), pytest. No network in the engine; no LLM (that's Slice 1b).

---

## File structure

- Create `argosy/services/allocation_engine.py` — the engine: dataclasses, glide-aware targets, holdings adapter, three modes, swap pairing, dispatcher.
- Create `tests/test_allocation_engine.py` — pure unit tests (no network/DB).
- Modify `argosy/api/routes/portfolio.py` — add `GET /api/portfolio/allocation-tasks`.
- Modify `argosy/services/retirement/windfall_allocator.py` — rebind the target side to the canonical doc via the engine (consumer audit).
- Reference (read, do not edit): `argosy/services/plan_proposal_diff.py`, `argosy/services/target_allocation_doc.py`.

Conventions to follow (from the repo): module docstring first; `from __future__ import annotations`; `get_logger(__name__)`; frozen dataclasses for value objects; defensive best-effort at I/O boundaries (engine itself is pure — callers handle missing plan/snapshot).

---

### Task 1: Engine value objects + replacement map

**Files:**
- Create: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_allocation_engine.py
"""Tests for the deterministic allocation engine (pure; no network/DB)."""
from __future__ import annotations

from argosy.services.allocation_engine import (
    AllocationCandidate,
    AllocationLeg,
    AllocationMode,
    REPLACES_SYMBOLS,
)


def test_value_objects_and_replacement_map():
    leg = AllocationLeg(side="BUY", symbol="CSPX", account_id="ibkr",
                        currency="USD", notional_usd=1000.0,
                        funding_source="cash")
    cand = AllocationCandidate(kind="BUY", legs=(leg,), horizon="now")
    assert cand.legs[0].symbol == "CSPX"
    assert cand.total_notional_usd == 1000.0
    # documented UCITS swaps are present
    assert REPLACES_SYMBOLS["SCHD"] == "FUSA"
    assert REPLACES_SYMBOLS["VOO"] == "CSPX"
    assert AllocationMode.CASH_ONLY_DEPLOY.value == "cash_only_deploy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: argosy.services.allocation_engine`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/allocation_engine.py
"""Deterministic allocation engine — plan-bound, glide-aware, no LLM.

Turns the canonical TargetAllocationDoc + current holdings/cash into executable
buy/trim/swap candidates. All amounts are computed here; the Slice-1b agent only
ranks/sequences/explains these candidates and invents no numbers.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date

from argosy.logging import get_logger

log = get_logger(__name__)


class AllocationMode(str, enum.Enum):
    PURE_REBALANCE = "pure_rebalance"
    CASH_ONLY_DEPLOY = "cash_only_deploy"
    REBALANCE_PLUS_CASH = "rebalance_plus_cash"


# Documented UCITS domicile swaps (S18). old US-domiciled symbol -> UCITS twin.
REPLACES_SYMBOLS: dict[str, str] = {
    "VOO": "CSPX", "SCHD": "FUSA", "VEA": "EXUS", "SCHG": "R1GR",
    "USMV": "SPMV", "VNQ": "DPYA", "SGOV": "IB01", "VGSH": "IBTA",
}


@dataclass(frozen=True)
class AllocationLeg:
    side: str                 # "BUY" | "SELL"
    symbol: str
    account_id: str
    currency: str
    notional_usd: float
    funding_source: str       # "cash" | "trim_proceeds"
    quantity: float | None = None


@dataclass(frozen=True)
class AllocationCandidate:
    kind: str                 # "BUY" | "TRIM" | "SWAP"
    legs: tuple[AllocationLeg, ...]
    horizon: str              # "now" | "this_quarter" | "later"
    est_tax_nis: float | None = None
    surtax_split_suggested: bool = False
    rationale: str = ""
    cites: tuple[str, ...] = ()

    @property
    def total_notional_usd(self) -> float:
        return round(sum(abs(l.notional_usd) for l in self.legs), 2)


__all__ = [
    "AllocationMode", "AllocationLeg", "AllocationCandidate", "REPLACES_SYMBOLS",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): allocation-engine value objects + UCITS replacement map"
```

---

### Task 2: Glide-aware class targets

**Files:**
- Modify: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def _doc(glide_dates_pct, class_final):
    """Build a TargetAllocationDoc with a glide and final class targets."""
    from datetime import date
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    classes = [
        AllocationClassDoc(
            label=lbl, snapshot_category=lbl, sigma_class="us_equity",
            target_pct=pct,
            instruments=[AllocationInstrument(symbol=sym, role="primary",
                                              weight_within_class_pct=100.0, domicile="IE")],
        )
        for lbl, pct, sym in class_final
    ]
    glide = [GlideWaypoint(quarter=i, date=d, composition_pct_by_class=comp)
             for i, (d, comp) in enumerate(glide_dates_pct)]
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="test", classes=classes, glide=glide,
    )


def test_class_targets_as_of_picks_latest_waypoint_on_or_before():
    from datetime import date
    from argosy.services.allocation_engine import class_targets_as_of
    doc = _doc(
        glide_dates_pct=[
            (date(2026, 3, 31), {"Core": 60.0, "Bonds": 40.0}),
            (date(2026, 9, 30), {"Core": 70.0, "Bonds": 30.0}),
        ],
        class_final=[("Core", 70.0, "CSPX"), ("Bonds", 30.0, "IB01")],
    )
    # as_of between the two waypoints -> the earlier (current) one, NOT the end-state
    assert class_targets_as_of(doc, date(2026, 6, 1)) == {"Core": 60.0, "Bonds": 40.0}
    # as_of after the last waypoint -> the last
    assert class_targets_as_of(doc, date(2026, 12, 1)) == {"Core": 70.0, "Bonds": 30.0}


def test_class_targets_as_of_falls_back_to_final_when_no_glide():
    from datetime import date
    from argosy.services.allocation_engine import class_targets_as_of
    doc = _doc(glide_dates_pct=[], class_final=[("Core", 65.0, "CSPX"), ("Bonds", 35.0, "IB01")])
    assert class_targets_as_of(doc, date(2026, 6, 1)) == {"Core": 65.0, "Bonds": 35.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k class_targets -q`
Expected: FAIL — `ImportError: cannot import name 'class_targets_as_of'`.

- [ ] **Step 3: Write minimal implementation** (append to `allocation_engine.py`, before `__all__`)

```python
def class_targets_as_of(doc, as_of: date) -> dict[str, float]:
    """Class-label -> target % as of ``as_of`` along the glide.

    Picks the latest glide waypoint dated on-or-before ``as_of`` (so a mid-
    transition date uses the CURRENT composition, not the end-state). When
    ``as_of`` precedes every waypoint, uses the first. Falls back to the final
    class targets when the doc carries no glide.
    """
    glide = list(getattr(doc, "glide", []) or [])
    if glide:
        glide.sort(key=lambda w: w.date)
        chosen = glide[0]
        for wp in glide:
            if wp.date <= as_of:
                chosen = wp
            else:
                break
        return dict(chosen.composition_pct_by_class)
    return {c.label: c.target_pct for c in doc.classes}
```

Add `from datetime import date` is already imported. Update `__all__` to add `"class_targets_as_of"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k class_targets -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): glide-aware class_targets_as_of"
```

---

### Task 3: Per-symbol target values (glide-aware) + holdings adapter

**Files:**
- Modify: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def test_target_values_by_symbol_uses_glide_pct_and_instrument_weights():
    from datetime import date
    from argosy.services.allocation_engine import target_values_by_symbol
    doc = _doc(
        glide_dates_pct=[(date(2026, 3, 31), {"Core": 60.0, "Bonds": 40.0})],
        class_final=[("Core", 70.0, "CSPX"), ("Bonds", 30.0, "IB01")],
    )
    # book = 1000; glide Core=60% -> CSPX 600, Bonds=40% -> IB01 400
    out = target_values_by_symbol(doc, total=1000.0, as_of=date(2026, 6, 1))
    assert out["CSPX"] == 600.0
    assert out["IB01"] == 400.0


def test_tradeable_holdings_filters_cash_and_nontradeable():
    from argosy.services.allocation_engine import tradeable_holdings

    class P:  # minimal stand-in for PortfolioPosition
        def __init__(self, symbol, usd, asset_type="equity"):
            self.symbol = symbol; self.usd_value_k = usd / 1000.0
            self.asset_type = asset_type

    class Snap:
        positions = [P("CSPX", 600.0), P("IB01", 400.0), P("-", 250.0, "cash"),
                     P("", 0.0), P("CASHUSD", 250.0, "cash")]

    holdings, cash = tradeable_holdings(Snap())
    assert holdings == {"CSPX": 600.0, "IB01": 400.0}
    assert cash == 500.0  # both cash rows aggregated, kept out of holdings
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k "target_values or tradeable" -q`
Expected: FAIL — names not defined.

- [ ] **Step 3: Write minimal implementation** (append before `__all__`)

```python
def target_values_by_symbol(doc, total: float, as_of: date) -> dict[str, float]:
    """symbol -> target USD value, using glide-aware class %s and per-instrument
    weights. A symbol in >1 class is summed."""
    class_pct = class_targets_as_of(doc, as_of)
    out: dict[str, float] = {}
    for c in doc.classes:
        pct_of_book_class = class_pct.get(c.label, c.target_pct) / 100.0
        for instr in c.instruments:
            v = pct_of_book_class * (instr.weight_within_class_pct / 100.0) * total
            out[instr.symbol] = round(out.get(instr.symbol, 0.0) + v, 2)
    return out


_CASH_TYPES = {"cash", "money_market", "mmf"}


def tradeable_holdings(snapshot) -> tuple[dict[str, float], float]:
    """(holdings_by_symbol_usd, total_cash_usd) from a PortfolioSnapshot.

    Filters the cash sentinel ("-"/blank) and cash-typed rows out of holdings
    and aggregates them into total_cash_usd. Symbols are upper-cased + summed.
    Account/currency splitting is deferred (v1 needs only the total for the
    cash-deploy math)."""
    holdings: dict[str, float] = {}
    cash = 0.0
    for p in getattr(snapshot, "positions", []) or []:
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        usd_k = getattr(p, "usd_value_k", None) or 0.0
        usd = float(usd_k) * 1000.0
        asset_type = (getattr(p, "asset_type", "") or "").lower()
        if asset_type in _CASH_TYPES:
            cash += usd
            continue
        if not sym or sym == "-":
            cash += usd  # blank-symbol rows are cash lines
            continue
        if usd == 0.0:
            continue
        holdings[sym] = round(holdings.get(sym, 0.0) + usd, 2)
    return holdings, round(cash, 2)
```

Update `__all__` to add `"target_values_by_symbol"`, `"tradeable_holdings"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k "target_values or tradeable" -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): glide-aware per-symbol target values + holdings adapter"
```

---

### Task 4: Cash-only deploy (buy-only water-fill) — the codex correctness fix

**Files:**
- Modify: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cash_only_deploy_never_trims_and_caps_at_cash():
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy
    # codex case: A=70, B=30, target 50/50, cash=10 -> buy only $10 of B, no trim of A
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"A": 50.0, "B": 50.0})],
        class_final=[("A", 50.0, "A"), ("B", 50.0, "B")],
    )
    cands = cash_only_deploy(doc, {"A": 70.0, "B": 30.0}, cash_usd=10.0,
                             as_of=date(2026, 6, 1), account_id="ibkr")
    # only one BUY leg, for B, exactly $10, funded by cash; A untouched
    assert len(cands) == 1
    leg = cands[0].legs[0]
    assert (leg.side, leg.symbol, leg.notional_usd, leg.funding_source) == \
           ("BUY", "B", 10.0, "cash")
    assert all(l.side != "SELL" for c in cands for l in c.legs)


def test_cash_only_deploy_rations_proportionally_when_cash_short():
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy
    # both under target by equal gaps; cash less than total gap -> split 50/50
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"A": 50.0, "B": 50.0})],
        class_final=[("A", 50.0, "A"), ("B", 50.0, "B")],
    )
    cands = cash_only_deploy(doc, {"A": 0.0, "B": 0.0}, cash_usd=100.0,
                             as_of=date(2026, 6, 1), account_id="ibkr")
    by = {c.legs[0].symbol: c.legs[0].notional_usd for c in cands}
    assert by == {"A": 50.0, "B": 50.0}
    assert round(sum(by.values()), 2) == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k cash_only -q`
Expected: FAIL — `cash_only_deploy` not defined.

- [ ] **Step 3: Write minimal implementation** (append before `__all__`)

```python
def cash_only_deploy(doc, holdings: dict[str, float], cash_usd: float, *,
                     as_of: date, account_id: str = "ibkr",
                     currency: str = "USD") -> list[AllocationCandidate]:
    """Buy-only, cash-constrained deployment toward the glide-aware targets.

    Targets are computed on the POST-deploy book (current + cash). Each
    under-target symbol's gap = max(0, target_value - current). Cash is
    deployed to gaps; if total gap exceeds cash, it is rationed pro-rata to the
    gaps (water-fill). NEVER emits a trim; buys sum to min(total_gap, cash).
    Returns one BUY candidate per funded symbol, largest first.
    """
    if cash_usd <= 0:
        return []
    post_book = round(sum(holdings.values()) + cash_usd, 2)
    targets = target_values_by_symbol(doc, post_book, as_of)
    gaps = {sym: max(0.0, tv - holdings.get(sym, 0.0)) for sym, tv in targets.items()}
    gaps = {s: g for s, g in gaps.items() if g > 0.0}
    total_gap = sum(gaps.values())
    if total_gap <= 0.0:
        return []
    scale = 1.0 if total_gap <= cash_usd else cash_usd / total_gap
    out: list[AllocationCandidate] = []
    for sym, gap in gaps.items():
        amount = round(gap * scale, 2)
        if amount <= 0.0:
            continue
        out.append(AllocationCandidate(
            kind="BUY",
            legs=(AllocationLeg(side="BUY", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=amount,
                                funding_source="cash"),),
            horizon="now",
            rationale=f"Deploy ${amount:,.0f} cash into {sym} toward its plan target.",
            cites=(f"plan_target:{sym}",),
        ))
    out.sort(key=lambda c: -c.total_notional_usd)
    return out
```

Update `__all__` to add `"cash_only_deploy"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k cash_only -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): buy-only cash-constrained water-fill deploy (codex fix)"
```

---

### Task 5: Pure-rebalance candidates (wrap diff_plan_vs_holdings) + swap pairing

**Files:**
- Modify: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def test_rebalance_pairs_trim_and_add_into_one_swap():
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    # plan targets FUSA 100%; holdings are all SCHD -> trim SCHD + add FUSA,
    # and SCHD->FUSA is in REPLACES_SYMBOLS, so it becomes ONE SWAP candidate.
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Dividend": 100.0})],
        class_final=[("Dividend", 100.0, "FUSA")],
    )
    cands = rebalance_candidates(doc, {"SCHD": 1000.0}, as_of=date(2026, 6, 1),
                                 account_id="leumi")
    swaps = [c for c in cands if c.kind == "SWAP"]
    assert len(swaps) == 1
    sides = {l.symbol: l.side for l in swaps[0].legs}
    assert sides == {"SCHD": "SELL", "FUSA": "BUY"}
    # legs reconcile: sell notional ~= buy notional
    sell = next(l.notional_usd for l in swaps[0].legs if l.side == "SELL")
    buy = next(l.notional_usd for l in swaps[0].legs if l.side == "BUY")
    assert abs(sell - buy) < 1.0


def test_rebalance_unpaired_trim_and_add_stay_separate():
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    # holding XYZ (not in plan, not in replacement map) -> standalone TRIM
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Core": 100.0})],
        class_final=[("Core", 100.0, "CSPX")],
    )
    cands = rebalance_candidates(doc, {"XYZ": 500.0, "CSPX": 500.0},
                                 as_of=date(2026, 6, 1), account_id="ibkr")
    kinds = sorted(c.kind for c in cands)
    assert "TRIM" in kinds and "BUY" in kinds and "SWAP" not in kinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k rebalance -q`
Expected: FAIL — `rebalance_candidates` not defined.

- [ ] **Step 3: Write minimal implementation** (append before `__all__`)

```python
def rebalance_candidates(doc, holdings: dict[str, float], *, as_of: date,
                         account_id: str = "ibkr", currency: str = "USD",
                         keep_band_pct: float = 1.0) -> list[AllocationCandidate]:
    """Closed-book rebalance candidates from the glide-aware plan, pairing a
    trim with its UCITS-replacement buy into a single SWAP where the
    REPLACES_SYMBOLS map applies."""
    from argosy.services.plan_proposal_diff import diff_plan_vs_holdings

    # Build a glide-adjusted doc view by overriding class target_pct with the
    # as-of waypoint; diff_plan_vs_holdings reads class.target_pct.
    pct = class_targets_as_of(doc, as_of)
    adj_classes = [c.model_copy(update={"target_pct": pct.get(c.label, c.target_pct)})
                   for c in doc.classes]
    adj_doc = doc.model_copy(update={"classes": adj_classes})

    deltas = diff_plan_vs_holdings(adj_doc, holdings, keep_band_pct=keep_band_pct)
    adds = {d.symbol: d for d in deltas if d.action == "add"}
    trims = {d.symbol: d for d in deltas if d.action == "trim"}

    out: list[AllocationCandidate] = []
    paired_adds: set[str] = set()
    for old_sym, trim in list(trims.items()):
        new_sym = REPLACES_SYMBOLS.get(old_sym)
        add = adds.get(new_sym) if new_sym else None
        if add is not None:
            notional = round(min(abs(trim.delta_value_usd), abs(add.delta_value_usd)), 2)
            out.append(AllocationCandidate(
                kind="SWAP",
                legs=(
                    AllocationLeg(side="SELL", symbol=old_sym, account_id=account_id,
                                  currency=currency, notional_usd=notional,
                                  funding_source="trim_proceeds"),
                    AllocationLeg(side="BUY", symbol=new_sym, account_id=account_id,
                                  currency=currency, notional_usd=notional,
                                  funding_source="trim_proceeds"),
                ),
                horizon="this_quarter",
                surtax_split_suggested=False,
                rationale=f"Domicile swap {old_sym}→{new_sym} (UCITS); size-matched.",
                cites=(f"plan_target:{new_sym}", f"replaces:{old_sym}"),
            ))
            paired_adds.add(new_sym)
            del trims[old_sym]

    for sym, trim in trims.items():
        out.append(AllocationCandidate(
            kind="TRIM",
            legs=(AllocationLeg(side="SELL", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(abs(trim.delta_value_usd), 2),
                                funding_source="trim_proceeds"),),
            horizon="this_quarter", rationale=trim.rationale, cites=(f"plan_target:{sym}",),
        ))
    for sym, add in adds.items():
        if sym in paired_adds:
            continue
        out.append(AllocationCandidate(
            kind="BUY",
            legs=(AllocationLeg(side="BUY", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(add.delta_value_usd, 2),
                                funding_source="trim_proceeds"),),
            horizon="this_quarter", rationale=add.rationale, cites=(f"plan_target:{sym}",),
        ))
    out.sort(key=lambda c: -c.total_notional_usd)
    return out
```

Update `__all__` to add `"rebalance_candidates"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k rebalance -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): rebalance candidates with UCITS swap pairing"
```

---

### Task 6: Top-level dispatcher `compute_allocation`

**Files:**
- Modify: `argosy/services/allocation_engine.py`
- Test: `tests/test_allocation_engine.py`

- [ ] **Step 1: Write the failing test**

```python
def test_compute_allocation_dispatches_modes():
    from datetime import date
    from argosy.services.allocation_engine import compute_allocation, AllocationMode
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Core": 100.0})],
        class_final=[("Core", 100.0, "CSPX")],
    )
    holdings = {"CSPX": 1000.0}
    # cash-only deploy: a pure buy
    c1 = compute_allocation(doc, holdings, AllocationMode.CASH_ONLY_DEPLOY,
                            cash_usd=500.0, as_of=date(2026, 6, 1))
    assert c1 and all(l.side == "BUY" for c in c1 for l in c.legs)
    # pure rebalance with on-target book: nothing to do
    c2 = compute_allocation(doc, holdings, AllocationMode.PURE_REBALANCE,
                            as_of=date(2026, 6, 1))
    assert c2 == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -k compute_allocation -q`
Expected: FAIL — `compute_allocation` not defined.

- [ ] **Step 3: Write minimal implementation** (append before `__all__`)

```python
def compute_allocation(doc, holdings: dict[str, float], mode: AllocationMode, *,
                       cash_usd: float = 0.0, as_of: date | None = None,
                       account_id: str = "ibkr") -> list[AllocationCandidate]:
    """Dispatch to the requested mode. ``as_of`` defaults to today."""
    from datetime import date as _date
    when = as_of or _date.today()
    if mode == AllocationMode.CASH_ONLY_DEPLOY:
        return cash_only_deploy(doc, holdings, cash_usd, as_of=when, account_id=account_id)
    if mode == AllocationMode.PURE_REBALANCE:
        return rebalance_candidates(doc, holdings, as_of=when, account_id=account_id)
    # REBALANCE_PLUS_CASH: deploy cash first, then rebalance the resulting book.
    deploy = cash_only_deploy(doc, holdings, cash_usd, as_of=when, account_id=account_id)
    post = dict(holdings)
    for c in deploy:
        for l in c.legs:
            post[l.symbol] = post.get(l.symbol, 0.0) + l.notional_usd
    return deploy + rebalance_candidates(doc, post, as_of=when, account_id=account_id)
```

Update `__all__` to add `"compute_allocation"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_engine.py -q`
Expected: PASS (all engine tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_engine.py tests/test_allocation_engine.py
git commit -m "feat(alloc): compute_allocation mode dispatcher"
```

---

### Task 7: API endpoint `GET /api/portfolio/allocation-tasks`

**Files:**
- Modify: `argosy/api/routes/portfolio.py`
- Test: `tests/test_allocation_tasks_route.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_allocation_tasks_route.py
"""Route test: /api/portfolio/allocation-tasks reads the canonical plan."""
from __future__ import annotations

from datetime import date
from fastapi.testclient import TestClient

import argosy.api.routes.portfolio as portfolio_routes
from argosy.api.main import create_app


def _doc():
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="test",
        classes=[AllocationClassDoc(label="Core", snapshot_category="Core", sigma_class="us_equity",
                 target_pct=100.0, instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                 weight_within_class_pct=100.0, domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1), composition_pct_by_class={"Core": 100.0})],
    )


def test_allocation_tasks_cash_deploy(monkeypatch):
    monkeypatch.setattr(portfolio_routes, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {"CSPX": 1000.0}, 0.0))
    client = TestClient(create_app())
    r = client.get("/api/portfolio/allocation-tasks",
                   params={"mode": "cash_only_deploy", "cash_usd": 500, "user_id": "ariel"})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"]
    assert all(leg["side"] == "BUY" for c in body["candidates"] for leg in c["legs"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_tasks_route.py -q`
Expected: FAIL — 404 (route missing) / `_load_current_doc_and_holdings` missing.

- [ ] **Step 3: Write minimal implementation** (add to `argosy/api/routes/portfolio.py`, after the sleeve routes)

```python
class AllocationLegDTO(BaseModel):
    side: str
    symbol: str
    account_id: str
    currency: str
    notional_usd: float
    funding_source: str
    quantity: float | None = None


class AllocationCandidateDTO(BaseModel):
    kind: str
    legs: list[AllocationLegDTO]
    horizon: str
    est_tax_nis: float | None = None
    surtax_split_suggested: bool = False
    rationale: str = ""
    cites: list[str] = []


class AllocationTasksDTO(BaseModel):
    mode: str
    cash_usd: float
    candidates: list[AllocationCandidateDTO]
    note: str


def _load_current_doc_and_holdings(user_id: str):
    """(TargetAllocationDoc | None, holdings_by_symbol, cash_usd) from the user's
    current accepted plan + latest snapshot. Best-effort; ({},0) on miss."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from argosy.config import get_settings
    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row, row_to_snapshot,
    )
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.models import PlanVersion

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    S = sessionmaker(bind=create_engine(url, connect_args={"check_same_thread": False}),
                     expire_on_commit=False)
    with S() as db:
        pv = db.query(PlanVersion).filter(
            PlanVersion.user_id == user_id, PlanVersion.role == "current",
        ).order_by(PlanVersion.id.desc()).first()
        doc = load_plan_target_allocation(pv) if pv is not None else None
        row = get_latest_snapshot_row(db, user_id)
        holdings, cash = ({}, 0.0)
        if row is not None:
            holdings, cash = tradeable_holdings(row_to_snapshot(row))
    return doc, holdings, cash


@router.get("/allocation-tasks", response_model=AllocationTasksDTO)
def get_allocation_tasks(
    mode: str = Query("cash_only_deploy"),
    cash_usd: float = Query(0.0, ge=0.0),
    user_id: str = Query("ariel"),
) -> AllocationTasksDTO:
    """Deterministic, plan-bound allocation candidates (no LLM). 'Plan target'
    is the canonical TargetAllocationDoc (glide-aware) — never the TSV."""
    from argosy.services.allocation_engine import AllocationMode, compute_allocation

    doc, holdings, snap_cash = _load_current_doc_and_holdings(user_id)
    if doc is None:
        return AllocationTasksDTO(mode=mode, cash_usd=cash_usd, candidates=[],
                                  note="No current canonical plan — accept a plan first.")
    deploy_cash = cash_usd or snap_cash
    cands = compute_allocation(doc, holdings, AllocationMode(mode),
                               cash_usd=deploy_cash)
    return AllocationTasksDTO(
        mode=mode, cash_usd=deploy_cash,
        candidates=[AllocationCandidateDTO(
            kind=c.kind, horizon=c.horizon, est_tax_nis=c.est_tax_nis,
            surtax_split_suggested=c.surtax_split_suggested, rationale=c.rationale,
            cites=list(c.cites),
            legs=[AllocationLegDTO(side=l.side, symbol=l.symbol, account_id=l.account_id,
                  currency=l.currency, notional_usd=l.notional_usd,
                  funding_source=l.funding_source, quantity=l.quantity) for l in c.legs],
        ) for c in cands],
        note=("Plan-bound (canonical TargetAllocationDoc, glide-aware). Amounts "
              "deterministic; tax shown as advisory only. The agent (Slice 1b) "
              "orders + explains these."),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_allocation_tasks_route.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/portfolio.py tests/test_allocation_tasks_route.py
git commit -m "feat(api): /api/portfolio/allocation-tasks (plan-bound deterministic)"
```

---

### Task 8: Rebind windfall allocator target source + consumer audit

**Files:**
- Modify: `argosy/services/retirement/windfall_allocator.py`
- Test: `tests/test_windfall_allocator.py` (existing — add a test; find with `Glob tests/test_windfall*`)

- [ ] **Step 1: Write the failing test** (add to the existing windfall test module)

```python
def test_windfall_targets_come_from_canonical_doc_not_tsv(monkeypatch):
    """The long-term allocation closes gaps against the canonical plan's
    glide-aware class targets, not the TSV-typed targets."""
    from datetime import date
    import argosy.services.retirement.windfall_allocator as wa
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    doc = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[AllocationClassDoc(label="Core", snapshot_category="Core",
                 sigma_class="us_equity", target_pct=100.0,
                 instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                 weight_within_class_pct=100.0, domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={"Core": 100.0})],
    )
    # event with $50k cash, empty book
    plan = wa.propose_allocations_from_plan(doc, holdings={}, cash_usd=50_000.0,
                                            as_of=date(2026, 6, 1))
    # all the long-term cash goes to the canonical instrument CSPX
    longs = [c for c in plan if c.kind == "BUY"]
    assert longs and all(l.symbol == "CSPX" for c in longs for l in c.legs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_windfall_allocator.py -k canonical_doc -q`
Expected: FAIL — `propose_allocations_from_plan` not defined.

- [ ] **Step 3: Write minimal implementation** (add a plan-bound entry to `windfall_allocator.py`; keep the old `propose_allocations` for the legacy TSV path until consumers migrate)

```python
def propose_allocations_from_plan(doc, holdings, cash_usd, *, as_of):
    """Plan-bound cash deployment — the canonical replacement for the TSV-driven
    long-term path. Delegates to the deterministic engine (glide-aware targets,
    buy-only cash-constrained). Returns AllocationCandidate[]."""
    from argosy.services.allocation_engine import AllocationMode, compute_allocation
    return compute_allocation(doc, holdings, AllocationMode.CASH_ONLY_DEPLOY,
                              cash_usd=cash_usd, as_of=as_of)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_windfall_allocator.py -q`
Expected: PASS (new + existing windfall tests).

- [ ] **Step 5: Consumer audit — run the suites that touch the allocator**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider -k "windfall or unallocated or rsu_prevest or portfolio" -m "not llm_eval" -q`
Expected: PASS. If any consumer breaks, it's because it read the TSV `allocation_delta_table`; leave the legacy `propose_allocations` intact (don't delete it this slice) so those paths keep working — the rebind is additive. Note any consumer still on the TSV path in the commit body for a follow-up migration.

- [ ] **Step 6: Commit**

```bash
git add argosy/services/retirement/windfall_allocator.py tests/test_windfall_allocator.py
git commit -m "feat(windfall): plan-bound cash deploy via allocation engine (TSV path retained for legacy consumers)"
```

---

### Task 9: Full-suite smoke + spec-coverage check

- [ ] **Step 1: Run the touched suites + smoke**

Run:
```
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -p no:cacheprovider -m "not llm_eval" tests/test_allocation_engine.py tests/test_allocation_tasks_route.py tests/test_windfall_allocator.py -q
pwsh -File scripts/smoke.ps1
```
Expected: all green.

- [ ] **Step 2: Verify against the clean plan (run 96)**

Once run 96's draft is accepted as the current plan, hit the endpoint live and confirm "Plan target" reflects CSPX/FUSA/etc (UCITS), not the spreadsheet:
Run: `curl -s "http://127.0.0.1:8000/api/portfolio/allocation-tasks?mode=cash_only_deploy&cash_usd=250000&user_id=ariel"`
Expected: BUY legs into UCITS instruments only; no trims; buys sum ≤ cash.

- [ ] **Step 3: Commit any fixes; Slice 1a done.**

---

## Self-review

- **Spec coverage:** rebind→Task 8; glide-aware→Task 2/3; holdings adapter→Task 3; three modes→Task 4/5/6; deterministic swap pairing→Task 5; advisory tax (`surtax_split_suggested`/`est_tax_nis` on the dataclass, no split engine)→Task 1 fields + endpoint passthrough; surface→Task 7; consumer audit→Task 8 Step 5; dependency on run 96→Task 9 Step 2. 1b (agent) is intentionally a separate plan.
- **Placeholder scan:** none — every code step is complete.
- **Type consistency:** `AllocationCandidate`/`AllocationLeg`/`AllocationMode`/`compute_allocation`/`class_targets_as_of`/`target_values_by_symbol`/`tradeable_holdings`/`cash_only_deploy`/`rebalance_candidates` used consistently across Tasks 1–8; DTOs mirror the dataclass field names.

## Out of scope (Slice 1b, separate plan)

The `AllocationAgent` (Opus ranker/sequencer/explainer + deployment-pace recommendation), its reconciliation validation, and the on-demand `ExecutableTask[]` half of the endpoint.
