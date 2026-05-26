# Plan UI Redesign + Agent Provenance — Design

**Status:** Approved by Ariel (sequencing: A+B first, then C+D, then E).
**Date:** 2026-05-26
**Author:** Claude (autonomous brainstorm session).
**Implementation:** `docs/superpowers/plans/2026-05-26-plan-ui-redesign-implementation.md`.

## Problem

`/plan` currently renders synthesis output as a single block of markdown (`horizon_long_md`) with bold-text headers, no visual structure, no charts, and no surface for the agent reasoning that produced it. Ariel's review verdict was "blob of text". The structured JSON behind the markdown (`targets`, `themes`, `actions`, `deltas_from_prior`, `cited_sources`, `posture`, `rationale`, `speculative_candidates`) is rich enough to render natively without an LLM second pass — we just haven't.

## Page purpose

Ariel's intent on opening `/plan`: **decide on the pending draft**. Audit is secondary but reachable. The page is a decision surface, not a living-document view.

## Section 1 — Layout

```
PLAN  ·  Latest: synth-2026-05-25-2215-fm-rejected
[Run synthesis] [Re-critique now]

╭── EXECUTIVE SUMMARY ──────────────────────────────────────────────╮
│  ⚠ Pending draft #6 · FM REJECTED (or APPROVED)                  │
│  Drafted 22:15 · derived from baseline #5 · run #19              │
│                                                                   │
│  [ Verdict tile ] [ Deltas tile ] [ Per-horizon status tile ]    │
│                                                                   │
│  Posture (long, excerpt): Own the mega-cap AI/cloud compounders… │
│                                                                   │
│  ┌─ FM OBJECTIONS (when rejected) ──────────────────────────┐    │
│  │ 🔴 Section 102 tax sequencing not handled                │    │
│  │ 🟠 Escalate-not-resolved (concentration program)         │    │
│  │ 🟠 ConcentrationAnalyst position data weak               │    │
│  │ 🟡 FX confidence low                                     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  [ Accept all ]  [ Reject + re-synthesize ]                      │
╰───────────────────────────────────────────────────────────────────╯

╭── VISUALIZATIONS ─────────────────────────────────────────────────╮
│  [ Allocation pre/post ]  [ NVDA share-count trajectory ]        │
│  [ Portfolio projection: bull/base/bear + safe-withdrawal ]      │
│  [ Delta map ]            [ Cited-sources heatmap ]              │
╰───────────────────────────────────────────────────────────────────╯

╭── PROPOSED CHANGES (per-horizon Tabs: Long | Medium | Short) ─────╮
│  Per-delta cards. Each card:                                      │
│    badge: ADDED/MODIFIED/REMOVED · TARGET/ACTION                  │
│    before/after summary                                           │
│    rationale (collapsible)                                        │
│    Source chips: clickable, map citation prefix → agent role,     │
│       drawer opens with that agent's full response_text excerpt   │
│    [Accept] [Edit]                                                │
╰───────────────────────────────────────────────────────────────────╯

╭── AGENT CASCADE (run #19) ────────────────────────────────────────╮
│   Compact horizontal node strip:                                  │
│   9 analysts → 3 debaters → synthesizer → 3 risk → FM            │
│   Color: green=ok, amber=warn, red=rejected. Click node → drawer. │
│  [ View full replay → /decisions/19 ]                             │
╰───────────────────────────────────────────────────────────────────╯

╭── CRITIQUE FINDINGS (existing card, untouched) ───────────────────╮
╰───────────────────────────────────────────────────────────────────╯
```

Colors: red/amber/green for severity (FM objections, deltas, cascade nodes). Accent color for action buttons. Muted backgrounds for the cards. Whitespace ≥24px between sections.

## Section 2 — Visualizations

Five charts in a responsive grid (2 cols on desktop, 1 col on tablet). All sourced from new `GET /api/plan/draft/visualizations?user_id=ariel` returning a single JSON payload (one network call, multiple chart inputs).

### 2.1 Allocation pre/post

- **Current side:** from `GET /api/portfolio/snapshot` (already wired). Group `positions` by `details`-or-`asset_type` into categories (`equity`, `cash`, `bond`, `etf`, etc.) and aggregate `usd_value_k`. Render as horizontal stacked bar OR donut.
- **Proposed side:** read the draft's targets where `unit` matches `pct_of_portfolio` or `pct_of_net_worth`. For each such target, render an overlay arrow showing the gap from current → target.
- **Fallback:** if no proposed-weight targets exist, show only the current side with a "no explicit weight targets in this draft" caption.

### 2.2 NVDA share-count trajectory

- **Today:** read current NVDA shares from portfolio snapshot.
- **Vest events:** from `user_context.identity_yaml::rsu_grants` (if structured) or from free-text fallback. Each vest date adds N shares.
- **Reduction program:** from `user_context.identity_yaml::nvda_sale_progress` (e.g., "1,440 shares remain to reduce"). Subtract over time.
- **Long-horizon ceiling target:** from the draft's long-horizon target where `label` contains "NVDA share count" — read `value` as the ceiling.
- **Chart type:** line chart with vest events as vertical reference lines, ceiling as horizontal reference line.

### 2.3 Portfolio value projection — bull/base/bear + safe-withdrawal

