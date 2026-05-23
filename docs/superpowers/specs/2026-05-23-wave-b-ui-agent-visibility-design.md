# Wave B-UI — Live agent-cascade visibility

**Spec date:** 2026-05-23
**Status:** Approved for implementation
**Author:** Ariel + Claude
**Plan to follow:** `docs/superpowers/plans/2026-05-23-wave-b-ui-agent-visibility-implementation.md`
**Predecessors:** Wave A + A.5 (telemetry columns + per-agent config) — landed on main at `72f34e3`.

## 1. Goal

Replace `/advisor`'s opaque "Thinking..." spinner with a **live cascade panel** showing each agent as it runs (role, model, status, tokens, cache hits, thinking, citation count). Evolve the home page's flat agent-activity firehose into a **decision-grouped accordion** where each decision expands to its cascade, with click-into-agent drilling to full reasoning + sources + citations.

Make every agent invocation visible while it happens AND replayable post-hoc.

## 2. Scope

### In scope

- **Backend WebSocket events** — emit `agent.run.started` + `agent.run.finished` from `BaseAgent.run()` via the existing `publish_event_threadsafe(name, payload)` plumbing.
- **Home page agent-activity restructure** — group flat rows by `decision_id` into a collapsible accordion. Each row: role, model, status (running/done/failed), tokens, cache_hit %, thinking_tokens, citations_count, duration. Click-to-expand shows the full response_text + sources + citations.
- **`/advisor` live cascade panel** — between the user message and the (pending) assistant response, render a vertical stack of agent rows updating live as WS events arrive. Auto-scrolls. Disappears (or collapses to a one-line summary) when the assistant response renders.
- **Shared `AgentRunCard` component** — reused on both surfaces. Renders one agent run with all the telemetry fields. Has a "view detail" affordance.
- **`AgentDetailDrawer`** — slides in from the right when an agent row is clicked. Shows: full `response_text` rendered as Markdown, sources list (with the inlined source bodies on claude_code, or document references on api_key), citations list (if non-empty), token/cost breakdown, prompt hash, decision context (which phase, which decision_run_id).
- **Filtering on home page** — current filters (date range, role) preserved; add filter by tier (T0/T1/T2/T3/plan) and a search box matching against response_text.
- **SDD §11 UI Design refresh** — document the new home-page accordion and the /advisor cascade panel.

### Out of scope (defer to Wave B-proper or later)

- Daily news-driven cascade trigger (Wave B-proper).
- Codex tandem live integration (Wave B-proper).
- Capturing extended-thinking text content (Wave A.6 — declined by Ariel).
- Tool-use streaming or runtime-fetch tracking (no production agents do runtime fetches today).
- "Skills loaded" — agents don't use the Claude Code skills system at runtime.
- A separate `/agents` page — home-page evolution is the chosen surface.
- Mobile responsive polish — Argosy is desktop-only for now (per SDD §11.1).

## 3. Components

### 3.1 Backend WebSocket events (Python)

In `argosy/agents/base.py::BaseAgent.run()`, around the existing `_call_model` invocation:

```python
async def run(self, **inputs):
    # ... existing build_prompt + setup ...

    try:
        from argosy.api.events import publish_event_threadsafe
        publish_event_threadsafe("agent.run.started", {
            "user_id": self.user_id,
            "agent_role": self.agent_role,
            "model": self.model,
            "decision_id": inputs.get("decision_id"),  # may be None for intake
            "intake_session_id": inputs.get("intake_session_id"),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "run_correlation_id": (run_id := str(uuid.uuid4())),
        })
    except Exception:  # noqa: BLE001 — best-effort, never block agent run
        run_id = str(uuid.uuid4())

    # ... existing _call_model ...

    try:
        publish_event_threadsafe("agent.run.finished", {
            "user_id": self.user_id,
            "agent_role": self.agent_role,
            "run_correlation_id": run_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "tokens_in": mc.tokens_in,
            "tokens_out": mc.tokens_out,
            "cache_input_tokens": mc.cache_input_tokens,
            "cache_creation_tokens": mc.cache_creation_tokens,
            "thinking_tokens": mc.thinking_tokens,
            "citations_count": len(json.loads(mc.citations_json)) if mc.citations_json else 0,
            "cost_usd": float(cost),
            "confidence": getattr(parsed, "confidence", None),
            "agent_report_id": report.id,
        })
    except Exception:
        pass
```

The `run_correlation_id` lets the UI pair start/finish events even before the AgentReport row is persisted.

### 3.2 Home page restructure (TypeScript / Next.js)

