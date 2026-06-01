# Wave 8 — Plan recap view ("what's the plan, in plain English")

**Drafted:** 2026-06-01 (night, wave 7 closed)
**Status:** scoping rev 1 — codex zigzag pending
**Triggered by:** Ariel pressing Accept All on synth #62 and landing on a confusing fallback view ("loading verdict…" + "showing last completed draft — superseded by a later synthesis that did not produce a fresh draft"). The page doesn't have a post-accept "your plan is canonical, here's what it says" view. Plus the broader complaint: the existing /plan page is engineer-focused (targets / themes / actions / deltas as separate sections) — a non-expert can't tell what the plan actually IS.

## What this wave fixes

Today's failure mode: **"FM approved my plan but I have no idea what it actually says."** Two layers:

1. **No post-accept recap view.** /plan was designed for the "draft to triage" loop; when the user accepts and the loop closes, the page falls back to showing the last non-pending draft (which is often the prior rejected one with confusing warnings). The accepted plan is canonical in the DB (`plan_version=19` after #62) but invisible in the UI.
2. **No human-centric layout.** The accepted plan IS rendered somewhere — in the off-canvas PlanRevisionSheet — but as raw `<pre>` text dump of `horizon_long_md` / `horizon_medium_md` / `horizon_short_md`. No charts. No "by when." No "expected retirement age." No "what changes month by month."

Wave 8 ships the recap view: visual-first, plain-English layout that composes existing visualizations (NVDA trajectory, cashflow projection, Monte Carlo, target progress) into a single "here's your plan" surface.

## Discovery — what already exists, what's missing

Three exploratory passes (Subagent A on `/retirement` + cashflow, Subagent B on `/home` + wealth-dashboard, Subagent C on `/plan` + post-accept state) surfaced this inventory:

### Already built (reusable)

| Component | File | Reuse for |
|---|---|---|
| NVDA trajectory chart (past sales + upcoming vests + reduction program) | `ui/src/components/plan/nvda-trajectory-chart.tsx` | Wave-8 NVDA glidepath visualization |
| Allocation snapshot + plan-target overlay | `ui/src/components/plan/allocation-chart.tsx` | Composes into the larger glidepath; snapshot half is done |
| Concentration card (current vs target reference line) | `ui/src/components/portfolio/wealth-dashboard.tsx:179-252` | Single-name concentration pattern |
| Target progress live strip (current / gap / status badge) | `ui/src/components/plan/delta-card.tsx:733-768` | Per-target gauge in recap |
| Cashflow projection (deterministic 3-scenario bands) | `/api/plan/draft/cashflow-projection` + `argosy/services/cashflow_projection.py` | Backbone of "what happens month by month" section |
| Monte Carlo projection (P10/P25/P50/P75/P90 bands + P(solvent) + P(ruin)) | `/api/plan/draft/cashflow-monte-carlo` + same service | Backbone of "will it work?" section |
| ExpectedRetirementAgeCard (base/bear/bull retirement age) | `ui/src/components/retirement/ExpectedRetirementAgeCard.tsx` | Headline "you can retire at..." |
| RuinProbabilityHero (categorical verdict + 95% CI) | `ui/src/components/retirement/RuinProbabilityHero.tsx` | "Will it work?" verdict badge |
| HolisticTimelineCard (overlaid RSU vests + life events + retire-ready zones) | `ui/src/components/retirement/HolisticTimelineCard.tsx` | Actions timeline |
| Pre-rendered markdown `horizon_*_md` | `/api/plan/draft` + `/api/plan/current/structured` | "Full plan" collapsible section, rendered with proper markdown component instead of `<pre>` |
| Per-target progress backend | `argosy/services/target_progress.py` + `/api/plan/draft/target-progress` | Per-target gauges |

### Missing (wave-8 builds)

| Gap | Description |
|---|---|
| **Post-accept route state** | /plan needs to detect "current plan exists + no pending draft" and render recap, not the draft-review fallback |
| **Allocation glidepath (multi-asset, time-series)** | Today's allocation chart is a single-point snapshot + target overlay. Need a stacked area chart showing how allocation EVOLVES from today through the plan's revisit_after dates |
| **Synthesizer-derived assumption pre-population** | Cashflow sliders today default to hardcoded 0.08/0.18/0.25 with no rationale. Need defaults pulled from the synthesizer's posture + sigma calibration + user's actual portfolio, with a rationale tooltip on each slider |
| **Monte Carlo on the recap view** | Currently MC lives on /retirement. The recap view needs to surface ruin probability + retirement-date distribution alongside the plan |
| **Actions timeline (cross-horizon)** | `actions[]` are buried inside each HorizonSection. Need a unified date-sorted timeline showing "what happens by when" across long/medium/short horizons |
| **Plain-English headline computation** | "Your plan was approved on DATE; next milestone X by DATE; expected safe retirement at age Y" — derived from current PlanVersion + cashflow projection + actions |
| **Cashflow assumption rationales** | Each slider should have a tooltip explaining "8% expected return: calibrated for your NVDA-heavy portfolio; current σ=29% via SigmaCalibrationCard" |

### Critically not duplicating

- `/retirement` already exists and is comprehensive (13 cards: ExpectedRetirementAgeCard, RuinProbabilityHero, HolisticTimelineCard, SafetyGatesPanel, GlidePathCard, RebalancingAlertsCard, PhaseExpenseCard, HealthcareCurveCard, TaxBreakdownCard, HishtalmutTimerCard, DecumulationOrderCard, LumpVsAnnuityCard, BituachLeumiCard). **Wave 8 does NOT replace /retirement.** /retirement is the deep-dive surface; the wave-8 recap view is the at-a-glance "what's the plan" view that links to /retirement for drilldown.
- `cashflow_projection.py` already does the math. **Wave 8 doesn't change the math.** It re-uses the existing routes + adds a sibling helper that derives synthesizer-aware default assumptions.

## The recap view layout

```
─────────────────────────────────────────────────────────────────
│ YOUR PLAN — approved 2026-06-01                                │
│                                                                │
│ Headline:                                                      │
│   You can safely retire at age 49 (base case)                  │
│   Next big move: cross-border attorney retainer by 2026-06-15  │
│   Then: NVDA tranche window opens 2026-06-17                   │
│                                                                │
│ [View full plan ↓]    [→ Drill into retirement details]        │
─────────────────────────────────────────────────────────────────

┌─ ALLOCATION GLIDEPATH ────────┐  ┌─ NVDA SHARE TRAJECTORY ──────┐
│  Stacked area chart:           │  │  Existing NVDA chart reused. │
│  asset class composition       │  │  Today's 64.9% → 15% target  │
│  today → 12mo → 24mo → long    │  │  across reduction program    │
│  Each band = an asset class    │  │  + upcoming RSU vests        │
│  (Equity / Fixed / Cash / RE)  │  │                              │
└────────────────────────────────┘  └──────────────────────────────┘

┌─ WHAT HAPPENS MONTH BY MONTH ─────────────────────────────────┐
│  Cashflow projection — your portfolio + expenses + income     │
│  over the next 30 years (3 scenarios: bear/base/bull)         │
│                                                                │
│  Pre-populated assumptions (click any to see why):             │
│  - Expected return: 8.0%/yr  ▸  why 8%?                       │
│  - Volatility (σ): 29%/yr  ▸  why high? (NVDA concentration)  │
│  - Tax rate: 25%  ▸  why 25%? (Israeli CGT marginal)          │
│  - Lifestyle drift: 0%/yr  ▸  why zero?                       │
│  - Retirement age: 49  ▸  from your goals_yaml                │
│                                                                │
│  [adjust assumptions ↓]                                       │
└────────────────────────────────────────────────────────────────┘

┌─ WILL IT WORK? ────────────────────────────────────────────────┐
│  Monte Carlo: 1000 paths over 50 years                         │
│  P(solvent at 95): 87% (95% CI: 84-90%)   ✅ ON_TRACK          │
│  P(ruin before 75): 1.2%                                       │
│                                                                │
│  Portfolio percentile bands (P10 / P50 / P90):                 │
│  [stacked-bands chart]                                         │
└────────────────────────────────────────────────────────────────┘

┌─ KEY ACTIONS THIS QUARTER ─────────────────────────────────────┐
│  Vertical timeline, sorted by date:                            │
│                                                                │
│  ●  2026-06-08   Refresh tax substrate (FM gate)               │
│  ●  2026-06-12   Wire plan_targets monitor                     │
│  ●  2026-06-15   Cross-border estate attorney retainer         │
│  ●  2026-06-17   NVDA RSU vest (729 sh, ~$157k gross)          │
│  ●  2026-06-17+  NVDA tranche window opens (200 sh max)        │
│  ●  2026-06-30   SGOV → UCITS T-bill migration                 │
│  ●  2026-07-15   plan_targets monitor go-live                  │
│  [show next 12 months ↓]                                       │
└────────────────────────────────────────────────────────────────┘

┌─ FULL PLAN (click to expand) ──────────────────────────────────┐
│  ▸ Long horizon (multi-year)                                   │
│     [renders horizon_long_md as formatted markdown]            │
│  ▸ Medium horizon (12-24 months)                               │
│     [renders horizon_medium_md as formatted markdown]          │
│  ▸ Short horizon (next 90 days)                                │
│     [renders horizon_short_md as formatted markdown]           │
└────────────────────────────────────────────────────────────────┘
```

## Six pieces of work

### Piece A — Post-accept state routing

`/plan` page state machine extended. Today: State D (no pending draft, recent accepted) falls back to last-completed-non-pending draft with stale messages. New shape:

- Detect `current_plan_version != null` AND `no_pending_draft`
- Branch to recap layout instead of the draft-review fallback
- Recap layout reads from `/api/plan/current/structured` (already exists) for the canonical plan
- "Run synthesis" CTA stays prominent for users who want a fresh round
- Pending-draft state continues to show the existing draft-review flow unchanged

### Piece B — Allocation glidepath (multi-asset, time-series)

New backend service `argosy/services/allocation_glidepath.py`:

- Input: current `portfolio_snapshot` (asset-class composition) + the plan's `targets[]` across all three horizons with `revisit_after` dates
- Output: `list[GlidepathPoint]` — for each month from today to the most-distant target date, the projected composition (linear interpolation between current and each target's stated `revisit_after`)
- Multi-horizon handling: per asset class, the closest-in-the-future target with `unit ∈ {pct_of_portfolio, pct_of_liquid}` wins as the endpoint; intermediate targets create waypoint vertices

New backend route: `GET /api/plan/current/allocation-glidepath?user_id=...` returning `list[GlidepathPoint]`.

New UI component `ui/src/components/plan/allocation-glidepath-chart.tsx`:
- Recharts `AreaChart` with stacked bands per asset class
- X-axis: months from today, formatted as YYYY-MM
- Y-axis: 0-100% composition
- Vertical reference line at "today" + each target's `revisit_after` date
- Tooltips show composition at the hovered date + which target's `revisit_after` is nearest

### Piece C — Synthesizer-aware cashflow assumption defaults

Today's defaults are hardcoded (`DEFAULT_MU_NOMINAL_ANNUAL = 0.08`, etc.). They should be derived from the synthesizer's plan + the user's actual portfolio + the existing `SigmaCalibrationCard` machinery, with a rationale string per slider.

New backend helper `argosy/services/cashflow_assumptions.py`:

- `get_default_assumptions(session, user_id, plan_version_id) -> DefaultAssumptionsResponse`
- Pulls:
  - `mu_nominal_annual`: from synthesizer's posture (e.g., capital-preservation → 6-7%; aggressive growth → 9-10%) OR fall back to hardcoded 8% with rationale="capital-preservation posture; weighted average of long-horizon equity expected return"
  - `sigma_annual`: from `argosy/api/routes/retirement.py::projection/sigma-calibrated` (auto-calibrates for NVDA-heavy portfolios; Ariel's gets ~29-30% vs the default 18%)
  - `tax_rate`: from goals_yaml (user-stated) or hardcoded 25% with rationale="Israeli capital-gains marginal rate at user's bracket"
  - `inflation_annual`: hardcoded 2.5% with rationale="BoI long-run target"
  - `retirement_age`: from goals_yaml or hardcoded 49 with rationale="user-stated FIRE target"
  - `lifestyle_drift_annual`: hardcoded 0% with rationale="conservative; matches goals_yaml `lifestyle_aspirations_note`"

Each field carries its `value` + `rationale_md` + `source` (synthesizer / sigma-calibrator / goals_yaml / default).

New route `GET /api/plan/current/cashflow-default-assumptions?user_id=...`.

UI: each slider in the recap's cashflow section reads these defaults on mount + shows a `▸ why?` tooltip with the rationale.

### Piece D — Monte Carlo on the recap view

Re-use `/api/plan/draft/cashflow-monte-carlo` (and add `/api/plan/current/cashflow-monte-carlo` symmetric to it) with the synthesizer-derived defaults from Piece C. Render:

- Re-use existing `RuinProbabilityHero` component for the verdict badge
- New `monte-carlo-bands-chart.tsx` (or reuse if exists in /retirement) showing P10/P50/P90 portfolio over time

### Piece E — Markdown rendering for horizon_*_md

Today the markdown is dumped in `<pre>` tags inside the off-canvas sheet. Replace with a proper markdown component. The repo already uses `<Markdown>` somewhere (subagent C found it referenced on the no-draft fallback path). Lift the same component into the recap's "Full plan" collapsible.

### Piece F — Actions timeline (cross-horizon)

New UI component `ui/src/components/plan/actions-timeline.tsx`:

- Parses `actions[]` across `horizon_long`, `horizon_medium`, `horizon_short`
- Each action has `label`, `horizon_kind ∈ {directional, parameterized, dated}`, `trigger_or_date`, `detail`, `rationale`
- For `dated` actions: extract the ISO date from `trigger_or_date`
- For `parameterized` actions: render with the parameter expression (e.g., "USD/NIS > 2.95" trigger)
- For `directional`: render as "ongoing" without a fixed date
- Vertical timeline sorted by date (dated first, then ongoing)
- Click any action → expands to show `detail` + `rationale` + `cited_sources`

### Piece G — Plain-English headline

New backend helper `argosy/services/plan_headline.py`:

- Inputs: current PlanVersion + cashflow projection + actions timeline
- Output: structured headline with three lines:
  1. **Bottom-line retirement readiness**: "You can safely retire at age 49 (base case) / age 51 (bear case)" — pulls from `effective_retire_ready_age()` for both scenarios
  2. **Next big move**: derived from the soonest-dated action across all horizons (e.g., "Cross-border attorney retainer by 2026-06-15")
  3. **Then**: the SECOND-soonest dated action

UI: prominent card at the top of the recap, with the three lines as large readable text.

## Scope checklist (wave 8)

- [ ] **State routing (Piece A)**: /plan page state machine handles "current exists + no pending draft" → recap layout. Existing draft-review state preserved
- [ ] **Backend service**: `argosy/services/allocation_glidepath.py` with `GlidepathPoint` + projection logic
- [ ] **Backend route**: `GET /api/plan/current/allocation-glidepath`
- [ ] **UI component**: `allocation-glidepath-chart.tsx` (Recharts AreaChart)
- [ ] **Backend service**: `argosy/services/cashflow_assumptions.py` with `DefaultAssumptionsResponse`
- [ ] **Backend route**: `GET /api/plan/current/cashflow-default-assumptions`
- [ ] **UI**: cashflow section consumes the defaults on mount + renders `▸ why?` tooltips with rationale
- [ ] **Backend route**: `GET /api/plan/current/cashflow-monte-carlo` (symmetric to existing draft route)
- [ ] **UI**: Monte Carlo bands chart + RuinProbabilityHero re-used in recap
- [ ] **UI component**: `actions-timeline.tsx` (vertical date-sorted timeline)
- [ ] **Backend service**: `argosy/services/plan_headline.py` with retirement-age + soonest-actions
- [ ] **Backend route**: `GET /api/plan/current/headline`
- [ ] **UI**: headline card at top of recap
- [ ] **Markdown rendering**: replace `<pre>` with `<Markdown>` in "Full plan" collapsible
- [ ] **Tests**: glidepath service edge cases (single horizon, conflicting targets, no targets); default-assumptions service per-field sourcing; headline service when retire_ready_age is null; actions timeline mixed kinds; UI smoke that the recap renders for a real plan_version=19 row

## Open questions (kept minimal)

1. **Defaults — synthesizer-derived vs hand-curated.** Piece C proposes pulling some defaults from the synthesizer's posture (mu derived from "capital-preservation" vs "aggressive growth") and others from hardcoded fallbacks. Question: is the synthesizer's posture field rich enough to deterministically derive a mu? Or should mu always start hardcoded with a rationale string and let the user adjust? **Proposed default: hardcoded 8% with a clear rationale ("capital-preservation portfolio expected return; conservative side of 7-10% historical equity real return")** for v1; revisit if the synthesizer's posture taxonomy becomes structured enough to derive numerically.
2. **Glidepath conflict resolution.** When the long-horizon target and medium-horizon target disagree on the endpoint composition for the same asset class (e.g., long says "NVDA 15%", medium says "NVDA 30%"), the glidepath must pick a path. **Proposed default: linear interpolation through both waypoints in date order** (today → medium target → long target). Codex zigzag may disagree.

## What this wave does NOT do

- Replace `/retirement` — it stays as the deep-dive surface; the recap links to it
- Backfill the recap onto historical PlanVersions — only the current accepted plan is shown
- Re-architect the synthesizer's output schema — recap consumes what's already emitted
- Build a "diff between accepted plans over time" view — that's a future wave
- Touch the draft-review flow (existing /plan UX when a pending draft exists)

## Dependencies

- Wave 5 substrate fixes must be stable (confirmed by #62)
- Wave 7 carry-forward stays as-is — the recap view reads CURRENT plan, not draft state
- Codex zigzag pending — rev 2 of this doc follows codex's review

## Timeline estimate

| Piece | Days |
|---|---|
| A — state routing | 1 |
| B — allocation glidepath service + chart | 2-3 |
| C — synthesizer-aware defaults + rationale | 1-2 |
| D — MC integration on recap | 1 |
| E — markdown rendering | 0.5 |
| F — actions timeline | 1 |
| G — headline + service | 1 |
| Tests + polish | 1-2 |
| **Total wave 8** | **~1.5-2 weeks of focused sessions** |

Ship order: **A → E → G → B → F → C → D**. State routing first so there's something to land into; markdown + headline next so the page has SOMETHING readable even before the charts; then visualizations.