- **Model:** parametric. Compute per-ticker historical 1y return (mu) + volatility (sigma) from yfinance daily closes. Portfolio mu/sigma = weighted by current usd_value_k.
- **Bands:** at year `t`, value = `today_value * (1 + mu*t ± k*sigma*sqrt(t))` for k = {-1, 0, +1}. Three bands.
- **Acceptable monthly redraw line:** annual income (dividends + interest from holdings) ÷ 12, projected at base growth.
- **Time horizon:** 10 years out at monthly resolution.
- **Label:** "Simplified parametric projection — not Monte Carlo. Bull/base/bear = ±1σ on annualized return."
- **Risk:** This is the hardest piece. Yfinance calls are slow and rate-limited; if unreachable, fall back to a stub chart with a "yfinance unavailable; data coming soon" message.

### 2.4 Delta map

- Grid: rows = each delta from `deltas_from_prior` across all 3 horizons. Columns = `added` | `modified` | `removed` × `long` | `medium` | `short`.
- Cell content: short summary + accept-state checkbox.
- Useful as a checklist — see at a glance how many changes are pending acceptance.

### 2.5 Cited-sources heatmap

- Matrix: rows = each target/action across all horizons (with horizon prefix). Columns = source categories derived from citation prefixes:
  - `user_context.*` → "user context"
  - `fundamentals/<TICKER>` → "fundamentals"
  - `technical/<TICKER>` → "technical"
  - `fx/*` → "fx"
  - `news/*` → "news"
  - `macro/*` → "macro"
  - `concentration/*` → "concentration"
  - `tax/*` → "tax"
  - `sentiment/*` → "sentiment"
- Cell color: dark = 1 citation, lighter shades = more citations of that category for that item.
- Lets the user spot items with thin grounding (single-category citations) versus well-supported items.

## Section 3 — Agent provenance

Three nested layers, lightest to heaviest.

### 3.1 Per-item source chips (lightest)

On each delta card, a `Sources ▸` row renders pill chips for each cited source, with the citation prefix mapped to the agent that emitted it:

| Citation prefix | Agent role label |
|---|---|
| `fundamentals/*` | FundamentalsAnalyst |
| `technical/*` | TechnicalAnalyst |
| `news/*` | NewsAnalyst |
| `macro/*` | MacroAnalyst |
| `fx/*` | FXAnalyst |
| `tax/*` | TaxAnalyst |
| `concentration/*` | ConcentrationAnalyst |
| `sentiment/*` | SentimentAnalyst |
| `user_context.*` | user_context |
| `docs/design/*`, `SDD*`, `domain_knowledge/*` | domain_kb |

Clicking a chip opens a side drawer with that agent's full reasoning excerpt (response_text from the corresponding `agent_reports` row of the decision run).

### 3.2 Compact cascade strip

Horizontal node strip at the bottom of `/plan`, before the deep-link footer. Built from `/api/agent-activity?decision_id=<decision_run_id>&detail=false` (existing endpoint). Each agent role rendered as a circular node with status dot:

- Green: agent ran successfully
- Amber: agent ran but warned (low confidence)
- Red: agent failed or was the FM and rejected

Click a node → same drawer as the source chips, but scoped to that agent's full output.

### 3.3 Full replay deep link

Footer link: `View full replay → /decisions/19`. The existing `/decisions/[id]/page.tsx` has mermaid + transcript views; we don't change that.

## Backend changes

1. **New** `GET /api/plan/draft/visualizations?user_id=ariel` — returns one JSON envelope with sub-objects for each chart:
   ```json
   {
     "allocation": { "current": [{"category": "equity", "usd_value_k": 1200, "pct": 65}], "proposed_targets": [{"label": "...", "target_pct": 35}] },
     "nvda_trajectory": { "today_shares": 5800, "vest_events": [{"date": "2026-06", "shares_added": 729}], "reductions": [...], "ceiling": 8000 },
     "projection": { "horizon_years": 10, "today_value_usd": 1850000, "monthly_points": [{"month": 0, "bull": ..., "base": ..., "bear": ...}], "safe_withdrawal_monthly": 9200 },
     "delta_map": [{"horizon": "long", "kind": "modified", "summary": "...", "item_id": "..."}],
     "sources_heatmap": [{"item_id": "...", "horizon": "long", "summary": "...", "categories": {"user_context": 2, "fundamentals": 1}}]
   }
   ```
2. **New** `GET /api/plan/draft/objections?user_id=ariel` — parses the FM agent_report for run #19 and returns structured objections:
   ```json
   {
     "approved": false,
     "objections": [{"severity": "RED|AMBER|YELLOW", "topic": "Section 102 tax sequencing", "detail": "...", "cited_sources": [...]}],
     "raw_response_excerpt": "..."
   }
   ```
3. **No schema changes.** All chart data is computed on the fly from existing tables + the parsed TSV.

## Out of scope (deferred)

- Fixing `_assemble_portfolio_summary` plumbing (separate work; would require re-running synthesis to get clean ConcentrationAnalyst input).
- Server-side FX conversion.
- Re-running synthesis with the fixed adapters (pre-existing open issue).
- Persistence of computed visualizations (recomputed per request; cache later if needed).
- Editing deltas inline (existing `<PlanRevisionSheet>` flow handles this; not changing).

## Implementation priority

Tier A (must ship): Exec summary card, FM objections card, per-delta cards with source chips, cascade strip, allocation pre/post chart.

Tier B (should ship): NVDA trajectory chart, delta map.

Tier C (nice-to-have, defer to stub if time runs out): Portfolio projection chart, cited-sources heatmap.

If time runs out, Tier A delivers the readable decision surface the user asked for; Tier B+C decorate it.
