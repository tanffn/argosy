# Plan tab — "Run synthesis" button + live agent cascade

| Field | Value |
|---|---|
| **Date** | 2026-05-24 |
| **Status** | Draft for implementation |
| **Author** | Ariel (with Claude lead + Codex review tandem) |
| **Wave** | Standalone UX fix; ~1 day; touches advisor route, plan_synthesis orchestrator, BaseAgent event emit, /plan page, AgentCascadePanel |
| **Related** | SDD §7.6 user-initiated synthesis; SDD §15.4 live-cascade visibility |

## Background

The advisor agent emits the text `"All data gathered. The synthesis agent can proceed."` once gap closure is complete, but no UI affordance fires synthesis — the existing `api.advisorCheckIn()` helper in `ui/src/lib/api.ts:845` has zero callers. The only ways plan synthesis runs today are the monthly cron (`monthly_cycle.tick`, 0 8 1 * *) and a "large" plan-amendment-chat cascade. The user wants to trigger synthesis on demand from `/plan` and see the multi-agent process live as it runs.

## Goal

Add a "Run synthesis" button to `/plan` that fires the Opus fleet (analysts → debates → synthesizer → risk → fund manager) and shows live agent activity while it runs. On completion: collapse the cascade, surface a link to the new draft on `/proposals`, refresh the plan view.

## Why this needs more than a button

`POST /api/advisor/check-in` (`argosy/api/routes/advisor.py:1324`) is **synchronous**: the handler is `def post_check_in(...)` and the response is sent only after `run_synthesis(...)` returns. That can take many minutes with the full Opus fleet. A live cascade requires the frontend to know the `decision_run_id` **while** agents are running — but today the frontend learns it only after everything is done.

Also: agents inside `run_synthesis` do not currently get `decision_id` in their `run_sync(...)` kwargs, so `BaseAgent.run()` reads `inputs.get("decision_id")` as `None` and the WS `agent.run.started/finished` payloads omit it. Filtering the cascade by `decision_id` would match zero rows.

Both must be fixed for the live cascade to work.

## Non-goals

