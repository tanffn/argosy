# Allocation + Discovery + UX — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. This master plan has four internal phases; build them in order (each is independently shippable + testable).

**Goal:** Turn the canonical Argosy plan into trustworthy, executable allocation tasks AND a fleet-graded high-potential discovery surface, then re-shape the UX so it's one coherent action hub.

**Architecture:** A deterministic allocation core (Phase 1a) feeds a thin judgment agent (Phase 1b). A cheap→expensive discovery funnel (Phase 2) sources high-growth names. A UX shell (Phase 3) folds it all into the Proposals hub. Each phase reuses, never rebuilds, existing substrate (`diff_plan_vs_holdings`, `TargetAllocationDoc`, the consult fleet, the trend radar, the speculative monitor, the scheduler).

**Tech Stack:** Python 3.12 / FastAPI / pydantic / pytest / Alembic; Next.js (client components) for UI. Backends: `claude_code` (CLI auth). Estimator=Sonnet, agents/fleet=Opus.

---

## Cross-phase data contracts (defined once, used everywhere)

These types are the seams between phases. Define them in Phase 1a / 2 and keep names stable.

- `AllocationCandidate{kind: BUY|TRIM|SWAP, legs[AllocationLeg], horizon, est_tax_nis?, surtax_split_suggested, rationale, cites[]}` — Phase 1a output, Phase 1b input.
- `ExecutableTask{seq, candidate: AllocationCandidate, horizon: now|this_quarter|later, pace: lump|tranched, pace_rationale, rationale, cites[]}` — Phase 1b output.
- `TrendCandidate` (exists, `trend_radar.py`) → `EstimatorVerdict{ticker, go: bool, conviction: HIGH|MED|LOW, sentiment: float, one_line}` (Phase 2) → `FleetPick{ticker, conviction, thesis_md, verdict, cites[]}` (Phase 2) → feeds the sleeve discovery list.
- `ScanState` (Phase 2 DB row) — last radar score + estimator/fleet verdicts per ticker, for smart-refresh diffing.

---

## Phase 1a — Deterministic allocation engine + rebind

**Detailed task steps live in `docs/superpowers/plans/2026-06-12-slice1a-allocation-engine.md`** (9 TDD tasks, complete code). Summary of what it delivers (build this phase first, exactly as written there):

1. Engine value objects + `REPLACES_SYMBOLS` map (`allocation_engine.py`).
2. `class_targets_as_of(doc, as_of)` — glide-aware class targets.
3. `target_values_by_symbol` + `tradeable_holdings` adapter (cash separated).
4. `cash_only_deploy` — buy-only, cash-constrained water-fill (codex correctness fix).
5. `rebalance_candidates` — closed-book diff + UCITS swap pairing.
6. `compute_allocation` mode dispatcher.
7. `GET /api/portfolio/allocation-tasks` (deterministic candidates).
8. Rebind `windfall_allocator` to the canonical doc (TSV path retained for legacy consumers) + consumer audit.
9. Suite + live verification against run-96's clean plan.

Exit criterion: "Plan target" everywhere traces to the canonical `TargetAllocationDoc` (glide-aware); the $X-deploy endpoint returns buy-only candidates summing ≤ cash.

---

## Phase 1b — Allocation agent (thin ranker/sequencer/explainer + pace)

**Files:**
- Create `argosy/agents/allocation_agent.py`, `argosy/services/executable_tasks.py`
- Create `tests/test_allocation_agent.py`, `tests/test_executable_tasks.py`
- Modify `argosy/api/routes/portfolio.py` (extend `/allocation-tasks?with_agent=true`)

### Task 1b.1: `ExecutableTask` + deterministic reconciliation validator

- [ ] **Step 1: Write the failing test** (`tests/test_executable_tasks.py`)

```python
from argosy.services.allocation_engine import AllocationCandidate, AllocationLeg
from argosy.services.executable_tasks import ExecutableTask, reconcile_or_raise


def _cand(sym, usd):
    return AllocationCandidate(kind="BUY", horizon="now",
        legs=(AllocationLeg(side="BUY", symbol=sym, account_id="ibkr",
              currency="USD", notional_usd=usd, funding_source="cash"),))

def test_reconcile_passes_when_totals_match():
    c = _cand("CSPX", 1000.0)
    t = ExecutableTask(seq=1, candidate=c, horizon="now", pace="lump",
                       pace_rationale="", rationale="buy core", cites=())
    reconcile_or_raise([t], [c])  # no raise

def test_reconcile_raises_on_invented_number():
    c = _cand("CSPX", 1000.0)
    bad = ExecutableTask(seq=1, candidate=_cand("CSPX", 9999.0), horizon="now",
                         pace="lump", pace_rationale="", rationale="x", cites=())
    import pytest
    with pytest.raises(ValueError):
        reconcile_or_raise([bad], [c])
```

