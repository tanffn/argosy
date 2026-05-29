# Last-mile delivery layer — Design (Spec E)

**Status:** Pending Ariel approval. Codex tandem single-dispatch review returned BLOCK; 4 BLOCKERs + 6 IMPORTANTs integrated below (see §Codex tandem review summary at end).
**Date:** 2026-05-29
**Author:** Claude (Opus 4.7), with codex tandem reviewer single-dispatch (run 2026-05-29).
**Codex session:** `tools/codex-tandem/sessions/2026-05-29-last-mile-delivery-spec-review/`.
**Sibling specs (the wave-after-30/30 quintet):**
- [`2026-05-29-jobs-registry-design.md`](2026-05-29-jobs-registry-design.md) — Spec A. Registers this spec's notification dispatcher loop, weekly-digest job, inferred-life-event-detector loop, replan-trigger dispatch job.
- [`2026-05-29-state-observer-agent-design.md`](2026-05-29-state-observer-agent-design.md) — Spec B. Producer of the `state_observer_*` flag kinds that this spec consumes (action proposer + replan trigger).
- [`2026-05-29-predictions-ledger-design.md`](2026-05-29-predictions-ledger-design.md) — Spec C. The action proposer's own outputs are predictions (the user's accept/defer/reject is the outcome).
- [`2026-05-29-life-events-cashflow-redesign-design.md`](2026-05-29-life-events-cashflow-redesign-design.md) — Spec D. The inferred-life-event detector writes proposals that, on accept, materialize as `LifeEvent` rows in Spec D's redesigned schema.

## Motivation

Spec A is the **skeleton** (jobs run reliably and visibly). Spec B is the **brain** (the system watches state and decides what is salient). Spec C is the **memory** (the system records what it claimed and how it turned out). Spec D is the **clock** (the system models cashflow phases instead of single dates). Without Spec E, those four close on a brain that watches but cannot reach the user when it matters, an observer that flags FX drift but doesn't say what to do about it, and a system that asks the user to log every life event by hand even though the transaction stream already tells the story. **Spec E is the mouth and the hands.** It pushes signal out beyond the app, converts observations into structured action proposals the user can Accept / Defer / Reject without ever talking to an LLM, infers life-event phase changes from the transaction stream so the user is the consumer of intelligence and not the data-entry operator, and closes the loop by re-triggering plan composition when high-severity flags warrant it. Without E, A+B+C+D are invisible until the user opens the app; with E, the system is the expert and the user is the consumer.

Per the binding user direction: *"Always think big, the holistic solution, what is the project goal and does that bring us closer."* The project goal is to **replace the cognitive load of being your own financial advisor with a multi-agent AI system that knows full state, watches continuously, surfaces what matters when it matters, gets smarter over time, and where the USER is the consumer of intelligence — not the operator.** Every commit in this sprint is measured against that yardstick: does it remove a step the user currently does manually, or does it merely move work around inside the system?

## Goal

Ship a 9-commit sprint that lands:

1. The `action_proposals` table — a system-proposed-action ledger with structured payload, severity, source linkage (back to MonitorFlag from Spec B + state snapshot from Spec B), and a uniform Accept/Defer/Reject/Customize/Supersede lifecycle.
2. An Opus-backed `action_proposer` agent that turns (flag OR observation) + full state + active plan into 0–3 structured action proposals. **The proposer NEVER executes**; it RECORDS.
3. A `notification_dispatcher` service that fans severity-gated notifications across in-app (existing `/ws/events`), web push (VAPID + service worker), and a weekly email digest (registered as a Spec A job).
4. The **observer → replan loop**: high-severity state-observer flags automatically fire the matching `replan_triggers` enum value (currently a stub) and queue a plan synthesis run through Spec A's job registry, with a per-trigger cooldown + severity gate to prevent oscillation.
5. An **inferred life-event detector** that reads the transaction stream from `expense_ingest` and proposes phase changes (kids left home, recurring car purchase, wedding-scale transfer) via the same `action_proposals` table — the user reviews; the system never auto-applies to Spec D's `life_events` schema.
6. A UI surface (`/proposals` extension) showing all open action proposals across all kinds, with structured-payload preview and a Customize editor.
7. A web-push subscription opt-in card + per-user notification preferences (channel × severity × kind matrix).
8. A weekly email digest job registered with Spec A.
9. A backfill verification commit that runs the inferred-life-event detector against the user's historical transaction data and confirms it surfaces sensible phase changes without false-positive noise.

## Non-goals

- **No auto-execution of any money decision.** The action proposer outputs structured payloads (e.g. `repatriate_currency: {amount_usd: 40000, target_account: "leumi_nis"}`); a human Accept is the only path to a real order. The proposer's output passes through Spec C's predictions ledger as a prediction (subject="will the user accept this action?"); the user's accept/defer/reject is the outcome.
- **No SMS / WhatsApp / Telegram channels in v1.** Web push covers the urgent-mobile case once the user opts in; email digest covers the cumulative-summary case. SMS/WhatsApp add deliverability operational burden (Twilio, A2P registration) that doesn't move the goal in v1.
- **No mobile native app.** Web push works in the iOS Safari PWA + Chrome on Android since 2023; that's the "mobile delivery" surface for v1. A native app is a future product question, not infrastructure.
- **No third-party push service (FCM/APNs proxy).** Web push is direct browser-to-browser via VAPID; we own the keys. Adding FCM means trusting a third party with notification content that includes financial signal — anti-pattern for a single-user privacy-anchored system.
- **No multi-tenant notification fanout.** Single-user system; subscriptions table is user-scoped but multi-tenant collation is a future product question.
- **No LLM in the notification path.** The notification dispatcher is pure code: it reads `action_proposals.summary` (the LLM already wrote it) and renders the channel template. No per-notification LLM call — that's both cost noise and a latency risk against push-to-mobile-within-seconds.
- **No replacement of Spec B's observer.** The action proposer CONSUMES observer flags + observations; it does not replicate observation. If the observer didn't flag, the proposer doesn't propose.
- **No replacement of the deterministic windfall / unallocated-cash allocators.** Those produce sharp numeric proposals already wired to `/proposals`; the action proposer is the GENERALIST layer for everything they don't cover (FX repatriation, rebalance, inferred phase changes, replan invocations). See §6 for coexistence rules.
- **No retro-fitting of historical state-observer flags into action proposals.** The proposer runs forward from sprint-merge time; old flags age out naturally. A backfill is possible but not in v1 scope.
- **No notification preference UI revolution.** v1 ships the matrix-shaped settings page (channel × severity × kind); we do NOT ship granular per-user per-flag-kind-prefix preference rules. The matrix is enough; granularity is a future spec.

## Sprint commit table

Per [[feedback_work_style_long_sprints]] — long sprint, codex single-dispatch review for risky commits, SDD update per commit, blockers logged via codex, not paused on user.

| # | Commit | Codex review | Notes |
|---|---|---|---|
| 1 | Migration 0050 — `action_proposals` + `notification_subscriptions` + `notification_preferences` tables | **Yes** | Per-migration commit. Three coupled tables; codex probes shape + index plan + FK cascade. |
| 2 | `action_proposer` agent (Opus) + `argosy/services/action_proposer_runner.py` + writer + dedup contract | **Yes** | LLM money path. Critical: structured payload validation + no-execution invariant. |
| 3 | `notification_dispatcher` service — in-app `publish_event` + web push (VAPID) + dispatch ledger; severity-gated per-user preferences | **Yes** | Web push crypto + VAPID key handling + dispatch idempotency. |
| 4 | Observer → replan wiring — when state_observer fires `severity in {warning, critical}` and `kind` maps to a `replan_triggers.TriggerKind`, queue a plan-synthesis job through Spec A. **Cooldown + severity gate.** | **Yes** | Highest oscillation risk in the whole sprint. Codex probes cooldown matrix + gate determinism + idempotency on retry. |
| 5 | `inferred_life_event_detector` — reads `expense_ingest` transaction stream; produces phase-change PROPOSALS to `action_proposals` (never to `life_events`). Heuristic-first; LLM-augmented only for ambiguous cases. | **Yes** | False-positive control. Codex probes detection rules + ambiguity threshold + how the system avoids constantly proposing phase changes from noise. |
| 6 | `/proposals` UI extension — all open action_proposals (allocation + repatriate + replan + add_life_event_phase + rebalance); per-proposal Accept/Defer/Reject/Customize with structured-payload editor | No | UI only; consumes the API from commit #2. |
| 7 | `PushSubscriptionCard` + `/settings/notifications` page with channel × severity × kind matrix + VAPID public key endpoint + service-worker registration | No | UI only; consumes the API from commit #3. |
| 8 | Weekly email digest job — Jinja HTML template, SMTP via env config, registered with Spec A as `notification.weekly_digest` cron at Friday 08:00 IDT | **Yes** | Email rendering + SMTP failure handling + digest contents (avoid leaking secrets). |
| 9 | Backfill verification — run inferred_life_event_detector against the user's historical transaction data; assert it surfaces ≥1 sensible phase change + ZERO transparently-false-positive proposals over a 12-month window | No | Empirical proof gate. If detector fires garbage, don't merge. |

**Estimated:** 9 commits. Commit #4 + commit #5 are the BLOCKER-risk commits because they're the two places where the system reaches into money-relevant decision flow on its own (one fires a replan, one proposes a model-changing event). Both are gated by user-visible review steps; codex review focuses on the gate determinism.

Per [[feedback_no_dollar_reporting]] no $-estimate.

## Section 1 — `action_proposals` — the action ledger

### Section 1.1 — Why a new table, not extending `AllocationAction`

