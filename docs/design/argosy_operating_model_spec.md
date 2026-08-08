# Argosy Operating Model — one closed-loop system (spec for adversarial review)

**Status:** DESIGN ONLY — not implemented. Written to be broken by an independent
reviewer. Re-derive every data claim against `db/argosy.db` (read-only:
`sqlite3.connect("file:db/argosy.db?mode=ro", uri=True)`) and the code. It is
better to surface a real flaw than to agree.

**Companion (already on master):** `docs/design/performance_scorecard_design.md`
(the realized-return-vs-benchmark scorecard; this spec references it as
Component C rather than restating it).

---

## 1. Purpose — the one goal, and the one test

Argosy exists to **maximize the family's wealth and reach the earliest *safe*
retirement** ("retire from work, not from life"; conservatism that quietly costs
retirement-years is the anti-goal). The test applied to every component here:
*does it move a real dollar or protect one, and can we prove it?*

This spec unifies three asks into **one system**, because they are not separate
features — they are links in a single loop and each is worthless without the
others:

- **#2** — evaluate *every* holding (with the right question per asset type).
- **#3** — turn every decision into a testable bet, grade it against the
  alternative, and learn why we were right or wrong.
- **Proof** — measure the whole thing as realized return vs benchmark (Component
  C, the scorecard).

## 2. The closed loop (system architecture)

```
        ┌─────────────────────── 0. INTEGRITY FLOOR ───────────────────────┐
        │  complete + reconciled book; conservation-checked writes;          │
        │  every job fails LOUD (no "ok" while broken)                       │
        └───────────────────────────────┬───────────────────────────────────┘
                                         ▼
   A. EVALUATE every holding  ──►  B. RECORD the bet  ──►  D. MEASURE at maturity
   (per asset type)                (testable at birth)     (vs the alternative)
        ▲                                                         │
        │                                                         ▼
        └──────────────  E. LEARN (post-mortem → feedback)  ◄──────┘
                                         │
                                         ▼
                     C. SCORECARD: realized return vs benchmark
                        (the single number that says "are we winning?")
```

**Invariant:** every arrow must be honest. The system's historical failure mode
(verified this session: a $2.4M/59% book erasure and 1,065 masked async errors,
both reported as success) is a *broken arrow that still reported green*. Every
component below therefore ships with an explicit failure state; **"silent
success" is a defect, not a display choice.**

---

## 3. Component 0 — Integrity floor (precondition; must land first)

A decision on a wrong book is worse than none. Two gates make the loop
trustworthy (both from the 2026-08-08 audit; owned partly by the parallel repair
work — this spec depends on them, does not re-implement them):

