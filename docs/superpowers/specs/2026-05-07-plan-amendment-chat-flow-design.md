# Argosy — Plan Amendment Chat Flow Design (Wave 4)

| Field | Value |
|---|---|
| **Date** | 2026-05-07 |
| **Status** | Design approved; spec awaiting user review |
| **Authors** | Ariel + Claude (collaborative brainstorm) |
| **Related** | [Plan distillate spec (Waves 1-3)](2026-05-05-plan-distillate-design.md), [SDD §6.11 Plan synthesis flow](../../design/SDD.md), [SDD §11 Events](../../design/SDD.md) |
| **Phasing** | Wave 4 lands inside SDD Phase 5+ alongside Argonaut autonomy |

---

## 1. Context & motivation

After Waves 1-3 the user can:

- Import a long-form baseline (Wave 1) → distilled into a structured anchor.
- Trigger a monthly synthesis or `/api/advisor/check-in` (Wave 2) → 5-phase fleet review produces a `role=draft` plan; user reviews + accepts via `<PlanRevisionSheet>`.
- Take a swing on speculative candidates from `current.short` → routed to Argonaut as a T0 paper proposal (Wave 3).

What's missing: between scheduled syntheses, the user should be able to **ask for a structural change in chat** — *"tighten my NVDA cap from 15% to 12%"*, *"shift the medium horizon toward growth given DeepSeek + tariffs"*, *"re-evaluate everything, the macro picture changed"*. Today the only path is `/check-in` which kicks off the full ~15 min 5-phase synthesis. That works for *"re-evaluate everything"* but is overkill for *"tighten one number"* and creates a UX dead-zone.

This design adds a **tiered amendment path** to the advisor chat:

- **Small** (~5s, inline): advisor proposes a strict-tightening Delta on the existing draft/current.
- **Medium** (~30s, async): advisor kicks off a lightweight Phase-3-only synthesis with the user's amendment as guidance.
- **Large** (~15 min, async): advisor kicks off the full 5-phase `run_synthesis(...)` — same as `/check-in` today.

The advisor's existing turn classifier picks the tier from the chat context. Medium and Large don't block the chat — they dispatch to a worker, return immediately, and ping the user via WebSocket + browser notification when the draft lands.

The result is the existing `role=draft` lifecycle in all three cases. The user still has the final accept/reject decision via the side sheet. No path writes to `role=current` directly.

---

## 2. Tier definitions

### 2.1 Small — inline Delta

**Triggers**: the advisor classifies the message as a *strict tightening* of one specific target/action/theme value the user references explicitly.

Examples (Small):
- "Change my NVDA cap from 15% to 12%."
- "Tighten the cash floor to 8%."
- "Shorten the IBIT harvest deadline by two weeks."

Examples (NOT Small — promote to Medium):
- "Loosen the NVDA cap to 18%." (loosens, not tightens)
- "Add a new target: max single-name concentration 10%." (adds, not edits one)
- "Tighten everything by 20%." (cross-target)

**Tightening rule**: a Delta is "tightening" if the proposed change reduces risk surface — lower position cap, higher cash floor, shorter time horizon, narrower drawdown allowance, removed action. The advisor's structured turn output emits a `direction: "tighten" | "loosen" | "ambiguous"` field; only `tighten` qualifies for Small. `loosen` and `ambiguous` auto-promote to Medium.

**Effect**: the advisor agent emits a fully-formed `Delta` (matching the Wave 2 type) with `accepted=true, user_edited=true` and applies it via the existing `PATCH /api/plan/draft/{id}/items/{item_id}` path (or a sibling for `current`). Inline confirmation in the chat.

**No new synthesis runs.** No Phase 3, no risk team, no fund manager. The advisor's reasoning *is* the audit trail (chat turn + structured output + linked `decision_runs` row).

### 2.2 Medium — lightweight Phase 3 synthesis

**Triggers**: the advisor classifies the message as a *theme shift on one horizon*, a multi-target tweak, a loosening, or an ambiguous edit.

Examples:
- "Shift the medium horizon toward growth — DeepSeek changed the math."
- "Loosen the NVDA cap to 18% — I want more concentration upside."
- "Add a tactical theme around tariff-resilient IL exposure."
- "Reconsider the short horizon given last week's fills."

**Effect**: dispatches `plan_amendment_flow.run_medium(...)` to a worker thread. The worker:

1. Opens a `DecisionRun` row with `decision_kind="plan_amendment_chat"`, `tier="medium"`.
2. Loads the active `current` plan + the pending draft (if any).
3. Calls `PlanSynthesizerAgent` with the user's amendment as `guidance`, the existing horizons as `prior_current_md`, a fresh portfolio snapshot, and the same speculation cap. **Skips** Phases 1, 2, 4, 5 — no analyst report aggregation, no debate, no risk team, no fund manager.
4. Applies `_enforce_speculation_cap` post-filter (Wave 3 layer 2) to the output — even Medium is cap-enforced.
5. Persists the result as `role=draft` (superseding any existing draft with the same idempotency rule as Wave 2).
6. Stamps `finished_at` on the `DecisionRun` row.
7. Emits `plan.draft.completed` (existing event from Wave 2) plus a new `plan.amendment.completed` with `tier="medium"`.

**Why no Phase 1/2/4/5 for Medium?** The synthesizer already saw the prior current plan + the analyst evidence baked into that plan's `synthesis_inputs_json`. The user's amendment is a constrained delta, not a full re-evaluation. Skipping the heavy phases is what makes Medium 30s instead of 15 min.

**Cost**: ~$0.50 per run.

### 2.3 Large — full 5-phase re-synthesis

**Triggers**: structural rethink, cross-horizon, "re-evaluate", explicit "run synthesis".

Examples:
- "Re-evaluate everything — DeepSeek + tariffs + the new RSU vest."
- "I want a fresh pass over all three horizons."
- "Run a full synthesis."

**Effect**: dispatches to the same worker pattern, but the work is just `plan_synthesis_flow.run_synthesis(..., trigger="check_in", guidance=user_message)` — i.e. literally the existing Wave 2 flow with the user's message as guidance. Decision kind: `"plan_amendment_chat"`, tier: `"large"`.

This is functionally what `/api/advisor/check-in` does today, but routed through the advisor chat rather than a separate API call.

**Cost**: ~$5-8 per run (same as `/check-in`).

### 2.4 Tier classification mechanism

The advisor's existing structured turn output gets a new optional field:

```python
class AdvisorTurn(BaseModel):
    # existing fields...
    amendment: AmendmentIntent | None = None


class AmendmentIntent(BaseModel):
    tier: Literal["small", "medium", "large"]
    direction: Literal["tighten", "loosen", "ambiguous"] | None = None  # only for Small
    proposed_delta: Delta | None = None  # only for Small; matches Wave 2 type
    rationale: str  # advisor's reasoning for the classification
    requires_confirmation: bool = False  # if True, Small-eligible but advisor wants explicit user OK
```

When the API route sees `amendment != None`, it routes:
- `tier="small"` AND `direction="tighten"` AND `proposed_delta` present → apply inline.
- `tier="small"` but `direction != "tighten"` → escalate to medium dispatch (advisor mis-classified; this is a safety net).
- `tier="medium"` → dispatch Medium worker.
- `tier="large"` → dispatch Large worker.

**Mis-classification recovery**: the advisor's chat turn surfaces the chosen tier in its rendered text (*"I'll apply this as a small tightening, on it now"* / *"This is a structural change — kicking off a full re-eval, ETA 15 min"*). The user can override by responding *"do a full synthesis instead"* — the next turn re-classifies. For an already-applied Small Delta, the existing `POST /api/plan/draft/{id}/items/{item_id}/reject` Wave 2 endpoint handles rollback (with `direction="loosen"` because the rejection is loosening the just-tightened state — auto-promotes to Medium).

---

## 3. Async dispatch architecture

Medium and Large share one worker pattern. The chat turn returns 202; the worker runs in a thread; events fire on completion.

### 3.1 Components

```
argosy/orchestrator/flows/plan_amendment/
  __init__.py            # re-exports public API + monkeypatchable helpers
  dispatcher.py          # run_medium, run_large, run_small (sync); async-safe entry points
  workers.py             # _medium_worker, _large_worker (called via asyncio.to_thread)
  classifier.py          # advisor turn → AmendmentIntent extraction (pure logic, no LLM)
```

Mirrors the Wave 2 `plan_synthesis/` package layout (post-M7 refactor).

### 3.2 Concurrency

**One in-flight async amendment per user.** Tracked in the `decision_runs` table via a partial unique index on `(user_id) WHERE decision_kind='plan_amendment_chat' AND status='running'`.

If the user fires another amendment while one is running, the dispatcher detects the existing row and the advisor's turn surfaces a confirmation:

> *"You already have a Large amendment running (started 3 min ago). Want me to cancel it and start over with this new request, or queue this after?"*

The user's next turn sets `cancel_existing=true|false` in `AmendmentIntent` (added field) and the dispatcher acts.