- [ ] **Step 2: Run → FAIL** (`pytest tests/test_executable_tasks.py -q`) — module missing.

- [ ] **Step 3: Implement** (`argosy/services/executable_tasks.py`)

```python
"""ExecutableTask + the hard reconciliation gate: an agent task may only wrap a
deterministic AllocationCandidate; its leg totals must match a 1a candidate
within tolerance (the agent invents no numbers)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from argosy.services.allocation_engine import AllocationCandidate

_TOL_USD = 1.0


@dataclass(frozen=True)
class ExecutableTask:
    seq: int
    candidate: AllocationCandidate
    horizon: Literal["now", "this_quarter", "later"]
    pace: Literal["lump", "tranched"]
    pace_rationale: str
    rationale: str
    cites: tuple[str, ...] = ()


def reconcile_or_raise(tasks: list[ExecutableTask],
                       candidates: list[AllocationCandidate]) -> None:
    """Raise ValueError if any task's notional doesn't match some candidate."""
    pool = [round(c.total_notional_usd, 2) for c in candidates]
    for t in tasks:
        amt = round(t.candidate.total_notional_usd, 2)
        if not any(abs(amt - p) <= _TOL_USD for p in pool):
            raise ValueError(
                f"task seq={t.seq} notional ${amt} reconciles to no 1a candidate")


__all__ = ["ExecutableTask", "reconcile_or_raise"]
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(alloc): ExecutableTask + reconciliation gate`.

### Task 1b.2: `AllocationAgent` (Opus) — order/group/pace/explain

- [ ] **Step 1: Write the failing test** (`tests/test_allocation_agent.py`) — stub the model call; assert the agent's structured output passes reconciliation, orders SELL/SWAP before BUY, and attaches a `pace` to BUY tasks.

```python
import argosy.agents.allocation_agent as aa
from argosy.services.allocation_engine import AllocationCandidate, AllocationLeg

def _c(kind, sym, usd, side):
    return AllocationCandidate(kind=kind, horizon="now",
        legs=(AllocationLeg(side=side, symbol=sym, account_id="ibkr",
              currency="USD", notional_usd=usd, funding_source="cash"),))

def test_agent_orders_and_paces(monkeypatch):
    cands = [_c("BUY","CSPX",1000,"BUY"), _c("SWAP","SCHD",500,"SELL")]
    # stub the LLM to return a deterministic ordering payload
    monkeypatch.setattr(aa, "_run_model", lambda prompt: {
        "tasks": [
            {"candidate_index": 1, "horizon": "this_quarter", "pace": "lump",
             "pace_rationale": "", "rationale": "swap first"},
            {"candidate_index": 0, "horizon": "now", "pace": "tranched",
             "pace_rationale": "VIX elevated", "rationale": "deploy core"},
        ]})
    tasks = aa.order_and_explain(cands, verdicts={}, market_context={"vix": 28})
    assert [t.candidate.kind for t in tasks] == ["SWAP", "BUY"]
    assert tasks[1].pace == "tranched"
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `argosy/agents/allocation_agent.py`: build an Opus prompt that receives the candidates (indexed), the per-position verdicts, and a market-context snapshot; instruct it to ONLY reorder/group/pace/explain by `candidate_index` (never new numbers); parse the JSON into `ExecutableTask[]` keyed back to the input candidates; call `reconcile_or_raise`. `_run_model` wraps the existing agent base (`backend=claude_code`, model=Opus). Follow the existing agent pattern in `argosy/agents/base.py`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(agent): allocation agent (order/group/pace/explain, reconciled)`.

### Task 1b.3: Wire `?with_agent=true` into `/allocation-tasks`

- [ ] Add the optional agent pass to the endpoint (on-demand; deterministic candidates still return instantly when false). Test that `with_agent=false` returns only candidates and `true` returns `executable_tasks` that reconcile. Commit.

Exit criterion: clicking "plan my execution" returns an ordered, paced, fully-reconciled task list; numbers all trace to Phase 1a.

---

## Phase 2 — High-potential discovery funnel (radar → estimator → fleet) + combined card + smart refresh

**Files:**
- Create `argosy/agents/quick_estimator.py` (NEW Sonnet agent class), `argosy/services/high_potential_funnel.py`
- Create migration `migrations/versions/0066_trend_scan_state.py`; model in `argosy/state/models.py`
- Modify `argosy/api/routes/portfolio.py` (funnel + combined endpoints), `argosy/orchestrator/loops/speculative_monitor_loop.py` (fold radar refresh into the daily sweep)
- UI: Create `ui/src/components/portfolio/discovery-card.tsx` (combined radar+monitor highlights); modify the sleeve card to a conviction discovery list

