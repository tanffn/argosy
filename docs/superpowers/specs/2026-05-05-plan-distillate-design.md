# Argosy — Plan Distillate & Monthly Synthesis Design

| Field | Value |
|---|---|
| **Date** | 2026-05-05 |
| **Status** | Design approved; spec awaiting user review |
| **Authors** | Ariel + Claude (collaborative brainstorm) |
| **Related** | [SDD §6 Intake](../../design/SDD.md#6-intake-phase), [SDD §3 Agent Fleet](../../design/SDD.md#3-agent-fleet), [SDD §10 Execution](../../design/SDD.md#10-execution--approval-workflow), [Jacobs_Wealth_Plan.md](D:/Google%20Drive/Family/Finances/Portfolio/Jacobs_Wealth_Plan.md) |
| **Phasing** | Wave 1 lands inside SDD Phase 1; Wave 2 inside Phase 3; Wave 3 inside Phase 5 |

---

## 1. Context & motivation

The Argosy advisor today can ingest a long-form wealth plan via `/api/intake/upload`, persist it in `plan_versions`, and run `PlanCritiqueAgent` against it. The critique is a one-shot pass: RED/YELLOW/GREEN findings rendered against the raw plan markdown.

This design replaces "treat the plan as a fixed document the advisor periodically critiques" with **"the plan is one input among several; a fleet of specialists periodically synthesizes a fresh plan that the user accepts or rejects."** The user's framing:

> A firm of experts whose mandate is to grow my wealth toward early retirement, leveraging what I already have. I speak to one person — the advisor. The specialists are visible behind him. I drop new info; the advisor routes; I get a synthesized answer. Monthly check-ins where I share the latest status and get a revised long/medium/short plan. "No change, continue as is" is a valid answer.

Three design pressures shaped this work:

1. **The plan ages.** The Jacobs Wealth Plan v2.0 (Feb 2026) is rich but every concrete number — current 66% NVDA, 3.09 NIS/USD, $171.81 share price, Q1/Q2/Q3/Q4 tranche schedules — decays the moment the file is written. Continuously injecting the full plan into LLM working memory would actively poison the advisor with stale specifics.
2. **The plan is not authority.** Loyal-advisor framing means every part of the plan must be challengeable by the specialist fleet at any time. The plan starts a conversation; it doesn't end one.
3. **The user wants one focal point.** The advisor surface is the only place the user interacts; the team behind it must be visible (citations, provenance) but the user shouldn't have to talk to them directly.

This document specifies how the plan flows through the system from baseline import through monthly synthesis to per-trade decisions.

---

## 2. Architecture overview

Three artifacts, three flows, one canonical plan.

### 2.1 Three artifacts

All rows in the existing `plan_versions` table, distinguished by a new `role` column:

| Role | Purpose | Lifetime | Notes |
|---|---|---|---|
| `baseline` | The user's imported source plan | Replaced when user imports a new baseline; otherwise indefinite | Stores raw markdown + a structured **distillate** (§3) |
| `draft` | A synthesis output awaiting user accept | Promoted to `current` on accept; replaced on re-synthesis; demoted to `superseded` on reject | At most one per user at a time |
| `current` | The synthesized plan the advisor anchors on | Demoted to `superseded` when next draft accepts | Exactly one per user at a time |
| `superseded` | Historical | Indefinite | Read-only; preserved for audit and lineage |

### 2.2 Three flows

| Flow | Trigger | Cost | Output |
|---|---|---|---|
| `plan_distill_flow` | Baseline import; baseline file change (via `plan_watcher`); manual re-distill | ~$0.30 | Populates `distillate_json` + `distillate_rendered` on the baseline row |
| `plan_synthesis_flow` | Monthly cycle (1st of month); user-initiated `/api/advisor/check-in` | ~$4-6 | New `role=draft` `plan_versions` row with three horizon documents |
| `decision_flow` (existing) | Per-trade triggers (price, news, cadence) | per SDD §3.7 | `proposals` rows; references `current` plan but doesn't produce one |

### 2.3 One canonical plan

The advisor's working memory anchors on `role=current`. The baseline distillate is **never** directly injected into advisor turns — its only consumer is `plan_synthesis_flow`. This is the structural enforcement of "starting line, not north star": the only path from Jacobs' opinions to the advisor runs through a synthesis pass that the fleet has reviewed and the user has accepted.

### 2.4 Cadence map (extends SDD §5.1)

| Cadence | New addition |
|---|---|
| Daily | `plan_watcher` — hashes configured baseline source path; on diff, re-distill (preserving user edits) |
| Monthly | `monthly_cycle` triggers `plan_synthesis_flow` on the 1st |
| Quarterly | Re-runs synthesis with extra prompt weight on medium-horizon revision |
| Annual | Re-runs synthesis with extra prompt weight on long-horizon revision (existing §5.1 "annual" cadence) |
| Ad-hoc | `/api/advisor/check-in` invokes synthesis with current state (user-initiated) |

---

## 3. The Jacobs distillate

### 3.1 Purpose

The distillate is the **only** representation of the baseline plan that downstream synthesis ever sees. It captures durable principles, decision rules, and targets-as-stated. It explicitly drops time-stamped numbers and dated implementation details.

Target size: 1500-2500 tokens rendered (vs. ~30k for the raw Jacobs plan).

### 3.2 Content schema

`PlanDistillate` pydantic model with the following fields. Each item carries `source_section: str` for click-through to the raw plan.

| Field | Type | Examples |
|---|---|---|
| `goals` | `list[Goal]` | Retirement target year (2031); target annual income (360k NIS); FI status; employment horizon (5+ years from 2026) |
| `principles` | `list[Principle]` | "UCITS-first for estate safety," "NIS salary covers NIS expenses (natural hedge)," "real-returns framework," "concentration is the load-bearing risk" |
| `risk_priorities` | `list[str]` (ordered) | `["concentration", "fx", "sector_overweight", "sequence_of_returns"]` — first item dominates |
| `decision_rules` | `list[DecisionRule]` | "Spread RSU sales across years for bracket-aware tax," "gap-weighted deployment," "no Defensive above cap," "never panic-convert NIS↔USD" |
| `targets` | `list[Target]` | Each carries `value`, `unit`, `stated_at: date`, `revisit_after: date`, `source_section: str`. Examples: NVDA → 15%, defensive 5-8% glidepath, Core 20-25%, Growth 15-20%, International 7-10%. |
| `constraints` | `list[Constraint]` | "No consolidate brokers," "UCITS preferred," "limited account capped at $1k," speculation cap |
| `stress_tolerance` | `str` | Free text — "willing to ride 30% drawdown while employed; salary buffers consumption" |

### 3.3 Explicit exclusion list

The distiller's system prompt enumerates these as items to drop (never extract):

- Current portfolio percentages (66% NVDA, 19% defensive)
- Current FX rates (3.09 NIS/USD)
- Specific dollar amounts at point-in-time ("$430k proceeds," "$171.81/share")
- Dated tranche schedules (Q1 2026 sells 2,500 shares)
- Share counts (12,748 NVDA shares)
- Implementation roadmap "next 30/90 days" sections — those belong in `current.short`, derived from synthesis

### 3.4 Provenance

Every extracted item carries a `source_section` pointing back to the plan heading it came from (e.g. `"Investment Strategy & Risk Management → Target Asset Allocation"`). Clicking an item in the UI opens the raw plan at that section.

### 3.5 Storage

Two new columns on `plan_versions`, populated only when `role=baseline`:

- `distillate_json` (json null) — `PlanDistillate` payload
- `distillate_rendered` (text null) — pre-rendered markdown view

Plus housekeeping for the watcher:

- `source_path` (str null) — e.g. `"D:/Google Drive/Family/Finances/Portfolio/Jacobs_Wealth_Plan.md"`
- `source_hash` (str null) — sha256 of `raw_markdown`
- `distilled_at` (datetime null) — last distill run timestamp

The raw plan stays in `raw_markdown` — distillation never replaces it.

### 3.6 Distiller agent

`PlanDistillerAgent` (`argosy/agents/plan_distiller.py`):

- Sonnet, single pass, `output_model=PlanDistillate`
- `require_citations=True` — every extracted item must reference a `source_section`
- `max_tokens=8192`
- System prompt includes the explicit exclusion list above

### 3.7 Lifecycle

- Runs once on baseline import (post-write callback in `intake_upload` flow)
- Re-runs on baseline file change (`plan_watcher` daily hash check)
- Manually re-runnable via "Re-distill" button on the plan-in-scope UI card

### 3.8 User editability

The user can edit individual distillate items. Each item carries a `user_edited: bool` flag.

- Re-distillation respects user edits (does not overwrite) unless force-flagged
- A user-edited item that conflicts with a fresh extraction surfaces a diff prompt: *"the source plan changed; you have N user-edited items; want to keep yours, take new, or merge?"*
- Plan-watcher's auto-rerun on file change never silently clobbers user edits

---

## 4. The plan synthesis flow

### 4.1 Overview

`plan_synthesis_flow` is a new orchestration distinct from the existing per-trade `decision_flow`. It runs at T3 depth (per SDD §4) and convenes the full agent fleet (per SDD §3.1-§3.5) for a plan-revision verb instead of a trade-revision verb.

The flow nests cleanly with `decision_flow`:

- Synthesis sets **targets** ("medium: NVDA → 12%; short: harvest IBIT loss")
- Decision flow proposes **trades** against those targets when conditions are right
- Synthesis writes `plan_versions` rows but **no `proposals` rows**

### 4.2 Trigger model

| Trigger | Source |
|---|---|
| Scheduled monthly | `monthly_cycle` (1st of month) |
| Scheduled quarterly | After quarter close — same flow with extra prompt weight on medium horizon |
| Scheduled annual | January 2nd — same flow with extra prompt weight on long horizon |
| User-initiated | `POST /api/advisor/check-in` — body may include `guidance` + `urgency` |

Synthesis is **idempotent**: if an unaccepted draft already exists, the next run replaces it (with a one-line audit note `superseded by run X`).

### 4.3 Inputs

The orchestrator assembles the input bundle before invoking Phase 1:

| Input | Source |
|---|---|
| Baseline distillate | `plan_versions WHERE role=baseline` (the active baseline; one per user) |
| Prior `current` plan | `plan_versions WHERE role=current` (empty on first synthesis) |
| Portfolio snapshot | Latest TSV/CSV ingest + IBKR positions; includes lots for tax-aware reasoning |
| Recent fills + decisions | `fills`, `proposals` last 90 days |
| Domain KB refs | Per-jurisdiction rules from `domain_knowledge/` (RAG'd by relevance) |
| User profile | `user_context` (identity, goals, constraints, life-event flags) |
| Watchlist signals | Watchlist agent's universe + recent material events (for speculative candidates) |

### 4.4 Five-phase fleet review

All phases write to `agent_reports`, stamped with one `decision_run_id` for end-to-end audit reconstruction. Wall-clock estimates below assume parallel execution within each phase and Sonnet/Opus default models per SDD §3.8.

```
Phase 1 — Analyst reports (parallel, ~3-5 min wall-clock)
  9 specialists run in parallel: fundamentals, news, technical,
  sentiment, macro, plan-critique, concentration, tax, fx
  → 9 structured reports

Phase 2 — Researcher debate (per-horizon, ~5 min wall-clock total)
  bull / bear / facilitator argue THESES, not trades:
    - long:    "do the principles + targets still hold?"
    - medium:  "given current state + analyst reports,
                what tactical posture for next 1-2 years?"
    - short:   "any specific calls for the next 30 days?
                anything to defer or pull forward?"
  → 3 DebateOutcome records (one per horizon, runs in parallel)

Phase 3 — Synthesizer (the "planner," Opus, ~1-2 min wall-clock)
  Inputs: distillate + prior current + analyst reports
        + 3 debate outcomes + portfolio snapshot
  Output: three HorizonSection drafts

Phase 4 — Risk team review (parallel, ~2 min wall-clock)
  aggressive / neutral / conservative
  Each reviews the FULL draft plan; produces RiskReview
  verdicts at the PLAN level (not per-trade).
  Risk facilitator merges → consolidated risk verdict.

Phase 5 — Fund manager integrity check (~1 min wall-clock)
  Validates: distillate hard-constraints honored?
             three horizons cohere?
             every target has rationale + cited source?
             "no change" justified by evidence if claimed?
  Green-lights → role=draft awaiting user accept.

Total wall-clock: ~12-15 minutes from trigger to draft-ready.
```

### 4.5 Output schema

Each horizon is a `HorizonSection`:

```python
class HorizonSection(BaseModel):
    horizon: Literal["long", "medium", "short"]
    freshness_expected: Literal["annual", "quarterly", "monthly"]
    status: Literal["no_change", "minor_revision", "major_revision"]
    posture: str                                  # 3-5 sentence narrative
    targets: list[Target]                         # numeric, with as-of + rationale
    themes: list[Theme]                           # qualitative tilts
    actions: list[Action]                         # directional / parameterized / dated by horizon
    speculative_candidates: list[SpeculativeCandidate]  # short only; empty on others
    deltas_from_prior: list[Delta]                # what changed vs. previous current
    rationale: str                                # why; references analyst reports + debate IDs
    cited_sources: list[str]                      # plan refs + domain_kb + agent_report_ids


class Target(BaseModel):
    label: str                  # e.g. "NVDA concentration"
    value: float                # the number (e.g. 0.15 for 15%)
    unit: str                   # "pct_of_portfolio" | "pct_of_net_worth" | "usd" | "nis" | "shares" | "ratio"
    stated_at: date             # when this target value was set in this synthesis
    revisit_after: date         # when to revalidate (drives next-cycle weighting)
    rationale: str              # why this value
    source_section: str | None  # if directly traceable to baseline distillate item


class Theme(BaseModel):
    label: str                  # e.g. "Tighter NVDA cap given DeepSeek + tariffs"
    direction: Literal["lean_into", "lean_away_from", "monitor"]
    rationale: str
    cited_sources: list[str]


class Action(BaseModel):
    label: str                  # e.g. "Sell NVDA tranche on next strength"
    horizon_kind: Literal["directional", "parameterized", "dated"]
    # directional: "continue NVDA reduction toward 15% over remaining horizon"
    # parameterized: "if VIX > 30 OR NVDA > $250: accelerate tranche size by 50%"
    # dated: "harvest IBIT loss before 2026-05-15"
    trigger_or_date: str | None
    detail: str                 # the action body
    rationale: str
    cited_sources: list[str]


class Delta(BaseModel):
    item_kind: Literal["target", "theme", "action", "speculative_candidate"]
    item_id: str                # stable id within the draft for per-delta accept/reject
    horizon: Literal["long", "medium", "short"]
    change_kind: Literal["added", "removed", "modified"]
    summary: str                # one-line for the side sheet
    prior: dict | None          # serialized prior version (for modified/removed)
    proposed: dict | None       # serialized proposed version (for added/modified)
    rationale: str
    cited_sources: list[str]
    accepted: bool = False      # flipped per-delta in the UI
    user_edited: bool = False
    user_edit_note: str | None  # populated when user edits the proposed value
```

### 4.6 Per-horizon character

| Horizon | Character | Typical `status` distribution | When weight peaks |
|---|---|---|---|
| **Long (5+ yrs)** | Posture-heavy, few targets, directional actions ("continue NVDA reduction toward 15% over remaining horizon") | `no_change` is the common case | Annual cycle (January) |
| **Medium (1-2 yrs)** | **Strategic centerpiece.** Tactical targets, themed actions, parameterized triggers ("if VIX > 30 OR NVDA > $250: accelerate tranche size by 50%") | Most monthly synthesis activity here | Quarterly cycle |
| **Short (~30 days)** | Dated, concrete, replaced every monthly cycle. Includes `speculative_candidates`. | Almost always `minor_revision` or `major_revision` | Monthly cycle |

### 4.7 Speculative candidates

Structural home in `short.speculative_candidates`. Surfaced as *"worth a small swing if you want it"* — never as a recommendation.

```python
class SpeculativeCandidate(BaseModel):
    ticker: str
    thesis_summary: str
    suggested_position_usd: float
    suggested_position_pct_of_net_worth: float   # MUST satisfy speculation cap
    risk_ceiling_check: bool                      # passes user's cap (e.g., < 0.1%)
    horizon_days: int                             # typically 5-60
    expected_drawdown_pct: float
    exit_trigger: str                             # "stop loss at -20%, take profits at +50%"
    sourced_from: list[str]                       # which analyst flagged this
```

Sourced from watchlist + news + sentiment in Phase 1; the synthesizer decides which to surface this month based on current portfolio state and `agent_settings.yaml::speculation.max_pct_of_net_worth`.

### 4.8 "No change" as a first-class outcome

Each horizon's `status` may be `no_change` with empty `deltas_from_prior` and an honest one-paragraph rationale:

> *"Nothing material changed — analyst reports are within band, plan-critique unchanged, macro regime stable."*

UI surface: *"This month: no changes recommended. Last reviewed [date]. Accept and continue?"* — one-click accept rolls the prior plan forward as the new `current`.

### 4.9 Routing matrix entry

Add a row to SDD §10.1:

| Decision kind | Account class | Mode | Path |
|---|---|---|---|
| `plan_revision` | Any | Any | Human queue, **always T3 depth, never auto-execute** |

No tier-mode override can downgrade a plan-revision flow. This puts plan synthesis under the same approval discipline as a T3 trade.

---

## 5. Schema migrations

Three small additive Alembic migrations on the existing `plan_versions` table. No new tables; reuses existing `decision_runs` and `agent_reports`.

### 5.1 `0015_plan_versions_lifecycle`

| Column | Type | Notes |
|---|---|---|
| `role` | enum: `baseline` \| `draft` \| `current` \| `superseded` | Was implicit; now explicit |
| `accepted_at` | datetime null | Populated when user accepts a draft |
| `accepted_by_user_id` | str null | Audit; future-proofs household sign-off |
| `superseded_at` | datetime null | Populated on demotion |
| `derived_from_id` | int null FK→`plan_versions.id` | Lineage for synthesized rows |
| `decision_run_id` | str null FK→`decision_runs.id` | Links synthesis row to the fleet-review run |

Same migration also adds `decision_runs.decision_kind` (values `trade_proposal` | `plan_revision`) — folded in for atomicity.

Constraints:

- Partial unique `(user_id) WHERE role='baseline'` — one active baseline per user
- Partial unique `(user_id) WHERE role='current'` — one current per user
- Partial unique `(user_id) WHERE role='draft'` — one in-flight draft (replaced if synthesis reruns)
- Index `(user_id, role)` — primary query path
- Index `(user_id, imported_at DESC)` — history view

State transitions:

```
intake_upload → role=baseline
  plan_watcher diff → re-distill (same row), OR new baseline; old baseline → superseded

monthly_cycle / check-in → role=draft
  user accept → role=current; prior current → superseded
  user re-runs → draft replaced (one-line audit note)
  user reject → draft → superseded with rejection_reason
```

### 5.2 `0016_plan_versions_distillate`

Populated only for `role=baseline` rows.

| Column | Type | Notes |
|---|---|---|
| `distillate_json` | json null | `PlanDistillate` payload |
| `distillate_rendered` | text null | Pre-rendered markdown view |
| `source_path` | str null | Path to source baseline file |
| `source_hash` | str null | sha256 of `raw_markdown` |
| `distilled_at` | datetime null | Last distill run timestamp |

### 5.3 `0017_plan_versions_synthesis`

Populated only for `role∈{draft,current,superseded}` synthesized rows.

| Column | Type | Notes |
|---|---|---|
| `horizon_long_json` | json null | `HorizonSection` for long horizon |
| `horizon_medium_json` | json null | `HorizonSection` for medium horizon |
| `horizon_short_json` | json null | `HorizonSection` for short horizon |
| `horizon_long_md` | text null | Pre-rendered for UI |
| `horizon_medium_md` | text null | Pre-rendered for UI |
| `horizon_short_md` | text null | Pre-rendered for UI |
| `synthesis_inputs_json` | json null | Provenance: `{baseline_id, prior_current_id, snapshot_id, fill_ids[], agent_report_ids[], debate_outcome_ids[]}` |

### 5.4 Why JSON columns instead of separate tables?

Synthesis writes one row; reads pull one row; UI renders one row. SQLite's JSON1 extension covers the rare cross-plan analytics queries. Matches the existing SDD pattern (`agent_reports.payload_json`). Future `plan_targets` materialized view can be added without touching the synthesis write path.

### 5.5 Reuse of existing tables

- `decision_runs` (migration 0004): now stamped with `decision_kind` to distinguish synthesis runs from trade runs
- `agent_reports`: receives the per-phase outputs of synthesis (Phase 1 analyst reports, Phase 2 debate outcomes, Phase 3 synthesizer output, Phase 4 risk reviews, Phase 5 FM verdict)
- `plan_critiques` (existing): unchanged. Continues to receive one-shot critiques on baseline import and on user-demand re-critique. Synthesis-time plan-critique runs land in `agent_reports` instead.

---

## 6. Prompt-injection strategy

### 6.1 Three rules across all agents

1. **The advisor only ever sees `current`** — never the baseline distillate. The distillate exists to feed synthesis only.
2. **Every plan-injecting prompt carries an authority disclaimer** defined once in `argosy/agents/_plan_authority.py`:

   > *"This plan is one input among portfolio state, market data, news, and your own analyses. Cite it when you reason; disagree when evidence warrants. The plan is not authority — your job is to be loyal to the user, not to the plan."*

3. **All plan context delivered via prompt caching.** Per the Anthropic SDK pattern, the plan + base system prompt + user context are marked with `cache_control` blocks; first turn of a session pays the cache miss; subsequent turns ride the hit.

### 6.2 The compact projection

Deterministic Python helper at `argosy/agents/_plan_projection.py` (no LLM call). Reads a `current` row and emits a ~500-800 token markdown block:

```
=== Your current plan (synthesized YYYY-MM-DD; accepted YYYY-MM-DD) ===

[long, freshness=annual]   Posture: <one-line summary>
                           Retirement: <year>; target income <amount>

[medium, freshness=quarterly]
   Top targets (with stated-at):
     - <target 1>
     - <target 2>
   Active themes:
     - <theme 1>
   Parameterized triggers:
     - <rule 1>

[short, freshness=monthly]
   Active actions (next ~30 days):
     - <action 1>
   Speculative candidates surfaced this month:
     - <ticker>: max <usd> (= <pct> NW) · <thesis> · <exit>

=== End plan ===
```

Regenerated only when `current` changes; otherwise read from cache.

### 6.3 Injection map by flow

| Flow | What's injected |
|---|---|
| Advisor turn (`gap_driven` or `user_driven`) | Compact projection + all 3 horizons' rendered MD + authority disclaimer |
| Decision flow — analysts | Compact projection only |
| Decision flow — bull/bear debate | Compact projection + medium themes (full prose) |
| Decision flow — trader | Compact projection + short actions (full prose) |
| Decision flow — risk team | Compact projection + relevant horizon prose |
| Decision flow — fund manager | All three horizons' full prose |
| Synthesis Phase 1 — analysts | Baseline distillate + prior current (compact + relevant horizon prose) |
| Synthesis Phase 2 — debaters | Distillate + prior current full + 9 analyst reports |
| Synthesis Phase 3 — synthesizer | Everything from Phase 2 + 3 debate outcomes |
| Synthesis Phase 4 — risk | Distillate + new draft (full) + analyst reports |
| Synthesis Phase 5 — fund manager | Distillate + new draft + risk verdicts |

### 6.4 Per-turn token shape

Approximate, with caching:

| Block | Tokens | Cache control |
|---|---|---|
| Base system prompt + authority disclaimer | ~1500 | ephemeral |
| Plan compact projection + horizon MD | ~6000 | ephemeral |
| User context summary + gap status | ~700 | ephemeral |
| Conversation history + current message | ~500-2500 | none |

### 6.5 Cache invalidation triggers

Explicit, not relying on TTL alone:

- `current` changes (draft accepted) → all advisor sessions invalidated
- Baseline re-distilled → synthesis-flow caches invalidated
- `user_context` changes (intake turn with `context_updates`) → user-context block invalidated

### 6.6 Decision: full-horizon-prose injection

**Decision: always include all three horizons' full markdown in advisor prompts.** Caching makes the cost trivial; complexity is the real expense; the user pays for clarity. Per the user's accuracy-over-cost preference, we don't optimize this.

---

## 7. UI flow during monthly check-in

The interaction honors "draft + explicit accept, with conversational walkthrough" — built on top of the advisor page improvements discussed in the parallel UI thread. No new screens.

### 7.1 Notification — three places at once

When `plan_synthesis_flow` writes a `role=draft` row:

- **Home brief** (`<AdvisorBriefCard>`) — new bullet kind `draft_plan`, `FileCheck` icon (rose-400 tint). CTA shifts from "Talk to advisor" to "Review monthly plan."
- **Advisor page** — sticky banner above the chat: *"Draft plan ready (synthesized YYYY-MM-DD) · N deltas vs. last month · [Review now] [Skim diff first]"*
- **Email** (if `alerts.email.enabled`) — short message + 1-click link with rotating token (per SDD §10.2 phishing-aware rules).

### 7.2 Initial review surface — side sheet, not modal

Click *Review now* → right-side `Sheet` slides in over the advisor page. Width ~50% of viewport on desktop, full-width on mobile. The chat stays visible-but-dimmed on the left so the conversational walkthrough can interleave.

Side-sheet layout:

```
┌─────────────────────────────────────────────────────────┐
│ Monthly plan revision · synthesized YYYY-MM-DD          │
│ Draft N · derived from baseline #X, prior current #Y    │
│                                                          │
│ [Walk me through it]   [Just show me the diff]   [✕]    │
│ ───────────────────────────────────────────────────────  │
│                                                          │
│ Tabs:  Deltas (3)  ·  Long  ·  Medium ★  ·  Short        │
│                                                          │
│ ───── Deltas tab content ─────                          │
│                                                          │
│ [Medium] NVDA target tightened 15% → 12%                 │
│   Why: <rationale>                                       │
│   Cited: <chips>                                         │
│   Prior: <prior value with as-of>                        │
│   [✓ Accept]   [✗ Reject]   [✎ Edit]   [↯ Discuss]      │
│                                                          │
│ ...                                                      │
│                                                          │
│ ───────────────────────────────────────────────────────  │
│ [Accept all remaining]   [Reject draft + re-synthesize] │
└─────────────────────────────────────────────────────────┘
```

Medium tab is starred (★) — visual cue that it's where most of the firm's work landed.

### 7.3 Conversational walkthrough — the "seed of C"

*Walk me through it* makes the side sheet a passive viewer and pushes the conversation into the chat surface. The advisor agent posts (in chat, normal Markdown bubble):

> *"Three deltas this month. Want to start with the medium-horizon NVDA target tightening — that's the load-bearing one. The macro analyst flagged DeepSeek efficiency gains accelerated and the conservative risk officer pushed for a tighter cap. I'm proposing 12% instead of 15%. Want to see the analyst report, or should I move on to the short-horizon harvest action?"*

User responds in chat. Each acceptance/rejection updates the side sheet's checkmarks live (WebSocket — see §7.6). The chat and the sheet are two views of the same accept-state.

When the user types a counter-proposal ("tighter, but 13%"), the advisor:

- Updates the draft target inline (writes back to `horizon_medium_json` with `user_edited=true`)
- Posts: *"Set medium NVDA target to 13% — noting your edit. Risk team didn't argue this exact number; I'll flag it for next monthly review to revalidate."*

Default-focused button on first open: **Walk me through it** (per accuracy-over-cost preference; conversational is the loyal-advisor default). *Just show me the diff* is secondary.

### 7.4 Accept / reject lifecycle

- **Accept individual delta** — flips `accepted: true` on that item; doesn't promote the draft. Audit row written.
- **Accept all remaining** — when all deltas are individually accepted (or in bulk), draft promotes to `role=current`; prior `current` becomes `superseded`. Advisor's prompt cache invalidated.
- **Reject draft + re-synthesize** — modal: *"What should the fleet reconsider?"* Free-text + optional structured guidance (checkboxes: "use Opus for plan-critique," "weight tax analyst more heavily," "include this domain doc"). Submits to `/api/advisor/check-in` with guidance attached. Original draft → `superseded` with `rejection_reason`.
- **Edit then accept** — small inline edit form per delta. Editable fields by `item_kind`:
  - `target`: `value`, `revisit_after`
  - `theme`: `label`, `direction`
  - `action`: `trigger_or_date`, `detail`
  - `speculative_candidate`: `suggested_position_usd`, `exit_trigger`, or "skip" (removes the candidate from the accepted draft)

  Writes back to draft JSON with `user_edited=true` and a `user_edit_note` capturing what the user changed. Re-synthesis preserves user edits unless force-flagged.
- **No-change accept** — if synthesis returned `status=no_change` for all three horizons, side sheet shows: *"No changes recommended this month. Last reviewed [date]. [Accept and continue] [Discuss anyway]"*. One-click accept rolls forward.

### 7.5 Provenance click-throughs

Every claim has a *Cited:* line with chips. Each chip click opens a side panel:

| Citation type | Click behavior |
|---|---|
| `agent_report:<id>` | Opens the full structured report from that analyst's run |
| `decision_run:<id>` | Opens the synthesis run timeline (all 14+ agent calls in order) |
| `domain_kb:<path>` | Opens the relevant KB file (Domain KB screen at `/domain-kb`) |
| `plan_section:<heading>` | Opens raw baseline plan at that section anchor |
| `prior_current:<id>` | Shows previous current plan's relevant horizon for diff context |

This is the "team behind the advisor made visible" surface — the user can always see who said what when.

### 7.6 New API endpoints

| Endpoint | Verb | Purpose |
|---|---|---|
| `/api/advisor/check-in` | POST | User-initiated synthesis. Body: `{ guidance: str?, urgency: "now" \| "scheduled" }`. Returns 202 with `decision_run_id`; UI subscribes via WebSocket. |
| `/api/plan/draft` | GET | Returns the pending draft (or 404) |
| `/api/plan/draft/<id>/accept` | POST | Promote draft → current; cache invalidated |
| `/api/plan/draft/<id>/reject` | POST | Body: `{ reason: str, guidance: str? }`. Marks draft superseded. |
| `/api/plan/draft/<id>/items/<item_id>/accept` | POST | Per-delta accept |
| `/api/plan/draft/<id>/items/<item_id>/edit` | PATCH | Per-delta user edit; sets `user_edited=true` |

`PlanCurrentDTO` (existing) extended to surface `baseline`, `current`, and `draft` shapes side-by-side; legacy `latest_critique_json` field preserved for back-compat.

### 7.7 New WebSocket events (extends SDD §11.3)

```
plan.draft.started        plan.draft.completed
plan.draft.delta.accepted plan.draft.delta.edited
plan.draft.accepted       plan.draft.rejected
plan.current.changed
```

The advisor page subscribes to all of these so the side sheet stays live.

### 7.8 Plan-in-scope card on advisor page (no draft pending)

When no draft is pending, the card shows:

- Plan label (e.g. "Argosy current — accepted YYYY-MM-DD")
- Active short actions count + status
- Top-3 medium themes
- "View full plan" / "Trigger check-in now" buttons

When a draft IS pending, the card foregrounds the banner from §7.1.

---

## 8. Phasing & deliverables

The work splits into three deliverable waves slotted into existing SDD phases. Each wave is independently shippable.

### 8.1 Wave 1 — Baseline distillate (inside SDD Phase 1)

The smallest valuable slice. Gives "plan as baseline input, not north star" immediately, before the full agent fleet is assembled.

| Item | Notes |
|---|---|
| Migration `0015_plan_versions_lifecycle` | role + lifecycle columns + partial unique indexes |
| Migration `0016_plan_versions_distillate` | distillate columns + source path/hash |
| `PlanDistillerAgent` | Sonnet; pydantic-bound; require_citations=True; explicit exclusion list |
| Distillation hook | Post-write callback in `intake_upload`; existing flow unchanged for callers |
| `<PlanInScopeCard>` | New component on advisor page; renders distillate; "Re-distill" button |
| Distillate edit UI | Per-item edit; `user_edited=true` flag |
| `plan_watcher` cadence | Daily; hashes `source_path`; on diff, re-imports + re-distills with edit-preservation |

Estimated effort: ~3-5 days.

### 8.2 Wave 2 — Synthesis flow + monthly check-in UI (inside SDD Phase 3)

Depends on the agent fleet (Phase 3 decision team) being assembled.

| Item | Notes |
|---|---|
| Migration `0017_plan_versions_synthesis` | horizon JSON + MD columns + synthesis inputs |
| `plan_synthesis_flow` | New 5-phase orchestration in `argosy/orchestrator/flows/plan_synthesis.py` |
| `_plan_authority.py` | Shared authority disclaimer module |
| `_plan_projection.py` | Compact-projection generator (deterministic) |
| Cadence integration | Hook from `monthly_cycle` (1st); also callable from `/api/advisor/check-in` |
| API endpoints | `/api/advisor/check-in`, `/api/plan/draft`, accept/reject/items |
| WebSocket events | `plan.draft.*`, `plan.current.changed` |
| `<PlanRevisionSheet>` UI | Side-sheet with per-delta accept; conversational walkthrough; live updates |
| `draft_plan` bullet kind | Added to `<AdvisorBriefCard>` (§6.9 update) |
| Citation click-throughs | Resolvers for all 5 citation types in §7.5 |

Estimated effort: ~2-3 weeks.

### 8.3 Wave 3 — Speculative candidates as live items (inside SDD Phase 5)

The framework lands in Wave 2 (`SpeculativeCandidate` field exists in `HorizonSection`). Wave 3 makes them actionable.

| Item | Notes |
|---|---|
| Speculative candidate routing | New routing-matrix row: T0 candidates passing risk-ceiling auto-route to Argonaut paper queue; live requires single-click |
| Speculation cap config | New `agent_settings.yaml` keys: `speculation.max_pct_of_net_worth`, `speculation.max_concurrent_positions` |
| Watchlist agent integration | Watchlist now feeds candidates into synthesis Phase 1 with thesis + drawdown estimate |
| Argonaut tab UI | Speculative candidates separated from rebalancing actions; one-click "skip" or "take a swing" |

Estimated effort: ~1 week.

### 8.4 SDD edits to land alongside Wave 1

1. **New §6.10 "Plan as baseline input"** — covers distillate concept, lifecycle, "starting line not north star," authority disclaimer; links forward to synthesis section (added in Wave 2).
2. **§3.6 update** — add `PlanDistillerAgent` and (Wave 2) `PlanSynthesizerAgent` to cross-cutting agents.
3. **§5.1 update** — add `plan_watcher` row; note that `monthly_cycle` triggers `plan_synthesis_flow` (Wave 2).
4. **§8.1 + §8.5 update** — describe new `plan_versions` columns; list migrations 0015-0017.
5. **§10 update** — add `plan_revision` to decision-kind dimension of routing matrix; always T3, always human queue (Wave 2).
6. **§11.1 update** — Plan + Advisor screens show new plan-in-scope card and side-sheet review.
7. **§A.2 update** — `cost.monthly_budget_usd` bumps to accommodate synthesis (~$15-20/month addition baseline).

---

### 8.5 Implementation-plan scope (next step after this spec)

The `writing-plans` skill that follows this spec covers **all three waves** end-to-end. The plan groups work by wave with explicit gates between them:

- **Wave 1 gate** — distillate produced, viewable, editable; plan-watcher running; passes its eval corpus
- **Wave 2 gate** — synthesis flow runs end-to-end; one full monthly check-in cycle accepted in paper-only state per SDD §3.5 soak rule
- **Wave 3 gate** — speculative candidates surfaced and routed against Argonaut paper queue; cap enforcement verified

Each wave can be paused, deferred, or replanned without invalidating the prior waves' work. Wave 2 depends on the Phase 3 decision team being assembled; Wave 3 depends on Phase 5 Argonaut autonomy. The plan flags these dependencies explicitly so the user can sequence them against the rest of the SDD roadmap.

---

## 9. Out of scope

Intentionally deferred:

- **Plan amendment chat flow** — advisor proposes a structural plan change directly via chat; user approves → new draft. Fits as Wave 2.5 follow-up; not blocking.
- **Multiple baselines / scenario plans** — would require relaxing `(user_id) WHERE role='baseline'` partial unique index. Future "what-if engine" project.
- **Household / spouse co-approval** — deferred per SDD §15.3 single-user accepted risk.
- **Cross-tenant baseline templating** — Phase 6+ productization concern.
- **Plan-critique → distillate auto-merge** — `plan_critiques` table runs against raw plan (one-shot critique on import); synthesis later supersedes its findings as one of 9 analyst inputs. We don't merge plan-critique findings into the distillate.
- **Distillate sharing across users** — privacy/segregation work belongs to productization phase.

---

## 10. Risks & open questions

### 10.1 Risks

| Risk | Mitigation |
|---|---|
| **Distiller drops something durable as if it were time-stamped** (e.g. the 3.5% SWR philosophy mistaken for a current target) | Explicit exclusion list in system prompt; agent eval test corpus with golden output for Jacobs plan; user editability with `user_edited=true` |
| **Synthesis converges on bad consensus** (all agents read same data; produce same wrong plan) | Existing SDD §15.1 mitigations apply: contrarian risk officer; cooling-off; weekly audit agent looks for systematic patterns |
| **Draft replaced silently while user mid-review** | Reject in-flight draft replacement if `accepted_at` is non-null on any item; surface "the underlying state changed; resync?" prompt |
| **plan_watcher clobbers user edits** | Edits flagged `user_edited=true`; watcher surfaces a 3-way merge prompt instead of overwriting |
| **Speculative candidate slips past cap** | `risk_ceiling_check` boolean is computed in synthesis from current state; rule-based preflight in §9.3 also enforces cap before Argonaut auto-execution |
| **Compact projection grows past token budget** | Hard cap of 1500 tokens on `_plan_projection.py` output; truncate themes/actions if oversize; warn in audit log |

### 10.2 Open questions (deferred to build)

| ID | Question | Owner phase | Impact if unresolved |
|---|---|---|---|
| **OPEN-DIST-1** | What happens to `current` if the user imports a *new* baseline (Jacobs v3.0)? Does `current` invalidate? Or just flag a "your plan source changed; want to re-synthesize?" prompt? | Wave 2 | UX choice; doesn't block Wave 1 |
| **OPEN-DIST-2** | Per-delta "user_edited=true" — does an edited delta in `current` propagate to next month's synthesis as a stronger signal, or is it a one-time override? | Wave 2 | Prompt-engineering choice |
| **OPEN-DIST-3** | When `monthly_cycle` and `/api/advisor/check-in` race (user clicks check-in on the 1st of month at 8:59am, scheduled cycle fires at 9:00am), how do we collapse? | Wave 2 | Single-flight lock keyed on user_id solves this trivially; spec out before build |
| **OPEN-DIST-4** | Does the synthesis flow need its own `agent_evals/` corpus per plan-revision prompt, or is the existing trade-decision corpus enough? | Wave 2 | Test investment scope |
| **OPEN-DIST-5** | Do we expose "synthesize at T2 depth instead of T3" as a debug knob for cost-experiments, or always run full T3? | Wave 2 | Per accuracy-over-cost: always T3 unless explicitly toggled |

---

## 11. Glossary

| Term | Definition |
|---|---|
| **Baseline** | The user's imported source plan (Jacobs Wealth Plan v2.0 today). One active per user. |
| **Distillate** | Compressed, structured extract of the baseline — durable principles, decision rules, targets-as-stated. ~1500-2500 tokens. The only representation of the baseline that synthesis ever sees. |
| **Current** | The synthesized plan the advisor anchors on. Exactly one per user. |
| **Draft** | A synthesis output awaiting user accept. Promoted to `current` on accept. |
| **Horizon** | One of `long` (5+ yrs), `medium` (1-2 yrs), `short` (~30 days). Each is a `HorizonSection` with its own targets, themes, actions, and freshness expectation. |
| **Synthesis** | The 5-phase fleet review (analysts → debate → synthesizer → risk → fund manager) that produces a draft plan. |
| **Speculative candidate** | A bounded-risk opportunity surfaced in `short.speculative_candidates`; must satisfy the speculation cap. Surfaced as "worth a small swing if you want it," never recommended. |
| **Authority disclaimer** | The shared prompt fragment reminding agents the plan is one input, not authority; loyalty is to the user. |
| **Compact projection** | Deterministic markdown summary of `current` injected into every advisor turn (~500-800 tokens). |
| **Wave** | A shippable phase of this work; Wave 1 = distillate; Wave 2 = synthesis + UI; Wave 3 = speculative candidates live. |

---

## 12. References

- `D:/Projects/financial-advisor/docs/design/SDD.md` — primary design doc (especially §3, §5, §6, §8, §10, §11, §A.2)
- `D:/Google Drive/Family/Finances/Portfolio/Jacobs_Wealth_Plan.md` — current baseline plan (Feb 2026 v2.0)
- `argosy/agents/plan_critique.py` — existing plan-critique agent (preserved; runs as one of 9 in synthesis Phase 1)
- `argosy/api/routes/intake.py` / `argosy/api/routes/advisor.py` — current upload + advisor turn flows
- `ui/src/app/advisor/page.tsx` — current advisor page (target of UI changes in Wave 1 & 2)
- Anthropic SDK prompt-caching docs — load via `claude-api` skill when implementing §6 cache_control blocks