### 3.3 Cancellation

New endpoint: `POST /api/advisor/amendment/{decision_run_id}/cancel?user_id=...` flips the `decision_runs` row to `status='cancelled'`. The worker checks `status` between phases and bails if cancelled. A cancelled large-tier run that's already past Phase 3 commits the partially-built draft as `role=superseded` (preserves work, doesn't surface to UI).

### 3.4 Worker lifecycle

```
chat turn (sync FastAPI route)
  ↓
classify amendment → AmendmentIntent
  ↓
if tier == small: apply inline, return 200
  ↓
else: open DecisionRun(status='running'), spawn worker via asyncio.to_thread,
       return 202 {decision_run_id, tier, eta_seconds}
  ↓
worker runs run_medium / run_large
  ↓
on success: persist draft, emit plan.amendment.completed, stamp DecisionRun finished_at
on failure: emit plan.amendment.failed, stamp DecisionRun status='failed' + error
on cancel:  emit plan.amendment.cancelled
```

The `run_medium`/`run_large` functions are sync, called from within `asyncio.to_thread` (same pattern as Wave 2's `monthly_cycle` → `run_synthesis` bridge).

---

## 4. Notifications

### 4.1 WebSocket events

Existing (Wave 2): `plan.draft.started`, `plan.draft.completed`. Both are emitted by `run_synthesis` and stay unchanged.

New (Wave 4):

```
plan.amendment.started     payload: {user_id, decision_run_id, tier, eta_seconds}
plan.amendment.completed   payload: {user_id, decision_run_id, tier, draft_id}
plan.amendment.failed      payload: {user_id, decision_run_id, tier, error}
plan.amendment.cancelled   payload: {user_id, decision_run_id, tier}
```

The Medium and Large workers emit via the existing `publish_event_threadsafe` helper (Wave 2 fix I3 — already thread-safe and async-loop-aware).

For Large, `plan.draft.completed` and `plan.amendment.completed` both fire (Large IS a synthesis run); UI subscribes to whichever is more useful for which surface.

### 4.2 Browser notifications

New module: `ui/src/lib/notifications.ts`. Exposes:

```typescript
export async function ensureNotificationPermission(): Promise<NotificationPermission>;
export function notify(title: string, body: string, opts?: NotificationOptions): void;
```

The first time the user fires a Medium or Large amendment, the chat surface calls `ensureNotificationPermission()` which prompts the browser. Permission state is read from `Notification.permission` directly each time `notify` is called — no localStorage caching needed (the browser already persists permission per-origin).

If permission is `denied` or `default`, `notify` is a silent no-op. The in-app banner (already triggered by `plan.draft.completed`) remains as the always-on surface.

WebSocket subscriber on the advisor page calls `notify` when `plan.amendment.completed` arrives:

```typescript
notify("Argosy", "Your plan revision is ready — review it now");
```

**Browser support fallback**: if `Notification` is undefined (Safari without permission flow, or ancient browsers), the helper is a silent no-op. The in-app banner is the only required path.

---

## 5. Audit trail

Each amendment writes one `decision_runs` row.

| Column | Value |
|---|---|
| `decision_kind` | `"plan_amendment_chat"` (new value; extends Wave 2's `"plan_revision"`) |
| `user_id` | the chatting user |
| `ticker` | `"(plan)"` (mirrors Wave 2 synthesis convention) |
| `tier` | new column: `String(8)`; values `"small"|"medium"|"large"` |
| `status` | `"running"|"completed"|"failed"|"cancelled"` |
| `started_at` / `finished_at` | per usual |
| `notes_json` | new (or reuse `metadata_json` if present): stores the user's amendment text + the `AmendmentIntent` for replay |

The resulting `plan_versions` row (when one is produced) carries `decision_run_id` pointing here, completing the lineage from chat-turn → DecisionRun → draft → (after accept) current.

For Small, the DecisionRun row still opens — the Delta application creates a `decision_runs` row with `tier="small"` so the chat-turn-to-plan-edit lineage is queryable from history alone.

---

## 6. Schema changes

Migration **0018** (`alembic/versions/0018_plan_amendment_chat.py`):

```python
# decision_runs additions
with op.batch_alter_table("decision_runs") as batch:
    batch.add_column(sa.Column("tier", sa.String(8), nullable=True))
    batch.add_column(sa.Column("notes_json", sa.Text(), nullable=True))

# Partial unique index for concurrency control
op.create_index(
    "ix_decision_runs_one_amendment_running_per_user",
    "decision_runs",
    ["user_id"],
    unique=True,
    postgresql_where=sa.text(
        "decision_kind='plan_amendment_chat' AND status='running'"
    ),
    sqlite_where=sa.text(
        "decision_kind='plan_amendment_chat' AND status='running'"
    ),
)
```

The partial unique index works on both Postgres and SQLite (3.8+).

The existing `decision_kind` column from Wave 2 already supports the new value (it's `String(20)`).

No new tables. No backward-incompatible changes.

---

## 7. API surface

### 7.1 Modified

`POST /api/advisor/turn` — response model `AdvisorTurnResponse` gains an optional `amendment` field:

```python
class AdvisorTurnResponse(BaseModel):
    # existing fields...
    amendment: AmendmentResultDTO | None = None


class AmendmentResultDTO(BaseModel):
    tier: Literal["small", "medium", "large"]
    decision_run_id: int
    status: Literal["applied", "running", "needs_confirmation", "cancelled_existing"]
    draft_id: int | None = None  # populated when tier=small (immediate) OR when status=cancelled_existing for the prior run
    eta_seconds: int | None = None  # populated for tier in {medium,large} with status=running
```

**Status semantics**:
- `applied` — Small Delta was applied; `draft_id` points at the affected draft.
- `running` — Medium/Large worker dispatched; `decision_run_id` and `eta_seconds` populated.
- `needs_confirmation` — concurrency conflict (existing run) or ambiguous direction; the advisor's turn text asks the user to pick.
- `cancelled_existing` — the user said "cancel and restart"; the prior run is cancelled, this turn confirms.

### 7.2 New

```
POST /api/advisor/amendment/{decision_run_id}/cancel?user_id=...
  → 200 {status: "cancelled"} on success
  → 404 if no running amendment with that id for that user
  → 409 if the run already finished (cannot cancel)
```

### 7.3 Unchanged

All Wave 2 draft lifecycle endpoints (`GET /draft`, `POST /draft/{id}/accept`, `POST /draft/{id}/reject`, per-delta accept/edit) are reused as-is. Wave 3's `/current/structured` and `/current/speculative/{ticker}/take` are reused as-is. The amendment flow does not introduce new draft-shape concepts.

---

## 8. Advisor agent changes

### 8.1 Prompt

`AdvisorAgent.build_prompt` gains a new instruction block (only when the user has an active `current` plan):

```
AMENDMENT INTENT DETECTION

If the user's latest message asks to change something about their current
plan (a target, theme, action, or speculative candidate), classify it:

  small  - strict tightening of one specific target/action they reference
           directly. Direction must be "tighten" (lowers risk surface);
           "loosen" or "ambiguous" → use medium instead.
  medium - theme shift on one horizon, multi-target tweak, loosening, or
           any change that involves cross-target reasoning.
  large  - structural rethink, cross-horizon, "re-evaluate everything",
           "run synthesis", or any request that asks the fleet to reconsider.

Emit the classification in the `amendment` field of your structured output.
For small with direction=tighten, also emit a fully-formed `proposed_delta`
with item_id, item_kind, horizon, change_kind, summary, prior, proposed,
rationale, and accepted=true.

Be conservative: when in doubt, classify as medium. The user can always say
"do a full synthesis" to escalate to large; they cannot easily reverse a
hasty small Delta.
```

### 8.2 Output schema

`AdvisorTurn` (existing pydantic model) gets the optional `amendment: AmendmentIntent | None` field. The chat surface renders the advisor's `text` field as before; the dispatcher reads `amendment` to drive the side-effects.

### 8.3 No model change

`AdvisorAgent` continues on its existing default model. The amendment classification is one extra field in the structured output; no perceptible cost increase per turn.

---

## 9. UI changes

### 9.1 Advisor page

Subscribe to `plan.amendment.*` events on the existing WebSocket connection. On `plan.amendment.started` (tier in {medium, large}), render a small status pill in the chat header:

> *⏳ Plan amendment in progress (medium · ETA 30s)*

with a [Cancel] button that calls `POST /api/advisor/amendment/{decision_run_id}/cancel`.

On `plan.amendment.completed`, replace the pill with a chat system message:

> *✅ Plan revision ready — [Review it now]*

The button opens the existing `<PlanRevisionSheet>`. Also fire `notify("Argosy", "Your plan revision is ready")` for browser notification.

On `plan.amendment.failed`, replace the pill with:

> *❌ Plan amendment failed: {error}. Retry?*

with a [Retry] button that re-sends the original chat message.

### 9.2 No new UI primitive

Reuses existing Wave 2 primitives (`<PlanRevisionSheet>`, draft-pending banner) and Wave 1 primitives (`<Card>`, `<Button>`).

### 9.3 Permission prompt

The first time the user fires a Medium or Large amendment in a session, the chat surface calls `ensureNotificationPermission()`. If denied, no follow-up prompts in the same session (state from `Notification.permission`).

---

## 10. Testing strategy

### 10.1 Unit tests

- `tests/test_plan_amendment_classifier.py` — pure logic: given an `AdvisorTurn` payload, the classifier extracts the `AmendmentIntent` correctly. Covers tier mismatches (e.g. Small with `direction="loosen"` gets escalated).
- `tests/test_plan_amendment_dispatcher.py` — dispatcher routes `AmendmentIntent` to the right worker. Stubs the workers; asserts which is called with what kwargs. Concurrency conflict path.
- `tests/test_plan_amendment_workers.py` — Medium worker stubs `PlanSynthesizerAgent.run_sync`, asserts it's called with `guidance=...`, prior_current_md from existing plan, etc. Large worker stubs `run_synthesis`, asserts `trigger="check_in"`, `guidance=user_message`.
- `tests/test_plan_amendment_cap_enforcement.py` — verify that even Medium amendments go through `_enforce_speculation_cap`. Covers a malformed Medium output that breaches the cap; the post-filter drops it.
- `tests/test_migration_0018.py` — schema assertions for the new columns + index.

### 10.2 Integration tests

- `tests/test_advisor_amendment_route.py` — `POST /api/advisor/turn` end-to-end with a stubbed advisor agent that emits `amendment` field. Covers all four `status` values. Cancellation endpoint tests.
- `tests/test_plan_amendment_concurrency.py` — fire two Medium amendments in quick succession; assert the second returns `needs_confirmation` and the first is preserved.
- `tests/test_plan_amendment_e2e.py` — `@pytest.mark.llm_eval` — full live test: send a real "tighten NVDA cap" chat message, assert advisor classifies as Small, Delta is applied, draft updated. Cost ~$0.05.

### 10.3 UI tests

Manual smoke per the existing Wave 1-3 pattern (no automated UI tests in the project today).

---

## 11. Out of scope

- **Multi-turn amendment refinement** — the design treats each amendment as one chat turn. *"Can you reconsider that?"* in a follow-up turn fires a fresh classification, not a continuation of the prior run. A future iteration could thread `amendment_context` across turns.
- **Background re-runs** — if a Medium amendment produces a draft the user later rejects with new guidance, the user has to re-ask in chat. The system doesn't auto-retry.
- **Multi-user concurrency** — single-user assumption from the broader system holds. The partial unique index is per-user.
- **Permission revocation UX** — if the user revokes browser notification permission mid-session, the in-app banner still works; the system does not detect or reprompt.
- **Cancel-mid-Phase-X granularity for Large** — the worker checks `status` between phases. Cancellation during a long Phase 1 (multi-LLM-call analyst sweep) won't interrupt the in-flight LLM calls; they finish, then the worker bails. Cost of cancellation is therefore non-zero for Large in flight.

---

## 12. Open questions

None at design time. The following may surface during implementation and are tracked here:

- **Should `plan.amendment.completed` and `plan.draft.completed` be merged for Large?** — Currently both fire. UI subscribes to `plan.amendment.*` for amendment-specific surfaces, `plan.draft.*` for general draft updates. May be redundant; revisit after first usage.
- **DecisionRun `metadata_json` vs new `notes_json` column?** — If the existing `decision_runs` table already has a JSON metadata column, reuse it. Confirm during migration design.
- **Tier escalation — Small → Medium auto-bump latency?** — When the advisor mis-classifies as Small with `direction="ambiguous"`, the dispatcher escalates to Medium. The user sees the Medium ETA (30s) but the advisor's chat already said "applying small tightening." Need to surface the escalation in the chat ("on second thought, this needs a fuller pass — give me 30s"). May require the advisor's prompt to be more conservative (default to Medium).

---

## 13. References

- Wave 1-3 design: [2026-05-05-plan-distillate-design.md](2026-05-05-plan-distillate-design.md)
- Wave 1-3 plan: [2026-05-05-plan-distillate-implementation.md](../plans/2026-05-05-plan-distillate-implementation.md)
- SDD §6.11 plan synthesis flow
- SDD §10.1 routing matrix (existing `plan_revision` row stays; `plan_amendment_chat` is a sibling)
- SDD §11.3 WebSocket events (extend list)
