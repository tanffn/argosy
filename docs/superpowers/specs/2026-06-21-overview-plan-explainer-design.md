# Overview — plain-language plan explainer (design spec)

**Date:** 2026-06-21
**Status:** Approved for implementation (brainstormed with Ariel, visual companion).
**Plan under glass:** pv62 / decision_run 117, role=current.

## 1. Goal

Expose every decision-relevant datum in the canonical plan as a **plain-language, visual story** a non-financial-expert can read. The data already exists in the expert surfaces (/plan, /portfolio, /retirement, /proposals); what's missing is (a) a human translation of what each number *means and why it matters*, and (b) a flagship narrative answering "am I FI yet, how close, what closes the gap?" with the right hero visual. We add a new **Overview** landing surface that tells this story, binding every number to the canonical resolver, and drilling into the expert surfaces for detail.

## 2. Non-goals / guardrails

- **Not a new data source.** Every number comes from `resolve_plan_numbers()` (the same resolver the expert surfaces read). No per-chart divergent sources. (feedback_plan_ui_one_canonical_source.)
- **No fabricated/hand-typed numbers.** Prose voice is human-authored templates; *values* are injected only via `{{fact:key}}` placeholders rendered centrally by `fact_registry`. (canonical-fact-registry doctrine.)
- **Overview explains; Proposals executes.** The Overview is pure explanation + inline "your move" nudges that deep-link to /proposals. The consolidated action checklist lives on **/proposals**, not here.
- **Does NOT reopen B1.** Chapter 5 (forward RSU income) is a **read-only display** of the deterministic vest projection. It is NOT wired into the FI-crossing or savings vector. No change to `fi_crossing` / `_apply_fi_crossing_year`.
- English only this session (narrative_json's HE is deferred).
- No manual UI click-through smokes — backend tests + live e2e are the verification surface.

## 3. Architecture

### 3.1 Route & layout
- New route `ui/src/app/overview/page.tsx`, `"use client"`, wrapped by the existing `layout.tsx` (nav + footer).
- **Layout C**: a sticky left **chapter rail** (the chapter titles; click-to-focus + scroll-spy highlight) and a **focused story panel** on the right. One chapter in focus at a time; smooth-scroll on rail click.
- Add `{ href: "/overview", label: "Overview", Icon: <lucide> }` to `PRIMARY_TABS` in `ui/src/components/nav.tsx`, placed right after Home. **Landing:** keep the existing `/` Home page as-is; Overview is a sibling top-of-nav tab (do NOT repurpose `/`). *(Decision: least churn; revisit making it the literal landing later.)*

### 3.2 Backend assembler
- New service `argosy/services/overview_assembler.py` (pure, testable): `build_overview(session, *, user_id) -> OverviewModel`.
  - Calls `get_current_plan(session, user_id)`; if `None` or `decision_run_id is None` → returns an `OverviewModel` with `available=False` and a reason (fail-loud, no fabrication).
  - Calls `resolve_plan_numbers(session, user_id=…, decision_run_id=plan.decision_run_id, include_canonical_ages=True)`.
  - Builds the chapter list (§4). Each chapter's headline is a deterministic template string with sign/threshold **branching done in Python**, with `{{fact:key}}` tokens for every magnitude, then passed through `render_placeholders(text, resolved, strict=False)`. `strict=False` so a pending fact leaves its token visible and the chapter is marked degraded rather than throwing.
  - For each chapter, runs `find_unauthorized_numbers(rendered_headline)`; if non-empty → raise in tests (guard against hand-typed numbers leaking into a template). At runtime, log + mark chapter degraded.
- New route `argosy/api/routes/overview.py`: `APIRouter(prefix="/overview", tags=["overview"])`, `GET /overview` → `OverviewResponse`. Uses `Depends(get_db)` (import from `argosy.api.routes.plan`), `user_id: str` query param. Register in `argosy/api/main.py` via `app.include_router(overview_router, prefix=api_prefix)`.

### 3.3 Response contract (authoritative — frontend + backend must match)

```python
# Pydantic (argosy/api/routes/overview.py)
class FactRef(BaseModel):
    key: str
    value: float | None
    unit: str
    status: str            # "resolved" | "pending"
    display: str | None    # rendered string, e.g. "₪11.5M" (None if pending)
    source_locator: str
    confidence: str | None

class VizPayload(BaseModel):
    kind: str              # "fi_crossing" | "liquid_split" | "alloc_vs_target"
                           # | "nvda_winddown" | "rsu_forward" | "phase_timeline" | "dual_track_age"
    data: dict             # kind-specific (shapes in §4)

class YourMove(BaseModel):
    label: str             # "Sell ~3,500 shares now"
    href: str              # "/proposals"

class OverviewChapter(BaseModel):
    id: str
    title: str             # rail label, e.g. "Can you stop working yet?"
    eyebrow: str           # small caps line, e.g. "CAN YOU STOP WORKING YET?"
    headline: str          # rendered plain-language sentence (placeholders resolved)
    degraded: bool         # true if any fact pending / unauthorized number found
    facts: list[FactRef]   # the facts cited, for the audit drill
    viz: VizPayload
    drill_label: str       # "See the full retirement detail"
    drill_href: str        # "/retirement"
    your_move: YourMove | None

class OverviewActionsBanner(BaseModel):
    open_count: int
    href: str              # "/proposals"

class OverviewResponse(BaseModel):
    available: bool
    reason: str | None
    plan_version_id: int | None
    decision_run_id: int | None
    as_of: str | None
    chapters: list[OverviewChapter]
    actions_banner: OverviewActionsBanner
```

```typescript
// ui/src/lib/api.ts (mirror exactly; one agent owns all api.ts edits)
export interface OverviewFactRef { key: string; value: number | null; unit: string; status: string; display: string | null; source_locator: string; confidence: string | null; }
export interface OverviewVizPayload { kind: string; data: Record<string, unknown>; }
export interface OverviewYourMove { label: string; href: string; }
export interface OverviewChapter { id: string; title: string; eyebrow: string; headline: string; degraded: boolean; facts: OverviewFactRef[]; viz: OverviewVizPayload; drill_label: string; drill_href: string; your_move: OverviewYourMove | null; }
export interface OverviewActionsBanner { open_count: number; href: string; }
export interface OverviewResponse { available: boolean; reason: string | null; plan_version_id: number | null; decision_run_id: number | null; as_of: string | null; chapters: OverviewChapter[]; actions_banner: OverviewActionsBanner; }
// api.overview: (userId) => getJSON<OverviewResponse>(`/api/overview?user_id=${encodeURIComponent(userId)}`)
```

## 4. The chapters (content + viz binding)

For each: facts cited, the headline template (Python branches on sign/availability; `{{fact:…}}` rendered centrally), the viz `kind` + `data` shape, drill target, and any `your_move`. Numbers in examples are illustrative.

1. **`fi` — "Can you stop working yet?"** (flagship)
   - Facts: `retirement.fi_total_capital_nis`, `portfolio.liquid_net_worth_nis`, `retirement.fi_margin_signed_nis`, `retirement.fi_crossing_year`.
   - Branching: if `fi_margin_signed_nis >= 0` → "You've reached it — you have {{liquid}} vs the {{fi_total_capital}} you need." else → "Almost. You need {{fact:retirement.fi_total_capital_nis}} to live off forever without working; you have {{fact:portfolio.liquid_net_worth_nis}} you can actually spend — so you're **{abs(margin) rendered} short**, and normal growth should close that by {{fact:retirement.fi_crossing_year}}." (The "short" amount is rendered from the signed fact's absolute value via a dedicated placeholder key the assembler registers, OR by rendering `fi_margin_signed_nis` and stripping the sign in prose — see §5.)
   - Viz `kind:"fi_crossing"`, `data: { progress_pct: number, target_nis: number, series: [{year:number, projected_liquid_nis:number}], crossing_year: number }`. The `series` is a deterministic forward projection of liquid wealth (reuse `fi_crossing` forward-FV math; assembler computes ~6 points now→crossing+buffer). Component: new `FiCrossingHero` = progress meter over a compact Recharts `LineChart` with a `ReferenceLine` at target and a `ReferenceDot` at crossing.
   - Drill: `/retirement`.

2. **`liquidity` — "What's actually spendable"**
   - Facts: `portfolio.total_net_worth_incl_residence_nis`, `portfolio.liquid_net_worth_nis`.
   - Headline: "You're worth {{fact:portfolio.total_net_worth_incl_residence_nis}} all in, but only the **{{fact:portfolio.liquid_net_worth_nis}}** that's liquid counts toward retiring — your home equity is real wealth you can't live off."
   - Viz `kind:"liquid_split"`, `data: { liquid_nis, illiquid_nis, total_nis }`. Component: a labeled segmented bar (liquid = "counts" / illiquid = "home, doesn't"). Plain CSS, no Recharts needed.
   - Drill: `/portfolio`.

3. **`allocation` — "Where your money sits vs the plan"**
   - Facts: per-class `allocation.{label}_target_pct` (resolver) + current from snapshot allocations (assembler reads `PortfolioSnapshotRow.allocations_json`). One-line "why" from the plan's `ips` section gloss (short canned lead-in + drill; do NOT inject LLM numbers).
   - Headline: "Here's how your money is split today versus the target mix the plan sets — and why."
   - Viz `kind:"alloc_vs_target"`, `data: { rows: [{label:string, current_pct:number, target_pct:number}] }`. Component: paired horizontal bars (now vs target) per class. New small component.
   - Drill: `/plan`.

4. **`nvda` — "Winding down your NVDA bet"**
   - Facts: `concentration.nvda_current_pct`, `concentration.nvda_target_pct`, `concentration.nvda_cap_pct`, `concentration.nvda_eligible_now_sh`, `concentration.nvda_sell_sh`, `concentration.nvda_target_sh`.
   - Headline: "NVDA is {{fact:concentration.nvda_current_pct}} of your money; the plan trims it toward {{fact:concentration.nvda_target_pct}}. Only about {eligible-share-of-sell rendered} is sellable at the low tax rate right now — the rest is worth waiting for."
   - Viz `kind:"nvda_winddown"`, `data: { current_pct, target_pct, cap_pct, eligible_now_sh, sell_sh, target_sh, held_sh }`. Component: a glidepath line (current→target) + a sell-now/wait split bar (eligible_now vs remaining). May reuse `NvdaTrajectoryChart` if a `NvdaTrajectoryResponse` is cheaply available; default to a dedicated compact component fed by `data`.
   - `your_move`: `{ label: "Sell ~{sell_sh, capped to eligible_now_sh} shares now", href: "/proposals" }`.
   - Drill: `/portfolio`.

5. **`rsu_income` — "The income still coming in"** (READ-ONLY projection)
   - Source: deterministic forward vest projection (`argosy/services/rsu_savings.py::project_quarterly_vests` aggregated to per-year net NIS). Assembler calls a **display-only** helper that produces `{year: net_nis}` for the next ~5 years. **No wiring into fi_crossing/savings.** Mark each `FactRef` source_locator as `rsu_savings.project_quarterly_vests (display only)`.
   - Headline: "Your NVDA grants keep paying out — about {first-year rendered} this year, tapering toward {last-year rendered} by {last-year} as older grants run off."
   - Viz `kind:"rsu_forward"`, `data: { years: [{year:number, net_nis:number}] }`. Component: a compact Recharts `BarChart`.
   - Drill: `/portfolio` (+ `/retirement`).

6. **`phases` — "Life phases ahead"**
   - Source: life-event cashflow phases (assembler reads the phase/expense source feeding `/api/retirement/phase-expenses`; reuse that service function, do not duplicate logic).
   - Headline: "Your spending isn't flat — kids, a wedding, a car every few years. Here's the road of what life costs over time."
   - Viz `kind:"phase_timeline"`, `data: { phases: [{label:string, start:number, end:number|null, annual_nis:number}] }` (start/end as ages or years, consistent). Component: a horizontal phase-timeline (segments along an age axis).
   - Drill: `/retirement`.

7. **`dual_track` — "When can you retire — two honest answers"**
   - Facts: `retirement.earliest_safe_age`, `retirement.preservation_age`.
   - Headline: "Retire and spend normally at about {{fact:retirement.earliest_safe_age}}, or keep every cent of principal safe and it's about {{fact:retirement.preservation_age}}. Same plan — it's your call on the risk."
   - Viz `kind:"dual_track_age"`, `data: { earliest_safe_age:number, preservation_age:number }`. Component: two markers on an age axis. Plain SVG/CSS.
   - Drill: `/retirement`.

### 4.1 Inline action tags + banner
- `your_move` appears only on chapters with a genuine user action (chapter 4 confirmed; others only if the assembler finds a matching open action). Renders as a small "▸ YOUR MOVE: … · do it →" chip linking to `your_move.href`.
- `actions_banner.open_count` = count of open, user-owned actions from `argosy/services/retirement/action_engine.py` (`PrioritizedAction` with `owner in {"ariel","joint"}`) **plus** open `ActionProposal`s — assembler aggregates. Rendered as a compact "N things are waiting for you →" banner at the top of the Overview, linking to `/proposals`.

## 5. Plain-language without fabrication (the critical mechanism)

- Templates live in `overview_assembler.py` as constants. Every monetary/percent/age/share/year magnitude in a template is a `{{fact:KEY}}` token — never a literal.
- Sign/threshold prose (e.g. "short" vs "ahead", "Almost" vs "Reached") is decided in Python from the resolved `value`, choosing between template variants. The chosen variant still carries only placeholders.
- For the FI "short" amount we need the **absolute** value with no sign. Approach: the assembler registers a derived display via the existing render path by rendering `retirement.fi_margin_signed_nis` and, since the registry renders the signed shekel value, the **template variant** is selected by sign and the word "short"/"ahead" supplies the direction; the rendered magnitude uses `abs`. If `fact_registry` renders a leading "−", add a tiny formatter `render_signed_abs(key, resolved)` in the assembler that calls `format_fact(abs(value), unit, display=…)`. (Build-time decision; keep all rendering through `fact_registry.format_fact` so policy stays central.)
- `render_placeholders(strict=False)` at runtime (degrade, don't crash); tests run `strict=True` and assert `find_unauthorized_numbers(headline) == []` for every chapter against a fixture plan.

## 6. Consistency test (the guardrail)

`tests/services/test_overview_consistency.py`:
- Build a fixture plan (reuse existing resolver fixtures / a seeded decision_run).
- For every `FactRef` in every chapter, assert `fact.value` and `fact.display` equal `resolve_plan_numbers(...).get(fact.key)` value / `render_fact(fact.key, resolved)` — i.e. the Overview shows exactly the resolver's number (and therefore the same as /retirement, /portfolio, /plan, which read the same resolver).
- Assert no chapter has `find_unauthorized_numbers(headline)` violations.
- Assert `available=False` path when no current plan (fail-loud, no chapters).

`tests/services/test_overview_assembler.py`: unit-tests each chapter builder against a resolved-facts fixture (headline branches on sign; pending fact → degraded; viz `data` shapes).

## 7. /proposals checklist header

- New component `ui/src/components/proposals/action-checklist-header.tsx`: renders a plain-language "What's on you to do — N of M done" checklist from the user-owned `PrioritizedAction` list + open `ActionProposal`s. Each row: plain title, one-line why, status (done/to-do), and an execute affordance (scrolls to / links the matching proposal section).
- Backend: if no endpoint exposes `action_engine` prioritized actions, add `GET /api/retirement/actions?user_id=` returning the `PrioritizedAction` list (owner-filtered). (Build-time verify; the engine exists at `action_engine.py:22`.)
- Slot into `ui/src/app/proposals/page.tsx` right after the page header (before "Ask the team").

## 8. Parallel execution — file ownership (no two agents share a file)

| Unit | Files (owned) | Depends on | Risky? (codex) |
|---|---|---|---|
| **B1 assembler+route** | `argosy/services/overview_assembler.py`, `argosy/api/routes/overview.py`, registration line in `argosy/api/main.py` | resolver, fact_registry, queries | **yes** (fact binding, sign branching) |
| **B2 rsu display helper** | display-only fn in `overview_assembler.py` calling `rsu_savings.project_quarterly_vests` (B1 owns the file → fold into B1, or a separate `argosy/services/overview_rsu.py`) | rsu_savings | **yes** (money math) |
| **B3 actions endpoint** | `argosy/api/routes/retirement.py` (add `/actions`) or new `argosy/api/routes/actions.py` | action_engine | no |
| **T tests** | `tests/services/test_overview_assembler.py`, `tests/services/test_overview_consistency.py` | B1 contract | — |
| **F1 types+nav** | `ui/src/lib/api.ts` (all overview + actions types/fetchers), `ui/src/components/nav.tsx` | §3.3 contract | no |
| **F2 page+layout** | `ui/src/app/overview/page.tsx` | F1 types | no |
| **F3 chapter+viz components** | `ui/src/components/overview/*` (FiCrossingHero, LiquidSplit, AllocVsTarget, NvdaWinddown, RsuForward, PhaseTimeline, DualTrackAge, ChapterPanel, ChapterRail) | F1 types | no |
| **F4 proposals header** | `ui/src/components/proposals/action-checklist-header.tsx`, slot edit in `ui/src/app/proposals/page.tsx` | F1 types, B3 | no |

**Sequencing:** Phase 1 = B1 (+B2) defines the contract → **codex review** → freeze §3.3. Phase 2 (parallel) = B3, T, F1; then F2/F3/F4 (need F1 types). api.ts and proposals/page.tsx each have a single owner to avoid edit conflicts.

## 9. Verification

- `.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/services/test_overview_assembler.py tests/services/test_overview_consistency.py` green.
- `cd ui ; npm run lint ; npm run typecheck` clean.
- Backend boots; `GET /api/overview?user_id=ariel` returns `available=true` with 7 chapters, each `degraded=false`, no unauthorized numbers.
- Codex review on B1/B2 (fact binding + rsu money-math) returns no blockers.
- Full `pytest -m "not llm_eval"` at end + commit each logical block.
