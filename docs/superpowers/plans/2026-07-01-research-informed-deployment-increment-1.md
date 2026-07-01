# Research-informed Deployment — Increment 1 (Deterministic Preflight) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A no-LLM deterministic preflight that takes the deployment engine's candidate buys and flags the exact failure class — re-buying NVDA via index look-through, adding T-bills on top of the funded SGOV reserve, and $0 to a missing gold sleeve — assigning each candidate a status with a plain reason code.

**Architecture:** New `argosy/services/deployment_funnel/` package. `deployment_advisor.assemble_deployment_plan` is demoted to a candidate generator; a pure pipeline enriches each candidate (live quote + history features + ingested `NewsSignal`), computes **effective** exposure via an explicit fund look-through map, applies a reserve policy against existing cash-like holdings, and runs deterministic gates producing a `CandidateStatus` + reason. Output is a structured `PreflightResult` (trace object). No DB writes, no LLM, no execution in this increment.

**Tech Stack:** Python 3.12, dataclasses (frozen value objects — repo convention), pytest. Reuses `argosy/services/contracts.py` (`AllocationCandidate`/`AllocationLeg`), the yfinance adapter, and `NewsSignal` reads.

## Global Constraints

- Money math + decision logic is risky work → **codex-tandem** the sizer/exposure math (per CLAUDE.md).
- No hardcoded/magic numbers that aren't Argosy-derived; look-through weights are an explicit, versioned, cited map (not invented per-run).
- Gold-at-ATH is a **feature, never a veto rule** — history features are recorded, never gate.
- Fail-closed on stale market data (`defer`, never silently proceed).
- Frozen dataclasses for value objects; `from __future__ import annotations` at top of every module.
- `BaseAgent`/DB not used in Increment 1 — pure functions given inputs (fully unit-testable).
- Dollar conservation invariant: Σ kept-candidate notionals ≤ deployable cash — assert in the orchestrator.

---

### Task 1: Package + typed contracts

**Files:**
- Create: `argosy/services/deployment_funnel/__init__.py`
- Create: `argosy/services/deployment_funnel/contracts.py`
- Test: `tests/services/deployment_funnel/test_contracts.py`

**Interfaces:**
- Consumes: `argosy.services.contracts.AllocationCandidate`
- Produces: `CandidateStatus` (enum), `PlanGap`, `HistoryFeatures`, `EnrichedCandidate`, `PreflightResult` (all frozen); `CANDIDATE_STATUSES` tuple.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/deployment_funnel/test_contracts.py
from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus, PlanGap, HistoryFeatures, EnrichedCandidate, PreflightResult,
)


def _cand(symbol="CSPX", usd=22000.0):
    return AllocationCandidate(
        kind="BUY",
        legs=(AllocationLeg(side="BUY", symbol=symbol, account_id="leumi",
                            currency="USD", notional_usd=usd, funding_source="cash"),),
        horizon="now",
    )


def test_enriched_candidate_and_result_round_trip():
    hf = HistoryFeatures(last_price=368.0, ath=372.0, pct_below_ath=1.08,
                         zscore_vs_window=1.9, drawdown_pct=1.08)
    ec = EnrichedCandidate(candidate=_cand(), symbol="CSPX",
                           effective_nvda_usd=1540.0, news_sentiment="neutral",
                           history=hf, status=CandidateStatus.APPROVE, reason="fills US core")
    assert ec.symbol == "CSPX"
    assert ec.status is CandidateStatus.APPROVE
    gap = PlanGap(asset_class="gold", current_target_pct=0.0,
                  proposed_target_pct=None, reason_refs=("0% vs typical 3-5%",),
                  blocked_amount_usd=45000.0)
    res = PreflightResult(deployable_usd=95000.0, enriched=(ec,), plan_gaps=(gap,),
                          kept_total_usd=22000.0)
    assert res.plan_gaps[0].asset_class == "gold"
    assert res.kept_total_usd <= res.deployable_usd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: argosy.services.deployment_funnel`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/deployment_funnel/__init__.py
"""Deterministic, research-informed deployment preflight (Increment 1)."""
```

