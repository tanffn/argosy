# Foundation Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Sub-agents here CANNOT run shell (sandbox denies) — they write + hand-verify; the orchestrator runs the tests. Risky money-math (H1, H3, the VERIFY) MUST be codex-verified (`tmp_review/codex_*.py`, `sandbox="danger-full-access"`).

**Goal:** Make the retirement engine + plan surfaces *correct and self-consistent* — so the earliest-safe retirement age and every derived number are honest and reconcile across /plan, /portfolio, /retirement — before resuming the (parked) dynamic-allocation feature.

**Architecture:** Fix the foundation in priority tiers. Tier 0 = the money-math that sets the headline age (return convention, CGT, late-life costs). Tier 1 = enforce single-source-of-truth on the surfaces. Tier 2 = the allocation visualization pipeline. Tier 3 = the safety/quality gates. Tier 4 = the canonical design doc. Each fix is TDD'd; money-math is codex-verified.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, numpy MC engine, pytest. Tests: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider <path>` (no commas in %-format; use f-strings).

**Provenance:** Adversarial arch/code review (34 confirmed issues, `tmp_review/SESSION9_REVIEW_FINDINGS.md`), codex money-math verdicts (`tmp_review/codex_h1_verdict.md`, `codex_alloc_design_verdict.md`, `codex_alloc_fi_verdict.md`). User decisions: foundation-first; scope = 2 blockers + 15 highs; medium/low/docs-detail deferred.

