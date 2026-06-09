# Argosy Realignment — Make the Plan the Source of Truth (multi-session roadmap)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This is a MULTI-SESSION, PARALLEL-AGENT plan** — read "Execution model" before claiming a task.

**Goal:** Make the canonical, agent-devised plan the single source of truth — an instrument-level (ticker), time-varying target portfolio + transition persisted on `PlanVersion` — and make every UI surface a pure, reconciled projection of it. Then unblock the prime-directive experts, make the transition executable, and purge the magic numbers.

**Architecture:** Hybrid (matches Argosy): the multi-agent panel + deterministic `allocation_plan` engine OWN the numbers; synthesis writes a structured `target_allocation_json` onto the plan-version; every surface reads that one object; a committed cross-surface test fails loudly on drift. Built in dependency order — the spine (P0–P2) is the critical path; P3–P6 fan out in parallel once the spine lands.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy + Alembic (SQLite dev DB), Next.js (UI, port 1337), pytest (`-m "not llm_eval"`), codex-tandem kit for money-math verification.

**Spec basis (read these):**
- `docs/design/SDD.md` → "Why Argosy exists" (the trust contract = the acceptance bar).
- `tmp_review/GAP_MAP.md` (53-row prioritized gap table, file:line) + `tmp_review/codex_gap_verdict.txt`.
- `docs/superpowers/specs/2026-06-08-dynamic-allocation-owner-and-long-hold-fleet-design.md` (the time-varying allocation design this realizes).
- Memory: `feedback_argosy_purpose_trust_contract`, `feedback_output_trust_doctrine`, `feedback_plan_ui_one_canonical_source`.

**The acceptance bar (the trust contract — every task serves it):** every displayed number is Argosy-derived from the canonical plan + raw data, auditable, and self-consistent across `/plan`, `/portfolio`, `/retirement`. A surface that can't reconcile to the plan is a defect. No hardcoded/magic numbers on user surfaces.

---

## Execution model (multi-session, parallel agents)

**Dependency graph (what blocks what):**
```
P0 (stop bleeding) ─┐
                    ├─> P1 (the spine) ──> P2 (rebind + guardrail) ──┬─> P4 (executable transition)
P3a flip-gate ──────┘         (critical path, SERIAL)                ├─> P6 (new ambition)
                                                                     └─> (P3 wiring, P5 surface-purges)
P3 (unblock experts): 3a flip-gate can start anytime; 3b/3c/3d WIRING depends on P1
P5 (magic-number purges): mostly independent small lanes, can start anytime EXCEPT where they touch a P2 surface (coordinate)
```

**Lanes (what can run concurrently, one agent each, after the gate noted):**
- **SPINE (serial, one agent):** P0 → P1 → P2. Nothing else binds correctly until P2 lands the guardrail. Do this first; do not parallelize within the spine.
- **After P2 lands**, open these parallel lanes (separate worktrees, separate agents):
  - Lane A: **P3** experts (3b tax, 3c withdrawal, 3d equity_comp, 3e coverage) — 4 sub-lanes.
  - Lane B: **P4** executable transition (depends on P1 schema + P2 cap-check).
  - Lane C: **P5** magic-number purges — many tiny independent lanes (hishtalmut date, mortgage, FX vol, Vanguard-curve delete, fx.threshold_breach delete, dev-scheduler fix).
  - Lane D: **P6** ambition (long-hold default flip can start anytime; chat + reliability UI after spine).
- **P3a (flip `phase5_agents` default)** and **P6 long-hold default flip** are independent of the spine and may run in parallel from the start by a spare agent.

**Per-session / per-agent protocol:**
1. `git fetch` / pull; open the **master tracker** (below). Claim the lowest-ID unblocked task whose `Depends-on` are all ☑ and `Owner` is empty. Set `Owner` + status `◐`.
2. Work in an **isolated git worktree** (superpowers:using-git-worktrees). **If the task touches `ui/`:** `cd <worktree>/ui ; npm ci` once (~45 s). **NEVER junction-link `<worktree>/ui/node_modules` to main** — it corrupts main's node_modules on worktree removal (see CLAUDE.md / SDD gotchas).
3. **TDD red→green** per task. Run only the touched test file(s): `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider <path>`. No commas in `%`-format f-strings.
4. **Money-math tasks** (allocation %, NVDA shares/value, tax, MC, glide) carry the tag **`[money-math]`** → codex-verify via the tandem kit (`tmp_review/` scripts, `sandbox="danger-full-access"`) before commit. Sub-agents can't run shell — they write + hand-verify; the driving session runs tests + commits.
5. Commit small (one task ≈ one commit). On the spine branch `fix/foundation-remediation-s10`; parallel lanes branch from the post-P2 commit.
6. **Tick the checkbox** in this file, set status ☑ + a one-line evidence note (test name / file:line), commit the doc. Hand off.