```python
# argosy/services/deployment_funnel/contracts.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from argosy.services.contracts import AllocationCandidate


class CandidateStatus(str, Enum):
    APPROVE = "approve_candidate"
    VETO = "veto"
    DEFER = "defer"
    REQUIRES_PLAN_CHANGE = "requires_plan_change"
    CAP_AT_PCT = "cap_at_pct"


CANDIDATE_STATUSES = tuple(s.value for s in CandidateStatus)


@dataclass(frozen=True)
class HistoryFeatures:
    """Price-history FEATURES for a candidate symbol. Recorded for judgment;
    NEVER a gate on their own (gold-at-ATH is evidence, not a rule)."""
    last_price: float | None
    ath: float | None
    pct_below_ath: float | None       # 0 == at ATH; 12.0 == 12% below
    zscore_vs_window: float | None
    drawdown_pct: float | None
    stale: bool = False


@dataclass(frozen=True)
class PlanGap:
    asset_class: str
    current_target_pct: float
    proposed_target_pct: float | None
    reason_refs: tuple[str, ...]
    blocked_amount_usd: float


@dataclass(frozen=True)
class EnrichedCandidate:
    candidate: AllocationCandidate
    symbol: str
    effective_nvda_usd: float          # incl. index look-through
    news_sentiment: str | None         # None => "no recent ingested signal"
    history: HistoryFeatures
    status: CandidateStatus
    reason: str
    cap_pct: float | None = None       # set when status is CAP_AT_PCT


@dataclass(frozen=True)
class PreflightResult:
    deployable_usd: float
    enriched: tuple[EnrichedCandidate, ...]
    plan_gaps: tuple[PlanGap, ...]
    kept_total_usd: float
    notes: tuple[str, ...] = ()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_contracts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/deployment_funnel/__init__.py argosy/services/deployment_funnel/contracts.py tests/services/deployment_funnel/test_contracts.py
git commit -m "feat(deploy-funnel): typed contracts (CandidateStatus, PlanGap, EnrichedCandidate, PreflightResult)"
```

---

### Task 2: Fund look-through map + effective-exposure

**Files:**
- Create: `argosy/services/deployment_funnel/look_through.py`
- Test: `tests/services/deployment_funnel/test_look_through.py`