### Task 2.1: `ScanState` table (smart-refresh persistence)

- [ ] **Step 1:** Write a test that the model + migration round-trips a row (`tests/test_trend_scan_state.py`): insert `ScanState(user_id, ticker, last_score, estimator_json, fleet_json, last_radar_at, last_estimated_at, last_fleet_at)`, read it back.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add `ScanState` model (mirror existing model style in `models.py`) + Alembic migration `0066` (create table `trend_scan_state`, PK `(user_id, ticker)`, JSON columns, timestamps). Use the existing migration template (copy 0065's header; `op.create_table(...)`).
- [ ] **Step 4:** Run → PASS; `alembic upgrade head` clean.
- [ ] **Step 5:** Commit `feat(db): trend_scan_state for smart discovery refresh (migration 0066)`.

### Task 2.2: Quick estimator agent (Sonnet) — the NEW cheap triage class

- [ ] **Step 1:** Write a test (`tests/test_quick_estimator.py`, stub the model) that `estimate(ticker, radar_context)` returns `EstimatorVerdict{ticker, go, conviction, sentiment, one_line}` and that `go=False` low-conviction names are filtered by `triage(candidates)`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `quick_estimator.py`: a Sonnet (`model="claude-sonnet-4-6"`) single-shot agent — a quick fundamentals+thesis+sentiment screen producing the structured `EstimatorVerdict`. `triage(cands, top_k)` runs the estimator over the radar shortlist and returns the survivors ranked. Follow the agent base pattern; schema-validated output.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(agent): Sonnet quick-estimator triage class`.

### Task 2.3: Funnel orchestration + smart refresh

- [ ] **Step 1:** Write a test (`tests/test_high_potential_funnel.py`, stub radar + estimator + fleet) that `run_funnel(user_id, force=False)`:
  (a) calls the radar; (b) for tickers whose `last_score` is unchanged AND `last_estimated_at` is fresh, **reuses** the stored estimate (smart refresh — no re-estimate); (c) re-estimates only new/changed tickers; (d) escalates the top `go` names to the fleet; (e) persists `ScanState`. Assert that an unchanged ticker is NOT re-estimated.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `high_potential_funnel.py`: `run_funnel` = radar → diff against `ScanState` → estimate only new/changed → escalate top-K `go` to the consult fleet (reuse the existing decision/consult flow on a single ticker) → store. "Changed" = radar score moved > threshold or new ticker. Returns `{picks: FleetPick[], estimated: EstimatorVerdict[], radar: TrendCandidate[], last_refreshed_at}`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(discovery): radar→estimator→fleet funnel with smart refresh`.

### Task 2.4: Combined discovery endpoint + fold into daily loop

- [ ] **Step 1:** Test `GET /api/portfolio/discovery` returns highlights (top picks + any monitor SELL/WATCH) + `last_refreshed_at`; `POST /api/portfolio/discovery/refresh` runs `run_funnel(force=...)` (smart by default). Test the monitor loop also triggers a smart funnel refresh.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add the two routes (read cached `ScanState` for the GET; the POST runs the funnel). Extend `SpeculativeMonitorLoop.tick` to also call `run_funnel(force=False)` (smart) so the daily sweep keeps discovery fresh.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(api): combined discovery endpoint + daily smart refresh`.

### Task 2.5: Sleeve → conviction discovery list (no $); combined UI card

- [ ] **Step 1:** Test (UI) the discovery card renders highlights with conviction/sentiment (no $ amounts), a "last refreshed" stamp, a refresh button, and click-to-zoom; the sleeve list shows conviction not dollars; a "size this" action (C-both-views) reveals conviction-weighted weights only on demand.
- [ ] **Step 2:** Run typecheck → FAIL (component missing).
- [ ] **Step 3:** Implement `discovery-card.tsx` (combines the old `trend-radar-card` + `speculative-monitor-card`: highlights always shown, last-refresh date, refresh button calling `/discovery/refresh`, click-to-zoom into the full radar + monitor). Convert the sleeve card to a conviction/sentiment discovery list (drop the `5% of $250k` framing + the dollar sizing; `source=fleet_validated` from the funnel; "size this" toggle applies conviction weights on demand). Remove the now-superseded `trend-radar-card.tsx` + `speculative-monitor-card.tsx` usages.
- [ ] **Step 4:** `cd ui ; npm run lint ; npx tsc --noEmit` → clean.
- [ ] **Step 5:** Commit `feat(ui): combined discovery card + conviction-only sleeve list`.

Exit criterion: one Discovery card shows fleet-graded high-growth picks (conviction, click-for-rationale) + live exit signals, with a smart refresh that only re-researches new/changed names. No hardcoded sleeve tickers; no `5% of $250k`.

---

## Phase 3 — UX shell (Proposals hub)

**Files:** Modify `ui/src/components/nav.tsx`, `ui/src/app/proposals/page.tsx`, `ui/src/app/consult/page.tsx`; Create `ui/src/components/ui/collapsible-section.tsx`

### Task 3.1: `CollapsibleSection` component + apply to long sections

- [ ] **Step 1:** Test that `CollapsibleSection` renders a header (`title` + a `summary` like "you have 7 actions") collapsed by default and expands on click.
- [ ] **Step 2:** typecheck → FAIL.
- [ ] **Step 3:** Implement the reusable component (header button + `aria-expanded` + animated body). Wrap the long Proposals sections (Action proposals, Discovery, allocation tasks) with it, each header showing a live count ("You have X actions — click to expand").
- [ ] **Step 4:** lint + typecheck clean.
- [ ] **Step 5:** Commit `feat(ui): collapsible sections with count headers`.

### Task 3.2: Nav reorder (Proposals next to Portfolio)

- [ ] **Step 1:** Test the nav order array places `proposals` immediately after `portfolio`.
- [ ] **Step 2/3:** Reorder the `PRIMARY` nav array in `nav.tsx`.
- [ ] **Step 4/5:** lint/typecheck; commit `feat(ui): move Proposals next to Portfolio`.

### Task 3.3: Fold Consult into the Proposals hub

- [ ] **Step 1:** Test that `/consult` redirects/embeds into Proposals and that an "Ask the team" input on Proposals dispatches a consult run whose result lands as a proposal/card.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add an "Ask the team" entry at the top of the Proposals hub that calls the existing consult/decision flow; render its output as a first-class card in the hub. Convert `/consult` to redirect to `/proposals#ask` (keep the route as a deep link). Remove Consult from the primary nav.
- [ ] **Step 4:** lint/typecheck clean; backend consult flow tests still green.
- [ ] **Step 5:** Commit `feat(ui): fold Consult into the Proposals hub (ask-the-team)`.

### Task 3.4: Notes audit

- [ ] **Step 1:** Enumerate every `note`/explanatory string surfaced on Proposals/Portfolio/Discovery (grep `note:` + card descriptions). For each, confirm it's accurate post-refactor (no "advisor seeds", no "5% of $250k", no stale source claims) and useful (says what the number means + its source).
- [ ] **Step 2:** Fix the strings inline (backend DTO notes + UI copy).
- [ ] **Step 3:** Commit `chore(ui): audit + correct surface notes for accuracy`.

Exit criterion: Proposals sits beside Portfolio, is the single action hub (Consult folded in), every long section collapses to a counted header, and all notes are accurate + useful.

---

## Final verification (all phases)

- [ ] Full backend suite: `pytest -m "not llm_eval"` green (pre-existing flaky discord/lifecycle/rewriter excepted — confirm each still passes in isolation).
- [ ] UI: `cd ui ; npm run lint ; npx tsc --noEmit` clean.
- [ ] Live: against run-96's accepted clean plan, `/allocation-tasks?cash_usd=250000` shows UCITS buy-only candidates; `/discovery` shows fleet-graded picks + exit signals with a recent refresh stamp.

## Self-review

- **Spec coverage:** plan-bound source + executable tasks → 1a + 1b; "deploy $X ad-hoc" → 1a cash_only_deploy + endpoint; market sentiment → 1b pace + Phase-2 satellite; fleet-driven sleeve (radar→Sonnet estimator→Opus fleet) → 2.2/2.3; combined card + highlights + last-refresh + refresh button + click-zoom + smart refresh → 2.4/2.5; conviction-not-$ sleeve (C-both-views) → 2.5; nav reorder → 3.2; consult fold → 3.3; collapsible sections → 3.1; notes audit → 3.4; tax advisory → 1a fields.
- **Placeholder scan:** backend-novel tasks carry full code; UI + agent tasks carry exact files, signatures, and test intent with representative code (finalized at task entry; no "TBD"/"handle errors" left).
- **Type consistency:** `AllocationCandidate`/`ExecutableTask`/`EstimatorVerdict`/`FleetPick`/`ScanState` used consistently across phases; `reconcile_or_raise`, `compute_allocation`, `run_funnel`, `estimate/triage`, `order_and_explain` names stable.

## Dependency notes

- Phases 1a/1b bind to run-96's clean UCITS `TargetAllocationDoc`.
- Phase 2 reuses `trend_radar.py`, `speculative_monitor.py`, the consult/decision fleet, and the scheduler — all already committed.
- Migration 0066 is the only schema change.