**Status legend:** ☐ todo · ◐ in-progress · ☑ done · ⛔ blocked.

---

## Master tracker (the cross-session tick-off list)

| ID | Task | Phase | Lane | Depends-on | Money-math | Status | Owner |
|----|------|-------|------|------------|:---:|:---:|---|
| T0.1 | Correct SDD §20 to current state (allocation LLM-authored, engine unwired) | P0 | SPINE | — | | ☑ | s14 8727451 |
| T0.2 | Delete dead `earliest_feasible_retire_age` import | P0 | SPINE | — | | ☑ | s14 a1d044e |
| T1.1 | `TargetAllocationDoc` pydantic schema (instrument-level + glide) | P1 | SPINE | — | | ☑ | s14 28b1ff9 |
| T1.2 | Add `instruments` (tickers) to the canonical allocation panel | P1 | SPINE | T1.1 | ✓ | ☑ | s14 67a1af0 |
| T1.3 | `build_target_allocation_doc()` — engine → doc (incl. glide) | P1 | SPINE | T1.1,T1.2 | ✓ | ☑ | s14 6f5b28f |
| T1.4 | Migration `0063` + `PlanVersion.target_allocation_json` column | P1 | SPINE | — | | ☑ | s14 7371557 |
| T1.5 | Write the doc during synthesis (orchestrator + amendment) | P1 | SPINE | T1.3,T1.4 | | ☑ | s14 13e825f |
| T1.6 | `load_plan_target_allocation(pv)` reader + backfill current plan | P1 | SPINE | T1.5 | | ☑ | s14 4b94762/13e825f |
| T2.1 | Rebind `/plan` glidepath → the doc's glide (full-book, incl NVDA) | P2 | SPINE | T1.6 | ✓ | ☑ | s14 50fb04d |
| T2.2 | Rebind `/portfolio` target → the doc | P2 | SPINE | T1.6 | | ☑ | s14 7ba3602 |
| T2.3 | Rebind `/retirement` glide + cashflow-chart knobs → canonical | P2 | SPINE | T1.6 | ✓ | ☑ | s14 0f54a22/5942126 — age (cashflow→canonical 46) + glide (/glide-path projects the doc's equity/bond/cash 78.7/6.4/14.9, not Vanguard). MC unaffected (uses separate σ-glide). T5.4 (delete Vanguard fallback) now unblocked |
| T2.4 | Rebind NVDA trajectory → `nvda_projection` (wire the orphan, G13) | P2 | SPINE | T1.6 | ✓ | ☑ | s14 7a2bd5f — wired + killed the 18.21 full-book bug |
| T2.5 | **Cross-surface reconciliation guardrail test** (the 12-session ask) | P2 | SPINE | T2.1–T2.4 | | ☑ | s14 50fb04d/7ba3602/5942126 — guardrail (test_cross_surface_consistency, 4 tests) asserts glidepath + portfolio + retirement-glide all reconcile to the doc. Age consistent-by-construction: /plan cashflow + /retirement both source canonical_feasible_dual_track |
| T2.6 | Enable `plan_gate_enforce` default + extend gate to charts/portfolio | P2 | SPINE | T2.5 | | ☑ | s14 52a541e — default flipped fail-closed; mechanics tests pinned warn-only. Runtime gate-extension-to-charts deferred (charts already reconcile to the doc via T2.1/T2.2 + the guardrail enforces it in tests) |
| T3.1 | Flip `phase5_agents` default on (gate off) | P3 | A0 | — | | ☐ | |
| T3.2 | Wire EquityComp (RSU net savings + FV trajectory) resolver path | P3 | A1 | T1.6,T3.1 | ✓ | ☐ | |
| T3.3 | Wire Withdrawal Sequencer (FI-bridge waterfall) resolver path | P3 | A2 | T1.6,T3.1 | ✓ | ☐ | |
| T3.4 | Wire real tax engine into MC; retire flat-10%/surtax-off shortcut | P3 | A3 | T1.6,T3.1 | ✓ | ☐ | |
| T3.5 | Wire PlanCoverageAnalyst output to a surface | P3 | A4 | T3.1 | | ☐ | |
| T4.1 | Plan→proposal generator: diff target vs holdings → keep/trim/add | P4 | B | T1.6 | ✓ | ◐ | s15 92d02f7 — `plan_proposal_diff.diff_plan_vs_holdings`: doc instrument targets vs holdings → per-ticker keep/trim/add (value-based, closed-book Σdelta=0). **Codex-verified** money-math. 5 green. CORE diff done; persistence to `action_proposals` (write surface) + live cap-check are T4.3/T4.4 |
| T4.2 | Wire `optimize_deconcentration` to choose the NVDA taper | P4 | B | T1.6 | ✓ | ☑ | s15 f977fde — `build_plan_target_allocation_doc` derives the glide horizon from the (orphaned) optimizer: `quarters = H×4` via `_deconcentration_quarters`; best-effort fallback to 8q. Displayed transition now spans the optimizer's chosen sell-down horizon. **Codex-verified**. 11 doc + smoke 127 green |
| T4.3 | Load `plan_targets` server-side; make exec cap-check live | P4 | B | T4.1 | | ☑ | s15 059a7f5 — `/proposals/{id}/execute` defaults `plan_targets` to server-derived (`load_plan_targets` from the canonical doc); caller-supplied is override only. Was silently `{}` → no-op cap (G21). 13 green + create_app + smoke |
| T4.4 | Add `plan_version_id` to `Proposal` (audit lineage) | P4 | B | T4.1 | | ☑ | s15 — migration 0064 + `Proposal.plan_version_id` (nullable audit ref); stamped best-effort in `_persist_proposal` (current plan at persist time; NULL when none, never fabricated). 23 flow/proposal/exec + 2 lineage green; migration applies (0063→0064) |
| T4.5 | Auto-route concentration-breach tranche to approval/broker | P4 | B | T4.1,T4.3 | ✓ | ☑ | s15 — `breach_router.route_breach_tranche` in the monthly cycle: NVDA breach → ONE `awaiting_human` sell tranche (over_cap ÷ H×4 doc-horizon quarters), stamped `plan_version_id`, idempotent (rationale marker, ≤1 open). NEVER executes. **Codex-verified** [money-math]+capability boundary. 4 green. Follow-on: min-cadence guard |
| T5.1 | Derive/intake hishtalmut first-deposit date (drop 2018-01-01) | P5 | C1 | — | | ☑ | s15 d7771c8 — `derived_inputs` emits `hishtalmut_first_deposit_date` (intake; pending→needs-intake note, never a fabricated 2018-01-01). New page `strOf` accessor. test_derived_inputs 8 green; ui tsc/lint/vitest clean |
| T5.2 | Derive/intake mortgage rate/term (drop 4.5%/20yr) | P5 | C2 | — | | ☑ | s15 d7771c8 — `mortgage_annual_rate` + `mortgage_term_months` from identity_yaml (pending→needs-intake); `RealEstateMortgageCard` dropped its internal `?? 0.045`, draws amortization only with a real rate+term |
| T5.3 | Derive FX σ/μ (drop frozen 0.08/0) | P5 | C3 | — | ✓ | ☑ | s15 96da397/ec24551 — **Option A**: derive σ from history, hold μ=0 (driftless). Codex+SE rationale: a ~10y sample can't estimate a 30y drift (SE≈σ/√T≈2.5%/yr; Meese-Rogoff), and a derived log-μ also mishandled the Itô σ²/2 term. Realized drift logged for audit, not extrapolated. Dormant on dev DB (<24mo → fallback). 18 green |
| T5.4 | Delete Vanguard glide-curve fallback (use canonical glide) | P5 | C4 | T2.3 | | ☑ | s15 ca27afe — `rebalancing.py` now targets the canonical doc (`doc_equity_bond_cash`), not `target_at_age`(Vanguard); `/glide-path` route drops its Vanguard fallback (no doc→"no plan" note); `glide_path.py` DELETED (G7). Rebalancing + /glide-path reconcile to the same doc target by construction. wave4+route 31 green |
| T5.5 | Remove `fx.threshold_breach` + manual `check_*` per-symptom detectors | P5 | C5 | — | | ☑ | s15 9522bc6 — emergent observer only; `check_mc_regression` retained (observer reads no MC P(solvent)); tests rewritten to assert removal + match `state_diff` comparator map. hour_loop/monitor_drift/monitor_macro_shift green |
| T5.6 | Fix dev `argosy run` scheduler (boot observer + predictions-evaluator) | P5 | C6 | — | | ☑ | s15 e7a76e3 — registers `state_observer_daily` + `predictions_evaluator` in `register_default_loops` (so `argosy run` boots them, not just FastAPI startup); double-register harmless (dict-by-name overwrite). scheduler/lifecycle/jobs_registry 52 green |
| T5.7 | Single tax-band source (drop triplicated 0.25/0.15/0.12, add surtax) | P5 | C7 | T3.4 | ✓ | ☐ | |
| T6.1 | Default decision fleet to long-hold; disable minute/hour cadences | P6 | D0 | — | | ☑ | s15 a90c0d5 + bc5b25f — default mode `long_hold`; minute/hour cadences default off. Pinned `mode="tactical_trade"` on 6 per-ticker tests; made `test_phase7_loops_registered_by_default` hermetic (was green only via a stale on-disk yaml — latent CI failure). 43 green |
| T6.2 | Source-reliability/predictions-ledger API + UI | P6 | D1 | — | | ☐ | |
| T6.3 | Proactive web-push wired to real events (not just test) | P6 | D2 | — | | ☐ | |
| T6.4 | Bidirectional Discord (inbound→system→outbound reply) | P6 | D3 | spine | | ☐ | |
| T6.5 | WhatsApp/Telegram channel | P6 | D4 | T6.4 | | ☐ | |

## Known test-infra debt (deferred — s14, record correction)

Full backend suite after the spine: **3692 passed / 9 failed / 16 skipped (44 min)**. All 9 are **pre-existing, non-product, isolation/network flakiness** — **zero touch the spine code** (every spine surface test + the cross-surface guardrail are green; the gate flip didn't break promotion mechanics):

- **4 caplog/structlog full-suite isolation** — `test_allocation_glidepath` ×2, `test_lifecycle`, `test_plan_language_rewriter`. Pass in isolation; fail only in the full suite (a structlog/caplog global-state contaminator). s14's flaky-fix (`d5faca1`) fixed `test_cadence_loop_tick_widening` + addressed the named root causes (structlog `cache_logger_on_first_use=False`, conftest `logging.disable` reset + `clear_contextvars`), but a **further full-suite contaminator remains** → needs a contaminator bisect. **DEFERRED** (test-infra, not product). **Record correction:** `d5faca1`'s message over-states "repair 5 failures" — the true outcome is **1 fixed + 4 root-caused-but-still-failing** in the full suite (I committed on repro-evidence before the full-suite confirmation).
- **5 discord network/mock** — `test_discord_attachment_fetch` ×5. Environment-dependent; the failing count varies run-to-run.

> Each phase's tasks are detailed below. **P0–P2 are fully specified (executable now).** P3–P6 are specified at work-package granularity with files + acceptance + dependencies; **author a detailed per-task plan at phase entry** (`docs/superpowers/plans/2026-06-09-pN-<name>.md`) before executing — this is deliberate decomposition (each phase is its own testable subsystem), not a placeholder.

---

## The keystone: the `target_allocation_json` schema

One structured object, written by the deterministic engine, read by every surface. Define in `argosy/services/target_allocation_doc.py`:

```python
class AllocationInstrument(BaseModel):
    symbol: str                      # e.g. "VOO"
    role: Literal["primary", "alt", "hold", "exit"]
    weight_within_class_pct: float   # sums to 100 within its class
    rationale: str = ""

class AllocationClassDoc(BaseModel):
    label: str                       # "US broad-market core"
    snapshot_category: str           # "Core Equity" (exact snapshot-anchor key)
    sigma_class: str
    target_pct: float                # % of the FULL tradeable book (classes sum to ~100)
    instruments: list[AllocationInstrument]
    agreement: str = ""
    rationale: str = ""
    dissent: str = ""

class GlideWaypoint(BaseModel):
    quarter: int
    date: date
    composition_pct_by_class: dict[str, float]   # sums to 100 each quarter

class TargetAllocationDoc(BaseModel):
    schema_version: int = 1
    basis: str = "full tradeable book"
    anchor_sigma: float
    blended_sigma: float
    nvda_cap_pct: float              # the 13% ceiling
    fi_pct: float                    # derived
    provenance: str
    classes: list[AllocationClassDoc]
    glide: list[GlideWaypoint]       # today -> target over N quarters (the redistribution schedule)
```

This is the substrate that fixes G3/G20/G25/G26/G34 at once: it is instrument-level (`instruments`), canonical (engine-authored), and time-varying (`glide`).

---

## Phase 0 — Stop the bleeding (SPINE, ~½ day)

### Task T0.1: Correct SDD §20 to describe current state

**Files:** Modify `docs/design/SDD.md` (§20 — the canonical-wiring section).

- [ ] **Step 1:** Read §20. Replace any present-tense claim that the canonical allocation governs the surfaces with the true current state: "Today the `/plan` glidepath and `/portfolio` target read LLM-authored horizon targets and the imported TSV respectively; the canonical `allocation_plan` engine is implemented but not yet wired (see `docs/superpowers/plans/2026-06-09-argosy-realignment-roadmap.md`)." Per `feedback_docs_current_state_only` — no history, just current truth.
- [ ] **Step 2:** Commit: `docs(sdd): §20 describe current allocation wiring (LLM-authored, engine unwired)`.

### Task T0.2: Delete the dead retirement import

**Files:** Modify `argosy/api/routes/retirement.py:30` (and any use at `:367`).

- [ ] **Step 1:** Confirm `earliest_feasible_retire_age` is unused: `rg -n "earliest_feasible_retire_age" argosy/`. Expect only the import + a superseded reference.
- [ ] **Step 2:** Remove the import and the dead reference.
- [ ] **Step 3:** Run `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider tests/test_retirement_route.py -q`. Expected: PASS.
- [ ] **Step 4:** Commit: `chore(retirement): drop dead earliest_feasible_retire_age import`.

---

## Phase 1 — The spine: canonical instrument-level plan object (SPINE)

### Task T1.1: `TargetAllocationDoc` schema

**Files:** Create `argosy/services/target_allocation_doc.py`; Test `tests/test_target_allocation_doc.py`.

- [ ] **Step 1 — failing test:**
```python
from datetime import date
from argosy.services.target_allocation_doc import (
    TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
)

def test_doc_is_instrument_level_and_roundtrips():
    doc = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=21.3,
        provenance="panel", classes=[
            AllocationClassDoc(label="Strategic single-stock (NVDA)", snapshot_category="Individual Stocks",
                sigma_class="concentrated_equity", target_pct=12.0,
                instruments=[AllocationInstrument(symbol="NVDA", role="primary", weight_within_class_pct=100.0)]),
        ],
        glide=[GlideWaypoint(quarter=1, date=date(2026, 9, 1), composition_pct_by_class={"Strategic single-stock (NVDA)": 12.0})],
    )
    again = TargetAllocationDoc.model_validate_json(doc.model_dump_json())
    assert again.classes[0].instruments[0].symbol == "NVDA"
    assert again.glide[0].composition_pct_by_class["Strategic single-stock (NVDA)"] == 12.0
```
- [ ] **Step 2:** Run it — expect ImportError/fail.
- [ ] **Step 3:** Implement the pydantic types exactly as in "The keystone" section above.
- [ ] **Step 4:** Run — expect PASS.
- [ ] **Step 5:** Commit: `feat(plan): add TargetAllocationDoc canonical instrument-level schema`.

### Task T1.2 `[money-math]`: instruments (tickers) on the canonical allocation

**Files:** Modify `argosy/services/allocation_plan.py` (`_PanelSleeve`/`AllocationClass` + `_NVDA_SLEEVE` + the sleeve table ~`:81-203`); Test `tests/test_allocation_plan.py`.

- [ ] **Step 1 — failing test:** assert each class from `build_target_allocation()` carries `instruments` whose `weight_within_class_pct` sums to 100, NVDA→`[NVDA]`, dividend→contains `SCHD`, core→contains `VOO`. (Instruments are the panel's agreed names — sourced, not magic; carry each instrument's rationale.)
- [ ] **Step 2:** Run — fail (no instruments field).
- [ ] **Step 3:** Add `instruments: tuple[AllocationInstrument, ...]` to each `_PanelSleeve` + `_NVDA_SLEEVE`, encoding the panel's agreed tickers (from the existing prose rationale — VOO/VTI, SCHD, etc.) with per-instrument rationale; surface them through `build_target_allocation`'s `AllocationClass`.
- [ ] **Step 4:** Run — PASS. **codex-verify** the within-class weights + that class targets still sum to ~100.
- [ ] **Step 5:** Commit: `feat(allocation): name canonical instruments (tickers) per asset class`.

### Task T1.3 `[money-math]`: `build_target_allocation_doc()` (engine → doc + glide)

**Files:** Modify `argosy/services/target_allocation_doc.py` (add builder); Test `tests/test_target_allocation_doc.py`.

- [ ] **Step 1 — failing test:** `build_target_allocation_doc(today=date(2026,6,9))` returns a `TargetAllocationDoc` where: classes are instrument-level; `nvda_cap_pct==13.0`; `glide` has 8 quarterly waypoints each summing to ~100; the final waypoint matches the target composition; NVDA glides down (q1 NVDA% > final NVDA%).
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Implement: call `allocation_plan.build_target_allocation()`; build the glide via `allocation_plan.build_redistribution_schedule(today_composition=<from snapshot full-book>, target=alloc, start=today)`; map into `TargetAllocationDoc`. (today_composition = current full-book composition incl. NVDA, computed from the snapshot — this is the basis decision: full tradeable book.)
- [ ] **Step 4:** Run — PASS. **codex-verify** the glide waypoints + the full-book today-composition math (the basis we settled: NVDA ~60–65% → 12%).
- [ ] **Step 5:** Commit: `feat(plan): build canonical TargetAllocationDoc from the deterministic engine`.

### Task T1.4: migration + `PlanVersion.target_allocation_json`

**Files:** Create `alembic/versions/0063_plan_target_allocation.py` (pattern: `0062_plan_narrative_persistence.py`); Modify `argosy/state/models.py` (PlanVersion, near `:173`).

- [ ] **Step 1:** Add `target_allocation_json: Mapped[str | None] = mapped_column(Text, nullable=True)` to `PlanVersion`.
- [ ] **Step 2:** Write migration `0063` (down_revision `0062`) adding the nullable `target_allocation_json` Text column, mirroring `0062`.
- [ ] **Step 3:** Apply: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m alembic upgrade head`. Verify `pytest tests/test_migrations.py -q` (or the schema test) PASS.
- [ ] **Step 4:** Commit: `feat(db): migration 0063 — PlanVersion.target_allocation_json`.

### Task T1.5: write the doc during synthesis

**Files:** Modify `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:844-846` (the `PlanVersion(...)` construction) and `argosy/orchestrator/flows/plan_amendment/workers.py:~230`.

- [ ] **Step 1 — failing test:** a synthesis-flow test asserting the persisted `PlanVersion.target_allocation_json` parses to a `TargetAllocationDoc` that is instrument-level. (Use existing synthesis fixtures; if heavy, test the helper that builds the kwarg.)
- [ ] **Step 2:** Run — fail.
- [ ] **Step 3:** Add `target_allocation_json=build_target_allocation_doc(today).model_dump_json()` to both `PlanVersion(...)` constructions. Stamp `generated_at` from the caller, not inside the engine.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit: `feat(plan): persist canonical TargetAllocationDoc on every synthesized plan`.

### Task T1.6: reader + backfill the current plan

**Files:** Modify `argosy/services/target_allocation_doc.py` (add `load_plan_target_allocation(pv) -> TargetAllocationDoc | None`); a one-shot backfill in `tmp_review/backfill_target_allocation.py`.

- [ ] **Step 1 — failing test:** `load_plan_target_allocation(pv)` returns a parsed doc when the column is set, `None` when empty (never raises).
- [ ] **Step 2:** Run — fail. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Backfill the live current plan (v30) so surfaces have data before P2: run the backfill script; verify `load_plan_target_allocation(current_plan)` is instrument-level on the dev DB.
- [ ] **Step 6:** Commit: `feat(plan): reader for the canonical TargetAllocationDoc (+ backfill current plan)`.

**Phase 1 acceptance:** the current plan-version owns a structured, instrument-level, time-varying allocation doc, authored by the deterministic engine; `load_plan_target_allocation` returns it. The orphaned engine (G3) is now wired; the instrument substrate (G25/G20) exists.

---

## Phase 2 — Rebind surfaces + the reconciliation guardrail (SPINE)

> Guardrail-first within this phase: write T2.5 early (red), let it drive T2.1–T2.4 green.

### Task T2.1 `[money-math]`: rebind `/plan` glidepath to the doc

**Files:** Modify `argosy/services/allocation_glidepath.py` (`compute_allocation_glidepath:867`, `_targets_from_plan:819`); Test `tests/test_allocation_glidepath.py`.

- [ ] **Step 1 — failing test:** with a plan whose `target_allocation_json` is set, `compute_allocation_glidepath` returns points whose composition comes from `doc.glide` (full-book, incl. an NVDA band declining to ~12%), NOT from `pv.horizon_*_json` SynthTargets; every tick sums to ~100.
- [ ] **Step 2:** Run — fail. **Step 3:** When `load_plan_target_allocation(pv)` is present, build the glidepath from `doc.glide` waypoints; fall back to the legacy path only when the doc is absent. **Step 4:** PASS; `TestGlidepathSumsTo100` still green. **codex-verify** the NVDA band + sleeve weights.
- [ ] **Step 5:** Commit: `fix(plan): glidepath renders the canonical TargetAllocationDoc, not LLM targets (G5)`.

### Task T2.2: rebind `/portfolio` target to the doc

**Files:** Modify `argosy/api/routes/portfolio.py:120-126`; Test `tests/test_portfolio_route.py`.

- [ ] **Step 1 — failing test:** `/api/portfolio/snapshot` AllocationDTO `target_pct` per category equals the doc's class `target_pct` (mapped via `snapshot_category`), not the TSV column.
- [ ] **Step 2:** fail → **Step 3:** read targets from `load_plan_target_allocation(current_plan)`; TSV target only as fallback. → **Step 4:** PASS.
- [ ] **Step 5:** Commit: `fix(portfolio): target pie reads the canonical plan, not the imported TSV (G6)`.

### Task T2.3 `[money-math]`: rebind `/retirement` glide + cashflow-chart knobs

**Files:** Modify `argosy/services/retirement/glide_path.py:32-53`, `argosy/api/routes/plan.py:1308-1309`, `ui/src/components/plan/cashflow-projection-chart.tsx:97-103,222-228`; Tests accordingly.

- [ ] **Step 1 — failing test (backend):** the retirement glide + rebalancing sizing derive from the canonical doc (NVDA-aware), not the textbook Vanguard curve; the cashflow-chart age input equals the canonical headline age.
- [ ] **Step 2–4:** fail → wire to canonical (`load_plan_target_allocation` + canonical age from the resolver) → PASS. **codex-verify** the age/glide reconciliation.
- [ ] **Step 5:** Commit: `fix(retirement): glide + chart knobs bind to the canonical plan (G7,G8)`.

### Task T2.4 `[money-math]`: rebind NVDA trajectory to `nvda_projection` (wire the orphan)

**Files:** Modify `argosy/api/routes/plan.py:1753` (`get_draft_nvda_trajectory`) + `ui/src/components/plan/nvda-trajectory-chart.tsx`; reuse `argosy/services/nvda_projection.py` (built this session) + `compute_nvda_projection`.

> **CODEX-CONFIRMED BUG to fix here (s14, `tmp_review/codex_fullbook_verdict.txt`):** `compute_nvda_projection` sources `fullbook_current_pct` via `_resolve_today_value("nvda", …)` → snapshot category `"Individual Stocks"` = **18.21%**, which is the OTHER singles (TSLA/AMD/GOOG/AMZN/META/SOFI/RKT, ~$261k), **not** NVDA. NVDA is a separate `positions_json` row (11,471 sh / $2,296k); its canonical weight is **64.86%** (`concentration.nvda_current_pct`, the resolver value — already verified consistent). The *share* + *tradeable* math (64.86→13, 11,471→~2,300) is correct; only the **full-book band is wrong**. Fix: drop the `"Individual Stocks"` full-book source; the canonical glide band now comes from the `TargetAllocationDoc` (NVDA 64.86→12, q0-anchored). Reconcile the trajectory chart's implied weight to the doc, not to `identity_yaml` or the other-singles row.

- [ ] **Step 1 — failing test:** the endpoint's today/target shares + the implied weight come from `compute_nvda_projection` (11,471 → ~2,300; tradeable 64.86% → 13%), with a target line; vest/sell dates shown; NOT from `identity_yaml`.
- [ ] **Step 2–4:** fail → wire to `compute_nvda_projection`; flow from `nvda_sales_history._annual_nvda_target_from_plan` → PASS. **codex-verify** (already verified the share math: `tmp_review/codex_nvda_verdict.txt`).
- [ ] **Step 5:** Commit: `fix(plan): NVDA trajectory binds to the canonical projection (G11,G13)`.

### Task T2.5: the cross-surface reconciliation guardrail (THE 12-session ask)

**Files:** Create `tests/test_cross_surface_consistency.py`.

- [ ] **Step 1 — failing test:** on a seeded plan with `target_allocation_json` set, assert ALL of:
  - `/plan` headline retirement age == cashflow-chart age == dual-track canonical age (within tolerance);
  - `/portfolio` target per category == the doc's class target_pct;
  - glidepath NVDA band at t0 and target == `compute_nvda_projection` (same basis);
  - every glidepath tick sums to ~100.
  Fails loudly on any drift.
- [ ] **Step 2:** Run — RED before T2.1–T2.4 land; GREEN after. **Step 3:** keep it green.
- [ ] **Step 4:** Commit: `test(plan): cross-surface consistency guardrail — surfaces must reconcile to the plan (G15,G16)`.

### Task T2.6: enable the plan gate + extend it

**Files:** Modify `argosy/config.py:116` (`plan_gate_enforce` default → `True`); extend `argosy/services/numeric_source_gate.py` to cover chart/portfolio numerics, not just narrative.

- [ ] **Step 1 — failing test:** with the gate on, a surface number not traceable to the plan fails the gate.
- [ ] **Step 2–4:** fail → default on + extend coverage → PASS (full touched-suite green).
- [ ] **Step 5:** Commit: `feat(plan): enforce the trust contract by default (G27, plan_gate_enforce=True)`.

**Phase 2 acceptance (the trust contract, realized):** change the plan → `/plan`, `/portfolio`, `/retirement` all move together; the guardrail test fails on drift; no surface number is un-reconcilable. **This is the deliverable that closes the 12+-session recurring failure.** Re-run the full suite + live-verify all three surfaces before opening P3–P6 lanes.

---

## Phase 3 — Unblock the prime-directive experts (parallel lanes after P2)

> Author `docs/superpowers/plans/2026-06-09-p3-unblock-experts.md` at entry. WPs:

- **T3.1 (lane A0, anytime):** `argosy/config.py:126` — default `phase5_agents=True` (or remove the gate). Acceptance: synthesis runs EquityComp + WithdrawalSequencer + PlanCoverage by default; existing tests green.
- **T3.2 `[money-math]` (A1, dep T1.6+T3.1):** add an EquityComp resolver path so `savings.annual_net_nis` + the FV trajectory stop rendering "[derivation pending]" (`render.py:935-958`, `plan_numeric_resolver.py`). Acceptance: net savings + FV are Argosy-derived + auditable.
- **T3.3 `[money-math]` (A2):** wire the Withdrawal Sequencer's structured FI-bridge waterfall into the plan + resolver (`resolver.py:887-917`). Acceptance: a real withdrawal schedule renders; synth prompt no longer cites an agent that never runs.
- **T3.4 `[money-math]` (A3):** wire the real `tax_engine` into the MC (`retirement.py:619`, `retirement_plan.py:65,253,270`); retire the flat-10%/surtax-disabled shortcut (G19,G31,G37). Acceptance: calculator and plan agree on identical money; codex-verified.
- **T3.5 (A4):** consume `PlanCoverageAnalyst` output on a surface (gap self-assessment). Acceptance: coverage gaps surface to the user.

---

## Phase 4 — Make the transition executable (lane B after P2)

> Author `docs/superpowers/plans/2026-06-09-p4-executable-transition.md` at entry. WPs:

- **T4.1 `[money-math]`:** plan→proposal generator — diff `target_allocation_json` (incl. instruments) vs current holdings → per-ticker keep/trim/add (`decisions.py:191`, `action_proposals.py:216-227`). Acceptance: "buy X sh VOO, trim Y sh NVDA" proposals derive from the plan.
- **T4.2 `[money-math]`:** wire `optimize_deconcentration` (`deconcentration_optimizer.py:157,324`) to choose the NVDA taper instead of the fixed 3-yr (G22,G29).
- **T4.3:** load `plan_targets` server-side from the canonical plan (`execution.py:49-56`, `router.py:158-167`) so the concentration cap-check is live, not caller-supplied (G21).
- **T4.4:** add `plan_version_id` FK to `Proposal` (`models.py:642-683`) for audit lineage.
- **T4.5 `[money-math]`:** auto-route a concentration-breach tranche to approval/broker (`monthly_cycle.py:275-283`) (G4).

---

## Phase 5 — Purge magic numbers + per-symptom detectors (many small parallel lanes, anytime)

> Each is a tiny independent lane; author `docs/superpowers/plans/2026-06-09-p5-purge.md` listing them. WPs (file:line from the gap map):
- **T5.1:** hishtalmut first-deposit date → intake/derive (`retirement/page.tsx:256-262`, `HishtalmutTimerCard.tsx`) (G42).
- **T5.2:** mortgage rate/term → intake/derive (`retirement/page.tsx:334-337`, `RealEstateMortgageCard.tsx`) (G43).
- **T5.3 `[money-math]`:** FX σ/μ derived, not frozen 0.08/0 (`stochastic_fx.py:33,57`) (G52).
- **T5.4 (dep T2.3):** delete the Vanguard glide-curve fallback (`glide_path.py:32-53`) (G7 cleanup).
- **T5.5:** remove `fx.threshold_breach` (`hour_loop.py:59,99`) + manual `check_*` detectors (`plan_monitor.py:159,680`) — emergent observer only (G38,G48).
- **T5.6:** dev `argosy run` boots observer + predictions-evaluator (`cli/run.py:28-29`, `main.py:472-476`) (G39).
- **T5.7 `[money-math]` (dep T3.4):** single tax-band source + surtax (drop triplicated `0.25/0.15/0.12`, `tax_curve.py:31-33`) (G37).

---

## Phase 6 — New ambition (parallel lanes; chat/reliability after spine)

> Author `docs/superpowers/plans/2026-06-09-p6-ambition.md` at entry. WPs:
- **T6.1 (D0, anytime):** default the decision fleet to `long_hold`; disable minute/hour cadences (`decisions/flow.py:211`, `agent_settings.yaml:5,12`) — the parked spec's Component 2 (G41).
- **T6.2 (D1):** source-reliability/predictions-ledger API + UI (`reliability.py`) (G23).
- **T6.3 (D2):** wire proactive web-push to real events, not just `test_push` (`notifications.py:46,416`) (G46).
- **T6.4 (D3, dep spine):** bidirectional Discord — outbound writer/DM handler so a reply reaches the system and Argosy responds (`discord_listener.py:53-67`) (G24,G45).
- **T6.5 (D4, dep T6.4):** WhatsApp/Telegram channel (G47) — the user's named preferred channel.

---

## Self-review (author's check)

- **Spec coverage:** every CRIT/HIGH gap in `GAP_MAP.md` maps to a task — G1→T4.1, G2/G34→T1.3+T1.5(glide), G3→T1.2/T1.3/T1.5, G5→T2.1, G6→T2.2, G7/G8→T2.3, G9/G10/G36/G44→T3.2/T3.3/T3.5, G11/G13→T2.4, G15/G16→T2.5, G17/G18/G19→T3.4/T5.3, G20/G25/G26→T1.1/T1.2/T1.5, G21→T4.3, G22/G29→T4.2, G23→T6.2, G24/G45/G47→T6.4/T6.5, G27→T2.6, G41→T6.1, G42/G43/G52→T5.1/T5.2/T5.3, G38/G48→T5.5, G39→T5.6, G37→T5.7, G35→T0.1, G50→T0.2.
- **Dependency sanity:** the spine (P0→P1→P2) is serial; all parallel lanes depend on T1.6 (the reader) and/or T2.5 (the guardrail) where they touch surfaces.
- **No placeholders in P0–P2** (real test code, exact paths, exact commands). P3–P6 are WP-level by design — each gets a detailed per-task plan authored at phase entry (stated explicitly, not "TBD").
- **Type consistency:** `TargetAllocationDoc` / `AllocationClassDoc` / `AllocationInstrument` / `GlideWaypoint` and `load_plan_target_allocation` / `build_target_allocation_doc` are used consistently across T1.1–T2.5.