`ui/src/app/page.tsx` currently fetches `/api/agent-activity` and renders flat rows. Restructure into a `<DecisionAccordion>` component that:

1. Groups rows by `decision_id` (or `intake_session_id` for intake flows; falls back to "Standalone" for unscoped runs).
2. Renders one collapsed row per decision: timestamp, tier, ticker (if any), N agents, total cost, total duration, status (in_progress / done / failed).
3. On expand, renders the cascade stack — ordered by `agent_reports.created_at` ascending — using the shared `<AgentRunCard>`.
4. Decisions currently in-progress (have any `agent.run.started` but not `agent.run.finished` events) render with a pulsing border + live updates from WS.

State management: a `useDecisionStream(userId)` hook subscribes to `agent.run.*` events via `useWSEvents([...])`, merges them with the polled REST snapshot, deduplicates by `run_correlation_id` + `agent_report_id`.

### 3.3 `/advisor` cascade panel (TypeScript / Next.js)

`ui/src/app/advisor/page.tsx`. Today: user types message → POST `/api/advisor/turn` → waits → renders assistant response.

Add an `<AgentCascadePanel>` that appears between the user message and the pending assistant response. While the POST is in-flight:

- Subscribe to `agent.run.started` / `agent.run.finished` filtered by the current `user_id` AND a `turn_id` (new, generated client-side and sent in the POST body so the backend can echo it in events).
- Render each agent as it appears in the cascade — collapsed by default to a one-line summary (role, model, status indicator).
- Auto-scroll as new agents arrive.
- When the POST resolves, collapse the panel to a one-line summary ("Cascade: 5 agents, $0.12, 14.3s") with a "view detail" affordance that re-expands.

The existing turn-attachment rendering (PDF / image chips) is preserved; the cascade panel sits below them.

### 3.4 `<AgentRunCard>` (shared component)

`ui/src/components/agent/AgentRunCard.tsx`. Props:

```typescript
type AgentRunCardProps = {
  row: AgentActivityRow;  // existing type (Wave A added cache_*/thinking/citations_count)
  status: "running" | "done" | "failed";
  durationMs: number | null;
  onSelect: () => void;
};
```

Rendering (Tailwind, matches existing Argosy palette per SDD §11.4):

```
┌─────────────────────────────────────────────────────────────┐
│ ● PlanCritiqueAgent       opus-4.7    14.3s   $0.04          │
│   in 1,820  out 580  cache_hit 78%   thinking 2,100         │
│   confidence HIGH    citations 6                             │
└─────────────────────────────────────────────────────────────┘
   ↑ click row to open AgentDetailDrawer
```

Status dot color: ● blue = running (pulse), ● green = done HIGH/MEDIUM confidence, ● amber = done LOW, ● red = failed.

### 3.5 `<AgentDetailDrawer>` (shared component)

`ui/src/components/agent/AgentDetailDrawer.tsx`. Slides in from the right (40% screen width). Tabs:

1. **Output** — `response_text` rendered as Markdown.
2. **Sources** — the inlined `<source>` XML bodies parsed and rendered as collapsible blocks, each with the source_id and content. (For api_key backend in the future: also shows the citation document blocks.)
3. **Citations** — parsed `citations_json` array as a list: claim_text → cited_quote, with the source_id linking back to the Sources tab.
4. **Cost & telemetry** — tokens/cost breakdown table, cache hit ratio, thinking budget vs used, prompt hash (for cache-debugging).

Closes on Esc or outside-click.

### 3.6 New API endpoint (optional, for richer home-page payload)

`GET /api/decisions/recent?user_id=ariel&limit=20` returning:

```json
[
  {
    "decision_id": "abc123",
    "kind": "plan_revision",
    "tier": "T2",
    "ticker": "AAPL",
    "started_at": "2026-05-23T13:45:00Z",
    "finished_at": "2026-05-23T13:48:14Z",
    "status": "done",
    "total_cost_usd": 0.42,
    "agent_count": 12,
    "agent_runs": [
      { "id": 4521, "agent_role": "fundamentals", "model": "claude-sonnet-4-6", "...": "..." },
      ...
    ]
  }
]
```

The UI today only knows about flat agent rows; this endpoint gives the cascade structure server-side instead of client-side grouping. Smaller wire payload, easier filter/search.

If implementation pressure dictates: skip this endpoint and group client-side from the existing `/api/agent-activity` response. The endpoint is a nice-to-have, not load-bearing.

## 4. Data flow