**Interfaces:**
- Produces: `LOOKTHROUGH_MAP: dict[str, dict[str, float]]` (symbol → {"nvda": w, "us": w} fractions), `LOOKTHROUGH_VERSION: int`, `effective_nvda_usd(symbol: str, notional_usd: float) -> float`, `effective_us_usd(symbol, notional_usd) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/deployment_funnel/test_look_through.py
import pytest
from argosy.services.deployment_funnel.look_through import (
    effective_nvda_usd, effective_us_usd, LOOKTHROUGH_MAP,
)


def test_cspx_carries_sp500_nvda_weight():
    # CSPX ~7% NVDA: $22,000 -> ~$1,540 effective NVDA.
    assert effective_nvda_usd("CSPX", 22000.0) == pytest.approx(1540.0, abs=1.0)


def test_r1gr_carries_higher_growth_nvda_weight():
    # R1GR ~14% NVDA (plan's own rationale): $13,000 -> ~$1,820.
    assert effective_nvda_usd("R1GR", 13000.0) == pytest.approx(1820.0, abs=1.0)


def test_gold_and_tbills_carry_zero_nvda():
    assert effective_nvda_usd("SGLD", 45000.0) == 0.0
    assert effective_nvda_usd("IB01", 3000.0) == 0.0


def test_direct_nvda_is_full_weight():
    assert effective_nvda_usd("NVDA", 5000.0) == 5000.0


def test_unknown_symbol_assumes_zero_lookthrough_but_flagged():
    # Unknown non-NVDA symbol: no look-through data -> 0 NVDA (conservative
    # for THIS cap direction) but present in the map's miss set for the caller.
    assert effective_nvda_usd("XYZ", 1000.0) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_look_through.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/deployment_funnel/look_through.py
from __future__ import annotations

# Explicit, versioned fund->constituent weight map for the HOUSEHOLD's held
# broad funds + candidate ETFs. NOT a live holdings feed — a small, cited,
# hand-maintained table sufficient for the correlated-exposure cap. Weights are
# fractions of the fund's NAV. Sources: index fact sheets (S&P 500, Russell 1000
# Growth) as of 2026-Q2; update LOOKTHROUGH_VERSION when refreshed.
LOOKTHROUGH_VERSION = 1

LOOKTHROUGH_MAP: dict[str, dict[str, float]] = {
    # US broad / growth — carry index NVDA weight.
    "CSPX": {"nvda": 0.07, "us": 1.00},   # iShares Core S&P 500 UCITS
    "VOO":  {"nvda": 0.07, "us": 1.00},
    "FUSA": {"nvda": 0.06, "us": 1.00},   # Fidelity US Quality Income
    "R1GR": {"nvda": 0.14, "us": 1.00},   # iShares Russell 1000 Growth
    "SCHG": {"nvda": 0.13, "us": 1.00},
    "QQQM": {"nvda": 0.08, "us": 1.00},
    "SPMV": {"nvda": 0.01, "us": 1.00},   # min-vol underweights NVDA
    "SPMO": {"nvda": 0.10, "us": 1.00},
    # World funds — partial US, small NVDA.
    "FWRA": {"nvda": 0.04, "us": 0.65},
    "ACWD": {"nvda": 0.04, "us": 0.63},
    "IWDA": {"nvda": 0.05, "us": 0.70},
    "EXUS": {"nvda": 0.00, "us": 0.00},   # World ex-US
    "EIMI": {"nvda": 0.00, "us": 0.00},   # EM
    # Alternatives / cash-like — zero NVDA, zero US-equity.
    "SGLD": {"nvda": 0.00, "us": 0.00},   # gold ETC
    "IGLN": {"nvda": 0.00, "us": 0.00},
    "SGOV": {"nvda": 0.00, "us": 0.00},
    "IB01": {"nvda": 0.00, "us": 0.00},
    "IBTA": {"nvda": 0.00, "us": 0.00},
    # Direct single-name.
    "NVDA": {"nvda": 1.00, "us": 1.00},
}


def _weight(symbol: str, key: str) -> float:
    return LOOKTHROUGH_MAP.get(symbol.upper(), {}).get(key, 0.0)


def effective_nvda_usd(symbol: str, notional_usd: float) -> float:
    """Dollars of NVDA exposure a buy of ``notional_usd`` in ``symbol`` adds,
    including index look-through. Unknown symbols contribute 0 (caller tracks
    misses)."""
    return round(notional_usd * _weight(symbol, "nvda"), 2)


def effective_us_usd(symbol: str, notional_usd: float) -> float:
    return round(notional_usd * _weight(symbol, "us"), 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_look_through.py -v`
Expected: PASS

- [ ] **Step 5: codex-tandem the weights + math, then commit**

Run a codex review of `look_through.py` (weights realism + math direction). Then:
```bash
git add argosy/services/deployment_funnel/look_through.py tests/services/deployment_funnel/test_look_through.py
git commit -m "feat(deploy-funnel): versioned fund look-through map + effective-NVDA/US exposure"
```

---

### Task 3: Reserve policy

**Files:**
- Create: `argosy/services/deployment_funnel/reserve.py`
- Test: `tests/services/deployment_funnel/test_reserve.py`

**Interfaces:**
- Produces: `CASH_LIKE_SYMBOLS: frozenset[str]`, `existing_cash_like_usd(holdings_usd: dict[str, float]) -> float`, `reserve_shortfall_usd(book_usd: float, holdings_usd: dict[str, float], reserve_target_pct: float) -> float`.
- Consumes: nothing (pure).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/deployment_funnel/test_reserve.py
from argosy.services.deployment_funnel.reserve import (
    existing_cash_like_usd, reserve_shortfall_usd,
)


def test_existing_cash_like_sums_sgov_and_cash():
    holdings = {"SGOV": 127040.0, "CASH_USD": 144940.0, "CSPX": 156820.0}
    assert existing_cash_like_usd(holdings) == 127040.0 + 144940.0


def test_reserve_already_funded_zero_shortfall():
    # Book $4.06M, 6% reserve target = ~$243k; existing cash-like $272k -> funded.
    holdings = {"SGOV": 127040.0, "CASH_USD": 144940.0}
    assert reserve_shortfall_usd(4_060_000.0, holdings, 6.0) == 0.0


def test_reserve_shortfall_when_underfunded():
    holdings = {"SGOV": 10000.0}
    # 6% of 1,000,000 = 60,000; existing 10,000 -> 50,000 shortfall.
    assert reserve_shortfall_usd(1_000_000.0, holdings, 6.0) == 50000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_reserve.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/deployment_funnel/reserve.py
