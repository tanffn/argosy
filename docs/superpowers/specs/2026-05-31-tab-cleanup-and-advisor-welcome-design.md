# Tab Cleanup + Advisor Welcome — Design

Date: 2026-05-31
Owner: Ariel (autonomous overnight sprint)
Scope: three independent UI fixes raised in one conversation. Backend
work limited to the minimum that the UI changes demand.

## Goals (verbatim from user)

1. **Life Events tab** — remove. The data feed should land via the
   Advisor tab.
2. **Advisor tab** — should be ready with a message as soon as you open
   it. Include a welcome + relevant information (due process, event,
   opportunity).
3. **Plan tab** — broken: returns a plain text document. Fix.

## Decisions (from the brainstorm)

- **Life Events removal scope** — UI only. Backend stays. The
  `/api/life-events` route, service, migrations 0042+0054, cashflow
  projection's dependency on the events table, retirement timeline
  phase markers, and the state observer's pre-event logic all remain.
  The 11 existing events keep feeding the model. New events can no
  longer be entered via a form; capture-via-Advisor is a follow-up
  spec (the route is still callable from anywhere).
- **Advisor welcome source** — hybrid. Static structured card from
  server-aggregated state renders instantly on mount; one background
  LLM call hydrates a "today's insight" paragraph below.
- **Proactive ask** — folded into the LLM hydration. The hydration's
  job is to pick the *single* most useful thing to say (gap question,
  opportunity, observation) and write 1-3 sentences. The auto-fired
  gap-driven turn (`askNext("")`) on mount is gone.

## Why each fix matters

- The Life Events tab existed because the user once wanted a form-driven
  surface for cashflow shapes. They have decided the conversational
  Advisor surface is the right entry point. Keeping the tab adds
  navigation noise and a second source of truth.
- The Advisor cold-start currently shells out to a full LLM turn on
  mount — the screenshot shows the agent producing a dense intake-
  status dump intended for the orchestrator, not a welcome. The user
  sees 5+ seconds of "Thinking…" followed by orchestrator jargon.
- The Plan tab currently has no `role='current'` and no `role='draft'`
  PlanVersion row. It falls through to the "no draft + has
  raw_markdown" branch which renders the baseline document as raw
  Markdown text. The structured surface (executive summary, allocation
  chart, NVDA trajectory, delta map, cashflow projection, proposed
  changes) is gated on a draft. Effective result: the page looks
  broken when in steady state between synthesis runs.

## Sections

### 1. Life Events UI removal (small, mechanical)

- `ui/src/components/nav.tsx:47` — remove the Life Events entry from
  `PRIMARY_TABS`.
- `ui/src/app/life-events/page.tsx` — delete the file.
- `ui/src/components/retirement/UpcomingVestCard.tsx:347` — the
  `DISABLED_PREFILL_HREF` and the active `buildLifeEventHref` deep
  link into `/life-events?section=...&prefill_*=...`. Redirect to
  `/advisor?seed=<prefilled prose>` so the vest-planning conversation
  happens in chat. Drop the disabled-link variant.
- `ui/src/components/retirement/HolisticTimelineCard.tsx:188` — the
  "Add life events on [link]" empty-state message points at
  `/life-events`. Repoint at `/advisor`.
- `ui/public/user-guide/index.html` — remove the `#tab-life-events`
  and `#inferred-life-events` table-of-contents entries, plus any
  section text that documents the form. Per binding feedback
  (`user_guide_is_manual`) the guide is a manual, not history — no
  "removed in 2026-05-31" callouts.
- Backend: untouched.

### 2. Advisor cold-start — static welcome + hybrid LLM hydration

#### 2a. Static welcome card (`<AdvisorWelcomeCard>`)

- New component at `ui/src/components/advisor/welcome-card.tsx`.
- On mount, fan out four-to-six existing routes in parallel
  (`Promise.all`): `/api/advisor/gaps`, `/api/proposals`,
  `/api/anomaly` (if it exists for the user surface), `/api/plan/draft`,
  the upcoming-vest endpoint, the in-flight-synthesis endpoint.
  Pure REST, no LLM. Renders in <500 ms — same latency budget as the
  rest of the page's data loads.
