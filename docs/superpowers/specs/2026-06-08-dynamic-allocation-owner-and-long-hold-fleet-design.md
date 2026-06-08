# Dynamic-allocation owner + long-hold-default fleet — design

## North star (the thing this design serves)

Argosy is a trustworthy, always-on financial brain for one family. It holds the
whole picture (holdings, RSUs, pensions, cashflow, Israeli tax, FX, life events,
concentration) and reasons over it like a top-tier advisor + analyst team —
transparently, with its work shown. Its single goal: **maximize the family's
financial position and secure the earliest *safe* retirement** — "retire from
work, not from life." Conservatism that quietly costs retirement-years is the
anti-goal. Every number is Argosy-derived from real data, explained, auditable,
and self-consistent across /plan, /portfolio, /retirement. **The user should not
have to be the investing expert — Argosy is, out loud.**

(Gap noted: this north star lives only in scattered auto-memories, not in the
SDD. A follow-on docs task adds a "Purpose" section to the SDD and consolidates
the vision in memory.)

## Problem

The allocation the system produces today is a **single static end-state**, and
the trading posture **defaults to tactical** — both misfit a long-hold investor
whose biggest real risk is a market crash in the early-retirement *bridge*
(age 47→67, before pension + Bituach Leumi). Concretely:

1. No agent owns a **time-varying** asset-class allocation. The allocation panel
   produced one frozen target and flagged its own gap ("no de-risking glide into
   age 47-57"). The "FI = 16 vs 19 vs 21%" debate was the wrong question: the
   answer is a *curve*, not a number the user is asked to pick.
2. Nothing makes the allocation **regime-aware**, even defensively — the macro
   agent classifies calm/turbulent/crisis (it reads VIX) but that never reaches
   the allocation.
3. The decision team defaults to `tactical_trade` (`decisions/flow.py`), and the
   minute + hourly tactical cadences are on — the wrong instrument for a
   buy-and-hold family portfolio.

## Decisions already made (with the user)

- **Architecture = hybrid (Approach A):** an agent owns the *policy/judgment* and
  narrates it; a deterministic engine computes the auditable math; the MC
  validates. Matches the rest of Argosy (withdrawal-sequencer agent + retirement
  engine) and satisfies both "agents are the experts" and no-magic-numbers.
- **Regime/VIX use = defensive sequence-risk ONLY.** It adjusts (a) cushion size
  and (b) the pace of deploying *new* proceeds. It never force-sells, never
  shifts the strategic equity mix for momentum, never times entries.
- **Fleet defaults to long-hold:** flip the trader default to `long_hold`; turn
  OFF the minute + hourly tactical cadences.

## Component 1 — the dynamic-allocation owner

### Engine (`argosy/services/allocation_path.py`, deterministic)

Produces an `AllocationPath`: the target asset-class composition at each time
tick (quarterly from today through ~retirement + the sequence-risk decade).
Built from two layers.

**Layer 1 — lifecycle (strategic, time-only).** Four phases, all derived:
- *Deconcentration (today → ~2y):* the NVDA taper today→target — the existing
  `allocation_plan` redistribution schedule becomes Phase 1 verbatim.
- *Pre-retirement (deconcentration-end → retirement):* hold near the steady-state
  engine mix (blends to `SIGMA_DIVERSIFIED`).
- *Early-retirement de-risk (retirement → +sequence-risk horizon):* the cushion
  (cash + short-IG) **peaks** here — this decade is where a crash is most lethal.
- *Re-risk (as pension/BL income comes online):* the cushion glides back down as
  the age-60 lump / age-67 annuity / Bituach Leumi reduce reliance on the liquid
  book.

The FI/cushion weight is therefore a **curve**, derived — not asserted. Sizing
principle (to be finalized + codex-verified): the cushion at age *a* ≈
`min(years-until-next-income-floor, sequence-risk-horizon) × net-annual-draw`,
expressed as % of the book, then **validated by the MC** (the cushion must keep
the typical-regime earliest-safe drawdown age at the optimizer's certified age
with a solvency margin). This dissolves the static "16 vs 19 vs 21" question:
the FI weight is highest right at retirement and declines toward the income
floors — a shape, not a point.

**Layer 2 — defensive regime overlay (bounded).** On top of the lifecycle curve,
keyed to the macro agent's regime (calm/turbulent/crisis):
- *turbulent/crisis:* increase the cushion by a **bounded** delta (capped) AND
  slow the deployment of *new* proceeds into equities (hold proceeds in cash
  longer).
- *calm:* deploy on the normal schedule; cushion at the lifecycle baseline.
- **Hard bounds (non-negotiable):** the overlay only moves the cushion buffer +
  deployment pace within a capped band; it NEVER force-sells existing equity,
  NEVER shifts the strategic mix for momentum, and is mean-reverting (not trend-
  following). Validated against `regime_switch_mc` (the path must survive a
  crisis cluster).

**Auditability:** every tick's weights, the cushion-sizing inputs (net draw,
income-floor schedule), the regime input, and the MC validation are emitted so
the value is reconcilable to raw data. No magic numbers.