from __future__ import annotations

# Holdings keys that already satisfy the reserve (cash + T-bill/short-Treasury
# ETFs). A new T-bill/cash candidate must not be recommended on top of these.
CASH_LIKE_SYMBOLS = frozenset({"SGOV", "IB01", "IBTA", "ERNS", "CASH_USD", "CASH_NIS"})


def existing_cash_like_usd(holdings_usd: dict[str, float]) -> float:
    return round(
        sum(v for s, v in holdings_usd.items() if s.upper() in CASH_LIKE_SYMBOLS), 2
    )


def reserve_shortfall_usd(
    book_usd: float, holdings_usd: dict[str, float], reserve_target_pct: float
) -> float:
    """How much MORE cash-like the book needs to hit the reserve target. 0 when
    already funded — the signal that a T-bill/cash candidate is redundant."""
    target = book_usd * reserve_target_pct / 100.0
    have = existing_cash_like_usd(holdings_usd)
    return round(max(0.0, target - have), 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_reserve.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/deployment_funnel/reserve.py tests/services/deployment_funnel/test_reserve.py
git commit -m "feat(deploy-funnel): reserve policy (existing cash-like counts against target)"
```

---

### Task 4: Enrichment (quote + history features + news), stubbed for tests

**Files:**
- Create: `argosy/services/deployment_funnel/enrich.py`
- Test: `tests/services/deployment_funnel/test_enrich.py`

**Interfaces:**
- Produces: `PriceProvider` (Protocol with `quote(symbol) -> float | None`, `history_high(symbol) -> float | None`, `zscore(symbol) -> float | None`), `build_history_features(symbol, provider) -> HistoryFeatures`, `news_sentiment_for(symbol, signals_by_symbol) -> str | None`.
- Consumes: `HistoryFeatures` from Task 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/deployment_funnel/test_enrich.py
from argosy.services.deployment_funnel.enrich import (
    build_history_features, news_sentiment_for,
)


class _StubProvider:
    def __init__(self, q, hi, z): self._q, self._hi, self._z = q, hi, z
    def quote(self, s): return self._q
    def history_high(self, s): return self._hi
    def zscore(self, s): return self._z


def test_history_features_computes_pct_below_ath():
    hf = build_history_features("SGLD", _StubProvider(368.0, 372.0, 1.9))
    assert hf.pct_below_ath == round((372.0 - 368.0) / 372.0 * 100, 2)
    assert hf.stale is False


def test_missing_quote_marks_stale():
    hf = build_history_features("SGLD", _StubProvider(None, 372.0, None))
    assert hf.stale is True


def test_news_sentiment_absent_returns_none():
    assert news_sentiment_for("SGLD", {}) is None


def test_news_sentiment_present():
    assert news_sentiment_for("NVDA", {"NVDA": "positive"}) == "positive"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_enrich.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/deployment_funnel/enrich.py
from __future__ import annotations

from typing import Protocol

from argosy.services.deployment_funnel.contracts import HistoryFeatures


class PriceProvider(Protocol):
    def quote(self, symbol: str) -> float | None: ...
    def history_high(self, symbol: str) -> float | None: ...
    def zscore(self, symbol: str) -> float | None: ...


def build_history_features(symbol: str, provider: PriceProvider) -> HistoryFeatures:
    """Deterministic price-history FEATURES. A missing live quote marks the
    candidate stale (the gates fail-closed to DEFER); features never gate."""
    last = provider.quote(symbol)
    ath = provider.history_high(symbol)
    z = provider.zscore(symbol)
    stale = last is None
    pct_below = (
        round((ath - last) / ath * 100, 2)
        if (last is not None and ath and ath > 0) else None
    )
    drawdown = pct_below  # single-window proxy in Increment 1
    return HistoryFeatures(
        last_price=last, ath=ath, pct_below_ath=pct_below,
        zscore_vs_window=z, drawdown_pct=drawdown, stale=stale,
    )


def news_sentiment_for(
    symbol: str, signals_by_symbol: dict[str, str]
) -> str | None:
    """Ingested NewsSignal sentiment for the symbol, or None => the trace must
    render 'no recent ingested signal' (NOT 'no news')."""
    return signals_by_symbol.get(symbol.upper())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_enrich.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/deployment_funnel/enrich.py tests/services/deployment_funnel/test_enrich.py
git commit -m "feat(deploy-funnel): candidate enrichment (history features + ingested news sentiment)"
```

---

### Task 5: Deterministic gates → status + reason

**Files:**
- Create: `argosy/services/deployment_funnel/gates.py`
- Test: `tests/services/deployment_funnel/test_gates.py`

**Interfaces:**
- Consumes: `AllocationCandidate`, `HistoryFeatures`, `CandidateStatus`, `effective_nvda_usd`, plan-target class set.
- Produces: `GateInputs` (frozen: current_effective_nvda_usd, book_usd, nvda_cap_pct, reserve_shortfall_usd, plan_classes: frozenset[str], class_of: dict[str,str]), `classify_candidate(cand, symbol, history, news_sentiment, gi) -> tuple[CandidateStatus, str, float | None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/deployment_funnel/test_gates.py
from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import CandidateStatus, HistoryFeatures
from argosy.services.deployment_funnel.gates import GateInputs, classify_candidate


def _cand(symbol, usd):
    return AllocationCandidate(
        kind="BUY",
        legs=(AllocationLeg(side="BUY", symbol=symbol, account_id="leumi",
                            currency="USD", notional_usd=usd, funding_source="cash"),),
        horizon="now")


def _hf(stale=False):
    return HistoryFeatures(last_price=100.0, ath=100.0, pct_below_ath=0.0,
                           zscore_vs_window=0.5, drawdown_pct=0.0, stale=stale)


_GI = GateInputs(
    current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
    nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
    plan_classes=frozenset({"US broad-market core", "Cash & T-bills"}),
    class_of={"CSPX": "US broad-market core", "IB01": "Cash & T-bills",
              "SGLD": "gold"})


def test_us_index_buy_over_nvda_cap_is_capped_or_vetoed():
    # Book already 56.6% NVDA; cap 13% => any look-through NVDA add breaches.
    st, reason, cap = classify_candidate(_cand("CSPX", 22000.0), "CSPX", _hf(),
                                         "neutral", _GI)
    assert st in (CandidateStatus.CAP_AT_PCT, CandidateStatus.VETO)
    assert "NVDA" in reason


def test_tbill_when_reserve_funded_is_vetoed():
    st, reason, _ = classify_candidate(_cand("IB01", 3000.0), "IB01", _hf(),
                                       None, _GI)
    assert st is CandidateStatus.VETO
    assert "reserve" in reason.lower()


def test_missing_plan_class_requires_plan_change():
    st, reason, _ = classify_candidate(_cand("SGLD", 45000.0), "SGLD", _hf(),
                                       None, _GI)
    assert st is CandidateStatus.REQUIRES_PLAN_CHANGE
    assert "plan" in reason.lower()


def test_stale_quote_defers():
    st, reason, _ = classify_candidate(_cand("SGLD", 45000.0), "SGLD",
                                       _hf(stale=True), None, _GI)
    assert st is CandidateStatus.DEFER


def test_ath_alone_does_not_veto():
    # A gold buy at ATH but WITH a plan class must not be vetoed for ATH.
    gi = GateInputs(current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
                    nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
                    plan_classes=frozenset({"gold"}), class_of={"SGLD": "gold"})
    at_ath = HistoryFeatures(last_price=372.0, ath=372.0, pct_below_ath=0.0,
                             zscore_vs_window=2.5, drawdown_pct=0.0)
    st, _, _ = classify_candidate(_cand("SGLD", 45000.0), "SGLD", at_ath, None, gi)
    assert st is CandidateStatus.APPROVE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_gates.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/deployment_funnel/gates.py
from __future__ import annotations

from dataclasses import dataclass

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import CandidateStatus, HistoryFeatures
from argosy.services.deployment_funnel.look_through import effective_nvda_usd
from argosy.services.deployment_funnel.reserve import CASH_LIKE_SYMBOLS


@dataclass(frozen=True)
class GateInputs:
    current_effective_nvda_usd: float
    book_usd: float
    nvda_cap_pct: float
    reserve_shortfall_usd: float
    plan_classes: frozenset[str]
    class_of: dict[str, str]


def classify_candidate(
    cand: AllocationCandidate,
    symbol: str,
    history: HistoryFeatures,
    news_sentiment: str | None,
    gi: GateInputs,
) -> tuple[CandidateStatus, str, float | None]:
    """Deterministic status for one candidate. Order matters: fail-closed on
    stale data first; then plan-gap; then reserve duplication; then the
    look-through concentration cap. Price HISTORY features never gate here."""
    notional = cand.total_notional_usd

    # 1. Fail-closed on stale market data.
    if history.stale:
        return (CandidateStatus.DEFER,
                "market quote stale — deferring rather than acting blind", None)

    # 2. Plan-gap: a class the plan doesn't contain must go through a plan change.
    cls = gi.class_of.get(symbol.upper())
    if cls is not None and cls not in gi.plan_classes:
        return (CandidateStatus.REQUIRES_PLAN_CHANGE,
                f"'{cls}' is not a sleeve in the current plan — raise a plan "
                f"change before buying", None)

    # 3. Reserve duplication: no net-new cash-like when the reserve is funded.
    if symbol.upper() in CASH_LIKE_SYMBOLS and gi.reserve_shortfall_usd <= 0.0:
        return (CandidateStatus.VETO,
                "reserve already funded — no added T-bills/cash", None)

    # 4. Concentration cap via look-through (effective NVDA, not nominal).
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0:
        cap_usd = gi.book_usd * gi.nvda_cap_pct / 100.0
        headroom = cap_usd - gi.current_effective_nvda_usd
        if headroom <= 0.0:
            return (CandidateStatus.VETO,
                    f"buying {symbol} adds ${add_nvda:,.0f} NVDA via index "
                    f"look-through; effective NVDA already over the "
                    f"{gi.nvda_cap_pct:.0f}% cap", None)
        if add_nvda > headroom:
            cap_pct = max(0.0, round(headroom / add_nvda * 100, 1))
            return (CandidateStatus.CAP_AT_PCT,
                    f"cap {symbol} at {cap_pct:.0f}% — full size adds "
                    f"${add_nvda:,.0f} NVDA via look-through, over the cap",
                    cap_pct)

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_gates.py -v`
Expected: PASS

- [ ] **Step 5: codex-tandem the gate ordering + cap math, then commit**

```bash
git add argosy/services/deployment_funnel/gates.py tests/services/deployment_funnel/test_gates.py
git commit -m "feat(deploy-funnel): deterministic gates (stale/plan-gap/reserve/look-through cap)"
```

---

### Task 6: Preflight orchestrator + kill switches (shadow-only)

**Files:**
- Create: `argosy/services/deployment_funnel/preflight.py`
- Modify: `argosy/config.py` (add `deployment_funnel_enabled` / `deployment_funnel_shadow` settings)
- Test: `tests/services/deployment_funnel/test_preflight.py`

**Interfaces:**
- Consumes: everything above; `PriceProvider`; a `holdings_usd` dict; a `signals_by_symbol` dict; `GateInputs` fields.
- Produces: `run_preflight(candidates, *, symbol_of, gate_inputs, provider, signals_by_symbol, deployable_usd) -> PreflightResult`.

- [ ] **Step 1: Write the failing test (fixture replays the real failure book)**

```python
# tests/services/deployment_funnel/test_preflight.py
import pytest
from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import CandidateStatus
from argosy.services.deployment_funnel.gates import GateInputs
from argosy.services.deployment_funnel.preflight import run_preflight


class _Provider:
    def quote(self, s): return 100.0
    def history_high(self, s): return 100.0
    def zscore(self, s): return 0.5


def _c(symbol, usd):
    return AllocationCandidate(
        kind="BUY",
        legs=(AllocationLeg(side="BUY", symbol=symbol, account_id="leumi",
                            currency="USD", notional_usd=usd, funding_source="cash"),),
        horizon="now")


def test_preflight_catches_the_three_failures():
    # The real failure book: 56.6% NVDA, reserve funded, gold not a plan class.
    gi = GateInputs(
        current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
        nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
        plan_classes=frozenset({"US broad-market core", "Cash & T-bills"}),
        class_of={"CSPX": "US broad-market core", "IB01": "Cash & T-bills",
                  "SGLD": "gold"})
    cands = [_c("CSPX", 22910.0), _c("IB01", 23616.0), _c("SGLD", 45000.0)]
    res = run_preflight(
        cands, symbol_of=lambda c: c.legs[0].symbol, gate_inputs=gi,
        provider=_Provider(), signals_by_symbol={}, deployable_usd=95000.0)
    by = {e.symbol: e.status for e in res.enriched}
    assert by["CSPX"] in (CandidateStatus.VETO, CandidateStatus.CAP_AT_PCT)
    assert by["IB01"] is CandidateStatus.VETO           # reserve funded
    assert by["SGLD"] is CandidateStatus.REQUIRES_PLAN_CHANGE
    assert any(g.asset_class == "gold" for g in res.plan_gaps)
    # Dollar conservation: kept (approved/capped) never exceeds deployable.
    assert res.kept_total_usd <= res.deployable_usd


def test_disabled_flag_is_respected_by_caller(monkeypatch):
    from argosy.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "0")
    assert get_settings().deployment_funnel_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError` / missing settings attr

- [ ] **Step 3: Add settings, then implement the orchestrator**

In `argosy/config.py`, add to the `Settings` model (match the existing pydantic-settings pattern; mirror `decision_funnel_*`):
```python
    deployment_funnel_enabled: bool = Field(
        default=False, alias="ARGOSY_DEPLOYMENT_FUNNEL_ENABLED")
    deployment_funnel_shadow: bool = Field(
        default=True, alias="ARGOSY_DEPLOYMENT_FUNNEL_SHADOW")
```

```python
# argosy/services/deployment_funnel/preflight.py
from __future__ import annotations

from typing import Callable

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus, EnrichedCandidate, PlanGap, PreflightResult,
)
from argosy.services.deployment_funnel.enrich import (
    PriceProvider, build_history_features, news_sentiment_for,
)
from argosy.services.deployment_funnel.gates import GateInputs, classify_candidate

# Statuses whose dollars count toward the "kept" (deployable) total.
_KEPT = {CandidateStatus.APPROVE, CandidateStatus.CAP_AT_PCT}


def run_preflight(
    candidates: list[AllocationCandidate],
    *,
    symbol_of: Callable[[AllocationCandidate], str],
    gate_inputs: GateInputs,
    provider: PriceProvider,
    signals_by_symbol: dict[str, str],
    deployable_usd: float,
) -> PreflightResult:
    """Deterministic, no-LLM preflight. Enriches + classifies each candidate and
    collects typed plan gaps. Pure given its inputs. Shadow-only: it computes and
    returns a result; it never persists or executes."""
    enriched: list[EnrichedCandidate] = []
    plan_gaps: list[PlanGap] = []
    kept_total = 0.0

    for cand in candidates:
        symbol = symbol_of(cand)
        hf = build_history_features(symbol, provider)
        sentiment = news_sentiment_for(symbol, signals_by_symbol)
        status, reason, cap_pct = classify_candidate(
            cand, symbol, hf, sentiment, gate_inputs)

        from argosy.services.deployment_funnel.look_through import effective_nvda_usd
        eff_nvda = effective_nvda_usd(symbol, cand.total_notional_usd)

        enriched.append(EnrichedCandidate(
            candidate=cand, symbol=symbol, effective_nvda_usd=eff_nvda,
            news_sentiment=sentiment, history=hf, status=status,
            reason=reason, cap_pct=cap_pct))

        if status is CandidateStatus.REQUIRES_PLAN_CHANGE:
            cls = gate_inputs.class_of.get(symbol.upper(), "unknown")
            plan_gaps.append(PlanGap(
                asset_class=cls, current_target_pct=0.0, proposed_target_pct=None,
                reason_refs=(f"{symbol} implies '{cls}', absent from the plan",),
                blocked_amount_usd=cand.total_notional_usd))
        elif status in _KEPT:
            frac = (cap_pct / 100.0) if (status is CandidateStatus.CAP_AT_PCT
                                         and cap_pct is not None) else 1.0
            kept_total += cand.total_notional_usd * frac

    kept_total = round(min(kept_total, deployable_usd), 2)
    return PreflightResult(
        deployable_usd=deployable_usd, enriched=tuple(enriched),
        plan_gaps=tuple(plan_gaps), kept_total_usd=kept_total)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/deployment_funnel/ -v`
Expected: PASS (all files)

- [ ] **Step 5: codex-tandem the orchestrator (dollar-conservation + cap fraction), then commit**

```bash
git add argosy/services/deployment_funnel/preflight.py argosy/config.py tests/services/deployment_funnel/test_preflight.py
git commit -m "feat(deploy-funnel): shadow-only deterministic preflight orchestrator + kill switches"
```

---

### Task 7: Wire preflight into the deploy-cash API (shadow annotation)

**Files:**
- Modify: `argosy/api/routes/portfolio.py` (deploy-cash handler — attach preflight statuses when the flag is on, shadow)
- Test: `tests/test_portfolio_deploy_cash_preflight.py`

**Interfaces:**
- Consumes: `run_preflight`, `GateInputs`, a `PriceProvider` backed by the yfinance adapter, holdings from the snapshot, `NewsSignal` reads, plan classes from `load_plan_target_allocation`.
- Produces: an added `preflight` block on the deploy-cash response (statuses + reasons + plan_gaps), only when `deployment_funnel_enabled`; the deterministic buy list is unchanged in shadow (annotation only).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_portfolio_deploy_cash_preflight.py
def test_deploy_cash_preflight_absent_when_disabled(client_with_db, monkeypatch):
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "0")
    r = client_with_db.get("/api/portfolio/deploy-cash?user_id=ariel&cash_usd=100000")
    assert r.status_code == 200
    assert "preflight" not in r.json() or r.json()["preflight"] is None