- **Conservation gate before persist, on ALL snapshot write paths.** Block or
  quarantine a write that drops an account/location, drops a cash currency, or
  shrinks total value / position count beyond a threshold vs the prior live row.
  (`portfolio_snapshot_store.persist_snapshot` is currently "intentionally dumb —
  always writes".)
- **Job status derived from work done, not just "did the tick raise?"** A tick
  that returns with `adapter_errors>0` / zero-work / non-empty `errors[]` must
  close degraded/error. (Being fixed in an isolated worktree in parallel.)

**Dependency, not scope:** Components A/B/C assume these hold. If they do not, A/B/C
produce confident numbers on silently-wrong inputs — exactly today's failure.

---

## 4. Component A — Evaluate every holding (the right question per asset type)

"Evaluate every position" is three questions, not one.

### A1. Single-name stock (category C) → "is the company thesis intact?" — EXISTS
The per-ticker decision fleet (`argosy/decisions/flow.py` via
`run_deep_decision`): analysts (fundamentals/news/sentiment/macro) → bull vs bear
debate → trader → a settled **verdict ∈ {BUY, ADD, HOLD, TRIM, SELL}** +
conviction + **falsifiers + typed revisit-triggers** (shipped this session, in
`verdicts.falsifiers_json` / `revisit_triggers_json`). Category C sets the
*target weight*, concentration cap, and estate/FX gates — it is not the thesis.
Falsifiers are FUNDAMENTAL-only, forward, and a trigger exists only if one reading
confirms it (else qualitative). **This piece works on clean data.** Coverage today
is single-names only.

### A2. ETF / fund (category C) → "is this the best *vehicle* for C?" — TO BUILD
A basket has no company thesis; a BUY/HOLD/SELL verdict is the wrong frame. Split
into two questions:

- **Allocation** (the plan's job, regime-robust, changes rarely): does category C
  still deserve its `target_pct`? Read from `plan_versions.target_allocation_json`
  (`classes[].target_pct`). Out of scope for A2 — it is Component-plan territory.
- **Vehicle selection (the new build):** is the held instrument X the best vehicle
  for C, or is there a **Y with the same exposure but a better score** on:
  1. **Cost** — total expense ratio.
  2. **Tracking** — tracking difference/error vs the category index.
  3. **Domicile / estate** — UCITS (Irish) vs US-domiciled → US-situs estate-tax
     exposure (a first-order concern for this family; see
     `feedback_canonical_allocation_ucits_preferred`).
  4. **Tax** — distributing vs accumulating; withholding treaty treatment.
  5. **Liquidity** — AUM, spread, volume.

  **Output:** a per-sleeve recommendation `{keep X}` or `{switch X→Y}` with a
  **tax-aware switching cost** (realized CGT on the embedded gain + spread)
  compared to the annualized benefit (fee + tracking + tax saving). A switch is
  proposed only when benefit clears the switching cost with margin — the same
  discipline as the HOLD-grading rule in B.

  **Mechanism:** map each held ETF → category via `resolve_sleeve_label`
  (`instrument_plan_class.py`); for each category, score the held vehicle + a
  **candidate universe** of same-category ETFs on the five axes; rank; emit
  keep/switch. This is a *deterministic comparator over instrument metadata*, not
  an LLM thesis — the fleet's role is only to sanity-check a proposed switch and
  confirm the exposure is genuinely equivalent (avoid a factor drift dressed as a
  "cheaper S&P").

### A3. NVDA (and any deliberately-unmanaged holding) → "present but unmanaged"
Never a managed BUY/SELL verdict, but **always** counted for concentration,
US-situs estate, FX, and net worth. Modelled `unmanaged-but-present`, never
absent. (Owner-binding; the parallel Stream D work.)

**Component A end state:** *every* position carries a current stance — a
company-thesis verdict (A1), a keep/switch-vehicle recommendation (A2), or an
unmanaged-but-present accounting (A3) — a plain-English reason, and the tripwires
that flip it. **Nothing is silently uncovered** (an unmapped/unclassified holding
is a visible "cannot evaluate", never a silent skip).

---

## 5. Component B — Prediction + learning loop (turn every decision into a graded, learned bet)

### B1. Testable at birth
Every actionable output of A (a BUY/SELL/TRIM/ADD, or an A2 switch) is written to
the **predictions ledger** (`predictions` table) *structured and falsifiable*:
- direction; expected outcome / **target band** (not a single price — a band, to
  avoid exact-tag noise); **timeframe** (`evaluation_due_at`); **stop**;
- the **falsifiers + revisit-triggers** from A1 (the thesis-break conditions);
- **the alternative it was chosen over** — for a HOLD, the *best-in-class peer*
  and the **switching-cost tolerance band**; for an A2 switch, the vehicle it beat.
- **Frozen at authoring** — no hindsight edits to a prediction's terms (the audit
  found hindsight mutation of prediction versions).

*Rationale:* the ledger's 63%-unparseable failure was predictions that were never
testable. If it cannot be scored, it is not saved as a prediction.

### B2. Graded vs the alternative, at maturity — not in a vacuum
At `evaluation_due_at`, **or earlier when a falsifier/revisit-trigger fires**,
score the bet:
- **Directional bets:** outcome bands (`prediction_outcomes.outcome_kind`:
  target/stop/expired-±) **and** return **vs the sleeve's benchmark** — a "correct"
  long that lagged its index is **not** a win (this is the link to Component C).
- **HOLD:** graded against the **best available alternative in the same class**,
  win only if the held name beat the peer by more than the **actual switching
  cost** (CGT on embedded gain + spread). *Blocked on cost basis:* `lots` is empty;
  needs an ingested Schwab cost-basis CSV.
- **A2 switches:** did the switched-to Y actually deliver the modeled fee/tracking/
  tax benefit net of the realized switching cost?

### B3. Learn — a post-mortem, not a checkmark
Each resolved bet gets a **categorized post-mortem** (an agent, fed the frozen
thesis + what actually happened): *thesis wrong / timing wrong / one-off event /
just market beta / data error*. That verdict feeds:
- **source weights** (which signal/source earns the right to drive a future BUY —
  today the ledger says long selection has *no* proven edge; shorts/avoid do);
- **agent prompts** (recurring miss-modes become explicit guardrails);
- **the actionability gate:** *only act on the long side where the ledger has
  proven edge.* Absent proof, default to the low-cost index/UCITS core.

### B4. Honest by construction (guards against the exact failures found)
- Score **all** bets incl. the ones that went nowhere (no survivorship bias).
- **Fail loud** if an evaluation batch scored zero (the evaluator "reported ok
  doing no work").
- Never persist a *settled* "unparseable" outcome for a transient fetch failure —
  retry; a systemic fetch regression must degrade the job, not silently settle the
  whole ledger.

---

## 6. Component C — Scorecard (the proof)

The single number that says whether the loop is net-positive: **realized
time-weighted return vs (a) the market and (b) our own policy indexed passively,
with attribution (allocation / selection / cash / FX) and the Information Ratio.**
Full design in `docs/design/performance_scorecard_design.md`. It closes the loop:
B's per-bet grading rolls up into C's realized-return truth, and C's
selection-attribution tells B/A whether active selection is *adding or destroying*
wealth vs indexing.

---

## 7. Data foundation (grounded; gaps flagged)

| Need | Exists? | Where / gap |
|---|---|---|
| Book / positions time series | Partial | `portfolio_snapshots.positions_json` (symbol/shares/price/value, account); irregular, ~4.5mo, **and currently corrupt** (needs repair) |
| Per-name verdict + falsifiers | Yes | `verdicts` (falsifiers_json, revisit_triggers_json) — shipped this session |
| Sleeve/category mapping | Yes | `instrument_plan_classes` + `resolve_sleeve_label` |
| Target weight per category | Yes | `plan_versions.target_allocation_json` (`classes[].target_pct`) |
| Predictions ledger + outcomes | Yes, broken | `predictions`, `prediction_outcomes` (63% unparseable, hollow evaluator, survivorship) |
| **ETF metadata** (fee/domicile/tracking/AUM) | **NO** | **gap for A2** — must source (provider API) + persist a durable `instrument_metadata` table |
| **Same-category candidate ETF universe** | **NO** | **gap for A2** — curated per-category universe |
| **Cost basis** (for HOLD/switch grading) | **NO** | `lots` empty; needs Schwab cost-basis CSV (owner ask) |
| Benchmark price history | Partial | live-fetch only; scorecard needs a durable `benchmark_prices` table |

---

## 8. Interfaces — how the components wire into one loop

- **A → B:** every actionable verdict/switch emits a structured prediction
  (B1). The falsifiers authored in A1 *are* the thesis-break conditions B2 watches.
- **B → E → A:** post-mortems update source-weights + the actionability gate that
  A's fleet consults before issuing a BUY (closes the learning loop).
- **B ↔ C:** B grades each bet vs its sleeve benchmark; C aggregates realized
  return vs benchmark and attributes selection skill — the same benchmark on both
  sides, so they reconcile.
- **0 underneath everything:** conservation + loud-failure guarantee A/B/C read a
  true book and never mistake a failed run for a clean one.

---

## 9. Build order (each stage makes the next honest)

1. **Integrity floor + repair the book** (parallel work; precondition).
2. **Prediction ledger, honest** (B1/B4 + fix unparseable/survivorship/hollow) —
   because until this works we cannot measure *anything*, including whether A's
   verdicts are any good.
3. **ETF vehicle-selection (A2)** — the missing coverage; deterministic comparator
   + universe + tax-aware switch, fleet sanity-check.
4. **Grading + learning (B2/B3)** — vs-alternative grading + post-mortem feedback;
   HOLD-grading unblocks when cost-basis CSV lands.
5. **Scorecard (C)** — the proof metric; some of it can start earlier (headline
   TWR) but attribution needs A2 + clean sleeves.

## 10. Explicitly out of scope / deferred
- Rebuilding the plan/allocation engine (regime-robust IPS already exists).
- Auto-execution of switches/trades (propose-and-ask stays).
- NVDA managed verdicts (deliberately unmanaged).
- Intraday/tactical trading (long-hold investor).

## 11. Open questions for the adversarial reviewer
1. **A2 equivalence risk:** how do we stop a "cheaper" ETF that is subtly a
   different exposure (factor tilt, hedged vs unhedged, different index) from being
   recommended as a like-for-like switch? Is the fleet sanity-check enough, or do
   we need a quantitative correlation/holdings-overlap gate?
2. **B2 benchmark fairness:** what is the right per-sleeve benchmark, and is
   grading a HOLD vs a *single* best-in-class peer robust, or should it be vs a
   category median?
3. **Switching-cost dependency:** the whole HOLD/switch grading is blocked on cost
   basis (`lots` empty). Is a broker `avg_price` fallback acceptable interim, or
   does that produce misleading grades?
4. **Learning-loop feedback safety:** could the E→A feedback (source-weights
   gating BUYs) create a doom loop that entrenches a bad prior or starves a
   genuinely new signal? What damps it?
5. **Circularity of C:** B grades vs benchmark and C measures vs benchmark — is
   there any double-counting or self-referential scoring between them?
6. **Is the loop honest end-to-end** given the audit? Point at any component here
   that could still report success while its input was silently wrong.