### Agent (`allocation_strategist`, Opus)

Owns the **policy**, not the weights: the sequence-risk horizon + phase
boundaries, the regime-response magnitudes + their hard bounds, and the plain-
English narration (per-phase rationale, agreement/dissent, the defensive-not-
timing posture). It sets *bounded policy parameters* the engine applies and the
MC validates — it cannot fabricate a number. This is the "expert" surface the
user wanted: transparent, explainable, on his side, auditable.

### Data flow

`macro agent → regime` ; `pension/income schedule + net draw + deconcentration
cadence (allocation_plan) + steady-state target → engine → AllocationPath` ;
`allocation_strategist sets policy + narrates` ; `AllocationPath → glidepath
chart (/plan) + plan narrative`. The static `allocation_plan` target is the
endpoint anchor of Layer 1.

## Component 2 — default the fleet to long-hold

- `decisions/flow.py`: flip the decision-team default `consult_mode`
  `tactical_trade` → `long_hold` (auto-neuters the technical/FX-timing inputs the
  long_hold prompt already ignores).
- `configs/ariel/agent_settings.yaml`: `minute.enabled = false`,
  `hour.enabled = false`. Keep daily/weekly/monthly/quarterly/annual + monitoring
  (state-observer, news, watchlist, plan-watcher).
- No persona rewrites needed (the `long_hold` trader persona already exists and
  is correct); this is a default-flip + cadence-disable.

## Testing

- TDD throughout (red → green per unit).
- Codex-verify the money math: the lifecycle cushion-sizing curve and the
  bounded regime overlay (sandbox=danger-full-access).
- MC-validate: the dynamic path holds the certified earliest-safe age with a
  solvency margin, and survives a `regime_switch_mc` crisis cluster.
- No magic numbers: every policy parameter sourced + explained; assert the
  overlay's hard bounds in tests (it can never force-sell or chase momentum).

## Scope / YAGNI

- Defensive-only regime response, bounded — explicitly NOT a tactical-tilt or
  alpha engine.
- One user (`ariel`); no multi-tenant generalization beyond what exists.
- Does NOT rewrite agent personas, inject goals_yaml, or clean the dead model
  config (the user deferred those).

## Out of scope / follow-ons

- Resume the paused allocation integration (#10) on top of this: the /plan
  glidepath renders the *dynamic* path; the narrative explains it.
- Docs: add a "Purpose / North Star" section to the SDD; refresh the stale
  retirement section (4.5%→5.0% dual-track); consolidate the vision in memory.
- Findings from the background arch/code review (task `wi5mw1341`) triaged
  separately.

## Open questions (to settle in the plan)

1. Exact lifecycle cushion-sizing function (the `min(years-to-floor, seq-risk-
   horizon) × net-draw` form vs an MC-minimized cushion per age) — codex to
   adjudicate.
2. Tick granularity of the path (quarterly vs annual) for the chart + persistence.
3. Whether the `allocation_strategist` is a new agent class or an extension of
   the concentration analyst (which already owns an NVDA sell-down glidepath).
