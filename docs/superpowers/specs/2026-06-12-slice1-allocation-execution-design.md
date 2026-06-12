# Slice 1 — Plan-bound allocation engine + executable-task agent

Revised after codex adversarial review (verdict REVISE → addressed; see
`tmp_review/codex_slice1_verdict.txt`). The central reshape: **all money math is
deterministic; the agent only ranks, sequences, and explains precomputed
candidates — it invents no numbers.**

## Problem

1. **Wrong source (critical).** The cash-deployment surface reads "current" AND
   "plan target" from the user's **TSV spreadsheet**
   (`windfall_detector._find_allocation_table`), not the canonical Argosy plan.
2. **Analysis without action.** Portfolio shows Verdict + per-position thesis
   but never emits **executable tasks** ("sell N SCHD → buy CSPX", "deploy $X").

## Decomposition

- **Slice 1a — deterministic allocation engine + rebind (the critical fix + ~80%
  of the value).** Pure math, high-trust, no LLM. Emits executable buy/trim/swap
  candidates from the canonical, glide-aware plan.
- **Slice 1b — allocation agent (ranker/sequencer/explainer)** on top of 1a's
  candidates. Thin; orders into now/this-quarter/later, groups, writes rationale,
  picks among precomputed alternatives. Validated to reconcile to 1a's numbers.

Build 1a first; it stands alone (deterministic executable tasks). 1b adds polish.

## Determinism boundary (investment philosophy)

Deliberate core-satellite split — this answers "shouldn't we exploit market
sentiment / beat the market?":