- Conditional sections in priority order:
  1. **Greeting** — "Welcome back" (always)
  2. **In progress** — amendments running (from
     `/api/plan/draft`'s amendment hint), plan synthesis in flight
     (`/api/plan/in-flight-synthesis`), advisor amendment
     pill, jobs queued (if we surface that). Skip the section when
     nothing is in flight.
  3. **Coming up** — next RSU vest within 90 days; next life event
     within 90 days. Skip when nothing is upcoming.
  4. **Needs your attention** — pending action proposals (not
     yet accepted/deferred/rejected), open anomalies, plan draft
     awaiting review. Skip when none.
  5. **Currently open** — 1-line summary of the gap-tracker counts.
- Each section item links to its canonical page (proposal → /proposals;
  anomaly → /expenses?flag=...; vest → /retirement#upcoming).
- Empty state (nothing in any section beyond greeting): "Welcome back.
  Nothing urgent — ask me anything below, or click a gap on the right
  to fill in context."

#### 2b. Background LLM hydration

- New backend route: `POST /api/advisor/insight` (or
  `GET /api/advisor/insight?user_id=...`).
- Single LLM call (Opus, per accuracy-over-cost). Reads the same state
  the welcome card already collected + recent conversation history +
  open gaps. Prompt brief: *"Pick the single most useful thing to
  surface to the user right now. Could be a question to fill the
  highest-impact gap, an observation about an opportunity, a heads-up
  about an event, or a clarifying suggestion. Write 1-3 sentences.
  Plain English — no agent jargon. If nothing meaningful to say,
  return an empty string."*
- The route emits the same `agent.run.finished` WS event the existing
  advisor turns emit, so the cascade panel infrastructure still works
  if/when we want to surface it for this call.
- UI: card has a slot below the static sections that says "Loading
  today's insight…" then renders the paragraph when ready (or hides
  itself if empty). The LLM call does NOT block the static card.
- Codex tandem on the prompt. LLM prompt = risky work per the
  Argosy convention.

#### 2c. Remove the auto-fired gap turn

- `ui/src/app/advisor/page.tsx:419` — remove
  `refreshGaps().then(() => askNext(""))`. Replace with just
  `refreshGaps()`. The welcome card + LLM hydration replace this
  behavior; the gap tracker sidebar remains clickable for focused
  conversations on a specific field.

#### 2d. Hide the per-turn USD cost

- The `AgentCascadePanel` renders a `· $0.0233` token-cost line per
  agent and per cascade header. Per binding feedback (no USD
  reporting), strip the USD figure from the user-facing surface.
  Keep it in the underlying telemetry / DB — just don't render it in
  the cascade panel UI. Scope: hide the `$` line in the panel header
  + each agent row.
- Search `ui/src/components/advisor/AgentCascadePanel.tsx` for the
  cost-formatting block and gate it behind a feature flag (or just
  delete — it's not load-bearing).

### 3. Plan tab — distillate fallback when no draft

- The page already handles four states: loading, in-flight synthesis,
  pending draft, and "no draft but has raw_markdown". The last branch
  is the regression — it dumps `<Markdown>{plan.raw_markdown}</Markdown>`
  with no structure.
- Replace the empty-state branch with a `<PlanBaselineView>` that
  reads `/api/plan/baseline` (which returns the distillate) and
  renders the distillate sections (`identity_anchors`, `targets`,
  `themes`, `actions`, `constraints`, …) as cards. The distillate is
  already structured — the only thing missing is a renderer.
- When no distillate exists either, fall back to the raw-markdown
  card with an explicit explanation: "Run `argosy ingest plan` or
  press Run synthesis to get a structured view."
- Same page; no new route in the URL hierarchy. Same data loads.

## Order of work (commits)

Each block is a logical-ship per the autonomous-overnight convention.

1. **commit/spec** — this doc itself.
2. **feat(life-events): remove UI surface** — sections 1 + retirement
   timeline link repoints + user-guide cleanup.
3. **fix(plan): distillate fallback when no draft** — section 3.
4. **feat(advisor): static welcome card** — section 2a + 2c (drop
   auto-fire).
5. **feat(advisor): LLM insight hydration** — section 2b. Codex tandem
   on the prompt.
6. **chore(ui): hide USD cost in cascade panel** — section 2d.
7. **docs(sdd): consolidate 2026-05-31 wave-2 handover** — final
   handover entry per the per-commit SDD convention.

## Non-goals (explicit)

- Advisor learning to capture life events from chat. The route is
  still callable; the agent-tool addition is a separate spec.
- Re-thinking the retirement timeline's phase-marker UX even though
  Life Events was the data entry point.
- Touching cashflow projection, plan synthesis, or any other backend
  consumer of life events.

## Testing surface

- Backend tests: a single test for the new `/api/advisor/insight`
  route (happy path + empty-response path). Existing backend tests
  unchanged; nothing else gains tests because nothing else changes
  behavior on the backend.
- Frontend: type-check + lint per the standard cadence. UI feature
  correctness is verified by running the dev server and clicking
  through (per `feedback_manual_ui_smokes_skipped` — there are no
  manual click-through scripts, but starting the dev server and
  loading each touched page IS the verification surface).

## Risks + open items

- The welcome card needs the upcoming-vest + upcoming-life-events
  routes to exist. They do (UpcomingVestCard already consumes one).
  Confirm both are user-scoped + cheap on cold start.
- The LLM hydration prompt is the only LLM-touching change. Codex
  tandem job: stress the prompt with empty-state, all-fresh-state,
  and lots-going-on-state. Don't ship the prompt without an audit
  pass.