- Auto-retry on the transient `claude.exe` exit-1 flake (handover gap #4 — separate concern).
- A fixed pipeline diagram (we show agents as they fire, not a fleet topology view).
- `useDecisionStream` filter API redesign — filtering stays in the panel layer.
- Cancellation of an in-flight synthesis — out of scope; deferred.
- Frontend test runner setup (deferred per binding pref; live browser verification suffices).

## Design

### 1. `/check-in` becomes truly async — backend

File: `argosy/api/routes/advisor.py:1324` (`post_check_in`).

Three changes:

**(a) Baseline guard runs FIRST**, in the route handler, before any row creation. If no active baseline exists for `user_id`, raise HTTP 404 immediately — same semantics as today, but moved earlier. Eliminates the "leaked running DecisionRun row" failure mode flagged in Codex round 2.

**(b) Pre-create the `DecisionRun` row inline** with `decision_kind="plan_revision"`, `status="running"`, `started_at=now`, `ticker="(plan)"`, `tier="T3"`. Commit, capture `id`. This is the exact row `run_synthesis(..., existing_decision_run_id=<id>)` will reuse.

**(c) Schedule a wrapper function via FastAPI `BackgroundTasks`** (same pattern as `post_turn` at line 348 and `_maybe_ingest_plan_attachments` at line 454). The wrapper:
  - Opens its own sync DB session. **No shared sync-session helper exists today** — `argosy/state/db.py` exposes only the async `get_session()`, and `argosy/api/routes/plan.py:65` `get_db()` is a FastAPI `Depends` generator that closes the session when the response returns (so it cannot be reused inside the background task).
  - Pattern: replicate the existing background-task pattern from `argosy/orchestrator/flows/plan_amendment/dispatcher.py:308` and `argosy/orchestrator/loops/monthly_cycle.py:228` — build a fresh `Engine` + `sessionmaker(bind=engine, expire_on_commit=False)` inside the wrapper, open a Session via the factory, pass it to `run_synthesis(...)`, close it in `finally`. The engine URL comes from `get_settings().database_url.replace("+aiosqlite", "")` (sync URL). An optional small refactor — extract this 3-line factory build into a `argosy.state.db.build_sync_session_factory()` helper — is left out of scope; the duplication is acceptable for now and matches the codebase pattern.
  - Calls `run_synthesis(session, user_id=..., trigger="check_in", guidance=..., existing_decision_run_id=<id>)`.
  - Wraps the call in `try/except Exception`. On exception: re-open a session via the same factory and mark the pre-created `DecisionRun` row `status="failed"`, `finished_at=now`. Log structured event `plan_synthesis.background_failed` with the error. Re-raise swallowed; the user sees the failure via the WS stream (no draft.completed event) and an eventual UI timeout (frontend concern).

The response:
```python
return CheckInResponse(
    status="accepted",
    decision_run_id=<int>,
    decision_audit_token=<str>,   # NEW: "plan-synth-<id>", matches orchestrator.py:136
    draft_id=None,                # populated later, surfaced via plan.draft.completed WS event
)
```

`CheckInResponse.draft_id` widens from `int` to `int | None`. `CheckInResponse.decision_audit_token: str` is added so the frontend doesn't have to know the string format — it uses this verbatim as the cascade panel's filter key. The audit-token format string stays the backend's concern alone.

### 2. Propagate `decision_id` to every agent call in the synthesis flow

File: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py`.

**Encoding decision:** pass the **string audit token** `decision_audit_token` (= `f"plan-synth-{decision_run_id}"`, already computed at orchestrator.py:136), NOT the integer. Three reasons:
- `AgentReport.decision_id` (models.py:253) is a String column. Stringifying inside SQLAlchemy would lose the `"plan-synth-"` prefix that disambiguates synthesis-run agent_reports from other decision-flow rows.
- The Wave B-UI follow-up establishes that O(1) WS↔DB row matching is by `run_correlation_id` (the uuid), not by decision_id format — so the format is free to remain whatever the backend prefers.
- The frontend can avoid coupling to the format because `/check-in` now returns `decision_audit_token` verbatim in the response.

Phase 1 routes through `_safe_run_agent(...)` + `common_kwargs` (line 382). Add one key:
```python
common_kwargs = dict(
    ...,
    decision_id=decision_audit_token,  # str; BaseAgent.run reads inputs.get("decision_id")
)
```

Phases 2–5 call agents directly via `run_sync(...)`. Each direct call site — verified locations: orchestrator.py:561 (phase 2 debates), :619 (phase 3 synthesizer), :831 (phase 4 risk), :881 (phase 5 fund manager); re-verify line numbers during implementation — gets an explicit `decision_id=decision_audit_token` kwarg.

Acceptance check: after the change, every `agent_reports` row produced by a synthesis run has `decision_id = "plan-synth-{N}"`, and every WS `agent.run.started/finished` event from those agents carries `"decision_id": "plan-synth-{N}"` in its payload.

### 3. Emit `decision_id` on `agent.run.finished` events

File: `argosy/agents/base.py:696` (the success `agent.run.finished` emit site) AND `base.py:732` (the failure-path emit site, inside the `except Exception as run_exc:` branch).

Today the started payload (line 614) carries `decision_id`, but neither finished payload does. Add it to both — and to the `intake_session_id` field too while we're touching the same dicts, to keep the started/finished payload schemas in sync. Frontend cascade filtering relies on at least one event per row carrying `decision_id`; covering both started and finished events (success + failure) removes the dropped-event reordering risk Codex flagged.

### 4. Widen `AgentCascadePanel` to accept `decisionId`

File: `ui/src/components/advisor/AgentCascadePanel.tsx`.

Add `decisionId?: string | null` (string, matching the audit-token format the backend now emits) to `AgentCascadePanelProps`. Validation: `turnId` and `decisionId` are mutually exclusive — pass exactly one (component renders nothing if both null).

Inside the panel, the row filter widens:
```typescript
const filtered = rows.filter((r) =>
  turnId !== null
    ? r.turn_id === turnId
    : decisionId !== null
      ? r.decision_id === decisionId
      : false
);
```

`useDecisionStream` itself is not modified. `AgentRunCard` and `AgentDetailDrawer` are unchanged.

The panel currently lives in `components/advisor/`. Leave it there — the folder name is misleading after this change but renaming would force imports across files unchanged by this feature. Documented as a follow-up.

### 5. `/plan` page wiring

File: `ui/src/app/plan/page.tsx`.

New state:
```typescript
const [synthesisDecisionToken, setSynthesisDecisionToken] = useState<string | null>(null);  // "plan-synth-<N>"
const [synthesisRunning, setSynthesisRunning] = useState(false);
const [synthesisDraftId, setSynthesisDraftId] = useState<number | null>(null);
const [synthesisError, setSynthesisError] = useState<string | null>(null);
```

Header — second button right of `Re-critique now`:
```tsx
<Button
  variant="default"
  onClick={onRunSynthesis}
  disabled={synthesisRunning || !plan?.plan_version_id}
  title={!plan?.plan_version_id ? "Import a baseline plan first" : undefined}
>
  {synthesisRunning ? "Synthesizing…" : "Run synthesis"}
</Button>
```

Click handler:
```typescript
const onRunSynthesis = useCallback(async () => {
  setSynthesisError(null);
  setSynthesisRunning(true);
  setSynthesisDraftId(null);
  try {
    const r = await api.advisorCheckIn(USER_ID);
    setSynthesisDecisionToken(r.decision_audit_token);
  } catch (e) {
    setSynthesisError(String(e));
    setSynthesisRunning(false);
  }
}, []);
```

Live cascade — between the header and the existing Critique findings card:
```tsx
{synthesisDecisionToken !== null && (
  <AgentCascadePanel
    userId={USER_ID}
    decisionId={synthesisDecisionToken}
    turnId={null}
    isResolved={!synthesisRunning}
  />
)}
```

Completion via WS subscription to `plan.draft.completed`. Reuse the existing `useWSEvents` hook the cascade panel already imports transitively. The subscription:
```typescript
useWSEvents({
  topics: ["plan.draft.completed"],
  onEvent: (e) => {
    if (e.payload.user_id !== USER_ID) return;
    if (synthesisDecisionToken === null) return;  // not our run
    setSynthesisDraftId(e.payload.draft_id);
    setSynthesisRunning(false);
    refresh();  // re-fetch planCurrent
  },
});
```

Inline "draft ready" affordance, shown when `!synthesisRunning && synthesisDraftId !== null`:
```tsx
<p>
  Draft #{synthesisDraftId} ready ·{" "}
  <Link href="/proposals">→ Review draft on /proposals</Link>
</p>
```

Failure: if `synthesisError` is set, render it; do not consume `synthesisDecisionToken` (no cascade panel). User can click again to retry.

### 6. `api.ts` type adjustment

File: `ui/src/lib/api.ts:845`.

```typescript
advisorCheckIn: (userId: string, guidance = "") =>
  postJSON<{
    status: string;
    decision_run_id: number;
    decision_audit_token: string;   // NEW: e.g. "plan-synth-42"
    draft_id: number | null;
  }>(
    `/api/advisor/check-in`,
    { user_id: userId, guidance, urgency: "now" },
  ),
```

## Data flow (end-to-end)

```
User clicks [Run synthesis] on /plan
   │
   ▼
api.advisorCheckIn("ariel")  ──►  POST /api/advisor/check-in
                                       │
                                       ▼
                                   baseline guard (sync, in handler)
                                   pre-create DecisionRun(status="running")
                                   commit; capture id
                                   schedule background task wrapper
                                       │
                                       ▼
                                   HTTP 202: {decision_run_id: N, decision_audit_token: "plan-synth-N", draft_id: null}
   │
   ▼
setSynthesisDecisionToken("plan-synth-N"); setSynthesisRunning(true)
   │
   ▼
Background task wrapper builds own sessionmaker, opens Session
   └──► run_synthesis(session, existing_decision_run_id=N, ...)
            │   (decision_audit_token = "plan-synth-N" computed inside, orchestrator.py:136)
            ├─► phase 1 (9 analysts in parallel)  ──► each agent: WS started + finished (decision_id="plan-synth-N")
            ├─► phase 2 (debates per horizon)     ──► same
            ├─► phase 3 (plan_synthesizer)        ──► same
            ├─► phase 4 (risk team)               ──► same
            ├─► phase 5 (fund manager)            ──► same
            ├─► PlanVersion(role="draft") + commit  (orchestrator.py:276)
            ├─► DecisionRun.finished_at + status="completed" + commit
            └─► _emit_event("plan.draft.completed", {user_id, draft_id})
                       │
                       ▼
                   WS broadcast
                       │
                       ▼
   <AgentCascadePanel decisionId="plan-synth-N"> filters useDecisionStream rows
   where row.decision_id === "plan-synth-N" → renders live AgentRunCards
                       │
                       ▼  (plan.draft.completed handler in /plan)
   setSynthesisDraftId(draft_id); setSynthesisRunning(false); refresh()
   → cascade collapses to "done" summary
   → inline link to /proposals appears
   → critique data re-fetched
```

## Failure modes

| What | Where | UI behavior |
|---|---|---|
| No baseline plan | `/check-in` returns 404 before any row created | Button disabled by gate; defensively, toast on the route's 4xx response. |
| Network failure on POST | Frontend fetch rejects | `synthesisError` set; cascade does not render; retry by re-click. |
| Background task throws inside `run_synthesis` | Wrapper catches, marks DecisionRun failed, logs | No `plan.draft.completed` ever fires for this run. Frontend stays in `synthesisRunning=true`. Mitigation: defer to a follow-up; for now, user can reload the page or wait for an explicit timeout (not in this spec — out of scope). |
| WS disconnect mid-run | useDecisionStream falls back to REST poll | Existing behavior; rows fill in from `/api/agent-activity`. |
| User reloads page mid-synthesis | State lost; cascade gone | DB has the running DecisionRun + emerging AgentReport rows; not reconstructed in this iteration. Documented limitation. |
| Two clicks (race) | Button disabled during `synthesisRunning` | Single in-flight only; second click is a no-op. |

## Tests

### Backend
- `test_post_advisor_checkin_returns_decision_run_id` — update for new response shape (`draft_id` is `None`, new `decision_audit_token` field is a string). The existing test monkey-patches `run_synthesis`; with BackgroundTasks the patched call should still be invoked when TestClient drains background tasks after the response (FastAPI/Starlette runs `BackgroundTasks` synchronously after sending the response within the same TestClient call). If the existing patching pattern breaks, switch to a direct unit test of the route's pre-create + scheduling.
- `test_post_advisor_checkin_404_when_no_baseline` — still passes; **add an assertion** that NO new DecisionRun row exists for the user after the 404 (proves the baseline guard runs BEFORE any row insert). This is the exact failure mode the spec fixes; without it the regression is invisible.
- `test_post_advisor_checkin_invalidates_home_brief_cache` — still passes (invalidation happens inside `run_synthesis` post-commit).
- **New:** `test_post_advisor_checkin_marks_decision_run_failed_on_exception` — patch `run_synthesis` to raise; drain background tasks; assert DecisionRun row is updated to `status="failed"` and `finished_at` is set.
- **New:** `test_plan_synthesis_propagates_decision_id_to_agent_kwargs` — patch `_safe_run_agent` (and at least one phase 2–5 direct caller — e.g. the synthesizer at orchestrator.py:619); assert each receives `decision_id="plan-synth-<id>"` in kwargs.
- **New:** `test_base_agent_emit_finished_payload_includes_decision_id` — in `tests/test_events.py` (or alongside the existing `test_agent_run_events.py` which already covers started); assert the `agent.run.finished` payload carries `decision_id` when `inputs["decision_id"]` is set. Cover **both** the success path (base.py:696) and the failure path (base.py:732) — Codex flagged the latter as a missed test surface.

### Frontend
- No test runner wired (binding pref). Verification = live browser:
  1. Start backend (`uvicorn argosy.api.main:create_app --factory --host 127.0.0.1 --port 8000` with the `ARGOSY_EXPENSE_SAMPLES_ROOT` env var).
  2. `cd ui ; npm run lint ; npm run typecheck ; npm run dev`.
  3. Open `http://localhost:1337/plan`. Confirm button present + disabled-state correct based on baseline existence.
  4. Click `Run synthesis`. Confirm cascade panel appears within ~1 s; agents stream as they fire; cascade is restricted to the synthesis run (no unrelated turns leak in).
  5. Wait for completion. Confirm toast/inline draft-link appears; `/plan` data re-fetched; cascade collapses to summary.
  6. Click the link → land on `/proposals?draft=N`; the new draft is the top row.

## Sequencing

Backend first (so the live cascade has data to show), frontend second.

1. base.py: add `decision_id` to `agent.run.finished` payload + test.
2. orchestrator.py: propagate `decision_id` through all 5 phases + tests.
3. advisor.py `/check-in`: baseline-first + pre-create + BackgroundTasks wrapper + test updates.
4. AgentCascadePanel: add `decisionId` prop + filter.
5. /plan page: button + state + WS subscription + completion handler.
6. api.ts: `draft_id` widened.
7. Live browser verification.

Each step is independently testable. Step 4 can land before steps 1–3 (the panel just renders nothing if no rows match) — but the full path doesn't work until steps 1–3 ship.

## Codex review history

- **Round 1** caught: (i) decision_id not populated → empty filter, (ii) plan_synthesizer.finished is not end-of-flow, (iii) WS finished fires before draft commit (race), (iv) /check-in is synchronous so frontend gets decision_run_id only at end. All resolved in this revision.
- **Round 2** caught: (i) leaked running DecisionRun if pre-create happens before baseline guard, (ii) request-scoped DB session unsafe inside BackgroundTasks, (iii) decision_id propagation incomplete for phases 2–5 (only phase 1 uses common_kwargs), (iv) decision_id missing from finished payload (only started has it). All resolved in this revision.
- **Round 3** (this spec file): (i) fabricated `get_sync_session()` helper that doesn't exist — replaced with the real `sessionmaker(bind=engine, ...)` pattern from `plan_amendment/dispatcher.py:308`, (ii) decision_id type mismatch (spec said int, AgentReport schema is String column) — resolved by passing the existing `decision_audit_token` string and adding `decision_audit_token` to the `/check-in` response so the frontend doesn't couple to the format, (iii) test plan didn't assert no-row-leak on 404 — added, (iv) finished failure-path emit at base.py:732 also needs `decision_id` — added to spec + test plan, (v) stale line citations (455→454, 697→696) — fixed.