**Already done this session (green — verify, don't redo):**
- σ-glidepath label matcher: exclusion-aware + `low_vol_equity=0.13` class (`sigma_glidepath.py`, `sigma_calibration.py`). Tests: `tests/test_sigma_glidepath.py` (35 pass).
- **H4** — `/portfolio` central return single-sourced from `RetirementAssumptions.mu_real_typical` via `wealth_dashboard.get_scenario_returns()` (was a stale 0.045). Tests: `tests/test_wealth_dashboard.py` (45 pass).
- **H2** — one CGT model: `scenario_mc.nvda_deconcentration_cgt()` (0.8 gain-fraction + surtax rate, PV-discounted over the taper) replaces the 0.15-of-sale haircut at the NVDA-sale site. Tests: `tests/test_scenario_mc.py`.
- `allocation_plan.py` service + redistribution schedule (Tasks #2/#3) — built + green, but its FI weight (21.3%) was derived on the *under-returning* engine; it will be re-derived after H1. Treat as a component, not final.

---

## TIER 0 — make the headline retirement age honest

### Task 1: H1 — explicit return-basis convention (the keystone)

**Why:** `cashflow_projection` computes `geometric ≈ μ − σ²/2`, treating `mu_real_typical=5%` as an *arithmetic* mean. But "5% real" is conventionally **geometric/compound** (Vanguard VCMM, BNY CMA). So the diversified book actually earns `5% − 0.18²/2 = 3.38%` compound → the engine **under-returns → the earliest-safe age is biased LATER (over-conservative)** — the prime-directive anti-goal. The `−σ²/2` math is correct; the fix is to stop feeding a geometric number where arithmetic is expected. (codex verdict: `tmp_review/codex_h1_verdict.md`.)

**Files:**
- Modify: `argosy/services/cashflow_projection.py` (the `project_monte_carlo` signature + the two drift computations, ~line 775 scalar + ~813 path)
- Modify: `argosy/services/retirement/retirement_plan.py:167-180` (`_run_mc` passes the basis)
- Test: `tests/test_cashflow_projection.py` (or `tests/test_scenario_mc.py` if that's where `project_monte_carlo` is exercised — grep first)

- [ ] **Step 1: Write the failing test** — geometric basis must NOT apply the variance drag, so for the same μ,σ its median terminal exceeds arithmetic basis (accumulation-only: far-future retirement, zero spend).

```python
def test_geometric_basis_skips_variance_drag():
    from argosy.services.cashflow_projection import project_monte_carlo, HouseholdState, PensionState
    hh = HouseholdState(monthly_expenses_nis=0.0, portfolio_value_nis=1_000_000.0,
                        fx_usd_nis=3.7, current_age_years=44.0, monthly_savings_nis=0.0)
    pens = PensionState(kupat_pensia_balance_nis=0.0, kupat_pensia_contribution_monthly_nis=0.0,
                        executive_insurance_balance_nis=0.0, keren_hishtalmut_balance_nis=0.0,
                        keren_hishtalmut_contribution_monthly_nis=0.0, kupat_gemel_balance_nis=0.0)
    common = dict(household=hh, pensions=pens, retirement_age=90.0, years=20, months=240,
                  mu_nominal_annual=0.05 + 0.025, sigma_annual=0.18, inflation_annual=0.025,
                  n_paths=2000, seed=42)
    arith = project_monte_carlo(**common, mu_nominal_basis="arithmetic")
    geom = project_monte_carlo(**common, mu_nominal_basis="geometric")
    # geometric basis ≈ +σ²/2 ≈ +1.6%/yr compound → higher median terminal
    assert geom.series[-1].portfolio_value_p50_nis > arith.series[-1].portfolio_value_p50_nis * 1.10
```

- [ ] **Step 2: Run to verify it fails** — Run: `... tests/test_cashflow_projection.py::test_geometric_basis_skips_variance_drag -v`. Expected: FAIL (`project_monte_carlo() got an unexpected keyword argument 'mu_nominal_basis'`).

- [ ] **Step 3: Implement** — add the param + branch (codex-prescribed). In `project_monte_carlo` signature add `mu_nominal_basis: Literal["arithmetic", "geometric"] = "arithmetic"` (default preserves all existing callers). Then both drift computations:

```python
# scalar path (~line 775):
if mu_nominal_basis == "geometric":
    log_drift = mu_nominal_annual / 12.0
else:
    log_drift = mu_nominal_annual / 12.0 - sigma_annual ** 2 / 24.0
# vector path (~line 813):
if mu_nominal_basis == "geometric":
    drift_row = mu_row / 12.0
else:
    drift_row = mu_row / 12.0 - sig_row ** 2 / 24.0
```

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Wire the dual-track core to geometric** — in `retirement_plan._run_mc`, pass `mu_nominal_basis="geometric"` to `project_monte_carlo` (the 5% real / bear-decade-real assumptions are all compound). Leave `regime_switch_mc` on arithmetic (its calm/turbulent/crisis μ are arithmetic regime means).

- [ ] **Step 6: Run the retirement-plan suite** — `... tests/test_retirement_plan.py -q`. Some assertions on solvency/age may shift EARLIER; update any that hardcode an age/P to source from the engine or to the new (correct) value, with a comment that the prior value was the over-conservative arithmetic-basis bug.

- [ ] **Step 7: Codex-verify** — re-run `tmp_review/codex_h1_mu_sigma.py` (or a short confirm) that the implemented basis matches the prescription and the age moved the expected direction.

- [ ] **Step 8: Commit** — `git commit -m "fix(retirement): treat 5% real as geometric (explicit MC return basis) — removes the over-conservative variance-drag that biased the safe age too late"`.

**NOTE (Part 2, for the parked dynamic-allocation work, NOT this task):** when the allocation has a bond/cash cushion (σ<0.18 via lower-return assets), the MC must use *allocation-aware arithmetic* μ paths (`mu_nominal_basis="arithmetic"`, `mu_nominal_path = inflation + weighted_arith_mu_from_allocation`). codex's sourced arithmetic-real-return-by-class table (anchor: us_equity σ=0.18 → 5.0% compound):

| class | σ | arithmetic real | geometric real |
|---|---:|---:|---:|
| us_equity | 0.18 | 6.62% | 5.00% |
| intl_equity | 0.20 | 7.30% | 5.30% |
| concentrated_equity (NVDA) | 0.45 | 11.8% | 1.7% |
| low_vol_equity | 0.13 | 5.4% | 4.6% |
| bonds | 0.06 | 2.4% | 2.2% |
| cash | 0.02 | 0.9% | 0.9% |
| real_estate | 0.15 | 3.4% | 2.3% |

Basis: us_equity `5.0% + 0.18²/2 = 6.62%`; bonds/cash/REIT = Vanguard 2026 nominal less 2.5% inflation + variance drag; NVDA = Damodaran ERP 4.18% × β≈2.24 (no alpha). Sources in `codex_h1_verdict.md`.

### Task 2: H3 — late-life expense phases into the solvency MC

**Why:** `phase_expenses.build_phase_expense_curve` (healthcare_ramp 1.10×+1.5%/yr 65-80; late_life_ltc 1.15×+3%/yr 81-95) only feeds the display endpoint; every ruin MC runs flat `expenses×(1+inflation)^t`. So the tail cost the UI shows the user is omitted from the verdict math → tail solvency looks better than reality (under-conservative on the tail — the opposite-direction error from H1, so they partly net).

**Files:**
- Modify: `argosy/services/cashflow_projection.py` (the per-tick expense series in `project_monte_carlo`)
- Modify: `argosy/services/retirement/regime_switch_mc.py` (same per-tick expense series)
- Test: `tests/test_cashflow_projection.py`, `tests/test_retirement_regime_switch_mc.py`

- [ ] **Step 1: Investigate** — read `phase_expenses.build_phase_expense_curve` (returns `ExpensePhase(start_age, end_age, monthly_multiplier, inflation_premium)`) and the expense loop in `project_monte_carlo` (grep `expense_growth`, the per-month expense array). Confirm whether `fi_methodology.permanent_annual_spend_nis` *already* embeds a healthcare line (if so, layering the phase multiplier double-counts — reconcile instead). Decide: multiply the per-tick expense by the age-resolved phase multiplier + inflation premium, OR (if double-count) relabel the phase card. Document the decision in the test.

- [ ] **Step 2: Write the failing test** — a household whose horizon spans the LTC phase has a strictly higher per-tick expense (and thus higher ruin prob / later safe age) than the flat-inflation baseline. (Write the concrete assertion after Step 1 fixes the approach — assert the phase-aware expense at age 85 ≈ baseline × 1.15 × cumulative inflation premium.)

- [ ] **Step 3: Run to verify it fails.**

- [ ] **Step 4: Implement** — build an age-indexed multiplier array from `build_phase_expense_curve` and apply it to the per-tick expense series in both engines. Keep it a single helper (e.g. `phase_expenses.phase_multiplier_at(age)`) so both engines share one source.

- [ ] **Step 5: Run both suites to verify pass.**

- [ ] **Step 6: Codex-verify** the phase math + the double-count decision (`tmp_review/codex_h3_phases.py`).

- [ ] **Step 7: Commit** — `git commit -m "fix(retirement): apply documented healthcare/LTC expense phases inside the solvency MC (was flat-inflation only)"`.

### Task 3: H1+H3 VERIFY — re-confirm the honest age + surface to Ariel

**Why:** H1 moves the age earlier, H3 moves it later; the NET is the honest headline. This is the most consequential number — measure it and report it before it propagates to the allocation/FI sizing.

- [ ] **Step 1:** write `tmp_review/verify_honest_age.py` — run `canonical_feasible_dual_track` + `build_retirement_plan` + `optimize_deconcentration` BEFORE (stash) and AFTER, printing the drawdown + capital-preservation ages per regime, P@95 at each candidate age, and the per-driver delta (H1 alone, H3 alone, combined).
- [ ] **Step 2:** run it; capture the corrected dual-track ages.
- [ ] **Step 3:** codex-sanity-check the combined result is decision-grade (not falsely optimistic).
- [ ] **Step 4:** write the corrected ages into a short note for Ariel (`tmp_review/honest_age_after_foundation.md`) — old vs new, the driver breakdown, and the downstream implication (the allocation FI weight will be re-derived on this basis). **Do NOT silently change the headline; present it.**

---

## TIER 1 — enforce single-source-of-truth

### Task 4: H10 + B2 — bind the plan narrative to the resolver; kill hardcoded numbers

**Why:** `plan_narrative` is the only user-facing numeric surface NOT bound to `plan_numeric_resolver`, and its prompt literally hardcodes stale figures ("real return 4.5%, SWR 3.5%, retire 49, FI 22M by 2031" — the exact ₪22M the resolver was built to kill). On the most-trusted /plan recap card.

**Files:**
- Modify: `argosy/agents/plan_narrative.py:124-133` (delete the literal numbers from the prompt; add a `resolved_numbers_block` kwarg to `build_prompt`)
- Modify: `argosy/services/plan_narrative.py:219-241` (call `resolve_plan_numbers` + `render_numbers_for_synth`, pass the block; mirror `orchestrator.py:2765-2796`)
- Test: `tests/` for plan_narrative

- [ ] **Step 1: Failing test** — the narrative prompt contains NO hardcoded headline number literals (assert the known stale strings — "22M", "4.5%", "3.5%", "retire 49"/"2031" — are absent from `build_prompt(...)` output), and `get_plan_narrative` passes a resolver-derived numbers block.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** — remove the parenthetical literals; instruct "use ONLY values present in `<resolved_numbers>`; otherwise write `[derivation pending]`." Populate from `resolve_plan_numbers(session, user_id, decision_run_id)` + `render_numbers_for_synth(...)`.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "fix(plan): bind plan-narrative to the numeric resolver; remove hardcoded stale headline numbers"`.

### Task 5: H8 + H9 — unify the /retirement σ basis (hero + scenario grid)

**Why:** Three σ bases on one page: the ruin hero (default `regime_switch`) discards the calibrated NVDA σ the page passes (hardcoded regime vols); `ScenarioGrid` runs flat σ=0.18 with NO reserve-netting / CGT haircut. So the headline P(solvent) is blind to NVDA concentration, and the grid is optimistically un-netted.

**Files:**
- Modify: `argosy/services/retirement/ruin_probability.py:159-178` (make `simulate_regime_switch` accept a σ scale / `regime_params` override; pass the calibrated σ through — or drop the discarded param + relabel)
- Modify: `argosy/services/retirement/scenario_mc.py:184-196` (`simulate_scenarios`: feed the calibrated σ-glide + reserve-netted + CGT-haircut deployable, and `mu_nominal_basis="geometric"` per H1)
- Test: `tests/test_scenario_mc.py`, retirement route tests

- [ ] **Step 1: Investigate** — read `simulate_regime_switch` + how `ruin_probability` selects the engine + what σ the route/UI passes. Decide: thread the calibrated σ through, vs honestly relabel.
- [ ] **Step 2: Failing test** — recalibrating σ (concentrated vs diversified) MUST move the hero P(solvent); the scenario grid base P(solvent) must use the reserve-netted/CGT-haircut deployable (lower) and the geometric basis — assert it differs from the un-netted value.
- [ ] **Step 3: Run to verify fails.**
- [ ] **Step 4: Implement** the σ + netting threading; keep all three cards on ONE basis.
- [ ] **Step 5: Run to verify pass; codex-verify the reconciliation.**
- [ ] **Step 6: Commit** — `git commit -m "fix(retirement): one volatility + netting basis across the /retirement hero, scenario grid, and dual-track"`.

---

## TIER 2 — the allocation visualization pipeline (unblocks the parked allocation work)

### Task 6: B1 + H5 — explicit (label, snapshot_category, σ_class, unit) triple; fix the 229% chart

**Why:** `build_target_allocation → to_synth_targets → build_glidepath` sums to **229%**: (a) `_normalize_pct_value` ×100's any sub-1% band (the 0.93% real-assets sleeve → 93%); (b) substring alias routing mis-anchors ("US broad-market core" matches no key → 0 + double-counted untargeted band; "US growth tilt (ex-NVDA)" trips the `nvda` substring). The fix is architectural: carry an explicit `snapshot_category` (+ scale/unit) on `SynthTarget` instead of substring + fraction heuristics.

**Files:**
- Modify: `argosy/agents/plan_synthesizer_types.py` (add optional `snapshot_category: str | None` + `asset_class_key` to `SynthTarget`)
- Modify: `argosy/services/allocation_glidepath.py:74-98` (use the explicit category when present; keep the alias map only as fallback), `:415-431` (`_normalize_pct_value`: never ×100 a single sub-1% band — gate on the whole-plan total ≈ 1.0), `:660-694` (untargeted bands: skip categories already bound via explicit category)
- Modify: `argosy/services/allocation_plan.py` (emit `snapshot_category` on every target from the `AllocationClass.snapshot_category` already defined)
- Test: `tests/test_allocation_glidepath.py`, `tests/test_allocation_plan.py`

- [ ] **Step 1: Failing test** — `build_glidepath` of the canonical target + real snapshot sums to ≈100% at every tick (assert `abs(sum(tick.composition.values()) - 100) < 0.5` for all ticks; today it's 229%).
- [ ] **Step 2: Run to verify fails** (229%).
- [ ] **Step 3: Implement** — add `snapshot_category` to `SynthTarget`; `allocation_plan.to_synth_targets`/`to_waypoint_targets` set it from `AllocationClass.snapshot_category`; `_resolve_today_value` uses it first; fix `_normalize_pct_value` (gate the ×100 on the whole-plan total, never a single band); fix untargeted-band double-counting.
- [ ] **Step 4: Run to verify pass** (sums to 100 + a per-tick sum-to-100 assertion in the service).
- [ ] **Step 5: Commit** — `git commit -m "fix(plan): thread explicit snapshot_category through SynthTarget; fix the 229% allocation glidepath (scale + alias + double-count bugs)"`.

---

## TIER 3 — the safety / quality gates

### Task 7: H6 + H7 — risk officers on Opus + real risk caps/constraints

**Files:**
- Modify: `argosy/agents/risk_officer.py:98` (`super().__init__(user_id=user_id, model=model)` — drop the hardcoded `"claude-sonnet-4-6"` that shadows the Opus default)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:3367` (+ `argosy/decisions/flow.py:180`, `cli/decide.py:111`, `api/routes/decisions.py:64`) — marshal `settings.tiers` (+ per-account caps) into a `risk_caps` dict; pass `constraints_yaml` as `user_constraints` in the plan path
- Test: `tests/test_*risk*`, agent-settings override test

- [ ] **Step 1: Failing test** — `RiskOfficerAgent(user_id="ariel").model` resolves to the Opus default (not Sonnet); and the plan-synthesis risk call receives a non-empty `risk_caps`. 
- [ ] **Step 2: Run to verify fails.**
- [ ] **Step 3: Implement** the model fix + caps marshalling helper (one helper, reused by all call sites).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "fix(risk): risk officers on Opus + fed real risk caps/constraints (were Sonnet with empty caps)"`.

### Task 8: H11 — prime directive into the synthesizer + risk officers

**Files:**
- Create/Modify: `argosy/agents/_plan_authority.py` (factor a shared `PRIME_DIRECTIVE` constant from `fund_manager.py:229-254`)
- Modify: `argosy/agents/plan_synthesizer.py:61`, `argosy/agents/risk_officer.py` (inject `PRIME_DIRECTIVE`; for the conservative risk perspective add the FI-cost counterweight)
- Test: prompt-content tests for both agents

- [ ] **Step 1: Failing test** — the synthesizer + risk-officer system prompts contain the prime-directive phrase ("earliest safe retirement" / "conservatism-that-delays-FI is anti-goal").
- [ ] **Step 2: Run to verify fails.**
- [ ] **Step 3: Implement** the shared constant + injection.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(agents): inject the prime directive into the plan author + risk gate (was FM-only)"`.

---

## TIER 4 — the canonical design doc (current-state only; no history)

### Task 9: H12-H15 — SDD purpose + dual-track + allocation + nav

**Files:** `docs/design/SDD.md` only.

- [ ] **Step 1: §1 Purpose / North Star** — add the prime directive verbatim ("maximize the family's financial position + secure the earliest safe retirement; conservatism that costs retirement-years is the anti-goal" + the "why a user uses Argosy" framing). Reconcile §1.2/§1.4/§15.3 so "alpha is incidental / plan adherence" reads as *subordinate* to the goal (today they contradict it). (H12)
- [ ] **Step 2: Retirement-readiness section** — the honest dual-track (drawdown vs capital-preservation, central age), `canonical_feasible_dual_track`, σ-glide / **5.0% real geometric** / interim-tax assumptions, FI methodology, the sell-rate optimizer. Cross-link from TOC + §11.1. (H13 + M6 stale return values)
- [ ] **Step 3: Allocation-model section** — `allocation_plan.py` as the single source of target weights + derived FI, the σ-glidepath redistribution, how targets persist into the plan + reach /portfolio + /retirement, and the (forthcoming) dynamic-allocation owner. (H14)
- [ ] **Step 4: §11.1 nav/screens** — add /retirement + /consult; PRIMARY + "More" split. (H15)
- [ ] **Step 5: Commit** — `git commit -m "docs(sdd): add Purpose/north-star + dual-track retirement + allocation sections; refresh nav + return assumptions"`.

---

## Deferred (NOT in this plan — batch later)

- Medium: M1 (FI-perpetuity SWR vs drawdown MC unreconciled — pick one derivation), M2 (dead `models.override`/`model_for_role` — wire or delete), M3 (narrative composition on managed sub-book — fixed by the full-book re-anchor), M4 (snapshot-ordering: 3 conventions → 1 helper), M5 (hardcoded `usd_fraction=0.65` — derive from holdings), M7 (document trader long_hold mode in SDD).
- Low: L1 (`_solve_spend_to_retire_now` upper-bracket), L2 (dead `interpolate_sigma_series` compression), L3 (allocation_plan rationale "~78% equity" double-counts NVDA), L4 (wealth_dashboard SWR 3.5% stale vs 3.0%), L5-L7 (CLAUDE.md/SDD line refs, migration counts, A.3 user_context shape).

## Follow-on (separate spec, already written)

- **Dynamic-allocation owner + long-hold-default fleet** — `docs/superpowers/specs/2026-06-08-dynamic-allocation-owner-and-long-hold-fleet-design.md`. Resume AFTER Tier 0 (the FI weight + cushion must be derived on the *corrected* engine, using the allocation-aware arithmetic μ table above). codex revisions to fold in: cushion = MC-minimized per age (the `years×draw` formula was too blunt → ~37% FI); age-60 is a *lump* unlock, not an income floor (double-count); the VIX overlay is gold-plating to do LAST with hard bounds (≤1yr turbulent / ≤2yr crisis extra cushion; deploy-delay ≤3/6 months then auto-catch-up; excess new proceeds only; test vs a V-shaped recovery). Mandate conflict to resolve: `fi_methodology` capital-preservation doctrine vs the earliest-safe-retirement north star.

## Self-review notes

- Spec coverage: all 2 blockers + 15 highs mapped (B1→T6, B2→T4, H1→T1, H2/H4 done, H3→T2, H5→T6, H6/H7→T7, H8/H9→T5, H10→T4, H11→T8, H12-H15→T9). ✓
- H1↔allocation interaction flagged (FI re-derivation). H3↔H1 net-direction flagged (the VERIFY task). H6 model-shadow pattern mirrors `trader.py:99` (left as-is — SDD-documented tier choice).
- Investigation steps (H3 double-count, H8/H9 σ-threading, B1 alias-vs-explicit) are concrete reads, not placeholders.
