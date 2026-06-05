# Retirement Optimizer + Transparent Plan — design spec (2026-06-05)

## Goal (prime directive)

`/plan` + `/retirement` must **optimize for the fastest safe retirement** and tell the
user **where/how to invest** to get there — not merely report one age. Every number
live-derived from current holdings × current BOI FX, every assumption shown, no magic
numbers. (feedback_argosy_prime_directive, feedback_output_trust_doctrine.)

## Definition of "safe" (the one parameter that sets the ages)

**Safe = P(portfolio never hits zero before age 95) ≥ BAR**, reserve-netted
(₪1.45M finite-liability reserve earmarked out), permanent-equivalent spend basis
(₪311,584/yr), private pension annuity + Bituach Leumi credited from 67, lump from 60.
**Default BAR = 0.90.** Bar is a surfaced, user-tunable assumption.

## Live acceptance anchors (computed 2026-06-05; the build must reproduce these)

Deployable (deconcentrated, reserve-netted) portfolio = **₪8,687,223**. σ-glide 34.4→18% / 3y.

- Earliest safe age @90%: **bull (5.5% real) = 47 · typical (4.5%) = 51 · bear (4.5%+−25%+low decade) = 59**
- Bar sensitivity (typical): 85%→50 · 90%→51 · 95%→53 · 99%→58
- **Spend-X to retire today (44) @90% typical = ₪227,440/yr (₪18,953/mo)** vs current ₪311,584/yr (≈27% cut)
- Methodology already codex-verified: 51 is the honest central age (band 50–52); double-count vs understated-NVDA-tax offset. See `tmp_review/codex_sigma_cgt_verdict.md`.

## Components (with file ownership + interface contracts — agents stay in their lane)

### 1. Engine cashflow series — `argosy/services/cashflow_projection.py`
Add deterministic per-tick income-composition fields to `MonteCarloPoint` so an
inflow/outflow chart can render the age-67 bridge:
- `bl_monthly_nis: float` (BL stipend, nominal at t, 0 before 67)
- `lump_amount_nis: float` (one-time at the age-60 unlock tick, else 0)
- `portfolio_net_draw_monthly_nis: float` (= shortfall = max(0, expenses − annuity_net − bl); the net pulled from portfolio to cover spend)
- `portfolio_gross_withdrawal_monthly_nis: float` (= shortfall / (1 − eff_tax); incl. tax drag)
These are deterministic (annuity/bl/lump/spend identical across paths). Do NOT change
solvency math or existing fields. Add to the DTO + `assumptions`. CODEX-VERIFY.

### 2. Deconcentration optimizer — NEW `argosy/services/retirement/deconcentration_optimizer.py`
`optimize_deconcentration(session, user_id, *, target_p_solvent=0.90, today=None) -> DeconcentrationPlan`
Sweeps NVDA sell-down horizon ∈ {1,2,3,4,5} yr. For each horizon: σ-glide reaches 18%
over that horizon; CGT realized = (nvda_pct − cap_pct)·portfolio · effective_cgt(horizon),
where **effective_cgt models tax-bunching**: realizing ₪5.7M of gain in fewer years pushes
more gain through the 3% high-income surtax (+2% from 2025 over ₪721,560/yr capital income),
so a 1-yr sell-down has a *higher* effective rate than spreading over 5. Return the horizon
minimizing the **typical** earliest-safe age, with full per-horizon table. `DeconcentrationPlan`
exposes: chosen_horizon_years, per_horizon[{horizon, eff_cgt_rate, cgt_nis, sigma_path_desc,
earliest_safe_age_typical}], target diversified allocation note. CODEX-VERIFY (tax + σ tradeoff).

### 3. Scenarios + spend frontier — `argosy/services/retirement/scenario_mc.py`
Reframe `earliest_feasible_scenarios` axis from (as_is/decon/bear ACTION) to
**(bull/typical/bear MARKET regime)**, all on the OPTIMAL deconcentrated plan from (2):
`retirement_scenarios(session, user_id, *, target_p_solvent=0.90, plan, today) -> list[FeasibleAgeResult]`
(bull μ=5.5%, typical μ=4.5%, bear μ=4.5%+−25%+low decade). Keep an `as_is` baseline
(σ flat 34.4%, no decon) computed separately for the "do nothing = never" contrast.
Add `spend_to_retire_at_age(session, user_id, *, retire_age, target_p_solvent, plan) -> float`
(binary search on annual spend; the retire-today X is `retire_age=current_age`) and
`spend_age_frontier(...) -> list[(spend, earliest_age)]`. Bind the canonical
`earliest_feasible_retire_age` used by /plan headline to the typical-scenario optimal age
(supersede the optimistic σ=18-flat-no-CGT 49). CODEX-VERIFY.

### 4. API — `argosy/api/routes/retirement.py` (+ `plan.py` headline binding)
- Extend `/projection/feasible-age` → return the 3 market-scenario ages + as_is baseline + the chosen optimizer horizon + per-horizon table.
- New `/projection/spend-frontier` → frontier + retire-today X.
- New `/projection/cashflow-streams` (or extend the MC bands endpoint) → the per-tick inflow/outflow series from (1).
- DTOs typed; USD mirror where existing endpoints do.

### 5. Frontend — `ui/src/components/`
- NEW `plan/cashflow-inflow-outflow-chart.tsx`: stacked **inflows** (portfolio_net_draw + annuity + bl + lump-marker) vs **spend** line over age. The age-67 annuity+BL inflow and age-60 lump are visually obvious. (User-requested.)
- Fix `plan/monte-carlo-bands-chart.tsx`: add a **solvency-% line** (right axis) and/or emphasize the P10 band so the pension bridge is visible (median-dominated scale currently hides it).
- 3-scenario age cards + spend↔age frontier (with retire-today X) + assumptions panel on `/plan` ("Your plan, in plain English") and `/retirement`. Drop statutory-67 from the SHOWN age anchors (keep in calc). Read `ui/AGENTS.md` (non-standard Next.js) before editing.

### 6. Integration — repoint off the optimistic "49"
`plan_headline.py` (_canonical_feasible/_readiness_anchors), `wealth_dashboard.py::_retirement`,
narrative regen. The shown earliest age becomes the typical optimal age (51 @90%). Regen PV30
narrative after synth-affecting changes.

## Verification
- `.venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider <path>` (PYTHONIOENCODING=utf-8; no commas in %-format).
- CODEX ZIGZAG on components 1/2/3 (money math, tax, σ tradeoff) before they land.
- Live HTTP smoke on :8000 for the endpoints; reconcile to the acceptance anchors above.

## Out of scope (v1)
Security-level "which stocks" selection (leans on the existing fleet per-ticker analysis);
multi-account tax-lot basis tracking (the double-count is a known ≤1yr conservative bias,
offset by understated NVDA gain-fraction — documented, not fixed here).