def test_deploy_cash_preflight_present_and_flags_when_enabled(client_with_db, monkeypatch):
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "1")
    r = client_with_db.get("/api/portfolio/deploy-cash?user_id=ariel&cash_usd=100000")
    assert r.status_code == 200
    pf = r.json().get("preflight")
    assert pf is not None
    # Shadow: the primary buy list is unchanged; preflight is additive.
    assert "enriched" in pf
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_portfolio_deploy_cash_preflight.py -v`
Expected: FAIL — no `preflight` key

- [ ] **Step 3: Implement the shadow annotation in the deploy-cash handler**

In the deploy-cash handler, after the existing plan is built, when
`get_settings().deployment_funnel_enabled` is true, build `GateInputs` from the
snapshot + `load_plan_target_allocation` (plan classes + `nvda_cap_pct`),
construct a `PriceProvider` from the yfinance adapter (with a cached/stale guard),
map the existing `DeploymentLine`s to `AllocationCandidate`s, call `run_preflight`,
and attach a serialized `preflight` block to the response. Do NOT alter the
existing `tiers` list in shadow mode. Wrap in try/except → on any failure, log
and omit the `preflight` block (never break deploy-cash).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_portfolio_deploy_cash_preflight.py tests/services/deployment_funnel/ -v`
Expected: PASS

