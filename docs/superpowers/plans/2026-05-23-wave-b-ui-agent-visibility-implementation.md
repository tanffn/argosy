# Wave B-UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Make every agent invocation visible in the UI — live cascade panel on `/advisor` replacing "Thinking...", and decision-grouped accordion on the home page with click-into-agent detail drawer.

**Architecture:** Backend emits `agent.run.started` + `agent.run.finished` WebSocket events from `BaseAgent.run()` via existing `publish_event_threadsafe`. UI subscribes via existing `useWSEvents`, merges with REST snapshot from `/api/agent-activity`, renders a shared `<AgentRunCard>` on both surfaces with a `<AgentDetailDrawer>` for drill-down.

**Tech Stack:** Python 3.12 / FastAPI (existing `publish_event_threadsafe`) / Next.js 16 / TypeScript / Tailwind / React 19.

**Spec:** `docs/superpowers/specs/2026-05-23-wave-b-ui-agent-visibility-design.md`

**Conventions:**
- Python interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`
- Backend tests: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
- UI lint+typecheck: `cd ui ; npm run lint ; npx tsc --noEmit`
- Branch: recommend `wave-b-ui` off `main` (currently at `72f34e3`).
- Commit per task. No `git add -A` or `git commit -a` — exact paths only.

---

## Task 1: Backend — `agent.run.started` + `agent.run.finished` events

**Files:**
- Modify: `argosy/agents/base.py` (`BaseAgent.run()` around the existing `_call_model` invocation + post-persist)
- Test: `tests/test_agent_run_events.py` (new)

**Steps:**
- [ ] **Write failing test** in `tests/test_agent_run_events.py` — patches `publish_event_threadsafe` with a `MagicMock`, runs a `_DummyAgent.run(...)` with mocked `_call_model`, asserts the mock was called twice (`agent.run.started` then `agent.run.finished`), and that the two events share the same `run_correlation_id`. Assert `finished` payload includes `tokens_in`, `tokens_out`, `cache_input_tokens`, `cache_creation_tokens`, `thinking_tokens`, `citations_count`, `cost_usd`, `confidence`, `agent_report_id`.
- [ ] **Run test** — confirm FAIL.
- [ ] **Edit `BaseAgent.run()`** to emit both events. Wrap each emission in `try/except Exception` (best-effort — must never block agent run). Use `uuid.uuid4()` for `run_correlation_id`, store on `self._current_run_id` so subagent dispatches (Tasks 2-onwards) can also reference it. Forward optional `turn_id` from `inputs` if present.
- [ ] **Run test** — confirm PASS.
- [ ] **Commit**: `feat(agents): emit agent.run.started + finished WebSocket events`.

---

## Task 2: Backend — `turn_id` echo in `/api/advisor/turn`

**Files:**
- Modify: `argosy/api/routes/advisor.py` (the `POST /api/advisor/turn` request body + dispatch)
- Test: `tests/test_advisor_route.py` (extend)

**Steps:**
- [ ] **Add `turn_id: str | None = None`** to the Pydantic request body model.
- [ ] **Thread `turn_id`** into `AdvisorAgent.run(turn_id=turn_id, …)` inputs.
- [ ] **Update test** in `tests/test_advisor_route.py` to verify the WS event payload echoes the `turn_id` when provided. (Patch `publish_event_threadsafe`, call the route with a `turn_id`, assert it appears in the captured event payload.)
- [ ] **Commit**: `feat(advisor): echo turn_id into agent.run.* events`.

---

## Task 3: UI — `<AgentRunCard>` component + status logic

**Files:**
- Create: `ui/src/components/agent/AgentRunCard.tsx`
- Create: `ui/src/components/agent/AgentRunCard.module.css` (or Tailwind inline if conventions prefer)

**Steps:**
- [ ] **Write the component** taking `{ row: AgentActivityRow, status, durationMs, onSelect }` per spec §3.4.
- [ ] **Layout** per the spec ASCII mock: status dot, role, model, duration, cost, then a sub-line with in/out/cache_hit%/thinking_tokens, then confidence + citations count.
- [ ] **Status dot color**: blue (pulse) running, green HIGH/MEDIUM done, amber LOW done, red failed.
- [ ] **Run UI lint** (`cd ui ; npm run lint -- src/components/agent/`) — clean.
- [ ] **Commit**: `ui(agent): AgentRunCard component`.

---

## Task 4: UI — `<AgentDetailDrawer>` component (tabbed)

**Files:**
- Create: `ui/src/components/agent/AgentDetailDrawer.tsx`

**Steps:**
- [ ] **Drawer scaffold** with shadcn `Sheet` (or whatever drawer primitive is in `ui/src/components/ui/`). Slides from right, 40% width.
- [ ] **Four tabs**: Output, Sources, Citations, Cost & telemetry. Use shadcn `Tabs`.
- [ ] **Output tab**: render `row.response_text` via `react-markdown`. Verify code blocks highlight (project already uses `rehype-prism` or similar — check `package.json`).
- [ ] **Sources tab**: parse `<source id="...">...</source>` blocks out of the prompt (NOT response — sources are inputs). Render each as a collapsible. NOTE: this requires the API response to include the user_prompt (or at least the sources extracted from it). If not currently exposed, add a `sources_summary: list[{id,body_preview}]` field to the AgentActivityRow response in Task 7.
- [ ] **Citations tab**: parse `row.citations_json` if non-null; render as a list of `{claim_text, cited_quote, source_id}`. Link source_id to the Sources tab.
- [ ] **Cost & telemetry tab**: table of tokens (in/out/cache_read/cache_write/thinking) + computed cost + cache hit ratio + prompt_hash (for cache-debugging).
- [ ] **Run UI lint + typecheck** — clean.
- [ ] **Commit**: `ui(agent): AgentDetailDrawer tabbed component`.

---

## Task 5: UI — `useDecisionStream` hook

**Files:**
- Create: `ui/src/lib/useDecisionStream.ts`

**Steps:**
- [ ] **Hook signature**: `useDecisionStream(userId: string, opts?: { turnId?: string })` returns `{ decisions: DecisionGroup[], byCorrelationId: Map<string, AgentRow>, isLoading: boolean }`.
- [ ] **Initial REST load**: fetch `/api/agent-activity?user_id=...&limit=100`, group client-side by `decision_id` (or `intake_session_id`, fallback "Standalone").
- [ ] **WS subscription** via existing `useWSEvents(['agent.run.started','agent.run.finished'])`. **Fix the cross-user filter bug** (SDD §15.4 known issue: home/proposals don't filter on `payload.user_id !== USER_ID`; only the advisor page does. Match the advisor pattern here.)
- [ ] **Dedupe + merge logic**: when an `agent.run.finished` event arrives, fetch the matching AgentReport via `/api/agent-activity?since=<event.finished_at - 5s>` to get the persisted row with `response_text`. Use `run_correlation_id` to upsert.
- [ ] **If `opts.turnId` is set**, filter the returned decisions to only those whose any row's `turn_id` matches.
- [ ] **Unit test** in `ui/src/lib/__tests__/useDecisionStream.test.ts` (or jsdom): simulate WS events arriving, assert correct grouping + ordering.
- [ ] **Commit**: `ui(lib): useDecisionStream hook with WS + REST merge`.

---

## Task 6: UI — `<AgentCascadePanel>` on /advisor

**Files:**
- Modify: `ui/src/app/advisor/page.tsx`
- Create: `ui/src/components/advisor/AgentCascadePanel.tsx`

**Steps:**
- [ ] **Generate client-side `turn_id`** (`crypto.randomUUID()`) before POSTing the advisor turn. Send in request body.
- [ ] **`<AgentCascadePanel>`** subscribes to `useDecisionStream(userId, { turnId })`. While the POST is in-flight, render a vertical stack of `<AgentRunCard>` (collapsed to one-line) for each row whose `turn_id` matches.
- [ ] **Auto-scroll** to bottom on new rows.
- [ ] **On POST resolve**: collapse to a one-line summary with a "view detail" affordance that re-expands.
- [ ] **Click any row** → opens `<AgentDetailDrawer>` for that row.
- [ ] **Replace the existing "Thinking..." spinner** with this panel.
- [ ] **Manual smoke**: open `/advisor`, send a message, watch the cascade fill in live.
- [ ] **Commit**: `ui(advisor): live cascade panel replaces 'Thinking...' spinner`.

---

## Task 7: UI — Home page restructure to `<DecisionAccordion>`

**Files:**
- Modify: `ui/src/app/page.tsx` (the agent-activity section)
- Create: `ui/src/components/agent/DecisionAccordion.tsx`

**Steps:**
- [ ] **`<DecisionAccordion>`** consumes `useDecisionStream(userId)` (no turnId filter). Renders one collapsed row per decision: timestamp, tier, ticker, agent count, total cost, total duration, status.
- [ ] **On expand**: vertical stack of `<AgentRunCard>` for that decision's runs, ordered by `created_at` ascending.
- [ ] **Click any row** → `<AgentDetailDrawer>` for that agent.
- [ ] **In-progress decisions**: pulsing border + auto-update from WS.
- [ ] **Replace the existing flat agent-activity section** in `page.tsx`.
- [ ] **Manual smoke**: home page should still show recent activity; expanding a row should show the cascade.
- [ ] **Commit**: `ui(home): DecisionAccordion replaces flat agent-activity firehose`.

---

## Task 8 (optional): Backend — `/api/decisions/recent` endpoint

**Files:**
- Modify: `argosy/api/routes/agent_activity.py` (or new route file)
- Test: `tests/test_decisions_recent_route.py` (new)

**Steps:**
- [ ] **Endpoint**: `GET /api/decisions/recent?user_id=...&limit=20`. Returns array of decision groups per spec §3.6.
- [ ] **Group AgentReport rows by decision_id**, sort decisions by latest `created_at` descending.
- [ ] **Test**: seed 2 decisions × 4 agents each, hit endpoint, assert grouping correct.
- [ ] **UI follow-up** in `useDecisionStream`: prefer this endpoint when available, fall back to client-side grouping otherwise.
- [ ] **Commit**: `feat(api): /api/decisions/recent grouped cascade payload`.

This task is OPTIONAL. Skip if Task 5's client-side grouping performs acceptably.

---

## Task 9: Backend — Sources echo in /api/agent-activity (if Task 4's Sources tab needs it)

**Files:**
- Modify: `argosy/api/routes/agent_activity.py` (extend `AgentActivityRow` model + handler)

**Steps:**
- [ ] **Add `sources_preview` field** to `AgentActivityRow` Pydantic model: `list[{source_id: str, body_chars: int, body_head: str}]`. Up to ~150 chars per body to keep response light.
- [ ] **Handler**: parse the agent's prompt_hash → re-derive sources from the prompt cache (or, simpler: persist a `sources_json` column on AgentReport at write time, populated by `_persist_*` sites). The simpler path adds a migration 0027 + column.
- [ ] **Or: skip this task** and have the Sources tab fetch a separate `/api/agent-activity/{id}/sources` endpoint that re-derives from the live agent run if available, else returns "(not captured)".
- [ ] **Recommended**: add migration 0027 + `sources_json` column + persist at the 4 sites (mirrors Wave A Task 8b pattern).
- [ ] **Commit**: `feat(state): persist sources_json on AgentReport for UI exposure`.

---

## Task 10: SDD §11 UI Design refresh + handover update

**Files:**
- Modify: `docs/design/SDD.md`

**Steps:**
- [ ] **§11 (UI Design)** — add a sub-section "Live agent cascade visibility (Wave B-UI)" describing the home-page accordion + /advisor cascade panel.
- [ ] **§11.3 (events)** — add the new `agent.run.started` + `agent.run.finished` events to the event inventory.
- [ ] **Handover note (top of SDD)** — update `Last edit:` to today; add Wave B-UI paragraph.
- [ ] **§8.5 (migrations)** — add 0027 row if Task 9 was done.
- [ ] **Commit**: `docs(sdd): Wave B-UI landed — §11 + §11.3 + handover`.

---

## Validation gates

- After every backend task: `pytest -m "not llm_eval" <touched paths>` passes.
- After every UI task: `cd ui ; npm run lint ; npx tsc --noEmit` clean.
- After Task 6 (advisor cascade): manual smoke — send an advisor turn, see the cascade fill in live.
- After Task 7 (home accordion): manual smoke — recent activity groups correctly.
- After Task 10 (SDD): final `pytest -m "not llm_eval" --tb=no -q | tail -3` should show no regressions.

## Final codex review

After Task 10, fire one codex tandem review covering the full Wave B-UI diff (`main..HEAD` at that point). The same pattern used at the end of Wave A. Address any blockers before declaring done.