- **The core (1a) is mechanical on purpose.** Timing the core of a long-hold
  book reliably *loses* after costs + taxes; rebalancing is already mildly
  counter-cyclical, and deploying a lump sum promptly beats averaging-in ~2/3 of
  the time. So 1a decides the **destination** (which instruments, how much to
  reach the glide-aware target) with no market-state input. This serves the
  prime directive (don't let conservatism or mistimed waiting cost years).
- **Market sentiment enters only in two bounded places:** (a) the Slice-2
  **satellite** (trend radar → estimator → fleet, with stop-loss discipline) —
  the small slice where alpha is deliberately sought; and (b) **1b's *pace*
  recommendation** — given the deterministic destination, 1b may advise lump vs.
  tranched deployment based on volatility/sentiment. Pace ≠ destination: 1b never
  changes amounts or instruments, only the *when*.

## Slice 1a — deterministic engine

New `argosy/services/allocation_engine.py`:

1. **Glide-aware targets.** `class_targets_as_of(doc, as_of_date) -> {label: pct}`
   derives class target %s from `doc.glide` for the as-of quarter (NOT the
   end-state). `now` / `this_quarter` use the current glide waypoint; end-state
   targets are reserved for "later". (Fixes: staged deconcentration ≠ sell-all-now.)
2. **Holdings adapter.** `tradeable_holdings(snapshot) -> (holdings_by_symbol,
   available_cash_by_account)`: normalize/aggregate symbols, filter to the
   tradeable book, separate cash (by account/currency), preserve account/location.
   No collapsing blank cash rows; no "trim cash" deltas.
3. **Three explicit modes** (the codex #1/#4 fix):
   - `pure_rebalance` → reuse `diff_plan_vs_holdings` (the closed-book primitive,
     unchanged) against glide-aware targets.
   - `cash_only_deploy(doc, holdings, cash_usd, as_of)` → **buy-only,
     cash-constrained**: allocate exactly `cash_usd` to the largest under-target
     gaps (water-filling toward targets); **never emits trims**; buys sum to
     ≤ cash. This is the "$X, where does it go" answer.
   - `rebalance_plus_cash` → only when the caller explicitly opts in: deploy cash
     AND rebalance (may trim).
4. **Deterministic amounts everywhere.** Buys/trims and the swap pairing are
   computed here — never by the agent.
   - **Swap pairing:** a deterministic `replaces_symbols` map (seed: SCHD→FUSA,
     VOO→CSPX, … the documented UCITS swaps) — added as a structured field on the
     instrument doc / a config map. A `SWAP` candidate is emitted only when the
     map exists and both legs size deterministically.
   - **Tax = advisory flag in v1 (no deterministic split engine).** The user is
     deploying *cash* (buys → no tax event), and the `lots` table is empty, so a
     deterministic surtax-split / tax-lot sequencer would be machinery without
     inputs. Instead, any TRIM/SWAP leg that realizes a gain carries an
     `est_tax_nis` (best-effort estimate) + a boolean `surtax_split_suggested`
     flag when the estimated realized gain would cross the ₪721,560 band — purely
     advisory ("consider splitting across tax years"). This sidesteps the
     conflicting in-repo surtax models (no model has to be *chosen* because
     nothing is *computed* deterministically yet). The full tax-lot-aware
     sequencer is a later slice, unlocked once lots are imported.
5. **Output:** `AllocationCandidate[]` — fully-priced, structured legs:
   `{kind ∈ BUY|TRIM|SWAP, legs[{side, symbol, account_id, currency,
   notional_usd, quantity?, funding_source ∈ cash|trim_proceeds}], horizon ∈
   now|this_quarter|later, est_tax_nis?, surtax_split_suggested?, cites[]}`.
   Renders instantly with no LLM.

Rebind: `windfall_allocator`'s target side switches to `class_targets_as_of`;
`_find_allocation_table` is retired from the target path (kept only if an audited
consumer still needs the raw TSV block). **Consumer audit (codex #11):** update +
test every `_allocate_long_term` / `propose_allocations` / `allocation_delta_table`
consumer — `/retirement/windfall/detect`, `unallocated_cash_detector`,
`rsu_prevest_planner` — preserving their response contracts or via a compat adapter.

## Slice 1b — allocation agent (thin)

`AllocationAgent` (Opus — money/decision-flow). **Input:** 1a's priced
`AllocationCandidate[]` + per-position Verdicts/theses + the advisory tax flags +
a market-context snapshot (volatility/sentiment already gathered for the fleet).
**Job:**
- order candidates into now / this-quarter / later, group related legs (the
  SCHD→FUSA SWAP as one task), pick among precomputed alternatives, write a
  one-line rationale per task, surface what's left unallocated;
- **deployment-pace recommendation** (the bounded place sentiment lives): given
  the deterministic destination, advise lump-now vs. tranched entry based on the
  market-context snapshot — as a recommendation attached to the BUY tasks. It
  **never changes amounts or instruments** (those are 1a's), only the *timing*.

**It produces NO new numbers** (amounts, instruments, tax figures all come from
1a). **Output:** `ExecutableTask[]` wrapping 1a candidates + ordering + pace
recommendation + rationale + cites. **Validation (hard):** every task's leg
totals must reconcile to a 1a candidate within tolerance, else the task is
rejected (fail-loud).

## Surface

`GET /api/portfolio/allocation-tasks?mode=&cash_usd=&as_of=` → 1a candidates
always (instant); 1b's ordered `ExecutableTask[]` on demand. Lives in the
Proposals hub (nav/shell work is Slice 3).

## Testing

- 1a (pure, no network): glide-aware targets pick the waypoint not end-state;
  `cash_only_deploy` never trims and sums to ≤ cash (the codex A=70/B=30/cash=10
  case buys only $10); holdings adapter filters cash/non-tradeable; swap pairing
  emits one SWAP with matched legs; a gain-realizing TRIM above the band sets
  `surtax_split_suggested` (advisory flag, no split computed); consumer-contract
  tests stay green.
- 1b: schema validation + reconciliation test (leg totals == 1a candidates; no
  invented numbers); SCHD→FUSA emitted as one SWAP; a pace recommendation is
  attached to BUY tasks without altering their amounts; rejects a task that
  doesn't reconcile.

## Dependency

Binds to the canonical `TargetAllocationDoc` (with glide) — runs against the
**clean UCITS plan from validation run 96** (in flight), not draft 35 / v34.

## Out of scope (later)

Slice 2 (discovery funnel + combined radar/monitor + smart refresh); Slice 3
(Proposals next to Portfolio, fold Consult, collapsible sections, notes audit).