```
User sends advisor turn
  → /advisor generates turn_id, POSTs /api/advisor/turn { turn_id, message, attachments }
  → backend persists user turn row, dispatches AdvisorAgent
       → AdvisorAgent.run() emits WS: agent.run.started (turn_id forwarded via inputs)
       → LLM call (cached system prompt, sources inlined if any)
       → AdvisorAgent.run() emits WS: agent.run.finished
       → if AdvisorAgent fans out (intake_extractor, plan_distiller, plan_critique, tax_analyst, …):
           each sub-agent emits its own start/finished events
  → POST resolves with the final assistant turn payload
  → /advisor re-renders: collapse cascade panel to summary, show assistant response
```

UI subscription pattern:
- `useWSEvents(['agent.run.started','agent.run.finished'])` hook returns a stream filtered to events matching the current `(user_id, turn_id)` pair.
- New events append to a local `cascadeRows` array.
- Existing `payload.user_id !== USER_ID` filter (per SDD §15.4 known issue) gets fixed for these events as part of this wave — required for correctness.

## 5. Error handling

| Scenario | Behavior |
|---|---|
| WS connection drops mid-turn | Cascade panel shows "disconnected" indicator; auto-reconnects via the existing WS hook. On reconnect, polls `/api/agent-activity` for any missed rows. |
| `agent.run.started` fires but `agent.run.finished` never does (agent crashed) | After 60s no finish, mark the row as "stalled" (red status dot). Don't block the UI — user can still see prior agents. |
| WS event references a `run_correlation_id` we never saw start | Render the row anyway, mark it "(missed start)". |
| `publish_event_threadsafe` raises | Caught and ignored in the agent path — event emission must never block an agent run (SDD §11.3 invariant). |

## 6. Testing

### Backend
- New unit test `tests/test_agent_run_events.py`: dispatch a mocked agent, assert both `agent.run.started` and `agent.run.finished` were published with the right shape (correlation id matches, telemetry fields present on finished).
- Idempotency test: emit `agent.run.finished` twice → DB row persisted once (the WS event is informational, not a write).

### UI
- Storybook (or jsdom test if no Storybook in repo): `<AgentRunCard>` renders correctly for each status (running/done/failed/stalled).
- `<DecisionAccordion>` groups N rows into one decision; collapse/expand round-trip preserves state.
- Hook test for `useDecisionStream`: simulate WS events arriving, assert local state merges + dedupes correctly.

### Integration
- Smoke test in `tests/test_advisor_route.py`: POST advisor turn, capture WS events emitted, assert at least one `agent.run.started` + one matching `.finished`.

## 7. Telemetry & observability

The cascade-panel itself is observability. No new telemetry beyond what Wave A already added.

`/internal/health/full` can grow a counter: in-flight WS subscribers per user. Useful if we ever scale beyond single-user.

## 8. Rollout plan

Sequential, each task is a separate commit:

1. Backend: emit `agent.run.started` + `agent.run.finished` from `BaseAgent.run()`. Add `run_correlation_id` to BaseAgent state.
2. Backend: emit the same events from the `_call_via_*` backends if they bypass `run()` directly (verify with grep).
3. Backend: add `turn_id` echoing in `/api/advisor/turn` so the UI can filter events to its current turn.
4. UI: `<AgentRunCard>` component + Storybook entry.
5. UI: `<AgentDetailDrawer>` component.
6. UI: `useDecisionStream` hook + tests.
7. UI: Restructure home-page agent-activity into `<DecisionAccordion>`.
8. UI: `<AgentCascadePanel>` on /advisor — replaces the "Thinking..." spinner.
9. (Optional) Backend: `/api/decisions/recent` endpoint.
10. SDD §11 UI Design refresh.

Each step lands as a separate commit on `main` (or a `wave-b-ui` branch if you want isolation — recommended given Wave A's lessons on branch isolation for multi-task waves).

## 9. Success criteria

- Sending an advisor turn shows agents appearing live (not all-at-once at the end).
- Home page agent-activity is grouped by decision; click expands cascade.
- Click any agent → drawer opens with full response, sources, citations, telemetry.
- No regressions in existing UI tests.
- New backend WS event tests pass.

## 10. Open questions deferred to implementation

- Should `<AgentDetailDrawer>` Markdown-render the response with code-block syntax highlighting? (Likely yes — `react-markdown` + `rehype-prism` are already in the project per `package.json`. Verify in Task 4.)
- Cascade-panel scroll behavior on long cascades (12+ agents): infinite list vs paginated? Start with infinite scroll (simpler).
- Decision grouping when `decision_id` is NULL (intake / advisor turns without a decision_run): group by `intake_session_id` if available, else show under "Standalone" header.
