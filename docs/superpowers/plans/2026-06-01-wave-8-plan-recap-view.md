# Wave 8 — Plan recap view ("what's the plan, in plain English")

**Drafted:** 2026-06-01 (night, wave 7 closed)
**Status:** scoping **rev 2** — codex zigzag SCOPE-CHANGES applied; round 2 confirmation pending
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

**At-a-glance blocks added per codex zigzag round 1** (rendered in or near the headline; details below):

| Block | Source | Why |
|---|---|---|
| **"What changed in this accepted round"** | the N `deltas_from_prior` items the user accepted (4 in #62's case: US-situs $1.37M target, FIRE-bridge analysis, multiple-compression theme, parameterized UCITS top-up trigger) | Non-expert needs to see WHAT they just signed off on, not just "approved" |
| **Current portfolio total value anchor** | latest `portfolio_snapshot.total_usd_value_k` (single number, big text) | Anchors all the percentages — without an absolute number, "64.9% NVDA" doesn't translate to "$2.3M" |
| **Insurance gaps callout** | existing `InsuranceGapsCard` summary (one line: "Life: 3M NIS face — covers X; Disability: missing"; or "No major gaps") | Non-expert plan reading often forgets risk-transfer; one line reminds |
| **Audit line** | `plan_version_id` + `decision_run_id` + `approved_at` timestamp + "View synthesis trail →" link | Trust + auditability; user can drill into the agents' reasoning if they want to |

## Six pieces of work

### Piece A — Post-accept state routing

`/plan` page state machine extended. Today: State D (no pending draft, recent accepted) falls back to last-completed-non-pending draft with stale messages. New shape:

- Detect `current_plan_version != null` AND `no_pending_draft`
- Branch to recap layout instead of the draft-review fallback
- Recap layout reads from `/api/plan/current/structured` (already exists) for the canonical plan
- "Run synthesis" CTA stays prominent for users who want a fresh round
- Pending-draft state continues to show the existing draft-review flow unchanged
- **Explicit state discriminator** (codex zigzag): the page renders one of `{ "no_plan", "pending_draft_triage", "in_flight_synthesis", "recap_current", "stale_fallback_with_warning" }` based on a single derived `view_state` value. Each state has a dedicated test that pins which sub-components render. This prevents the #61/#62 regression where the page silently falls through to a stale view without anyone noticing. Tests cover the FIVE branches explicitly so adding a state in the future requires updating the test matrix.

### Piece B1 — Allocation glidepath BACKEND service + contract

Split per codex zigzag round 1: B1 ships the backend logic (the highest-risk piece — semantics + interpolation + edge cases) so B2's chart can land against a stable, tested API.

New backend service `argosy/services/allocation_glidepath.py`:

- **Input**: current `portfolio_snapshot` (asset-class composition) + the plan's `targets[]` across all three horizons with `revisit_after` dates.
- **Inclusion filter** (codex zigzag): only targets with `unit ∈ {"pct_of_portfolio", "pct_of_liquid"}` are eligible for the glidepath. Other-unit targets (`usd`, `nis`, `shares`, `months`, etc.) are excluded from the glidepath and surfaced in the actions timeline / Full Plan section instead. Documented as a hard rule + tested.
- **Output**: `list[GlidepathPoint]` — for each month from today to the most-distant in-scope target date, the projected composition computed by linear interpolation between waypoints (today's snapshot → each in-scope target's `revisit_after`).
- **Multi-horizon waypoint stitching**: per asset class, all in-scope targets become waypoints sorted by `revisit_after`. The path interpolates from today → waypoint 1 → waypoint 2 → … → last waypoint.
- **Direction-reversal guardrail** (codex zigzag): if an intermediate waypoint **reverses direction** relative to today's value and the eventual endpoint (e.g., current NVDA 64.9% → medium 70% → long 15%), the intermediate is **collapsed** (skipped) unless the target carries an explicit `intentional_hold_or_rise=True` annotation. Default behaviour: smooth monotonic glidepath. Loud warning event logged when a target is collapsed so the user/audit can see WHY.

New backend route: `GET /api/plan/current/allocation-glidepath?user_id=...` returning the glidepath payload with `points: list[GlidepathPoint]` + `collapsed_waypoints: list[CollapsedWaypoint]` + `excluded_targets: list[ExcludedTarget]` (the non-% targets) so the UI can surface "we excluded N targets from the glidepath; see them in the timeline."

### Piece B2 — Allocation glidepath UI chart

New UI component `ui/src/components/plan/allocation-glidepath-chart.tsx`:

- Recharts `AreaChart` with stacked bands per asset class
- X-axis: months from today, formatted as YYYY-MM
- Y-axis: 0-100% composition
- Vertical reference lines at "today" + each in-scope target's `revisit_after` date
- Tooltips show composition at the hovered date + which target's `revisit_after` is nearest
- Sidebar callout when `collapsed_waypoints` or `excluded_targets` are non-empty: "N targets are surfaced in the Actions Timeline / Full Plan instead of on this chart; click to see why."

Ships against the B1 contract; B1 lands first.

### Piece C — Cashflow assumption defaults with rationale (v1: deterministic)

Per codex zigzag round 1: **narrowed for v1** — no synthesizer-posture-string interpretation. The previous draft proposed pulling `mu` from `posture: "capital-preservation"` via free-text mapping; codex flagged this as hidden coupling (synthesizer vocabulary change → silent default shift). For v1, defaults come from three deterministic sources only:

1. **Sigma calibrator** (existing): `/api/retirement/projection/sigma-calibrated` already auto-calibrates portfolio σ for the user's actual positions (NVDA-heavy → ~29-30% vs the default 18%). Wave 8 consumes this directly.
2. **goals_yaml** (existing): user-stated values for `retirement_age`, `tax_rate` (if present), `lifestyle_drift_annual`.
3. **Hardcoded fallback with rationale**: for any field not derivable from (1) or (2), use a hardcoded default with an explicit rationale string.

New backend helper `argosy/services/cashflow_assumptions.py`:

- `get_default_assumptions(session, user_id, plan_version_id) -> DefaultAssumptionsResponse`
- Per-field sourcing for v1:
  - `mu_nominal_annual`: hardcoded `0.08` with rationale="Long-run real-equity expected return; conservative side of 7-10% historical range. Override with your own number if you have a specific portfolio view." (v2 may derive from a structured `risk_profile_enum` IF the synthesizer's output schema is extended to emit one — see open question below.)
  - `sigma_annual`: from sigma-calibrator endpoint with `source="sigma_calibrator"` and rationale showing the calibrated value + the contributing positions (e.g., "Calibrated for your portfolio's NVDA weight: σ=29% vs the unweighted default 18%.")
  - `tax_rate`: from `goals_yaml.tax_rate_pct` if present, else hardcoded `0.25` with rationale="Israeli CGT marginal rate at user's bracket. Adjust if your effective rate is different."
  - `inflation_annual`: hardcoded `0.025` with rationale="Bank of Israel long-run target."
  - `retirement_age`: from `goals_yaml.retirement_target_age` if present, else hardcoded `49` with rationale="Default FIRE target. Override to model what-ifs at other ages."
  - `lifestyle_drift_annual`: hardcoded `0.0` with rationale="Conservative; matches goals_yaml `lifestyle_aspirations_note` if user expects flat real spend."

Each field carries `value` + `rationale_md` + `source ∈ {"sigma_calibrator", "goals_yaml", "default"}`.

New route `GET /api/plan/current/cashflow-default-assumptions?user_id=...`.

UI: each slider in the recap's cashflow section reads these defaults on mount + shows a `▸ why?` tooltip with the rationale.

**v2 follow-on (NOT in wave 8 scope)**: if the synthesizer's output schema is extended to emit a structured `risk_profile_enum: Literal["conservative", "balanced", "aggressive"]` with optional numeric hints (e.g., `mu_hint: float | None`, `sigma_hint: float | None`), the assumptions helper can consume those deterministically. **String-posture interpretation is explicitly out of scope** — codex flagged this as hidden coupling and we agree.

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

- [ ] **State routing (Piece A)**: /plan renders one of 5 explicit states (`no_plan`, `pending_draft_triage`, `in_flight_synthesis`, `recap_current`, `stale_fallback_with_warning`) via a single derived `view_state`. Existing draft-review preserved
- [ ] **State discriminator tests** (Piece A): one test per branch pins which sub-components render. Prevents the #61/#62 fall-through regression
- [ ] **B1 backend — glidepath service**: `argosy/services/allocation_glidepath.py` with `GlidepathPoint` + waypoint interpolation + direction-reversal guardrail + pct-only filter + `excluded_targets` + `collapsed_waypoints` payload fields
- [ ] **B1 backend route**: `GET /api/plan/current/allocation-glidepath`
- [ ] **B1 tests**: single-horizon path; multi-horizon stitching; direction-reversal collapse with logged warning; non-% exclusion list populated correctly; explicit `intentional_hold_or_rise` opt-in honored
- [ ] **B2 UI — glidepath chart**: `allocation-glidepath-chart.tsx` (Recharts AreaChart) + sidebar callout for excluded/collapsed targets. Ships against the B1 contract; depends on B1 landing
- [ ] **C backend — cashflow defaults** (v1 deterministic): `argosy/services/cashflow_assumptions.py` consuming sigma-calibrator + goals_yaml + hardcoded fallbacks with rationale strings; NO synthesizer-posture string interpretation
- [ ] **C backend route**: `GET /api/plan/current/cashflow-default-assumptions`
- [ ] **C UI**: cashflow section consumes defaults on mount + renders `▸ why?` tooltips with rationale
- [ ] **D backend route**: `GET /api/plan/current/cashflow-monte-carlo` (symmetric to existing draft route)
- [ ] **D UI**: Monte Carlo bands chart + `RuinProbabilityHero` re-used in recap
- [ ] **E markdown rendering**: replace `<pre>` with shared `<Markdown>` component in "Full plan" collapsible
- [ ] **F UI — actions timeline**: `actions-timeline.tsx` (vertical date-sorted timeline); includes non-% targets per B1's `excluded_targets` payload so nothing gets dropped
- [ ] **G backend — headline service**: `argosy/services/plan_headline.py` with retirement-age + soonest-actions + total portfolio value + insurance-gap summary
- [ ] **G backend route**: `GET /api/plan/current/headline`
- [ ] **G UI**: headline card at top of recap + the four at-a-glance blocks (what changed / total portfolio value / insurance gaps / audit line)
- [ ] **Tests across the wave**: B1 covered above; C per-field sourcing tests (sigma_calibrator vs goals_yaml vs default); G headline when retire_ready_age is null; F mixed-kind actions; E markdown safety (no XSS / arbitrary HTML); smoke test that the recap renders for a real plan_version=19 row in CI

## Open questions (kept minimal)

Codex zigzag round 1 resolved the original open question #1 (synthesizer-derived mu — answer: no, hardcoded-with-rationale for v1; structured enum-only contract for v2 if ever wanted). Remaining open questions are minimal + bounded:

1. **Direction-reversal default behaviour** (codex zigzag added). When an intermediate waypoint reverses direction relative to today's value and the eventual endpoint (e.g., current NVDA 64.9% → medium 70% → long 15%), the matcher collapses the intermediate by default unless the target has an explicit `intentional_hold_or_rise=True` annotation. **Question for Ariel**: does the synthesizer ever emit "let it run for a year, then cut harder" plans where the rising intermediate IS intentional? If yes, we'd want a way for the synthesizer to flag it via the new annotation (schema extension). **Proposed default**: skip the schema extension for v1; the default-collapse behaviour matches every plan we've seen so far. Revisit only if a real plan surfaces a legitimate intentional rise.
2. **Inclusion rule for non-% targets** (codex zigzag added). Only `pct_of_portfolio` + `pct_of_liquid` units are in the glidepath; other-unit targets (`usd`, `nis`, `shares`, `months`, etc.) are surfaced in the Actions Timeline / Full Plan instead. **Question**: should the recap have a third visualization dedicated to non-% targets (e.g., a "FI dollar target progress" gauge for the 22M NIS bare-FI target)? **Proposed default**: defer to a future wave; for v1 they appear as items in the Actions Timeline + Full Plan only.

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
