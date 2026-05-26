# Plan UI Redesign — Implementation Plan

Spec: `docs/superpowers/specs/2026-05-26-plan-ui-redesign-design.md`.

Three tiers (A, B, C). Tier A is the floor — must ship. Tier B is the target — should ship. Tier C is decoration — graceful degradation acceptable.

## Tier A — must ship

### A.1 Backend: `GET /api/plan/draft/objections`

`argosy/api/routes/plan.py` — new route that:
- Calls `get_pending_draft(db, user_id)`; 404 if no draft.
- Queries `agent_reports` for the FM row of `decision_run_id = pv.decision_run_id`.
- Parses the response_text as the `FundManagerPlanRevisionDecision` JSON envelope (with tolerance for prose-wrapped JSON; reuse the same `JSONDecoder(strict=False).raw_decode` pattern as `plan_synthesizer.py`).
- Returns `{ approved, objections[], raw_response_excerpt }`.
- Severity mapping: derive from keyword scan on the FM's reasons (`tax`/`section 102` → RED, `escalate`/`unresolved` → AMBER, `confidence`/`weak` → YELLOW). Best-effort.

Test: `tests/test_plan_draft_objections.py` — populate a draft + FM agent_report, call endpoint, verify objections parsed.

### A.2 Backend: derive per-delta provenance fields

Tweak `argosy/api/routes/plan.py::DraftResponse` to enrich each delta with `provenance_agent_labels: list[str]` derived from its `cited_sources`. Citation-prefix → agent mapping table (see spec). Done client-side or server-side; server-side keeps the UI lighter.

Add the mapping function `_citation_prefix_to_agent_label(prefix)` in plan.py. Apply when shaping `_horizon_view`.

### A.3 UI: New `/plan` page layout

Rewrite `ui/src/app/plan/page.tsx`:
- Top: `<PlanHeader />` with version_label + buttons (existing).
- New `<ExecutiveSummaryCard />` component:
  - Status badge (FM rejected / approved).
  - Lineage line.
  - Three small tiles: verdict, deltas count, per-horizon status grid.
  - Posture excerpt (first 200 chars of `horizon_long.posture`).
  - `<FMObjectionsCard />` nested (only when `approved=false`).
  - Action buttons: Accept all / Reject + re-synthesize (reuse existing endpoints).
- New `<DeltaCard />` component per delta:
  - Badge: ADDED/MODIFIED/REMOVED · TARGET/ACTION.
  - Before/after summary lines.
  - Collapsible rationale.
  - Source chips row (per A.2).
  - Per-delta accept button (reuse existing endpoint).
- New `<AgentCascadeStrip />` component:
  - Fed by `/api/agent-activity?decision_id=plan-synth-19&detail=false`.
  - Renders agent role nodes in pipeline order (analysts → debaters → synthesizer → risk → FM).
  - Click node → drawer with full response_text.

### A.4 UI: Allocation pre/post chart

- New `<AllocationChart />` component.
- Fetches `/api/portfolio/snapshot` (existing) and pulls weight targets from `draft.horizon_long.targets`.
- Recharts horizontal stacked bar: one bar for "current", one for "proposed (where explicit)".
- Falls back to current-only when no weight targets.

### A.5 Source-chip drawer

New `<AgentReasoningDrawer />` component:
- Opens on chip click or cascade-node click.
- Fetches `/api/agent-activity?decision_id=...&agent_role=...&detail=true`.
- Renders full response_text with the existing `<Markdown>` component.
- Closes via Sheet primitive.

### A.6 Tests + lint + tsc

`pytest tests/test_plan_draft_objections.py tests/test_api_routes.py tests/test_plan_draft_api.py -m "not llm_eval" -q`.
`cd ui && npm run lint && npx tsc --noEmit`.

## Tier B — should ship

### B.1 Backend: `GET /api/plan/draft/visualizations`

Single envelope returning:
- `nvda_trajectory` — derived from portfolio snapshot + user_context.identity_yaml.
- `delta_map` — flat list of all deltas, normalized.
- `sources_heatmap` — per-item citation count by category.

Skips `allocation` (already done in A.4) and `projection` (Tier C).

### B.2 UI: NVDA trajectory chart

`<NvdaTrajectoryChart />` with Recharts LineChart + ReferenceLine for ceiling, ReferenceDot for vest events.

### B.3 UI: Delta map

`<DeltaMap />` as a 3×3 grid (horizon × kind), with cell counts as primary visual + tooltip showing item list.

### B.4 UI: Cited-sources heatmap

`<SourcesHeatmap />` as a colored matrix (items × categories) using a simple `bg-opacity-{count}` scale.

## Tier C — defer-friendly

### C.1 Portfolio projection chart

- Backend: yfinance pull for each ticker in portfolio, compute mu/sigma (annualized).
- Endpoint: extend `/api/plan/draft/visualizations` with `projection` key.
- If yfinance unreachable: return `{ "projection": null, "reason": "yfinance unavailable" }`.
- Frontend: `<ProjectionChart />` — Recharts ComposedChart with three Area series (bull/base/bear) + Line for safe-withdrawal.
- **Risk note:** parametric model only — explicit label "Simplified projection — not Monte Carlo".

**Tandem-review trigger:** the mu/sigma weighted-combine math + the band formula are money math. Fire the codex-tandem reviewer on the projection backend before merging (per memory `feedback_use_tandem_for_risky_work`).

## Phase E (multi-ticker advisor) — separate spec to follow

After Phase 1 lands, write a sibling spec for the multi-ticker advisor flow + execute. Out of scope for this plan.

## Sequencing

1. A.1 (objections endpoint) + A.2 (per-delta provenance) in one backend commit.
2. A.3 (page layout) + A.5 (drawer) — large UI commit.
3. A.4 (allocation chart) — UI commit.
4. Verify Tier A end-to-end on `/plan`. Commit.
5. B.1 + B.2 + B.3 + B.4 — Tier B backend + UI commit.
6. C.1 — Tier C if time remains; else stub.
7. Run all backend + UI checks. Update SDD handover.
8. Move to Phase E spec/impl.

## Rollback

Each tier is self-contained behind feature additions, no migrations. Roll back by `git revert` the relevant commits; no data backfill needed.
