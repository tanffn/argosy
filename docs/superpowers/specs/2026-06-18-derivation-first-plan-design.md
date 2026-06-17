# Derivation-first plan — design spec

**Status:** DRAFT (overnight build, 2026-06-17→18). Branch `feat/derivation-first-plan`.
**Why:** Over two days of tweaks never converged a clean plan because the pipeline
patches a *prose document* and *inherits* its load-bearing numbers from prior docs and
past behavior instead of *deriving* them from the goal. The canonical example: the NVDA
sale cadence `3,000 sh/yr` was never an Argosy derivation — it is a `Target` cell copied
out of Ariel's own 2025/2026 NVDA-sales tracking spreadsheet (FFS tab "NVDA Sales
History": Target 3,000). A description of past behavior got laundered, via a
`plan_doc:rsu_cadence` citation, into the plan's prescription, and forty drafts then
defended it.

## Prime directive (the optimization target)
Maximize the family's financial position and secure the **earliest safe** retirement;
conservatism that costs retirement years is the anti-goal. Every derived number is the
output of optimizing against THIS, subject to hard constraints.

## The one rule
**INPUTS ≠ DERIVED, and a derived number may NEVER be seeded from past behavior or a
prior doc's "target."**

- **INPUTS (only the user/brokerage can supply):** current holdings & prices, account
  structures, tax constraints (Section-102 clocks, capital-track eligibility dates),
  income, life events, hard preferences (UCITS-preferred domicile; NVDA the one
  sanctioned US-situs sleeve), and the GOAL.
- **DERIVED (the team computes, from goal + current state + constraints, from first
  principles):** target allocation, NVDA deconcentration rate **and horizon**, glide
  path, FI capital target, SWR / required real yield, savings assumptions, retirement
  date. The baseline doc is mined for *facts and goals only*; its "targets" are
  discarded.

## Locked current-state inputs (FFS 12-Jun-26 raw; resolver-confirmed)
| Fact | Value | Source |
|---|---|---|
| NVDA position | 11,471 sh @ $200.14 = **$2,295,806** | FFS schwab RSU row |
| NVDA weight (investable book) | **62.52%** | resolver `concentration.nvda_current_pct` |
| NVDA risk cap / IPS target | 13.0% cap / 12.0% target | resolver `concentration.nvda_cap_pct` |
| Investable securities book | ≈ $3.67M (= 2.296M / 0.6252) ≈ ₪10.9M | derived from above |
| Net worth (investable basis) | ₪11,954,153 | resolver `portfolio.net_worth_nis` |
| Liquid net worth | ₪11,687,926 | resolver `portfolio.liquid_net_worth_nis` |
| Total incl. RE + pension | ≈ $14.15M | FFS "Sum: 14,148" (K USD) |
| Real estate | Keret ₪2.5M/−₪0.35M loan; Atlanta $318K/−$219K; Pipera/Obor (EUR) | FFS RE tab |
| Pensions (Dec-25) | Ariel ₪2,015,054 (KH 384K + Pension 800,147 + Exec 755,907 + PF 75K); Noga partial (PF 75K, rest "?") | FFS pensions tab |
| FX USD/NIS | 2.965 (band 2.807–3.164) | resolver `fx.usd_nis` |
| Savings annual net | ₪218,227 (flagged unstable: 297→284→218) | resolver `savings.annual_net_nis` |
| Spend T12 / FI basis | ₪277,008 / ₪311,584 | resolver `spend.*` |
| FI target / total capital | ₪10,386,133 / ₪11,836,133 | resolver `retirement.fi_*` |
| US-situs estate exposure | ₪9,514,477 | resolver `concentration.us_situs_estate_exposure_nis` |
| Section-102 cliff | grant 213000 (Jun-2022) lots NOT capital-track eligible until **2027-01-01** | baseline doc / tax |

## Net-worth scope disambiguation (the FI-contradiction root)
Three DISTINCT bases, must be labeled and never conflated:
1. **Investable securities book** (~$4.0M / ₪11.95M) — what the deconcentration % and
   allocation target are computed against.
2. **Liquid net worth** (₪11.69M) — investable minus near-term earmarks; the HONEST FI
   sufficiency basis.
3. **Total net worth** (~$14.15M) — incl. real-estate equity + pensions; NOT spendable
   for FI, used only for solvency/estate context.

**FI sufficiency must be tested on basis (2), liquid.** On that basis the current margin
is **≈ −₪148K (NOT +₪118K)** — FI is *not yet* met. The +118K figure compared the FI
target against basis (1)/(3) and is the codex BLOCK. This must be derived honestly.

## Derived decisions to compute (each: methodology → codex re-derive blind → lock)
1. **NVDA deconcentration schedule.** Target NVDA ≤ 12% of the investable book.
   Required reduction ≈ 11,471 → ~2,200 sh (sell ~9,270 sh). Derive the rate+horizon
   that MINIMIZES (concentration tail-risk cost + tax drag) under the prime directive
   and the 102 cliff: pre-2027-01-01 sells of grant-213000 lots are taxed as ordinary
   income (capital-track only after the 24-mo clock), so the optimizer should pull
   capital-track-eligible lots first and front-load post-2027 sells, while not letting a
   62.5% single-stock position ride for tax reasons longer than the tail-risk justifies.
   Output: a per-year share schedule + the horizon (a DERIVED year, not an input).
2. **Target allocation (phase-aware).** Per the approved rebuild — FI sleeve 8–10% now,
   growth-tilted, EM/gold/crypto/REIT diversifiers, UCITS-preferred, NVDA the only
   sanctioned US-situs sleeve. Derive target weights from the goal + phase, not the
   static 21.3% defensive artifact.
3. **FI capital target + required real yield (SWR).** Derive the spend basis (incl.
   life-event phases), a defensible SWR (not 4.5% by default), and the capital required
   on the LIQUID basis → the honest FI margin and the **earliest safe** retirement date
   (dual-track: typical vs preservation).
4. **Savings assumption.** Resolve the unstable floor to ONE defensible figure with a
   stated derivation; the whole trajectory rests on it.
5. **Glide.** The path from current allocation → target as deconcentration proceeds.

## Architecture backstop (so this never recurs)
- Canonical plan = a small **typed decision object** (`PlanDecisionModel`): inputs +
  derived values, each derived value carrying its formula/derivation provenance. No
  free-floating anchors.
- Every surface (MD bodies, dashboard, actions JSON, /retirement) is a **pure projection**
  of that object → cross-surface contradiction impossible by construction.
- **Re-derivation reviewer** (not ratifier): recomputes each derived number from raw,
  blind to the stated value, and BLOCKs on divergence. A citation is a claim, not truth.
- **Single fail-closed promote gate:** nothing is `role='current'` while ANY authority
  (codex / deterministic gate / FM / reader / re-derivation) is BLOCK. Relabel slug +
  strip stale receipts on promotion.
- **Refuse to synthesize on stale/low-confidence inputs:** block + route to refresh.

## Codex zigzag scope (this build)
The money-math is risky → codex re-derives, not ratifies, on: (a) net-worth basis for FI;
(b) the NVDA deconcentration rate+horizon optimization; (c) FI sufficiency + earliest-safe
date on the liquid basis; (d) the phase-aware target weights. Iterate until codex signs
the methodology, THEN compute, THEN codex re-derives the numbers blind.