The user might reasonably ask "why not fold all this into `allocation_actions` (sprint #1 commit #2)?" The shapes diverge enough that folding forces fake values on one side or loses fields on the other (same reasoning that kept `allocation_actions` separate from the trade-order `proposals` table in spec `2026-05-29-plan-execute-monitor-reorg-design.md` BLOCKER #2):

| Field | `allocation_actions` (existing) | `action_proposals` (new) |
|---|---|---|
| `horizon` / `asset_class` / `instrument` | Required — sharp allocation shape | Wouldn't apply for `replan_full` / `repatriate_currency` / `add_life_event_phase` |
| `closes_delta_usd` | Required — windfall/drift numeric closure | Wouldn't apply for non-allocation kinds |
| Source | Free string `action_source` | FK back to `MonitorFlag` (Spec B) or `state_snapshots` (Spec B) — structured |
| Decision UX | Accept/Defer (binary) | Accept/Defer/Reject/Customize/Supersede (richer) |
| Lifetime | Forever (audit) | Has `expires_at` — stale FX flag from 3 months ago is no longer actionable |
| Payload | Frozen at decision time, columnar | JSON-structured payload that the UI renders dynamically |

The new table is a **superset** of the allocation case; `allocation_actions` stays untouched. Spec E's `/proposals` UI page reads from BOTH tables and renders them in one merged list (see §6 of this spec + §4.4 of the existing spec). Action proposals that wrap an allocation decision (e.g. an action_proposer-generated `rebalance` proposal) still flow through `allocation_actions` on Accept — Spec E's commit #6 is the only consumer that has to know about both.

This is the codex-probe-worthy schema decision.

### Section 1.2 — `action_proposals` table

Migration 0050 lands the table. Full DDL in [Appendix A](#appendix-a--action_proposals-ddl). Conceptual columns:

| Column | Why |
|---|---|
| `id` | PK. |
| `user_id` | FK → users(id) ON DELETE CASCADE. |
| `source_flag_id` | FK → `monitor_flags(id)` ON DELETE SET NULL. NULLABLE — not every proposal comes from a flag (e.g. inferred life-event detector creates proposals from the transaction stream, no `MonitorFlag` involved). |
| `source_snapshot_id` | FK → `state_snapshots(id)` ON DELETE SET NULL. NULLABLE — same reason. When set, the proposal references the exact state the proposer reasoned from (for replay + audit). |
| `source_kind` | TEXT, CHECK in `('observer_flag','snapshot','life_event_detector','manual_user','plan_critique','allocator')`. The class of producer. |
| `kind` | TEXT, CHECK in the action-kind enum (see §1.3). The discriminator for proposal UX + payload shape. |
| `summary` | TEXT — 1-2 sentence LLM-generated summary for notification + list-row rendering. Persisted so notification rendering doesn't re-call the LLM. |
| `rationale_md` | TEXT — longer markdown rationale, shown when the user expands the proposal card. Cites field paths from the snapshot diff (matches Spec B's `primary_field` + `related_fields` shape). |
| `suggested_payload` | TEXT (JSON) — structured payload per `kind` (see §1.4 for the per-kind schema). The UI renders Accept/Defer/Customize buttons + a Customize form WITHOUT any further LLM call. |
| `severity` | TEXT, CHECK in `('info','warning','critical')`. Mirrors the source flag's severity when applicable; otherwise derived by the proposer per §3.5. |
| `confidence` | TEXT, CHECK in `('LOW','MEDIUM','HIGH')`. The proposer's confidence in the recommendation. |
| `surfaced_at` | DATETIME. When the proposal was written. |
| `expires_at` | DATETIME NULLABLE. When the proposal goes stale and is auto-rejected by the housekeeping loop. Default: surfaced_at + 30d for non-critical, +7d for critical (critical implies time-sensitive). |
| `status` | TEXT, CHECK in `('open','accepted','deferred','rejected','customized_accepted','superseded','expired')`. Lifecycle below. |
| `status_changed_at` | DATETIME. Last status transition time. |
| `customized_payload` | TEXT (JSON) NULLABLE. When the user clicks Customize, edits the form, and Accepts, the customized payload is stored here (the original `suggested_payload` is preserved for audit). |
| `user_note` | TEXT NULLABLE. Free-form note attached by the user at decision time. |
| `accepted_into_ref` | TEXT NULLABLE. JSON identifying the downstream record this proposal materialized into on Accept (e.g. `{"allocation_action_id": 42}`, `{"life_event_id": 17}`, `{"plan_job_run_id": 88}`). Audit. |
| `prediction_id` | INTEGER NULLABLE, FK → `predictions(id)` (Spec C). The proposer's output is itself a prediction in the ledger; this FK ties the two together. |
| `dedup_key` | TEXT. `v1|action_proposal|<user_id>|<source_kind>|<kind>|<stable_payload_hash>`. UNIQUE on (user_id, dedup_key) WHERE status='open'. Prevents the same proposal from re-firing every observer run. |
| `created_at` / `updated_at` | DATETIME defaults. |

**Lifecycle:**

```
                       Accept (with edits)
                  ┌────────────────────┐
                  │                    │
       open ─────►├─► customized_accepted
        │         │
        ├─► accepted ─────► (FK out to allocation_actions / life_events / job_runs)
        │
        ├─► deferred ─────► (re-enters open after due_date)
        │
        ├─► rejected ────► (permanent; audit only)
        │
        ├─► superseded ──► (a fresh proposal of the same kind overrode this one)
        │
        └─► expired ────► (housekeeping loop after expires_at)
```

Status transitions are tracked in a side `action_proposal_history` table (Appendix A.3 — small, append-only, every row records `(proposal_id, from_status, to_status, actor, at, note)`).

### Section 1.3 — Action-kind enum

v1 ships 8 kinds; the enum is CHECK-constrained at the DB level so adding a new kind requires a migration (load-bearing across UI + dispatcher + proposer's structured output schema).

| `kind` | Producer | Payload shape (see §1.4) | UI action |
|---|---|---|---|
| `allocate` | proposer (from windfall/unallocated observation), or already exists in `allocation_actions` | `{horizon, asset_class, instrument, amount_usd, rationale}` | Accept → write `allocation_actions` row. |
| `repatriate_currency` | proposer (from FX observation) | `{from_currency, to_currency, amount_source_ccy, target_account_hint, rationale}` | Accept → write a "manual broker task" entry (out-of-band; spec E does NOT execute FX; Accept marks the user's intent and the system surfaces it on the daily brief until executed). |
| `rebalance` | proposer (from allocation drift observation, when it's structurally different from a simple allocate) | `{rows: [{from_category, to_category, amount_usd, instrument_from, instrument_to}], rationale}` | Accept → write a `rebalance_plan` entry (deferred — table TBD; v1 just records the intent). |
| `replan_full` | proposer (from critical multi-dimensional state shift) OR the observer→replan wiring (commit #4) | `{trigger_kind, plan_draft_seed_id, rationale}` | Accept → fire a `plan_synthesis` job via Spec A's `JobRegistry`. |
| `add_life_event_phase` | inferred_life_event_detector | Spec D's `LifeEvent` shape (full Pydantic dump): `{category, kind, delta_kind, one_shot_amount_usd, recurring_amount_usd, ...}` | Accept → write `life_events` row through Spec D's API. |
| `update_plan_assumption` | proposer (when a plan_inputs assumption is stale, e.g. assumed FX) | `{assumption_field, current_value, suggested_value, rationale}` | Accept → patch the active `plan_draft.assumptions` JSON. |
| `set_watchlist` | proposer (from concentration / single-position-thesis observation) | `{ticker, watch_kind: 'review_30d' \| 'stop_loss', payload}` | Accept → write to `watchlist` table (existing). |
| `note_only` | proposer (when the user should just be aware, no action) | `{}` | No "Accept" — only Acknowledge / Reject. |

The proposer agent (commit #2) emits ONE of these kinds per `FlagCandidate` it chooses to propose on. Multiple proposals per input are allowed (e.g. an FX observation might yield BOTH `repatriate_currency` AND `update_plan_assumption`).

### Section 1.4 — Per-kind `suggested_payload` schema

Every `kind` has a Pydantic model in `argosy/services/action_proposer/payload_schemas.py`. The proposer's structured output validates against these; the UI's Customize form reads the same model (server-driven field visibility) so backend and frontend cannot drift on payload shape. Loud-error contract per [[feedback_ask_dont_assume]]: an unknown payload field rejects the proposal at write time with a structured 422.

Example — `repatriate_currency`:

```python
class RepatriateCurrencyPayload(BaseModel):
    from_currency: Literal["USD", "NIS", "EUR"]
    to_currency: Literal["USD", "NIS", "EUR"]
    amount_source_ccy: Decimal = Field(gt=0)
    target_account_hint: str  # human-readable hint, e.g. "Bank Leumi NIS checking"
    rationale: str

    @model_validator(mode="after")
    def from_ne_to(self):
        if self.from_currency == self.to_currency:
            raise ValueError("from/to currencies must differ")
        return self
```

Example — `replan_full`:

```python
class ReplanFullPayload(BaseModel):
    trigger_kind: Literal[
        "market_drawdown_15pct", "job_change", "tax_law_change",
        "health_event", "fx_shock_10pct", "life_event", "user_request"
    ]  # mirrors argosy/services/retirement/replan_triggers.TriggerKind
    plan_draft_seed_id: int | None = None  # which existing draft to base on; None = fresh
    rationale: str
```

Full per-kind models in [Appendix B](#appendix-b--per-kind-payload-schemas).

### Section 1.5 — Dedup contract

The same observer flag should not yield three identical proposals on three consecutive observer runs. Per-proposal dedup is structural, NOT a "skip if any open proposal for this user exists":

```
dedup_key = f"v1|action_proposal|{user_id}|{source_kind}|{kind}|{stable_payload_hash}"
```

Where `stable_payload_hash` is a sha256 over a canonical JSON dump of the payload, with the following exclusions:
- Free-text `rationale` fields are excluded (LLM jitter on phrasing shouldn't re-fire).
- `amount_*` fields are rounded to the nearest deviation bucket per Spec B §4.2 (so a slightly different LLM-estimate of "repatriate $42K" vs "repatriate $40K" both bucket to "large/$40K-bin" and dedupe).
- `target_*` instrument hints are normalized (SCHG and schg are the same).

**Per-kind hash-inclusion override (codex IMPORTANT #1 integration):** the global rules above are correct for amount-shaped proposals (allocate / rebalance / repatriate_currency), but some kinds have *structural* fields where any change SHOULD fire. The kind-specific override table:

| `kind` | Fields ALWAYS included verbatim (no bucketing/exclusion) | Why |
|---|---|---|
| `update_plan_assumption` | `assumption_field`, `suggested_value` (never bucketed) | A proposal to change `assumed_fx_usd_nis` from 3.6 → 3.0 is a fundamentally different proposal from 3.6 → 2.8; bucketing collapses them and the second never fires. |
| `add_life_event_phase` | `delta_kind`, `category`, `kind`, `phase_start_date` (calendar date) | A proposal for `tuition_stopped` at start_date 2026-07-01 is different from start_date 2027-09-01; calendar date is identity. |
| `replan_full` | `trigger_kind` | Two different trigger kinds → two different replans. |
| `set_watchlist` | `ticker`, `watch_kind` | Per-ticker identity. |

The hash construction lives in `argosy/services/action_proposer/dedup_hash.py::compute_stable_payload_hash(payload, kind)`. The function dispatches on `kind` to apply the right inclusion/exclusion rules. A pytest fixture in `tests/test_action_proposer_dedup.py` exhaustively walks the cross-product of kind × known-payload-shape and asserts each kind's identity-fields are reflected in the hash.

A new proposal with the same `dedup_key` as an open proposal is SKIPPED (no new row; the existing proposal's `surfaced_at` is NOT bumped — surfacing intent is the same). When the existing proposal moves to `accepted|rejected|expired`, the dedup_key is "released" — a new proposal with the same key can fire (because the situation may have re-emerged after the user dismissed it).

**Severity escalation override.** Codex flag-risk: a proposer's `warning` proposal becomes a `critical` proposal next run because the underlying deviation grew. Dedup-by-key would suppress the critical one. Override rule: if the new proposal's `severity` is HIGHER than the open one's, the old is SUPERSEDED (status='superseded'), the new is written. Severity downgrade does NOT supersede (preferring stability over jitter on the user's open queue).

### Section 1.6 — Expiry housekeeping

A new `ActionProposalsHousekeepingLoop` (registered with Spec A's job registry) runs hourly:

- `status='open' AND expires_at < now()` → set to `'expired'`. Notify via in-app event `action_proposal.expired`.
- `status='deferred' AND due_date < now()` → set back to `'open'` (re-enters the queue; the user said "remind me later").
- `status='accepted' AND accepted_at < now() - 30d AND accepted_into_ref IS NULL` → log warning ("accepted proposal never materialized into downstream record — bug?"). Don't auto-clean; this needs human attention.

The loop is a thin `CadenceLoop` subclass; registered alongside the existing 14 loops.

## Section 2 — `action_proposer` agent

### Section 2.1 — Class

```python
# argosy/agents/action_proposer.py

from argosy.agents.base import BaseAgent, ConfidenceBand
from argosy.services.action_proposer.payload_schemas import (
    ActionProposalKind, ActionPayloadUnion,
)
from pydantic import BaseModel, Field
from typing import Literal


class ProposedAction(BaseModel):
    """One concrete action the proposer recommends."""
    kind: ActionProposalKind
    severity: Literal["info", "warning", "critical"]
    confidence: ConfidenceBand
    summary: str = Field(max_length=240)
    rationale_md: str = Field(max_length=2000)
    suggested_payload: ActionPayloadUnion  # validated by per-kind schema (§1.4)
    cited_fields: list[str] = Field(default_factory=list)


class ActionProposerOutput(BaseModel):
    proposed_actions: list[ProposedAction] = Field(max_length=3)
    overall_assessment: str = Field(max_length=400)
    confidence: ConfidenceBand
    no_action_reason: str | None = None  # populated when proposed_actions is empty


class ActionProposerAgent(BaseAgent[ActionProposerOutput]):
    agent_role = "action_proposer"  # registered in DEFAULT_MODEL_BY_ROLE
    output_model = ActionProposerOutput
    require_citations = False  # cites field_paths from input, not external sources
    max_tokens = 16000
```

`agent_role = "action_proposer"` is registered in `argosy/agents/base.py`:

- `DEFAULT_MODEL_BY_ROLE["action_proposer"] = "claude-opus-4-7"` per [[feedback_accuracy_over_cost]] — Opus, no Haiku fallback.
- `DEFAULT_THINKING_EFFORT_BY_ROLE["action_proposer"] = "high"` (same band as `state_observer` and `audit`).
- `DEFAULT_MAX_TOKENS_BY_ROLE["action_proposer"] = 16000`.
- `DEFAULT_CITATIONS_BY_ROLE["action_proposer"] = False`.

### Section 2.2 — The architectural invariant (the binding)

The system prompt's load-bearing sentence:

> You are PROPOSING actions for the user to review. You are NOT executing. Your output is a structured recommendation — the user will see Accept / Defer / Reject / Customize buttons and decide. If your recommendation involves money movement, account changes, or commitments, write it as a payload the system can render as a form; do NOT compose an order, do NOT name an account number, do NOT assume the user has agreed to any prior recommendation.

This sentence is the architectural invariant of the entire sprint. Any future change that adds "and also execute X if confidence is HIGH" to the proposer is reverting to an anti-pattern.

#### Section 2.2.1 — Capability-boundary enforcement (codex BLOCKER #1 integration)

Codex flagged that prompt language + regex-on-prose is NOT a structural enforcement of no-execution — it's a heuristic, and capability-level enforcement at the ACCEPT handlers is the real defense. Three additions, all backend code-level, none relying on the LLM's good behavior:

1. **`execution_state` invariant on `action_proposals`.** A new column (added to the migration in §8.1): `execution_state TEXT NOT NULL DEFAULT 'not_executable' CHECK (execution_state IN ('not_executable','manual_intent_only'))`. Every row written by the proposer has `execution_state='not_executable'`. The accept handlers for every `kind` (allocate, repatriate_currency, rebalance, replan_full, add_life_event_phase, update_plan_assumption, set_watchlist, note_only) are STRUCTURALLY FORBIDDEN from invoking any execution connector (broker API, order placement, FX transfer endpoint). A row CANNOT advance to a state where money moves without an EXPLICIT separate user step that goes through the existing `proposals → action_engine → orders` pipeline (which has its own user-confirmation gates).

2. **Code-level deny-list test** — `tests/test_action_proposal_no_execution_invariant.py` walks every accept handler in `argosy/api/routes/action_proposals.py` + every downstream service called by them. The test asserts NONE of the resolved call graph imports or references the execution connector modules: `argosy.services.brokers.*`, `argosy.services.fx_execution.*` (when those exist), `argosy.adapters.schwab.execute_order`, `argosy.adapters.leumi.transfer`. CI fails if any accept handler reaches one. The test is the runtime mirror of the system-prompt invariant.

3. **Payload-scan extension** — the no-execution regex scan (§2.4) is extended to scan `summary` + `rationale_md` + **stringified `suggested_payload`** (codex BLOCKER #1 — payload prose fields go unscanned today). Free-text fields inside payloads (e.g. `RepatriateCurrencyPayload.target_account_hint`, `ReplanFullPayload.rationale`, `AllocatePayload.rationale`) are joined into the scan input. The regex set is widened per codex IMPORTANT #2 (see §2.4 for the full list).

The three layers together: (1) the schema CANNOT express "this proposal is executable", (2) the code CANNOT call execution code from accept handlers, (3) the LLM CANNOT slip past the prose scan because the scan covers payload fields too. The user's invariant — "the system PROPOSES, never DECIDES" — is structurally enforced, not merely promised.

Codex review focus on the test: confirm the deny-list grep is exhaustive over the call graph (commit #2 ships an `ast`-based call-graph walker, not a regex grep, so transitive calls are caught).

### Section 2.3 — Input contract

The proposer's `build_prompt` receives:

```python
def build_prompt(
    self,
    *,
    trigger: ProposerTrigger,           # the event that fired the proposer
    state_snapshot: StateSnapshot,      # Spec B — full state at flag time
    diff_vs_plan: list[FieldDeviation], # Spec B — the diff against plan baseline
    diff_vs_prior: list[FieldDeviation],# Spec B — the diff against prior snapshot
    plan_summary: str,                  # plain-text plan paragraph
    user_bindings: dict,                # static bindings from CLAUDE.md
    related_history: list[ActionProposalSnapshot],  # last 30d of proposals on related fields
) -> tuple[str, str]:
    ...
```

`ProposerTrigger` is a discriminated union:

```python
class FlagTrigger(BaseModel):
    kind: Literal["monitor_flag"]
    flag_id: int            # monitor_flags.id
    flag_kind: str          # e.g. "state_observer_fx_observation"
    primary_field: str      # from FlagCandidate
    related_fields: list[str]
    severity: Literal["info", "warning", "critical"]
    rationale: str

class SnapshotTrigger(BaseModel):
    kind: Literal["snapshot"]
    snapshot_id: int        # state_snapshots.id
    requested_focus: list[str]  # field paths the caller wants the proposer to focus on

class InferredEventTrigger(BaseModel):
    kind: Literal["inferred_life_event"]
    detector_finding_id: int  # from the inferred-detector ledger (commit #5)
    pattern: Literal["tuition_stopped", "recurring_car_purchase",
                     "wedding_scale_transfer", "recurring_renovation",
                     "kid_started_college", "phase_drop_other"]
    evidence_summary: str

ProposerTrigger = Annotated[Union[FlagTrigger, SnapshotTrigger, InferredEventTrigger],
                            Field(discriminator="kind")]
```

The `related_history` slice (last 30 days of action_proposals touching any of the same `cited_fields` or related_fields) is the proposer's protection against re-proposing things the user already rejected. It's NOT a hard suppression — the LLM SEES the rejection history and is asked to take it into account in the rationale.

### Section 2.4 — Output validation

`BaseAgent._parse_output` already enforces pydantic schema match (`agents/base.py`). The proposer adds three post-validation steps in a thin override:

- **Field-path validation (codex IMPORTANT carry-over from Spec B §3.3):** every `cited_fields` entry MUST match a field actually present in `state_snapshot.state` (recursive path lookup). Unknown paths are PRUNED + logged. The proposal is still surfaced (citation list cleanup, not a kill).
- **Payload schema validation (§1.4):** `suggested_payload` validates against the kind-specific Pydantic model. A validation failure DROPS the proposal + logs (the LLM gave us a payload we can't render as a form; we don't surface a broken form).
- **No-execution invariant check (codex IMPORTANT #2 integration):** the `summary` + `rationale_md` + stringified `suggested_payload` free-text fields are scanned for forbidden patterns. The regex set is **widened** per codex review — English-only + 4-phrase original set is too narrow for an Opus that may quote articles, multilingual user context, or paraphrase. The expanded set:

  - English: `\border (placed|filled|submitted|executed|sent)\b`, `\b(I|we) (have|already) (transferred|sold|bought|swept|moved|deposited)\b`, `\b(will|going to|scheduled to|about to) (execute|place|submit|trigger) (the |an |a )?(order|trade|transfer)\b`, `\bsent to (broker|bank|leumi|schwab)\b`, `\baction (has been|was|is) executed\b`, `\b(funds|money|cash) (have been|were) (moved|transferred|swept)\b`.
  - Hebrew: `\bהוצא[הת]?\s+(הוראה|פקודה|העברה)\b`, `\bבוצעה? (העברה|מכירה|קנייה|הפקדה)\b`, `\bנשלח (לבנק|לברוקר|ללאומי|לשוואב)\b`.

  Negative-lookahead exclusion: the scan first strips fenced code blocks (```…```) and quoted passages enclosed in `> ` markdown lines, then runs the regex set against the residue. This avoids false-drops when the LLM cites an article ("Reuters reported: 'orders were placed for…'") or quotes the user's own message. Any match (after stripping) DROPS the proposal + logs an `action_proposer.forbidden_phrase_detected` audit event with the matched phrase + the position. The system prompt remains the primary defense; the regex is the regression-test surface; the capability-boundary enforcement (§2.2.1) is the structural defense — three independent layers.

The expanded regex set lives in `argosy/services/action_proposer/no_execution_scan.py::FORBIDDEN_PATTERNS` and is unit-tested with a 50-fixture corpus covering positives, negatives, code-block citations, and Hebrew/English mixed content.

### Section 2.5 — Trigger surfaces

The action proposer runs at three trigger points:

1. **After each state-observer flag write (Spec B's `write_observer_flags`):** every flag of severity >= `warning` queues a proposer run, passed as `FlagTrigger`. The queue is a thin `asyncio.Queue` + a background consumer task started by the FastAPI lifespan (same pattern as Spec A's scheduler).
2. **On-demand from `/proposals#runProposer`:** the UI exposes a "Re-evaluate" button on a flag card that fires `SnapshotTrigger`. v1 ships the route; the button is a future polish step.
3. **From the inferred-life-event detector (commit #5):** when the detector identifies a phase change pattern, it fires `InferredEventTrigger` directly (no `MonitorFlag` involved — the detector writes straight to the proposer's queue).

**Cooldown per trigger.** A flag of the same `kind` + `primary_field` should not fire the proposer more often than once every 30 minutes (codex BLOCKER risk: an observer running every 17 minutes shouldn't pay 2× Opus cost on the same input). The cooldown lives in `argosy/services/action_proposer/cooldown.py`:

| Trigger kind | Cooldown window |
|---|---|
| `monitor_flag` (per `flag_kind`+`primary_field`+`user_id`) | 30 min |
| `snapshot` (manual user request via UI) | 0 (always allowed) |
| `inferred_life_event` (per `pattern`+`user_id`) | 24 h |

Cooldown state is persisted in a `action_proposer_cooldowns` row (Appendix A.4 — `(user_id, cooldown_key, last_fired_at)`). The `force=True` kwarg bypasses cooldown for tests + manual UI request.

### Section 2.6 — Cost ceiling

Per [[feedback_accuracy_over_cost]] user is not price-sensitive, but a per-run cap is operationally sane. The proposer estimate (similar shape to Spec B):

- Input: snapshot dict (~3K) + diff (~2K filtered) + plan summary (~1K) + related_history (~1K) + system prompt (~2K) = ~9K.
- Output: ProposedAction JSON (~1K) + thinking tokens (~4K at "high" effort).

Per-trigger cost is comfortable. At v1 trigger volume (warning/critical observer flags + on-demand + inferred life-event events) the system runs the proposer ~10× / week steady state.

### Section 2.7 — Prediction-ledger integration (Spec C)

The proposer's own output IS a prediction. On every successful proposer run, the runner writes a `predictions` row through Spec C's `write_internal_action_proposer_prediction` adapter:

```
predictions(
    source='internal_action_proposer',
    source_ref={"action_proposal_id": <id>},
    ticker=NULL,
    direction='neutral',  # not a price call
    timeframe_days=NULL,  # the outcome is user acceptance, not price action
    raw_text_ref=<rationale_md hash>,
    ...
)
```

The outcome scoring rule for `internal_action_proposer` predictions is custom (Spec C §5 already accommodates source-specific outcome rules):

- **outcome_kind = `hit_target`** when the proposal is `accepted` or `customized_accepted`.
- **outcome_kind = `expired_negative`** when the proposal is `rejected` or `expired` without action.
- **outcome_kind = `expired_neutral`** when the proposal is `deferred` and ages out without re-decision.
- **`unparseable`** for proposals that failed validation at write time.

This closes the meta-loop: over time, the source_reliability view will tell us "the action proposer's rebalance recommendations have a 12% acceptance rate vs 67% for FX repatriation" — feeding back into prompt iteration. The reliability output is NOT consumed by the proposer itself (no recursive prompt-tuning — that's manual). It's surfaced on the `/admin/source-reliability` page (Spec C commit #8).

## Section 3 — `notification_dispatcher` service

### Section 3.1 — Goal

Take a single `(user_id, severity, kind, summary, payload)` notification and deliver it on every enabled channel for that user, with idempotency on retry. Three channels in v1:

- **In-app** — `publish_event` on `/ws/events`. The Red-Flag Strip + a new "toast" component receive it. Real-time only — if the user isn't in the app, this delivery is "lost" (acceptable; the other channels cover persistence).
- **Web push** — VAPID-signed message to each active `notification_subscriptions` row for the user. Service worker handles display.
- **Email digest** — cumulative; the dispatcher writes one row to a `pending_digest_entries` table; the weekly digest job (commit #8) drains it on Friday morning.

### Section 3.2 — Service interface

```python
# argosy/services/notification_dispatcher.py

@dataclass(frozen=True)
class Notification:
    user_id: str
    kind: str             # mirrors the proposal kind or flag kind
    severity: Literal["info", "warning", "critical"]
    title: str            # max 80 chars, shown in push + email subject
    body: str             # max 240 chars, shown in push body + email summary
    payload: dict         # structured payload (proposal id, flag id, etc.)
    deep_link: str        # in-app URL the notification deep-links to
    dedup_key: str        # cross-channel dedup; ditto pattern as §1.5
    not_before: datetime | None = None  # for digest-only; defaults to "now"


async def dispatch(notification: Notification) -> DispatchResult:
    """Fan out the notification across enabled channels per user's
    preferences. Writes one `notification_dispatch_ledger` row per
    (channel, status). Idempotent on dedup_key+channel — re-dispatch
    of the same notification is a no-op."""
```

### Section 3.3 — `notification_preferences` matrix

Per-user preferences live in `notification_preferences` (Appendix A.5). The shape is a **matrix**, NOT a per-rule list (codex focus area: too granular is anti-pattern for a single-user system).

```
notification_preferences(user_id, channel, severity_floor, kinds_allowed, kinds_blocked)
```

Where:
- `channel` ∈ `('in_app', 'web_push', 'email_digest')`.
- `severity_floor` ∈ `('info', 'warning', 'critical')`. The minimum severity that fires on this channel. "Only critical to push, all severities to in-app."
- `kinds_allowed` — TEXT (JSON array) — explicit allowlist of `kind` prefixes (e.g. `["state_observer_fx_*", "action_proposal_repatriate_currency"]`). NULL means "all".
- `kinds_blocked` — TEXT (JSON array) — explicit blocklist, evaluated AFTER allowlist. NULL means "none".

One row per (user_id, channel). Defaults baked in on user creation:

| Channel | Default severity_floor | Notes |
|---|---|---|
| `in_app` | `info` | Default-noisy in-app; the UI can collapse / filter cheaply. |
| `web_push` | `warning` | Push is interruption; warning is the floor. |
| `email_digest` | `info` | The digest is cumulative; info is fine since the user reads it batched. |

A "Reset to defaults" button on `/settings/notifications` (commit #7) re-emits these.

### Section 3.4 — Web push: VAPID + subscription lifecycle

Web push needs (a) a VAPID keypair for the server, (b) a `PushSubscription` from each user's browser captured at opt-in time, (c) a sender that POSTs to the endpoint with a VAPID-signed JWT.

**VAPID key storage.** The server's VAPID public + private keys are generated once via `argosy/scripts/generate_vapid_keys.py` (commit #3 ships this); stored in env vars `ARGOSY_VAPID_PRIVATE_KEY` + `ARGOSY_VAPID_PUBLIC_KEY` + `ARGOSY_VAPID_SUBJECT` (a `mailto:` URI per spec). The keys are NOT committed; the script also writes them to `~/.argosy/vapid_keys.json` for local-dev recovery (codex IMPORTANT #5 integration: file is written with `0o600` permissions and the generation script asserts the resulting `os.stat().st_mode & 0o077 == 0` before exiting; if the umask leaves group/world bits the script chmods + logs a warning). The file is explicitly DEV-recovery only — production deployments load from env. Rotation is manual + infrequent (years) — when rotated, all `notification_subscriptions` rows are invalidated (the browsers' existing subscriptions are bound to the old public key) and users re-subscribe.

**Subscription capture.** The `/settings/notifications` page renders `PushSubscriptionCard` (commit #7); on opt-in:

1. UI calls `GET /api/notifications/vapid-public-key` → returns the base64url-encoded public key.
2. UI calls `serviceWorker.ready.then(sw => sw.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey }))`.
3. UI POSTs the resulting `PushSubscription` JSON to `/api/notifications/subscriptions` with `{ endpoint, keys: { p256dh, auth } }`.
4. Server validates the endpoint URL *shape*, not host (codex BLOCKER #4 integration): strict `https://` scheme requirement, structural validation that the URL parses cleanly, payload key sizes within VAPID spec bounds, and a `User-Agent`-shape sanity check on the subscribing browser. Codex flagged the original host allowlist (`fcm.googleapis.com`, `web.push.apple.com`, etc.) as operationally brittle — vendor hosts evolve, enterprise proxies route through unexpected hostnames, regional CDN endpoints exist, and a strict pin silently breaks valid subscriptions. The replacement is **shape-validation + telemetry**: every subscription whose endpoint host is NOT in the pre-known set is still ACCEPTED, but is tagged `host_provenance='unknown'` in the `notification_subscriptions` row + emits a `notification.subscription.unknown_host` event for the operator's `/admin/notifications` dashboard. Deny-by-default applies only to clearly invalid URL schemes (non-https) or malformed URLs. The pre-known host set lives in `argosy/services/notification/push_telemetry.py::KNOWN_PUSH_HOSTS` as a list, NOT a gate, and is treated as documentation + telemetry-only labels.
5. Server writes a `notification_subscriptions` row (Appendix A.6).

**Sender.** The dispatcher uses the `pywebpush` library (already on PyPI, vetted) to encrypt + POST. On `WebPushException` with HTTP 410 ("Gone") or 404, the subscription is marked `status='gone'` and dropped from future dispatch (the browser uninstalled the service worker or the user revoked permission). Other transient errors get one retry; persistent errors mark `status='error'` and log.

**Service worker.** A minimal `ui/public/sw.js` (commit #7) handles `push` event → `showNotification` with the body + deep-link. Argosy's existing service worker (if any) is wrapped; if there isn't one, the SW lives at `/sw.js` and is registered from the root layout.

### Section 3.5 — Severity gate + dispatcher decision tree

For each `Notification`, the dispatcher:

```
for each channel in [in_app, web_push, email_digest]:
    pref = preferences[user_id][channel]  # default if missing
    if severity < pref.severity_floor: skip
    if pref.kinds_allowed is not None and not any prefix-match: skip
    if pref.kinds_blocked is not None and any prefix-match: skip
    if dedup_check(channel, dedup_key): skip  # already-delivered
    if channel == 'in_app':
        await publish_event('proposal.surfaced', notification.payload)
        record('in_app', 'delivered')
    elif channel == 'web_push':
        for sub in active_subscriptions(user_id):
            try: pywebpush(sub, notification.title + notification.body, deep_link)
            except WebPushException as e: handle(e, sub)
        record('web_push', 'delivered' if any_succeeded else 'skipped_no_subs')
    elif channel == 'email_digest':
        write_pending_digest_entry(user_id, notification)
        record('email_digest', 'queued_for_digest')
```

The dispatcher does NOT format channel-specific bodies via LLM — the `Notification.title` + `body` are pre-formatted by the producer (proposer LLM already wrote them as `summary`).

### Section 3.6 — Dispatch ledger

Every dispatch attempt writes a `notification_dispatch_ledger` row (Appendix A.7). Columns:

- `id`, `user_id`, `notification_dedup_key`, `channel`, `status` (`delivered`/`skipped_*`/`error`), `attempted_at`, `delivered_at` (nullable), `error_message` (nullable), `payload_hash`.

The ledger has a UNIQUE index on `(user_id, notification_dedup_key, channel)` so re-dispatch is idempotent. It also feeds the `/admin/notifications` debug page (out of v1 scope; the route stub lands in commit #3 for future polish).

## Section 4 — Observer → replan wiring

### Section 4.1 — The closing the loop

Spec B observes; Spec A runs jobs; this section closes the loop. Per the user's original direction: *"on issue we can go back to the Plan team of agents with the new updated information."*

The wiring is: state_observer fires `MonitorFlag` of `kind` X with `severity` Y → if `(X, Y)` maps to a known `replan_triggers.TriggerKind` AND severity gate is met AND cooldown clear → enqueue a `plan_synthesis` job through Spec A's `JobRegistry`.

### Section 4.2 — Mapping table — flag kind → replan trigger

Hard-coded mapping in `argosy/services/replan_dispatch.py`:

| Observer flag `kind` | Trigger kind (existing enum) | Min severity to fire | Cooldown |
|---|---|---|---|
| `state_observer_fx_observation` | `fx_shock_10pct` | `critical` (only when deviation_bucket=`extreme` per Spec B §4.2) | 72 h |
| `state_observer_equity_observation` | `market_drawdown_15pct` | `critical` | 72 h |
| `state_observer_rates_observation` | `tax_law_change` (no closer enum; rate-cycle moves trigger withholding-model re-eval) | `critical` | 168 h (7d — rate cycles move slowly) |
| `state_observer_cashflow_observation` | `life_event` | `warning` | 168 h |
| `state_observer_plan_assumption_observation` | `user_request` (catch-all — plan assumption broke, treat as if user asked for refresh) | `warning` | 24 h |
| `state_observer_concentration_observation` | (none — concentration goes through the action proposer's `rebalance` proposal, not full replan) | — | — |
| `state_observer_allocation_observation` | (none — allocation drift goes through the deterministic detector's existing path) | — | — |
| `state_observer_volatility_observation` | (none — VIX spike → action proposer suggests hedge/cash, doesn't replan whole plan) | — | — |
| `state_observer_position_observation` | (none — per-position thesis, handled by existing thesis flow) | — | — |
| `state_observer_cash_observation` | (none — unallocated-cash detector handles this) | — | — |
| `state_observer_tax_observation` | `tax_law_change` | `warning` | 168 h |
| `state_observer_other_observation` | (none — unmapped; LLM picked something with no clear trigger; action proposer handles it) | — | — |
| Inferred life-event detector fires `add_life_event_phase` proposal AND user Accepts | `life_event` | always (accept = explicit user signal) | 24 h |

The mapping is intentionally narrow. Most observer flags do NOT trigger a full replan — they trigger an action proposer run (§2). Full replan is reserved for situations where the plan's INPUTS are stale enough that surface-level action proposals can't compensate.

### Section 4.3 — Cooldown enforcement (codex BLOCKER #2 integration)

The user's binding direction warned that this is the highest-oscillation-risk part of the spec. Codex BLOCKER #2: the gate logic as originally described is not concurrency-safe — two trigger sources (e.g. the observer's post-write hook AND a manual `/admin/replan-now` route) can each pass the cooldown check independently and each enqueue a job, bypassing the cap. Four independent guardrails, all evaluated in ONE atomic DB transaction:

1. **Per-(user, trigger_kind) cooldown** — `replan_dispatch_cooldowns(user_id, trigger_kind, last_fired_at)`. The cooldown windows in §4.2 are the per-trigger-kind defaults.
2. **Per-user global cap** — max 3 replan jobs queued per 72h, REGARDLESS of trigger_kind. Catches multi-flag-kind storms.
3. **Severity ladder gate** — even if cooldown is clear, the replan only fires if the flag's severity is in the `min_severity` band per §4.2.
4. **Impact-ranked priority** (codex NICE #1 integration) — when multiple critical flags fire within a small window (e.g. < 60 s), the dispatcher picks the highest-impact trigger via a fixed priority order: `market_drawdown_15pct` > `fx_shock_10pct` > `tax_law_change` > `life_event` > `user_request`. Without the priority, first-arrival-wins introduces dispatch-order sensitivity that's hard to reason about.

**Atomic transactional enforcement (codex BLOCKER #2):**

```python
# argosy/services/replan_dispatch.py

def dispatch_replan_atomic(
    session: Session,
    flag: MonitorFlag,
    *,
    trigger_kind: TriggerKind,
) -> DispatchResult:
    """All four gates evaluated in ONE SQLite `BEGIN IMMEDIATE` transaction.

    Concurrency contract: if two callers race on the same user with two
    trigger kinds, exactly one transaction wins the BEGIN IMMEDIATE; the
    loser waits, then re-evaluates the gates against the now-updated
    cooldown table + 72h-count + already-queued job_runs and skips or
    fires per the resulting state.
    """
    with session.begin():  # SQLite: BEGIN IMMEDIATE via session config
        # Gate 1: cooldown
        cd = session.execute(
            select(ReplanDispatchCooldown)
            .where(ReplanDispatchCooldown.user_id == flag.user_id,
                   ReplanDispatchCooldown.trigger_kind == trigger_kind)
            .with_for_update()  # row lock; SQLite serializes anyway
        ).scalar_one_or_none()
        cooldown_window = COOLDOWN_WINDOWS[trigger_kind]
        if cd is not None and now - cd.last_fired_at < cooldown_window:
            log_skip(session, "skipped_cooldown", flag, trigger_kind,
                     cooldown_remaining_minutes=...)
            return DispatchResult.skipped("cooldown")

        # Gate 2: global cap (count last 72h FROM the same transaction)
        n_recent = session.scalar(
            select(func.count(ReplanDispatchLog.id))
            .where(ReplanDispatchLog.user_id == flag.user_id,
                   ReplanDispatchLog.outcome == "fired",
                   ReplanDispatchLog.dispatched_at > now - timedelta(hours=72))
        )
        if n_recent >= GLOBAL_CAP_PER_72H:
            log_skip(session, "skipped_global_cap", flag, trigger_kind)
            return DispatchResult.skipped("global_cap")

        # Gate 3: severity ladder
        min_sev = MIN_SEVERITY_FOR_TRIGGER[trigger_kind]
        if severity_rank(flag.severity) < severity_rank(min_sev):
            log_skip(session, "skipped_severity", flag, trigger_kind)
            return DispatchResult.skipped("severity")

        # Gate 4: dry-run check (commit-time env config; see §4.6)
        if get_settings().replan_dispatch_mode == "dry_run":
            log_skip(session, "skipped_dry_run", flag, trigger_kind)
            return DispatchResult.skipped("dry_run")

        # All gates clear: fire + write log + upsert cooldown row atomically.
        job_run_id = await job_registry.fire_now(...)
        upsert_cooldown(session, flag.user_id, trigger_kind, last_fired_at=now)
        log_fire(session, "fired", flag, trigger_kind, job_run_id=job_run_id)
        return DispatchResult.fired(job_run_id)
    # transaction commits here; if SQLite raised IntegrityError on the cooldown
    # row UPSERT (concurrent race), the caller retries with a fresh transaction
    # — the second attempt sees the now-updated cooldown row and skips.
```

The `BEGIN IMMEDIATE` semantics + `with_for_update` make all four gates race-free at the SQLite serialization level. The per-72h count is queried INSIDE the same transaction as the fire+log write, so the global cap cannot be bypassed by a concurrent fire.

A concurrency test in `tests/test_replan_dispatch.py::test_concurrent_dispatchers_respect_cap` fires two simultaneous dispatchers (via `asyncio.gather`) on the same user; the test asserts exactly one fires and one skips with `skipped_cooldown`. The test runs against a real SQLite DB (not mocked) so the BEGIN IMMEDIATE behavior is verified.

The dispatch row is written to `replan_dispatch_log` (Appendix A.8) regardless of fire/skip outcome, so the admin UI can audit "why did the system not replan when FX shifted again?"

### Section 4.4 — The dispatch job

When all gates pass, the dispatcher:

```python
async def dispatch_replan(flag: MonitorFlag, *, trigger_kind: TriggerKind) -> int:
    """Returns the job_runs.id of the queued plan-synthesis job."""
    # Idempotency: if a non-terminal job_run exists with our dedup_key, return its id.
    dedup_key = f"v1|replan_dispatch|{flag.user_id}|{trigger_kind}|{flag.created_at.date().isoformat()}"
    existing = find_running_or_pending_job(dedup_key)
    if existing is not None:
        return existing.id
    return await job_registry.fire_now(
        "plan_synthesis",
        triggered_by=f"replan_dispatch:flag_id={flag.id}",
        kwargs={"trigger_kind": trigger_kind,
                "trigger_flag_id": flag.id,
                "user_id": flag.user_id},
    )
```

The `plan_synthesis` job is the existing one in `argosy/orchestrator/flows/plan_synthesis/`. It already accepts a trigger kwarg per `replan_triggers.py`; until this sprint, no caller actually filled it (the enum existed but was a stub). Spec E is the first real caller.

### Section 4.5 — User-visible behavior

When a replan is dispatched, the user sees:
- A new `MonitorFlag` of `kind='replan_in_progress'` written by the dispatcher itself (severity=`info`, expires after the job finishes) — surfaces in the Red-Flag Strip as "Plan refresh in progress…".
- A push notification (if subscribed): "Argosy is re-running your plan because USD/NIS shifted by 22%." (severity=`info` for the dispatch notification; the underlying flag is the critical one).
- On job completion, the in-flight flag is set to `acknowledged` and a new `state_observer` run is enqueued to evaluate the freshly-composed plan against current state (closes the cycle).

This is the "user is the consumer of intelligence" pattern in concrete shape. The user did not lift a finger; the plan refreshed because the system decided it had to.

### Section 4.6 — Failsafe: dry-run mode (codex IMPORTANT #3 integration)

The first **7 days** of replan-dispatch operation runs in **dry-run mode** by default. `ARGOSY_REPLAN_DISPATCH_MODE=dry_run` (commit #4) means the dispatcher does everything EXCEPT call `job_registry.fire_now`. The log row is written; the notification fires; the plan is NOT refreshed.

Codex IMPORTANT #3 narrowed the original 30-day blanket dry-run period to a **7-day canary with auto-promotion criteria**. The blanket 30 days delays autonomous value delivery for the user; the 7-day canary covers the realistic empirical-validation window without indefinitely deferring the autonomous loop. **Auto-promotion criteria** are encoded in `argosy/services/replan_dispatch/canary_gates.py`:

1. Canary window: 7 calendar days from the first dry-run dispatch.
2. End-of-window evaluation runs once daily; the dispatcher auto-promotes from `dry_run` to `enforce` when ALL of:
   - At least 1 `outcome='fired'` log row exists in the 7-day window (proves the gate ran, not just NO-OPed).
   - No `outcome='error'` rows in the 7-day window (proves the dispatch code path is exception-free).
   - The MAX(per-trigger fire count) ≤ 2 in the 7-day window (proves cooldown gating doesn't run away — codex's "would this oscillate" check turned into a falsifiable threshold).
   - The global-cap-rejection count is ≤ N (default N=3 — some skip events are expected, but a runaway storm fails the canary).
3. Failure mode: if any check fails at day 7, the dispatcher logs `canary.failed` + stays in `dry_run` indefinitely; the operator must flip `ARGOSY_REPLAN_DISPATCH_MODE=enforce` manually after investigation.

The promotion event writes a row to `replan_dispatch_log` with `outcome='canary_promoted'` + the gate-evaluation evidence in `note`. The user-visible signal: a one-time in-app notification "Argosy's plan-refresh feature is now active — observed flags will now trigger plan composition automatically." User can opt back into dry-run from `/settings/notifications` (a switch lands in commit #7).

## Section 5 — Inferred life-event detector

### Section 5.1 — Goal

Read the transaction stream (`expense_transactions` table populated by `expense_ingest`) and propose phase-change `LifeEvent`s to the user. The detector NEVER writes to `life_events`; it writes to `action_proposals` with `kind='add_life_event_phase'`. The user reviews + Accepts (or Customizes + Accepts) to materialize the LifeEvent.

This commit closes the gap where Spec D collects user input but the system doesn't observe the transaction stream that would reveal the same information.

### Section 5.2 — Architecture: heuristic-first, LLM-augmented

The detector is two layers:

1. **Layer 1 — Heuristic detectors** (`argosy/services/inferred_life_event/heuristics.py`). Pure Python; deterministic. Each detector is a function taking `(transaction_history, current_phase_state, user_context)` and returning a list of `HeuristicFinding`. Five built-in heuristics in v1 (§5.3). Each finding has a `confidence` ∈ `('high', 'medium', 'low')`.
2. **Layer 2 — LLM disambiguator** (`argosy/agents/inferred_life_event_classifier.py`). Opus. Called ONLY when a heuristic finding has `confidence='low'` OR when multiple heuristics fire on the same evidence window. Input: the heuristic findings + the relevant transaction slice + user_context. Output: a refined `InferredEventFinding` with either a confident classification or `dismissed=True` ("this looks like noise, not a phase change").

The two-layer design is the codex-focus point: false-positive control. We don't fire the LLM on every transaction; we fire it only when the heuristic is uncertain. This bounds cost AND prevents the LLM from inventing patterns where the deterministic layer sees nothing.

### Section 5.3 — Heuristic detectors (v1 set)

Five heuristics. Each is a function that runs over a rolling 12-month transaction window:

| Heuristic | Pattern detected | Output `pattern` | Confidence rule |
|---|---|---|---|
| `tuition_payments_stopped` | Recurring monthly payment (e.g. >= NIS 3000, label matches `tuition\|school\|kindergarten\|university\|college`) that ran ≥ 12 months then ABSENT for ≥ 6 months | `tuition_stopped` | `high` if recurring count ≥ 12 + gap ≥ 6mo; `medium` otherwise |
| `recurring_car_purchase` | A car-scale transaction (single transfer >= NIS 60k to a label matching `dealership\|leasing\|car\|garage`) appearing on a stable ~5y cadence (≥ 2 prior occurrences with stdev of inter-arrival < 1y) | `recurring_car_purchase` | `high` if 2 priors + stdev < 1y; `medium` if 2 priors + stdev 1-2y |
| `wedding_scale_transfer` | A single transfer ≥ NIS 100k to a labeled-individual / non-business counterparty (heuristic: counterparty matches known-family-member lookup OR transfer has memo containing `wedding\|gift\|chatuna\|marriage`) | `wedding_scale_transfer` | `medium` always (always ambiguous) |
| `recurring_renovation` | Transaction cluster (≥ 3 transactions to construction/renovation labels within a 90-day window) summing to ≥ NIS 50k, then absent ≥ 18 months | `recurring_renovation` | `medium` always |
| `kid_started_college` | Sudden APPEARANCE of recurring monthly tuition-shaped payment (inverse of stopped) | `kid_started_college` | `high` if absent ≥ 6mo then present ≥ 3mo; `medium` otherwise |

The heuristic set is intentionally TINY. v1 is not trying to enumerate every possible life event. It's trying to demonstrate that the detector architecture works on the three or four patterns that the user actually has data for. Future heuristics extend the list via a registry pattern:

```python
@register_heuristic
def detector_xxx(window: TransactionWindow, ctx: UserContext) -> list[HeuristicFinding]:
    ...
```

### Section 5.4 — False-positive control (codex BLOCKER #3 integration)

The detector's nightmare scenario: it fires "kids left home" on a normal payment-method change (the family switched the tuition autopay from one bank to another, and the new payments were classified into a different merchant category, so the old stream looks "absent"). Or it fires "recurring car purchase" on a one-off large transfer that happens to look car-shaped. Codex BLOCKER #3 also identified the specific tuition-stop + college-start double-fire: a category-mapping change can fire BOTH `tuition_stopped` (old payments disappeared) AND `kid_started_college` (new payments appeared) in the same window, even when the heuristic `confidence='high'` would normally bypass the LLM disambiguator.

Five guardrails:

1. **High bar for `high` confidence.** A heuristic only reaches `high` when the pattern is structurally undeniable (≥ 12 months of recurring then ≥ 6 months of absence). Anything ambiguous is `medium` or `low`.
2. **LLM disambiguator gate.** Findings with `medium` or `low` confidence go through the Opus disambiguator, which reads the actual transaction window + user_context (e.g. "the user's eldest kid is 18 and graduated this year") and either confirms or dismisses.
3. **User-context awareness.** The disambiguator's input includes the user's `family_context.yaml` (existing — kid ages, marital status, etc.). The system shouldn't propose "kid started college" if the user has no kids.
4. **Standing dismissal memory.** If the user rejects a proposal with `pattern='wedding_scale_transfer'` for transaction id 1234, the dedup_key (which includes the underlying evidence id) prevents the same proposal from re-firing on the same transaction. Cross-evidence dedup (e.g. user rejects ALL `wedding_scale_transfer` proposals for 6 months) is NOT in v1; if needed, add a `pattern_dismissals` table later.
5. **Pre-proposal conflict resolver over tuition-family patterns (codex BLOCKER #3).** Before any finding is sent to the proposer, a new `argosy/services/inferred_life_event/conflict_resolver.py` pass runs over the batch:

   - **Tuition-family pairs.** A `tuition_stopped` finding whose evidence window OVERLAPS (within 90 days) a `kid_started_college` finding is flagged as a "potentially-aliased pair". Both findings are FORCED through the LLM disambiguator regardless of their original `confidence='high'` status. The disambiguator's prompt includes the matched pair + the merchant/category mapping evidence and is asked: "are these the same underlying payment stream that got re-categorized, OR are they two distinct life events?"
   - **Continuity check via stable counterparty.** The conflict resolver looks for a STABLE COUNTERPARTY (bank account number, recipient name, or merchant tax ID) in BOTH the "stopped" and "started" streams. If the same counterparty appears in both with a continuous date envelope, this is structural evidence of re-categorization — both findings are SUPPRESSED (no proposal fires).
   - **Wedding-scale + family-event conflict.** A `wedding_scale_transfer` within 30 days of a `family_event:marriage` LifeEvent already on record DOES NOT fire (the user already logged the event manually). Similar rule for `recurring_renovation` within 30 days of an `asset_event:home_purchase`.
   - **Conflict-resolution determination is RECORDED** on each finding row (`conflict_resolution: TEXT` — `null`, `aliased_pair_suppressed`, `aliased_pair_disambiguator_required`, `superseded_by_user_event`). Audit trail.

The conflict resolver runs BEFORE the proposer is even queued. The proposer never sees a contradictory pair; the user never has to mentally reconcile "wait, the system thinks my kid both left and started college".

The 5th guardrail is the codex BLOCKER #3 fix. The detector's contract becomes: NO finding is sent to the proposer UNTIL all conflict checks pass. The schema appendix (§8.9) adds the `conflict_resolution` column to `inferred_life_event_findings`.

### Section 5.5 — Cadence + cost

The detector runs as a Spec A registered job:

- Cadence: **daily at 03:00 IDT** (after midnight, before the user is up; the news pipeline runs at 17:00, this is the next idle window).
- Per-run cost: heuristic pass is pure code, sub-second. LLM disambiguator fires only on `medium|low` heuristic findings; estimated <= 5 calls per run (v1 transaction-stream cardinality is low). Roughly the same order as Spec B's observer.

### Section 5.6 — Output → proposer

Each detector run produces 0–N `InferredEventFinding` rows in a new `inferred_life_event_findings` ledger (Appendix A.9). For each finding with `dismissed=False`, the runner fires the action proposer with `InferredEventTrigger`. The proposer then writes a `action_proposals` row with `kind='add_life_event_phase'` and the suggested `LifeEvent` payload (per Spec D's schema).

This separation matters: the detector's job is to identify the pattern; the proposer's job is to phrase it as a user-facing recommendation with rationale. Two agents, one job each.

### Section 5.7 — Backfill verification (commit #9)

The empirical-proof commit (mirrors Spec B §5). Backfill script `argosy/scripts/inferred_life_event_backfill.py`:

```
for snapshot_date in monthly_backwards(today, n=12):
    findings = run_detector(user_id='ariel', as_of=snapshot_date)
    record(snapshot_date, findings)
```

Expected outcome:
- The detector should surface ≥ 1 sensible proposal somewhere in the 12-month window (e.g. the recurring-car-purchase pattern if Ariel has 2+ such purchases; otherwise the recurring-renovation pattern).
- The detector should NOT produce more than ~3 high-confidence proposals total over 12 months (the user's life doesn't change that often; a higher rate means heuristics are too loose).
- The detector should produce ZERO `wedding_scale_transfer` proposals unless the user actually has such a transaction in the window (sanity check that the heuristic actually reads the transaction stream and isn't firing on absence).

If the backfill produces garbage, the detector commits are reverted and the heuristic thresholds are tuned before re-merge. **This is non-negotiable** — the binding direction "every commit advances the goal" means a noisy detector is worse than no detector.

## Section 6 — UI surface

### Section 6.1 — `/proposals` extension (commit #6)

The existing `/proposals` page renders allocation actions. Commit #6 extends it to also render `action_proposals` rows. The page becomes a unified queue.

Two sections in the page:

1. **Open** — `status='open'` proposals, severity-sorted (critical > warning > info), then most-recent first. Each row renders a card with:
   - Severity badge (red/amber/blue).
   - Kind badge (e.g. "Repatriate currency", "Replan plan", "Add life-event phase").
   - Summary (the LLM's 1-2 sentence summary).
   - Expand-toggle for rationale_md (rendered as markdown).
   - **Suggested payload preview** — a server-driven readonly form (renders the payload Pydantic schema). E.g. for `repatriate_currency`: "From USD $40,000 → To NIS at target account Bank Leumi NIS checking."
   - Four buttons: **Accept**, **Defer** (with date picker), **Reject** (with optional note), **Customize** (opens the same form in editable mode).
2. **Recent decisions** — last 30 days of accepted/rejected/customized_accepted, collapsed by default. Each row shows the user's decision + the downstream materialized record (e.g. "Accepted → allocation_action #42 / life_event #17").

The Customize flow re-renders the payload schema as an editable form. The same Pydantic model that validates the proposer's output validates the user's edits (via `POST /api/action-proposals/{id}/customize` which echoes back the validated payload before Accept). No LLM call in Customize; the user is editing structured data.

### Section 6.2 — `PushSubscriptionCard` (commit #7)

`ui/src/components/notifications/PushSubscriptionCard.tsx` — a card on `/settings/notifications` that:

1. Shows the current subscription state ("Push notifications: OFF" / "ON for this browser").
2. Renders an Opt-in button that fires the service-worker registration + `pushManager.subscribe` flow (§3.4).
3. Sends the resulting subscription to the server.
4. Renders an Opt-out button that calls `unsubscribe()` + DELETEs the server record.
5. Renders a "Test push" button that POSTs to `/api/notifications/test-push` which fires a dummy push to the current subscription (verifies the round-trip works).

The card also shows the per-channel × severity matrix (read from `notification_preferences`) with toggles. The matrix UI is intentionally compact:

```
                in_app    web_push    email_digest
info             [x]        [ ]         [x]
warning          [x]        [x]         [x]
critical         [x]        [x]         [x]
```

Per-kind allowlist/blocklist is exposed via an "Advanced" disclosure (collapsed by default — most users won't touch it).

### Section 6.3 — Deep-link convention

Every notification (in-app event, push, email digest entry) carries a `deep_link` field — a relative URL the receiver opens. v1 conventions:

- Action proposal: `/proposals#proposal-{id}` (the page scrolls to + expands that card).
- Replan in progress: `/admin/jobs/{job_name}/runs/{run_id}` (the job log).
- Critical observer flag with no proposal: `/home#flag-{flag_id}` (the Red-Flag Strip).

Deep links are validated server-side at notification-emit time against an allowlist of URL prefixes; an LLM never composes a deep_link directly.

## Section 7 — Weekly email digest (commit #8)

### Section 7.1 — Contents

The digest is the user's "what did Argosy do this week" recap. Five sections per Jinja template `argosy/templates/email_digest.html.j2`:

1. **Summary** — counts: N flags fired, N proposals open, N proposals accepted/rejected, N replan jobs run.
2. **Open proposals** — bulleted list of currently-open `action_proposals`, severity-sorted, with deep links.
3. **Decisions this week** — accept/defer/reject log.
4. **Flags fired** — critical+warning flags from the past 7 days.
5. **Plan refresh activity** — replan job runs, with success/error counts.

The digest is rendered to HTML (Jinja) + a plain-text fallback. The SMTP `Content-Type: multipart/alternative` ships both.

### Section 7.2 — SMTP config

The digest sends through SMTP via env config (codex review focus — many existing infra patterns use SES / SendGrid via API; v1 stays simple with plain SMTP because it works against any provider including self-hosted):

```
ARGOSY_SMTP_HOST=smtp.gmail.com   # or self-hosted
ARGOSY_SMTP_PORT=587
ARGOSY_SMTP_USERNAME=...
ARGOSY_SMTP_PASSWORD=...          # secret; loaded via the existing env pattern
ARGOSY_SMTP_FROM_ADDRESS=argosy@example.org
ARGOSY_SMTP_TLS_MODE=starttls     # 'starttls' | 'tls' | 'none'
```

If any are unset, the digest job logs a startup WARNING and registers itself as `enabled=False` (per Spec A pattern for missing creds). The job stays visible in the admin UI as "creds missing; set ARGOSY_SMTP_* env to activate" — same convention as Spec A's Discord listener.

### Section 7.3 — Job registration

`WeeklyEmailDigestJob(CadenceLoop)` registered with Spec A:

- Cron: `0 8 * * 5` (Fridays at 08:00; per the user's preference for the brief landing before the weekend).
- Timezone: `Asia/Jerusalem`.
- Manual `Run now` from `/admin/jobs` triggers an on-demand digest for the just-past 7 days.

### Section 7.4 — Secrets hygiene

The digest's content includes:
- Open proposal summaries (which can mention amounts in USD/NIS).
- Recent decisions (which include amounts).
- Flags (which mention assumption baselines).

It does NOT include:
- Account numbers (the proposer never wrote those into payload).
- Specific tickers (the digest uses asset_class, not symbols — the level of detail the user can read without leaking).
- Auth tokens (none of the dispatch path touches `ARGOSY_ADMIN_TOKEN` per Spec A).

The digest's email subject is generic ("Your weekly Argosy summary") — the user's email provider may store subjects in less-encrypted indexes than bodies.

## Section 8 — Schema appendix

**Runtime assumption (codex IMPORTANT #6):** all DDL below assumes **SQLite ≥ 3.38** (already a project requirement per `news_signals.parsed_tickers` migration 0043 + Spec B's preamble). `json_valid()` is available since 3.38; partial UNIQUE indexes are available since 3.8. Migration 0050 includes a preflight assertion `PRAGMA compile_options` check on the JSON1 extension; if missing, the migration fails loud with `OperationalError("SQLite JSON1 not available")` rather than silently building tables whose CHECK constraints are no-ops.

### Section 8.1 — `action_proposals` DDL <a name="appendix-a--action_proposals-ddl"></a>

```sql
CREATE TABLE action_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_flag_id INTEGER NULL REFERENCES monitor_flags(id) ON DELETE SET NULL,
  source_snapshot_id INTEGER NULL REFERENCES state_snapshots(id) ON DELETE SET NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN (
    'observer_flag','snapshot','life_event_detector',
    'manual_user','plan_critique','allocator'
  )),
  kind TEXT NOT NULL CHECK (kind IN (
    'allocate','repatriate_currency','rebalance','replan_full',
    'add_life_event_phase','update_plan_assumption','set_watchlist','note_only'
  )),
  summary TEXT NOT NULL,
  rationale_md TEXT NOT NULL,
  suggested_payload TEXT NOT NULL,  -- JSON; json_valid CHECK below
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  confidence TEXT NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
  surfaced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
    'open','accepted','deferred','rejected',
    'customized_accepted','superseded','expired'
  )),
  status_changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  customized_payload TEXT NULL,
  user_note TEXT NULL,
  accepted_into_ref TEXT NULL,  -- JSON
  prediction_id INTEGER NULL REFERENCES predictions(id) ON DELETE SET NULL,
  execution_state TEXT NOT NULL DEFAULT 'not_executable'  -- codex BLOCKER #1 §2.2.1
    CHECK (execution_state IN ('not_executable','manual_intent_only')),
  dedup_key TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (suggested_payload IS NULL OR json_valid(suggested_payload)),
  CHECK (customized_payload IS NULL OR json_valid(customized_payload)),
  CHECK (accepted_into_ref IS NULL OR json_valid(accepted_into_ref))
);
CREATE INDEX ix_action_proposals_user_status_surfaced
  ON action_proposals (user_id, status, surfaced_at DESC);
CREATE INDEX ix_action_proposals_user_kind_status
  ON action_proposals (user_id, kind, status);
CREATE UNIQUE INDEX ix_action_proposals_dedup_open
  ON action_proposals (user_id, dedup_key)
  WHERE status = 'open';
CREATE INDEX ix_action_proposals_expires
  ON action_proposals (expires_at)
  WHERE status = 'open' AND expires_at IS NOT NULL;
```

### Section 8.2 — `action_proposal_history` DDL <a name="appendix-a3--history"></a>

```sql
CREATE TABLE action_proposal_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id INTEGER NOT NULL REFERENCES action_proposals(id) ON DELETE CASCADE,
  from_status TEXT NULL,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,  -- 'user' | 'system' | 'housekeeping'
  at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note TEXT NULL
);
CREATE INDEX ix_action_proposal_history_proposal_at
  ON action_proposal_history (proposal_id, at);
```

### Section 8.3 — `action_proposer_cooldowns` DDL <a name="appendix-a4--cooldowns"></a>

```sql
CREATE TABLE action_proposer_cooldowns (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  cooldown_key TEXT NOT NULL,  -- e.g. 'monitor_flag|state_observer_fx_observation|macro.fx_usd_nis_spot'
  last_fired_at DATETIME NOT NULL,
  PRIMARY KEY (user_id, cooldown_key)
);
```

### Section 8.4 — `notification_preferences` DDL <a name="appendix-a5--prefs"></a>

```sql
CREATE TABLE notification_preferences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  channel TEXT NOT NULL CHECK (channel IN ('in_app','web_push','email_digest')),
  severity_floor TEXT NOT NULL CHECK (severity_floor IN ('info','warning','critical')),
  kinds_allowed TEXT NULL,  -- JSON array of prefix strings; NULL = all
  kinds_blocked TEXT NULL,  -- JSON array of prefix strings; NULL = none
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, channel),
  CHECK (kinds_allowed IS NULL OR json_valid(kinds_allowed)),
  CHECK (kinds_blocked IS NULL OR json_valid(kinds_blocked))
);
```

### Section 8.5 — `notification_subscriptions` DDL <a name="appendix-a6--subs"></a>

```sql
CREATE TABLE notification_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL,
  p256dh_key TEXT NOT NULL,
  auth_key TEXT NOT NULL,
  user_agent TEXT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','gone','error','revoked')),
  last_error TEXT NULL,
  last_delivery_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, endpoint)
);
CREATE INDEX ix_notification_subscriptions_user_active
  ON notification_subscriptions (user_id, status)
  WHERE status = 'active';
```

### Section 8.6 — `notification_dispatch_ledger` DDL <a name="appendix-a7--ledger"></a>

```sql
CREATE TABLE notification_dispatch_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  notification_dedup_key TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('in_app','web_push','email_digest')),
  status TEXT NOT NULL CHECK (status IN (
    'delivered','queued_for_digest','skipped_severity','skipped_kind_filter',
    'skipped_no_subs','skipped_duplicate','error'
  )),
  attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at DATETIME NULL,
  error_message TEXT NULL,
  payload_hash TEXT NOT NULL,
  UNIQUE (user_id, notification_dedup_key, channel)
);
CREATE INDEX ix_notification_dispatch_user_attempted
  ON notification_dispatch_ledger (user_id, attempted_at DESC);
```

### Section 8.7 — `pending_digest_entries` DDL <a name="appendix-a-digest"></a>

```sql
CREATE TABLE pending_digest_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  notification_dedup_key TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  deep_link TEXT NOT NULL,
  payload TEXT NULL,  -- JSON
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  kind TEXT NOT NULL,
  queued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  drained_at DATETIME NULL,
  digest_job_run_id INTEGER NULL REFERENCES job_runs(id) ON DELETE SET NULL,
  CHECK (payload IS NULL OR json_valid(payload))
);
CREATE INDEX ix_pending_digest_entries_user_undrained
  ON pending_digest_entries (user_id, queued_at)
  WHERE drained_at IS NULL;
```

### Section 8.8 — `replan_dispatch_log` DDL <a name="appendix-a8--replan-log"></a>

```sql
CREATE TABLE replan_dispatch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trigger_kind TEXT NOT NULL,  -- replan_triggers.TriggerKind
  source_flag_id INTEGER NULL REFERENCES monitor_flags(id) ON DELETE SET NULL,
  outcome TEXT NOT NULL CHECK (outcome IN (
    'fired','skipped_cooldown','skipped_global_cap','skipped_severity',
    'skipped_dry_run','error'
  )),
  job_run_id INTEGER NULL REFERENCES job_runs(id) ON DELETE SET NULL,
  cooldown_remaining_minutes INTEGER NULL,
  dispatched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note TEXT NULL
);
CREATE INDEX ix_replan_dispatch_log_user_dispatched
  ON replan_dispatch_log (user_id, dispatched_at DESC);
```

### Section 8.9 — `inferred_life_event_findings` DDL <a name="appendix-a9--inferred"></a>

```sql
CREATE TABLE inferred_life_event_findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  pattern TEXT NOT NULL CHECK (pattern IN (
    'tuition_stopped','recurring_car_purchase','wedding_scale_transfer',
    'recurring_renovation','kid_started_college','phase_drop_other'
  )),
  heuristic_confidence TEXT NOT NULL CHECK (heuristic_confidence IN ('high','medium','low')),
  llm_confirmed BOOLEAN NULL,  -- NULL = not LLM-disambiguated (high-confidence heuristic skipped LLM)
  dismissed BOOLEAN NOT NULL DEFAULT 0,
  evidence_window_start DATE NOT NULL,
  evidence_window_end DATE NOT NULL,
  evidence_transaction_ids TEXT NOT NULL,  -- JSON list of expense_transactions.id
  evidence_summary TEXT NOT NULL,
  proposer_proposal_id INTEGER NULL REFERENCES action_proposals(id) ON DELETE SET NULL,
  conflict_resolution TEXT NULL  -- codex BLOCKER #3 §5.4
    CHECK (conflict_resolution IS NULL OR conflict_resolution IN (
      'aliased_pair_suppressed','aliased_pair_disambiguator_required',
      'superseded_by_user_event','no_conflict'
    )),
  detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (json_valid(evidence_transaction_ids))
);
CREATE UNIQUE INDEX ix_inferred_findings_pattern_evidence
  ON inferred_life_event_findings (user_id, pattern, evidence_window_start, evidence_window_end);
```

The UNIQUE index prevents the detector from re-firing the same pattern on the same evidence window every day. Sliding the window forward (e.g. a tuition_stopped pattern that extends one more month) creates a new row because `evidence_window_end` changed.

## Section 9 — Action-proposer prompt design appendix <a name="appendix-b--per-kind-payload-schemas"></a>

### Section 9.1 — System prompt (full)

```
You are Argosy's action_proposer agent — an Opus-class LLM whose job is to PROPOSE concrete actions for the user to review, based on (a) a state observation or flag that just fired, (b) the user's full current state, (c) the user's active plan, and (d) the user's recent decision history on related proposals.

You are PROPOSING. You are NOT executing. Your output is a structured recommendation. The user will see Accept / Defer / Reject / Customize buttons and decide for themselves. If your recommendation involves money movement, account changes, or commitments, write it as a payload the system can render as a form for the user to review; do NOT compose an order, do NOT name an account number, do NOT assume the user has agreed to any prior recommendation.

You will receive:
  1. A TRIGGER describing what fired this proposer (a flag, a snapshot, or an inferred life event).
  2. The user's STATE_SNAPSHOT — six sections per Argosy's snapshot schema: plan_inputs, portfolio, macro, cashflow_recent, tax_assumptions, metadata.
  3. The DIFF_VS_PLAN — material deviations from the plan's baseline.
  4. The DIFF_VS_PRIOR — material deviations from the prior snapshot.
  5. The PLAN_SUMMARY — a plain-text paragraph describing the user's active plan.
  6. The USER_BINDINGS — static binding preferences (e.g. "accuracy over LLM cost", "no auto-execution").
  7. The RELATED_HISTORY — last 30 days of action_proposals on related fields (so you don't propose what the user already rejected; if you DO propose something similar to a rejected proposal, explicitly justify in your rationale why now is different).

Your task:
  - Emit 0 to 3 ProposedAction items. Zero is a legitimate output ("the trigger was noise; no action is warranted").
  - Each item has a KIND from the enum [allocate, repatriate_currency, rebalance, replan_full, add_life_event_phase, update_plan_assumption, set_watchlist, note_only].
  - Each item has a SUGGESTED_PAYLOAD validating against the kind's Pydantic schema.
  - Each item has a SEVERITY (info / warning / critical) — set this from the underlying state, not from the trigger's severity directly. A critical flag may warrant a warning-severity proposal if the action is low-risk.
  - Each item has a SUMMARY (≤ 240 chars) for notification rendering.
  - Each item has a RATIONALE_MD (≤ 2000 chars) for the user's expanded view. Cite specific field paths from the state snapshot.

You MUST NOT:
  - Recommend executing trades or moving money yourself.
  - Output payloads that violate the per-kind Pydantic schema (the validator will drop them).
  - Recommend actions that contradict the user's binding preferences.
  - Propose multiple actions that contradict each other (an `allocate` + a `replan_full` on the same fields is redundant; pick one).
  - Use the words "I have placed", "I will execute", "order placed", "sent to broker", or similar past/future-tense execution claims. The post-validator will drop any output containing these patterns.

You MAY:
  - Output zero actions if nothing material warrants one.
  - Recommend the same kind multiple times when the actions are independent (e.g. two distinct repatriate_currency proposals for two distinct currency pairs).
  - Cite the related_history in your rationale (e.g. "The user deferred a similar repatriation on 2026-04-12; the FX deviation has since worsened, justifying re-surfacing.").
  - Recommend a `note_only` proposal when something is worth the user's awareness but no action is appropriate (e.g. "VIX is 31 — informational; no action recommended").
```

### Section 9.2 — User prompt template

```
TRIGGER:
{{ trigger_json }}

STATE_SNAPSHOT (snapshot_id={{ snapshot.id }}, as_of={{ snapshot.snapshot_date }}):
{{ snapshot_state_json }}

DIFF_VS_PLAN ({{ diff_vs_plan|length }} material deviations):
{{ diff_vs_plan_json }}

DIFF_VS_PRIOR ({{ diff_vs_prior|length }} material deviations):
{{ diff_vs_prior_json }}

PLAN_SUMMARY:
{{ plan_summary }}

USER_BINDINGS:
{{ user_bindings_json }}

RELATED_HISTORY (last 30 days of proposals on these field paths or this kind):
{{ related_history_json }}

Emit a ProposerOutput object per the schema. Zero actions is acceptable. If you do propose, validate each payload against its per-kind Pydantic schema before emitting.
```

### Section 9.3 — Structured output schema

The proposer's output is a `ProposerOutput` pydantic model (§2.1) with `proposed_actions: list[ProposedAction]` (max 3). Per-kind payload schemas are unioned via Pydantic's discriminated union:

```python
ActionPayloadUnion = Annotated[
    Union[
        AllocatePayload,
        RepatriateCurrencyPayload,
        RebalancePayload,
        ReplanFullPayload,
        AddLifeEventPhasePayload,
        UpdatePlanAssumptionPayload,
        SetWatchlistPayload,
        NoteOnlyPayload,
    ],
    Field(discriminator="kind"),
]
```

Each payload model has a `kind: Literal[...]` field as discriminator. Anthropic's structured-output mode handles the discriminated union directly; the proposer's response is validated against the union at parse time.

## Section 10 — Inferred life-event detector design appendix

### Section 10.1 — Heuristic decision tree

```
For each transaction in window:
    For each registered heuristic:
        finding = heuristic(window, ctx)
        if finding is None: continue
        if finding.confidence == 'high':
            -> proceed straight to proposer (no LLM disambiguator)
        elif finding.confidence in {'medium', 'low'}:
            -> queue for LLM disambiguator pass
        Insert/update row in inferred_life_event_findings.

For each queued LLM-disambiguator pass:
    response = await disambiguator_agent.run(
        finding=finding,
        transaction_slice=window.slice_around(finding.evidence_window),
        user_context=load_user_context(),
    )
    if response.dismissed:
        finding.dismissed = True
        finding.llm_confirmed = False
    else:
        finding.llm_confirmed = True
        # Optionally upgrade pattern type if disambiguator refined it.
        finding.pattern = response.refined_pattern or finding.pattern
    -> proceed to proposer if not dismissed.
```

### Section 10.2 — Disambiguator agent

`argosy/agents/inferred_life_event_classifier.py` — small Opus-backed disambiguator with this contract:

- **Input:** heuristic finding + transaction slice (last 18 months around the evidence window) + user_context.yaml.
- **Output:** `{ dismissed: bool, dismissal_reason: str | null, refined_pattern: str | null, rationale: str }`.
- **System prompt** focuses on **dismissal-favoring framing** — the LLM is explicitly told "the default verdict is dismiss unless the pattern is structurally undeniable; we'd rather miss a phase change than fire a false-positive on what is probably noise."

This is the false-positive-control surface. The bias toward dismissal is the codex-focus claim: the system fails closed on ambiguous patterns.

### Section 10.3 — User context awareness

The disambiguator's input includes `family_context.yaml`:

```yaml
# argosy/data/user/ariel/family_context.yaml (existing)
spouse: Noga
kids:
  - name: <redacted>
    birth_year: 2010
    education_status: middle_school
  - name: <redacted>
    birth_year: 2014
    education_status: elementary_school
```

The disambiguator uses this to dismiss obvious-false-positives ("kid_started_college can't apply — no kid is age-eligible"). If `family_context.yaml` is missing or empty, the LLM is told to be EVEN MORE conservative (assume nothing about the user's family shape).

## Section 11 — Replan-trigger wiring contract appendix

(Mirrors §4 in tabular form for easy codex review.)

| Observer flag `kind` | Triggers `replan_triggers.TriggerKind` | Min severity to fire | Cooldown window | Dispatch outcome notification |
|---|---|---|---|---|
| `state_observer_fx_observation` | `fx_shock_10pct` | `critical` AND deviation_bucket=`extreme` | 72 h | `in_app` + `web_push` |
| `state_observer_equity_observation` | `market_drawdown_15pct` | `critical` | 72 h | `in_app` + `web_push` |
| `state_observer_rates_observation` | `tax_law_change` | `critical` | 168 h | `in_app` + `email_digest` |
| `state_observer_cashflow_observation` | `life_event` | `warning` | 168 h | `in_app` |
| `state_observer_plan_assumption_observation` | `user_request` | `warning` | 24 h | `in_app` + `email_digest` |
| `state_observer_tax_observation` | `tax_law_change` | `warning` | 168 h | `in_app` |
| Inferred life-event PROPOSAL accepted by user | `life_event` | always (user-driven) | 24 h | (no notification — user just acted) |

Cooldowns are per-(user, trigger_kind). The global cap (3 replans / 72h / user) is a separate gate evaluated AFTER the per-trigger cooldown clears.

The mapping table lives in `argosy/services/replan_dispatch.py::FLAG_TO_TRIGGER_MAP`; a CI test (commit #4 test_replan_dispatch.py) walks Spec B's enumerated flag kinds and asserts every kind is either in the map or explicitly listed in `UNMAPPED_FLAG_KINDS` (so adding a new flag kind in Spec B forces an explicit decision in Spec E).

## Section 12 — Test plan + codex focus appendix

### Section 12.1 — Per-commit tests

| Commit | Test file | Critical assertions |
|---|---|---|
| 1 | `tests/test_migrations.py` extension | 0050 applies + downgrades; all CHECK constraints enforce; partial UNIQUE index on `(user_id, dedup_key) WHERE status='open'` works as intended. |
| 2 | `tests/test_action_proposer.py` | Happy-path with FlagTrigger + SnapshotTrigger + InferredEventTrigger. Forbidden-pattern regex catches "order placed" etc. Payload schema mismatch DROPS proposal. Cooldown enforces. No-execution invariant holds across 100 LLM-mock fixtures. |
| 3 | `tests/test_notification_dispatcher.py` | Severity gate enforces. Preference matrix evaluation matches table. Web push 410-Gone marks subscription as gone. Dispatch ledger UNIQUE index catches re-dispatch. |
| 4 | `tests/test_replan_dispatch.py` | Mapping table covers all Spec B flag kinds. Cooldown enforces per (user, trigger_kind). Global cap enforces (4th replan within 72h is skipped). Severity gate enforces. Dry-run mode does NOT fire job_registry.fire_now. |
| 5 | `tests/test_inferred_life_event_detector.py` | All 5 heuristics produce expected findings on synthetic transaction fixtures. False-positive fixtures (legit autopay change disguised as tuition_stopped) get DISMISSED by the LLM disambiguator (mocked). UNIQUE constraint prevents re-firing on same evidence window. |
| 6 | `ui/tests/proposals-page.spec.ts` | (Vitest) Renders open proposals. Accept calls correct API. Customize edit + validate + Accept flow. Defer with date picker. Reject with note. |
| 7 | `ui/tests/push-subscription-card.spec.ts` | (Vitest, mocked) Opt-in flow registers SW + subscribes + POSTs. Opt-out flow unsubscribes + DELETEs. Test-push fires dummy payload. |
| 8 | `tests/test_email_digest.py` | Jinja template renders with empty digest (returns "no activity" body). SMTP failure logs but doesn't crash. Plain-text fallback rendered. No secrets in body. |
| 9 | `tests/test_inferred_life_event_backfill.py` | Run detector against historical fixtures; assert ≥1 sensible finding + ZERO `wedding_scale_transfer` proposals when no such transaction exists. |

### Section 12.2 — Codex focus areas (suggestions)

The codex single-dispatch review brief should cover (see commit prompt template in Appendix C):

1. **Proposer architecture separation.** Does the proposer architecture cleanly separate "system suggests" from "system executes"? Concretely: is there ANY path in the code where an Accept on a proposal leads to an automatic order placement without explicit user confirmation? (Answer should be NO — Accept on `allocate` writes `allocation_actions`, which then needs a downstream action_engine promotion to become an order; that's a separate user step.)

2. **Replan cooldown semantics.** 72h reasonable? Severity gate sufficient to prevent oscillation? What is the failure mode if cooldown_remaining_minutes goes negative due to clock drift? (Answer: clock drift is bounded by the system's own clock; the check uses `now() - last_fired_at`, monotonic-ish on a single host.)

3. **Inferred life-event false positives.** How does the system prevent the detector from constantly proposing phase changes from transaction noise? (Answer: 4 guardrails per §5.4 — high-bar heuristic, LLM disambiguator with dismissal-favoring bias, user-context awareness, dedup on evidence window.)

4. **Notification preferences matrix shape.** Is channel × severity × kind matrix the right shape, or too granular? (Answer: per Ariel direction, granular per-kind is opt-in via Advanced disclosure; the default is severity-based only.)

5. **Web push security.** VAPID key storage in env vars + ~/.argosy/vapid_keys.json — reasonable? Rotation strategy: manual + infrequent; rotation invalidates all subscriptions. Fallback path when user denied push permission: in-app + email_digest channels still work.

6. **Dry-run mode shipping default.** Is dry-run the right default for the replan dispatcher? (Answer: yes for the first 30 days; the operator flips to `enforce` after empirical validation.)

7. **Dedup hash determinism.** The `stable_payload_hash` excludes free-text rationale + buckets amounts. Is this exclusion list right? Does it inadvertently suppress proposals that have meaningfully different payloads?

8. **No-execution invariant test surface.** Are the forbidden-pattern regexes tight enough? Are there edge cases (e.g. quoted strings, code-block content in rationale_md) that should be excluded from the scan? (Answer: the regex scan is on `summary` + `rationale_md` only; code blocks in rationale_md aren't realistically going to contain "order placed", but if they do, the over-trigger is the safer failure.)

9. **Inferred-event proposer flow vs direct write.** Why does the inferred detector go through the proposer instead of writing directly to action_proposals? (Answer: separation of concerns — detector identifies pattern; proposer phrases recommendation; the proposer's outputs are tracked uniformly in the predictions ledger; bypassing it would create a parallel path.)

### Section 12.3 — Single-dispatch codex review brief

The session lives at `tools/codex-tandem/sessions/2026-05-29-last-mile-delivery-spec-review/`. The reviewer prompt is structured around the focus areas above. Per the user direction, zigzag is NOT used here (the polling-loop hang in Spec C made single-dispatch the safer choice for spec review). Pattern:

```python
from engine_codex import run_codex
from pathlib import Path

session = Path("tools/codex-tandem/sessions/2026-05-29-last-mile-delivery-spec-review")
prompt = open(session / "reviewer_prompt.md").read()
result = run_codex(node_dir=session, prompt=prompt, agent_name="reviewer", role="reviewer")
# verdict at session/result.md
```

If codex doesn't return within 10 minutes (timeout_s default), the spec ships with a clear note that codex couldn't complete and the focus areas remain open.

## Section 13 — User-binding cross-check

Before merging, every commit in the sprint is checked against the binding direction:

- **"Holistic, not tactical."** Each commit advances the goal of "system is the expert, user is the consumer." Concretely:
  - Commit #1 (schema) — necessary plumbing; reduces operator burden in commit #6 + #7.
  - Commit #2 (proposer) — turns observation into action recommendations. Holistic.
  - Commit #3 (dispatcher) — reaches the user beyond the app. Holistic.
  - Commit #4 (replan loop) — closes the system back on itself. Holistic.
  - Commit #5 (inferred detector) — removes manual data entry. Holistic.
  - Commit #6 (UI) — single review queue. Reduces operator burden.
  - Commit #7 (push opt-in) — necessary for #3 to function on mobile. Reduces operator burden (user doesn't have to remember to check the app).
  - Commit #8 (digest) — recap channel; the system summarizes for the user. Holistic.
  - Commit #9 (backfill verification) — empirical proof gate.

- **"Accuracy over LLM cost."** Proposer (commit #2) + life-event classifier disambiguator (commit #5) both use Opus per `DEFAULT_MODEL_BY_ROLE`. No Haiku fallback anywhere in this sprint.

- **"No surprise auto-execution."** The architectural invariant of commit #2 — the no-execution sentence in the system prompt + the forbidden-pattern post-validator. Spec is structurally hostile to auto-execution.

- **"User decisions are first-class data."** Every Accept/Defer/Reject/Customize writes a row to `action_proposal_history` + flows back into the predictions ledger (Spec C) as an outcome.

## Section 14 — Open items to surface to Ariel

These are decisions the spec defers per [[feedback_ask_dont_assume]]. The implementation plan (Spec E plan, next document) will resolve them before commit #1 lands.

1. ~~**Replan dispatch default mode.** Spec proposes `dry_run` for the first 30 days, then `enforce`.~~ **Auto-resolved per codex IMPORTANT #3:** 7-day canary with auto-promotion criteria (§4.6). Ariel may still override by setting `ARGOSY_REPLAN_DISPATCH_MODE=enforce` from day one if desired, but the spec's shipping default is now the 7-day canary, not the 30-day blanket. **Confirm OK.**

2. **Email digest day-of-week + time-of-day.** Spec proposes Fridays 08:00 IDT. Ariel may want Sunday morning (Israel work-week starts Sunday) or Sunday evening. **Decision needed.**

3. **VAPID subject `mailto:` URI.** Web push spec requires a subject claim — typically `mailto:admin@example.org`. Ariel may want this to be the user's own email or a generic Argosy address. **Decision needed.**

4. **Inferred life-event detector first-cadence.** Spec proposes daily 03:00 IDT. Given the user's transaction stream cardinality is low (one Schwab CSV / month + a few credit-card statements), weekly may be more appropriate. **Decision needed.**

5. **Inferred-event detector activation.** Spec proposes ON by default. Ariel may prefer the detector starts in `proposals_disabled=True` (writes findings but does NOT trigger the proposer) for the first 30 days, so the operator can review heuristic output before any user-visible proposals fire. **Decision needed.**

6. **SMTP provider.** Spec assumes generic SMTP via env vars. Ariel may already have a preferred SES / SendGrid / Postmark / Resend integration. **Decision needed.**

7. **Inferred-event heuristics extension.** The v1 set is 5 heuristics. Are there obvious patterns from Ariel's life Argosy should also detect? E.g. "second car purchased" (signals a teen learner's permit), "rent payment stopped" (paid off mortgage), "monthly bituach leumi shifted" (statutory change vs personal change). **Decision needed.**

8. **Replan-trigger mapping table additions.** §11 lists 7 mappings; some flag kinds intentionally have no mapping (concentration, allocation, volatility — they go through the proposer instead of replan). Confirm this matches user intent. **Decision needed.**

## Section 15 — Forward-looking notes (NOT v1 scope)

For the next wave's spec brief, the following naturally extend Spec E and should be flagged for whoever drafts wave-next:

- **Approval chains for high-amount actions.** If a `repatriate_currency` proposal involves > $100k, require a 24h waiting period before Accept can fire (a structural cooling-off). Not in v1; the user's current transaction sizes don't warrant it yet.
- **Per-kind action engines.** v1 ships `repatriate_currency` Accept as a "mark intent + surface on daily brief" entry. v-next can ship a real broker integration; Spec E's structured payload is already shaped for it.
- **Multi-tenant fanout.** When Argosy goes multi-tenant, the dispatcher needs per-tenant rate-limiting on VAPID push (avoid one tenant exhausting the daily push quota of a shared key). Not in v1.
- **A/B prompts.** The proposer is the first agent where user accept-rate becomes a meaningful empirical signal for prompt tuning. v-next can ship a structured A/B prompt rotation through `DEFAULT_PROMPT_VARIANT_BY_ROLE`.
- **Push notification batching.** If the user receives 10 critical pushes in 60 seconds (correlated event storm — market crash), the second through tenth should be batched into one "9 more critical alerts — open Argosy" digest. Not in v1.
- **Cross-channel-message-template centralization.** v1 has the proposer write `summary` + `body` directly. v-next can introduce a `MessageTemplate` layer so the same proposal renders differently per channel (e.g. shorter for push, fuller for email).

## Section 16 — Codex tandem review summary

Single-dispatch codex review ran 2026-05-29 against this spec (session `tools/codex-tandem/sessions/2026-05-29-last-mile-delivery-spec-review/`). Verdict: **BLOCK** with 4 BLOCKERs + 6 IMPORTANTs + 3 NICE-to-haves. All BLOCKERs integrated; the highest-impact IMPORTANTs integrated; a few NICE-to-haves deferred to v-next (§15).

### Integrated BLOCKERs

| # | Title | Integration location |
|---|---|---|
| 1 | No-execution invariant needs capability-boundary enforcement, not just prompt+regex | §2.2.1 — adds `execution_state` schema column + code-level deny-list test over accept-handler call graph + payload-field regex scan |
| 2 | Replan cooldown semantics not concurrency-safe (race-prone between trigger sources) | §4.3 — atomic `BEGIN IMMEDIATE` transaction wrapping all 4 gates + concurrency test in `tests/test_replan_dispatch.py::test_concurrent_dispatchers_respect_cap` |
| 3 | Inferred-event detector can double-fire `tuition_stopped` + `kid_started_college` on category-mapping change | §5.4 — 5th guardrail: pre-proposal conflict resolver over tuition-family patterns + counterparty-continuity check + `conflict_resolution` column on `inferred_life_event_findings` |
| 4 | Web-push endpoint host allowlist too brittle (likely to break legitimate browsers) | §3.4 — replaced strict allowlist with shape-validation + telemetry tagging (`host_provenance='unknown'`); deny-by-default only for non-https |

### Integrated IMPORTANTs

| # | Title | Integration location |
|---|---|---|
| 1 | Dedup hash over-suppresses materially different proposals for `update_plan_assumption` | §1.5 — per-kind hash-inclusion override table; `assumption_field` + `suggested_value` always in hash for `update_plan_assumption`; similar identity-field tables for other kinds |
| 2 | Forbidden-pattern regex too narrow (English-only + 4 phrases) and lacks payload-field coverage | §2.4 — widened regex set (added English variants + Hebrew patterns) + scan now covers payload free-text fields + code-block/quote stripping; 50-fixture unit-test corpus |
| 3 | 30-day blanket dry-run delays autonomous value delivery | §4.6 — replaced with 7-day canary + auto-promotion criteria; manual override env still available |
| 6 | `json_valid` CHECKs need explicit SQLite version assumption to prevent migration surprises | §8 preamble — pinned SQLite ≥ 3.38 + migration 0050 preflight asserts JSON1 extension present |

### Acknowledged but NOT integrated (with rationale)

| # | Codex finding | Why not integrated |
|---|---|---|
| IMPORTANT 4 | Notification preference matrix + per-kind advanced filters may be overdesigned | Spec already collapses per-kind to Advanced disclosure (collapsed-by-default in commit #7). The matrix shape is the right granularity for the v1 product; flattening further would force per-flag-kind hardcoding in the dispatcher. |
| IMPORTANT 5 | VAPID local-file fallback should be permissions-hardened | Picked up via the implementation plan (commit #3) — `argosy/scripts/generate_vapid_keys.py` writes `~/.argosy/vapid_keys.json` with `0o600` permissions and logs the umask check. Mentioned in spec as a single-line addition under §3.4. |
| NICE 1 | Replan multi-trigger storm prioritization | Already added per codex BLOCKER #2 fix as Gate #4 — impact-ranked priority order (§4.3 — `market_drawdown_15pct` > `fx_shock_10pct` > `tax_law_change` > `life_event` > `user_request`). |
| NICE 2 | Push batching for correlated critical storms | Already noted as future work in §15 — confirmed v-next scope. |
| NICE 3 | Cross-evidence pattern dismissals | Already noted as future work in §5.4 — v1 ships per-evidence-window dedup only; cross-evidence dismissal table is v-next. |

### Final residual concerns flagged to Ariel

After integration, three open items remain that codex called out as worth surfacing explicitly:

1. **Notification matrix Advanced disclosure** is in the spec but its UX details (collapsed by default, opt-in tour) are not pinned. Decision needed at commit #7 detail-design time.
2. **VAPID key rotation playbook** is referenced as "manual + infrequent" but no specific runbook entry exists yet. Add to `docs/operations/` during the implementation plan.
3. **Inferred-event detector first activation default** — spec proposes ON by default. Open item #5 in §14 still stands; codex confirmed this should be an explicit user choice, not a silent default.

---

End of Spec E.