- [ ] **Step 5: codex review the route wiring, then commit**

```bash
git add argosy/api/routes/portfolio.py tests/test_portfolio_deploy_cash_preflight.py
git commit -m "feat(deploy-funnel): attach shadow preflight to /deploy-cash behind the kill switch"
```

---

## Self-Review

**Spec coverage (Increment 1 rows):** candidate generation (Task 7 maps engine lines) ✓; enrichment quote+history+news (Task 4) ✓; look-through map + effective exposure (Task 2) ✓; reserve policy (Task 3) ✓; deterministic gates → status incl. plan-gap (Task 5) ✓; typed `PlanGap` (Task 1, emitted Task 6) ✓; trace object + reason codes (Tasks 5–6 reasons; DB persistence deferred to Increment 1b, noted) ✓; kill switches + shadow (Task 6) ✓; dollar-conservation invariant (Task 6 test) ✓; fixture replay of the failure book (Task 6 test) ✓. Increments 2 (bounded risk + sizer) and 3 (change-request) are explicitly out of this plan.

**Deferred from the spec (tracked, not dropped):** DB persistence of the trace + the shadow-validation harness (dollar-conservation history, repeat-run stability, accept/reject labels) → Increment 1b; the LLM risk layer + deterministic sizer → Increment 2; plan change-request emission + recompute-against-amended-plan → Increment 3.

**Placeholder scan:** none — every code step carries real code; the only prose step (Task 7 Step 3) describes wiring against interfaces defined in Tasks 1–6.

**Type consistency:** `CandidateStatus`, `HistoryFeatures`, `PlanGap`, `EnrichedCandidate`, `PreflightResult` used identically across Tasks 1/5/6; `GateInputs` fields match between Task 5 def and Task 6 use; `effective_nvda_usd` signature consistent Tasks 2/5/6; `PriceProvider` protocol consistent Tasks 4/6/7.
