# In-Process Jobs Registry + Admin UI — Design

**Status:** Pending Ariel approval. Codex tandem zigzag review returned BLOCK; 5 BLOCKERs + 6 IMPORTANTs integrated below.
**Date:** 2026-05-29
**Author:** Claude (Opus 4.7), with codex tandem zigzag review.
**Codex session:** `tools/codex-tandem/sessions/2026-05-29-jobs-registry-spec-review/`.
**Sibling specs:** [`2026-05-29-plan-execute-monitor-reorg-design.md`](2026-05-29-plan-execute-monitor-reorg-design.md), [`2026-05-29-anomaly-detection-rsu-prevest-design.md`](2026-05-29-anomaly-detection-rsu-prevest-design.md). This is sprint A in the post-sprint-#2 wave; it does not block the other three specs but it underpins how Discord listener + news pipeline + future monitor agents are run in production.

## Problem

The codebase is accumulating long-lived background work — the Discord listener (`argosy/cli/discord_ingest.py`), the news ingest pipeline (`argosy/services/news_ingest.py` + `news_analyst_runner.py`), the 14 existing `CadenceLoop` implementations behind `argosy/orchestrator/scheduler.py`, plus the monitor agent + daily-automation flows from sibling sprint #1. Today these are wired in two incompatible ways:

- **In-process `Scheduler`** runs the 14 `CadenceLoop` instances when `argosy run` is invoked (see `argosy/cli/run.py:28`). It persists per-loop state in the `cadence_state` table. But this scheduler is NOT yet started by `argosy/api/main.py::create_app` — the FastAPI process and the scheduler are separate processes that the operator must remember to start.
- **External cron / supervisor** is what the Discord listener docstring expects ("the supervisor that schedules `run_discord_listener` is expected to call `load_creds` first and skip the listener if credentials are not present" — `discord_listener.py:18-22`). Sprint #1 commit #16 explicitly punted restart-on-disconnect "to the caller (cron or supervisor)".

The user has rejected the cron path explicitly: "scatters operational state outside the codebase, makes debugging require RDP into the host, hides what's running from the UI, breaks portability." The mental model the user wants is the inverse: **one process, every recurring task visible in the UI, every task triggerable manually from the UI, the same code path used by both manual and scheduled triggers**. This spec lands that infrastructure as generic plumbing. Discord listener and news pipeline are early users; they are NOT the design center. Spec #1 monitor agents (allocation drift, MC regression, macro-shift), the daily-brief runner, the backup loop, watchlist polling, and any future recurring agent register through the same `JobRegistry` surface.

## Goal

Ship an in-process `JobRegistry` that supersedes external cron expectations, attaches itself to the FastAPI process lifecycle, exposes a `/api/jobs` registry route + a `/api/jobs/{name}/run-now` manual-trigger route that runs the **same code path** as the scheduled tick, and surfaces all of it in a new `/admin/jobs` Next.js page. Audit history of every run lives in a new `job_runs` table.

## Non-goals

- **No multi-process / distributed scheduling.** One Argosy process owns the registry. Multi-tenant productization (separate ARGOSY_HOME per tenant) is fine because each gets its own process; this design does not need a Celery/Redis/RQ-style queue.
- **No replacement of the existing `Scheduler` class.** `JobRegistry` wraps it via composition (codex BLOCKER #5 — see §1.6). The `Scheduler` already owns the per-loop coroutine + `cadence_state` writes. The registry layer adds the registry view, the manual-trigger API, and the `job_runs` audit history. Codex flagged "two parallel schedulers" as a risk; the design explicitly delegates to the existing Scheduler.
- **No replacement of `cadence_state`.** That table stays — it is the scheduler's per-loop pointer (next_due_at, last_tick_at, last_status, last_error). `job_runs` is per-run history (one row per execution, append-only). They serve different purposes; both stay. Dual-write failure matrix is specified in §1.7 (codex BLOCKER #4).
- **Bounded transient retry only.** v1 ships a narrow retry policy: 1 retry with jitter (0.5-2s) for transport-layer / timeout errors on RSS / HTTP-fetch / external-API jobs. Business-rule errors (DB constraint violations, validation failures) hard-fail without retry. Codex IMPORTANT #2 integration — full retry-policy contract in §1.8.
- **No mutation of schedule from the UI.** The registry surfaces `schedule` as read-only metadata. Schedule changes go through `agent_settings.yaml` (existing config path) + process restart. The user explicitly rejected an in-UI schedule editor as "complexity for the sake of complexity in a single-user system."
- **No external secret-store integration.** Discord creds stay at `~/.argosy/discord_creds.json`. The registry exposes "credentials present: yes/no" as a status field, not the secret itself.

## Section 1 — Architecture

### Section 1.1 — One scheduler, two views

```
                  ┌────────────────────────────────────────────┐
                  │  FastAPI process (uvicorn argosy.api.main)│
                  │                                            │
                  │  ┌─────────────┐    ┌──────────────────┐  │
                  │  │ Scheduler   │←───│ JobRegistry      │  │
                  │  │ (existing)  │    │ (new, this spec) │  │
                  │  └──────┬──────┘    └────────┬─────────┘  │
                  │         │                    │            │
                  │    coroutine            view+manual       │
                  │    per loop             trigger API       │
                  │         │                    │            │
                  │         ▼                    ▼            │
                  │  ┌─────────────┐    ┌──────────────────┐  │
                  │  │cadence_state│    │ job_runs (new)   │  │
                  │  │  (pointer)  │    │  (per-run audit) │  │
                  │  └─────────────┘    └──────────────────┘  │
                  │         ▲                    ▲            │
                  │         │                    │            │
                  │         └────────┬───────────┘            │
                  │                  │                        │
                  │           CadenceLoop.tick()              │
                  │       (single code path — manual          │
                  │        + scheduled both call this)        │
                  └────────────────────────────────────────────┘
```

The registry does NOT own the coroutine that runs each loop. The existing `Scheduler` keeps that responsibility (`scheduler._run_loop`). The registry holds:
- A reference to each registered `CadenceLoop`.
- The `Scheduler` instance it was bound to at startup.
- Last-run metadata cached in-memory (refreshed on every tick + every manual trigger).
- A small async lock per job that serializes manual + scheduled triggers (Section 1.4).

### Section 1.2 — FastAPI lifecycle binding (key change)

Today the `Scheduler` is only started by `argosy run` (separate CLI process). For the registry to be visible at `/api/jobs` and for `Run now` to actually fire a tick, the scheduler MUST run inside the FastAPI process. This spec wires `argosy/api/main.py::create_app` to:

1. On `@app.on_event("startup")`: construct a `Scheduler`, call `register_default_loops()`, construct a `JobRegistry` over that scheduler, register additional non-`CadenceLoop` jobs (Discord listener, news daily, see commits #5/#6), then `asyncio.create_task(scheduler.run_forever())`.
2. Stash the registry on `app.state.job_registry` so the route handlers can grab it.
3. On `@app.on_event("shutdown")`: call `scheduler.stop()` + `await` the run-forever task with a 5-second join timeout, then close the discord-listener supervisor task if running.

**Operator-mode flag.** Codex IMPORTANT integration (anticipated): in dev, the operator may want the FastAPI process WITHOUT the scheduler (so `npm run dev` doesn't hammer the news APIs during UI work). The startup hook reads `ARGOSY_RUN_SCHEDULER` env (default `1`); set to `0` to skip scheduler boot but keep `/api/jobs` route serving a stale-but-readable registry view that says `status: not-running`. The `argosy run` CLI sets the env to `1` explicitly; the bare uvicorn `create_app` factory picks up the default. Tests pass `0` explicitly.

### Section 1.3 — Single code path contract (the binding user preference)

The user's binding requirement: "Manual and scheduled triggers MUST run the same code path."

**Codex BLOCKER #2 — narrowed contract.** The provable claim is **"single writer to `job_runs` for `CadenceLoop` executions; one entrypoint per execution kind."** Not "every code path that touches a loop goes through one function" — that's stronger than achievable (e.g., tests will always be able to call `loop.tick()` directly, and they should). The narrower contract is enforced by:

- `Scheduler.fire_once` (public) becomes the **only** path callers outside the registry are allowed to use to fire a registered loop. Its body delegates to `_run_through_registry(name)` when registry mode is enabled (the default in `create_app`), which goes through the registry's adapter. Tests / one-shots that intentionally bypass audit history call `loop.tick()` directly — they cannot accidentally appear in `job_runs`.
- A module-level assertion in `argosy/services/jobs/registry.py::_traced_fire_once`: it's the only function permitted to insert into `job_runs`. A `pytest` collection-time scan + a runtime grep test (`tests/test_jobs_registry.py::test_job_runs_single_writer`) walks the codebase looking for `INSERT INTO job_runs` / `JobRun(` constructions outside the registry module. CI fails if a second writer appears.
- The `LongRunningJob` supervisor is also "inside the registry" — it constructs `JobRun` rows but does so via a single registry-owned helper `_open_job_run` / `_close_job_run` that is the same helper used by `_traced_fire_once`. One helper, two callers (CadenceLoop adapter + LongRunning supervisor), one writer point.

Concrete implementation:

```python
# argosy/services/jobs/registry.py

class JobRegistry:
    async def fire_now(self, name: str, *, triggered_by: str = "user") -> int:
        """Manual trigger. Returns the job_runs.id of the started run.

        Implementation contract:
          1. Acquire the per-job async lock with 1s timeout (Section 1.4).
             On timeout: raise AlreadyRunning(job_run_id=<winner>).
          2. Open a job_runs row via _open_job_run with status='running',
             manual_trigger=True, triggered_by=triggered_by.
          3. Call self._scheduler._fire_once(loop, force=True) — the EXACT
             same call the scheduler's _run_loop uses on a scheduled tick.
             `force=True` bypasses the market-hours guard (matches existing
             scheduler.fire_once semantics).
          4. On exception: _close_job_run with status='error',
             error_message=str(exc). Do NOT swallow the exception — let
             _fire_once's existing logging fire too. _close_job_run is the
             ONLY function that finalizes a job_runs row.
          5. On success: _close_job_run with status='ok',
             output_summary=<from loop.last_output_summary>.
          6. Release the lock.
        """
```

A test asserts the adapter + LongRunning supervisor (via the shared `_open_job_run` / `_close_job_run` helpers) are the only writers to `job_runs`.

### Section 1.6 — `JobRegistry` ↔ `Scheduler` integration: composition, not callable seam

**Codex BLOCKER #5 integration.** An earlier draft proposed adding `pre_tick: Callable` and `post_tick: Callable` slots to `Scheduler` for the registry to hook into. That implicit-extension-point pattern was flagged as API widening without a hard contract.

**Replacement:** explicit composition. `JobRegistry` constructs a `RegisteredScheduler(Scheduler)` subclass that overrides `_fire_once` to wrap the parent call with `_open_job_run` / `_close_job_run`:

```python
# argosy/orchestrator/scheduler.py

class Scheduler:
    """Unchanged from today. No callable seams added."""
    # ...

# argosy/services/jobs/registry.py

class RegisteredScheduler(Scheduler):
    """Scheduler variant that records every _fire_once into job_runs via the
    bound JobRegistry. The override is small and visible: subclass-typed,
    not callable-injected.
    """

    def __init__(self, *args, registry: "JobRegistry", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._registry = registry

    async def _fire_once(self, loop: CadenceLoop, *, force: bool = False) -> None:
        run_id = await self._registry._open_job_run(
            job_name=loop.name,
            manual_trigger=force and self._registry._is_manual_context(),
            triggered_by=self._registry._current_trigger_label(),
        )
        try:
            await super()._fire_once(loop, force=force)
            await self._registry._close_job_run(
                run_id, status="ok",
                output_summary=getattr(loop, "last_output_summary", None),
            )
        except Exception as exc:
            await self._registry._close_job_run(
                run_id, status="error", error_message=str(exc),
            )
            raise

    async def _record_tick(self, *args, **kwargs) -> None:
        """cadence_state write is unchanged; runs AFTER super()._fire_once
        finishes (BLOCKER #4 ordering — see §1.7).
        """
        await super()._record_tick(*args, **kwargs)
```

The seam is now a Python class — type-checked, IDE-navigable, testable independently. `Scheduler` itself stays untouched (BLOCKER #5 — no public API widening). `RegisteredScheduler` lives in the registry module because it's the registry's concern.

### Section 1.7 — Dual-write failure matrix (`job_runs` ↔ `cadence_state`)

**Codex BLOCKER #4 integration.** A tick run produces TWO DB writes — a `job_runs` row (per-run audit) and a `cadence_state` update (scheduler pointer). The failure-handling matrix:

| `job_runs` write | `cadence_state` write | Operator-visible state | Re-fire behavior |
|---|---|---|---|
| `_open_job_run` ok | (no cadence_state write yet) | row in `job_runs` with status='running' | normal flow |
| `_close_job_run` ok | `_record_tick` ok | audit + pointer both fresh | normal flow — wait next_due |
| `_close_job_run` ok | `_record_tick` fails | audit fresh, pointer stale | **scheduler re-fires next iteration** because `cadence_state.last_tick_at` not bumped. The duplicate run produces a NEW `job_runs` row; the first run's audit-row is intact. Codex flagged: this is acceptable IF jobs are idempotent (news_ingest, discord_listener, monitor agents all are). The retention loop (§2.1) compacts duplicate `ok` runs older than 30d. |
| `_close_job_run` fails | `_record_tick` ok | audit row stuck in 'running'; pointer fresh | **scheduler waits next_due normally** (no immediate re-fire). The orphaned 'running' row is reaped by the cleanup pass (running > 24h → 'cancelled', §2.1). |
| `_close_job_run` fails | `_record_tick` fails | audit stuck in 'running'; pointer stale | scheduler re-fires; orphan row reaped. Worst case, but recoverable. |
| `_open_job_run` fails | (`_fire_once` not entered) | no audit row; pointer stale | scheduler logs error + skips this tick. Next tick fires normally. Audit gap is acceptable — DB unavailable means everything is broken anyway. |

**Authoritative-field rules:**
- `cadence_state.last_tick_at` is the scheduler's source of truth for "when did we last fire" (drives `next_due_at` computation).
- `job_runs.started_at` / `finished_at` is the operator's source of truth for "did the work actually happen + how long did it take".
- The two can disagree (per the matrix); the UI surfaces both ("Pointer says X; last completed run was Y") only when they diverge by > 1 cadence interval, otherwise just shows `job_runs.finished_at`.

**Ordering invariant (pinned).** `_close_job_run` is called BEFORE `_record_tick`. If both fail, we prefer "audit stuck in running" (operator-visible problem) over "audit lost" (silent). Pin this order in the `RegisteredScheduler._fire_once` body + a test in `tests/test_jobs_registry.py::test_dual_write_ordering`.

**Idempotency key.** Each `job_runs` row gets a deterministic `idempotency_key = f"{job_name}|{started_at.isoformat(timespec='seconds')}|{triggered_by}"`. The registry uses a UNIQUE INDEX on this key to make `_open_job_run` retries safe (e.g., on a transient DB blip the registry retries the INSERT; the second attempt no-ops). Schema appendix updated.

### Section 1.8 — Retry policy (bounded, transient-only)

**Codex IMPORTANT #2 integration.** No retry was too brittle for network-dependent jobs (RSS fetch, macro feed HTTP, news_analyst LLM call). Spec ships:

- **Transport-layer retries:** 1 retry with jitter (sleep 0.5-2.0s uniform random) for `aiohttp.ClientError`, `asyncio.TimeoutError`, `httpx.TransportError`, and Anthropic SDK's `APIConnectionError` / `APITimeoutError`. Codified in `argosy/services/jobs/retry.py::retry_transient(coro, *, attempts=2)`.
- **Business-rule errors:** no retry. `IntegrityError`, `ValidationError`, schema mismatches all hard-fail. The tick records `status='error'` and the operator triggers `Run now` after fixing.
- **LLM-content errors:** no retry. `BadRequestError` (prompt format issue), `RateLimitError` (Anthropic-side overload) hard-fail; the latter could be argued for retry, but the existing cost-guard layer in `argosy/orchestrator/cost_guard.py` already throttles. v1 stays simple.
- **Per-job opt-out:** a `CadenceLoop` subclass that wants different retry semantics overrides `RETRY_CONFIG: RetryConfig = RetryConfig.DEFAULT` with a custom `RetryConfig` instance. v1 only news_ingest and discord_listener use non-default values; everything else inherits `DEFAULT`.

A retried call surfaces as ONE `job_runs` row, not two. The retry happens inside the tick's `aiohttp.get` call, not at the scheduler level. The retry attempt count goes into `output_summary.notes` ("transport retry: 1 attempt") so operators see it.

### Section 1.4 — Concurrency: per-job async lock

If a scheduled tick is mid-run and the user clicks `Run now`, two coroutines must NOT call `tick()` simultaneously — a CadenceLoop's idempotency contract (e.g., `news_ingest` dedup on `(source, source_ref)`) may rely on a single in-flight writer per row range. The lock semantics:

- Each registered job has an `asyncio.Lock` keyed by `loop.name`.
- The scheduled path (`scheduler._run_loop`) acquires the lock before calling `_traced_fire_once`.
- The manual path (`registry.fire_now`) tries `lock.acquire()` with a 1-second timeout.
- The timeout value is configurable per-job via `JobMetadata.lock_acquire_timeout_s` (default 1.0; codex NICE #1).

**409 contention response shape** (codex IMPORTANT #1 — the winner's `run_id` is NOT always known; nullable):

```json
{
  "error": "already_running",
  "conflict_reason": "lock_held",
  "job_run_id": 4421,                    // null if winner is mid-supervisor-restart
                                          // or the in-memory holder marker is stale
  "lock_holder_state": "running",         // "running" | "starting" | "unknown"
  "lock_acquired_at": "2026-05-29T14:00:00Z",  // null if state is "unknown"
  "retry_after_s": 5
}
```

Deterministic client behavior: UI checks `lock_holder_state`. `"running"` → "Job is still running. View its history." `"starting"` → "Job is starting up. Try again in a moment." `"unknown"` → "Job appears busy but state is uncertain. Refresh page."

**Long-running carve-out interaction:** the Discord listener's lock is held for the lifetime of its connection (hours). `Run now` against a connected listener returns 409 with `conflict_reason='longrunning_connected'` + a hint that the right operation is `POST /api/jobs/{name}/reconnect` (a separate route landed in commit #5). See Section 3.

### Section 1.5 — `source_kind` taxonomy

The registry exposes a `source_kind` field per job so the UI can group them. Four values, exhaustive at v1:

| `source_kind` | Examples | UI grouping |
|---|---|---|
| `ingest` | discord listener, news daily, RSS poll, schwab CSV ingest if cron-driven | "Data in" |
| `monitor` | allocation drift, MC regression, macro shift, watchlist, plan_watcher | "Watchdogs" |
| `maintenance` | backup, process cooling, audit, fleet self-review | "Housekeeping" |
| `notification` | daily brief composer, push-to-home jobs | "Outbound" |

This taxonomy is fixed at v1. Adding a new kind requires a code change + migration noted as "this is a load-bearing enum across UI + DB"; codex review must confirm none of the existing 14 loops + 2 new jobs are mis-bucketed. (My initial mapping is in §6 commit detail.)

## Section 2 — `job_runs` audit table

### Section 2.1 — Migration 0048 schema

```sql
CREATE TABLE job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT NOT NULL,
  started_at DATETIME NOT NULL,
  finished_at DATETIME NULL,
  status TEXT NOT NULL CHECK (status IN ('running','ok','error','skipped','cancelled')),
  skip_reason TEXT NULL,  -- 'market_closed' | 'disabled' | 'creds_missing' | 'lock_busy' | <other>
  error_message TEXT NULL,
  manual_trigger INTEGER NOT NULL DEFAULT 0,  -- BOOLEAN: 0 scheduled, 1 manual
  triggered_by TEXT NULL,  -- 'scheduler' | 'user:<id>' | 'startup' | 'supervisor' | 'system'
  output_summary TEXT NULL,  -- JSON object: per-job free-form counts/refs
  duration_ms INTEGER NULL,
  idempotency_key TEXT NOT NULL,  -- '{job_name}|{iso-seconds-started_at}|{triggered_by}' — see §1.7
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_job_runs_idempotency UNIQUE (idempotency_key)
);
CREATE INDEX ix_job_runs_job_started ON job_runs (job_name, started_at DESC);
CREATE INDEX ix_job_runs_status_started ON job_runs (status, started_at DESC)
  WHERE status IN ('running','error');
```

**Why a partial index on `status IN ('running','error')`:** the admin UI's main query is "show me anything currently running OR most recent errors per job"; both are tiny subsets. Full-table index on status would still scan a lot of ok rows over time.

**SKIPPED audit semantics (codex IMPORTANT #4).** Guard-skipped runs (market-hours closed, job disabled, creds missing) DO get a `job_runs` row with `status='skipped'` and a `skip_reason` enum. The UI shows these in the per-job history collapsed by default ("4 skipped runs in the last hour — expand"). Rationale: operator must be able to audit "why didn't X fire" — silently dropping the tick robs that visibility. The existing scheduler's `_record_tick(status=TickStatus.SKIPPED, ...)` already exists for `cadence_state`; commit #2 wires the same path into `_open_job_run` + immediate `_close_job_run(status='skipped', skip_reason=...)`.

**`output_summary` is documented JSON-in-TEXT** (codex NICE #5). The column type is `TEXT`. Application layer guarantees `json.loads(output_summary)` succeeds; a `CHECK (output_summary IS NULL OR json_valid(output_summary))` constraint is added (SQLite 3.38+ ships `json_valid`; `argosy.state.db` already requires 3.38+ for the existing `parsed_tickers` column in `news_signals`).

**Retention.** `job_runs` grows linearly; for ~17 jobs × ~24 ticks/day average × 365 days ≈ 150k rows/year, which SQLite handles easily but is unnecessary for debugging. A new `RetentionLoop` (commit #7) prunes `status='ok'` rows older than 30 days; `status='error'` rows kept forever (codex IMPORTANT #2 / NICE #3 — "errors forever is the right default"); `status='running'` rows older than 24h flipped to `status='cancelled'` with `error_message='reaped: stale running row'` (orphaned by a process kill). The 30d/forever knobs are settings-configurable per Ariel preference, and the retention loop itself can be disabled via `cadences.job_runs_retention.enabled=false`.

### Section 2.2 — `output_summary` JSON shape per job

Each job is free to write its own summary blob, but the UI relies on a small common structure:

```json
{
  "counts": {"fetched": 12, "persisted": 9, "duplicates": 3},
  "refs": ["news_signals.id=4451..4460"],
  "notes": "RSS feed yahoo-NVDA returned 0 (last poll same)."
}
```

Convention: top-level keys `counts` (dict[str,int]), `refs` (list[str]), `notes` (str). Anything else under `extra` (dict). Codex IMPORTANT (anticipated): pin this convention in the JobRegistry type so jobs that return arbitrary blobs are coerced/rejected at adapter time, not at UI render time.

## Section 3 — Long-running jobs (Discord listener carve-out)

Most jobs tick: invoke `tick()`, do work for seconds, return. The Discord listener is different — `run_discord_listener` returns only when the gateway disconnects (potentially hours later). Naively wrapping it as a `CadenceLoop` with `interval_seconds=60` would either:

- Start a new listener every minute (wrong — multiple connections per channel; Discord ratelimits).
- Block the scheduler's per-loop coroutine for hours (acceptable BUT then `cadence_state.last_tick_at` is stuck at the connect time, looking like a hang).

**Carve-out: `LongRunningJob`** — a new sibling class to `CadenceLoop` in `argosy/orchestrator/loops/base.py`:

```python
class LongRunningJob(abc.ABC):
    """A job whose tick is itself a long-lived coroutine.

    Contract:
      - `run()` returns only when the job naturally completes
        (Discord disconnect, news daemon shutdown, etc.).
      - The supervisor (JobRegistry) restarts it according to the
        exit_intent + backoff policy below.
      - `connection_status()` is a fast read returning
        "connected" | "reconnecting" | "stopped"; the registry
        polls this every 10s for the UI's last_run_status field
        instead of waiting for `run()` to return.
      - `exit_intent` (set by run() before returning) distinguishes
        operator_stop / clean / crashed — drives restart decision.
    """
    name: str = "base_longrunning"

    @abc.abstractmethod
    async def run(self) -> None: ...

    @abc.abstractmethod
    def connection_status(self) -> Literal["connected", "reconnecting", "stopped"]: ...

    @property
    def exit_intent(self) -> Literal["unset", "operator_stop", "clean", "crashed"]:
        """Set by run() (or by a cancel handler) before the supervisor sees
        the return. 'operator_stop' = the operator clicked 'Stop' from the
        UI (no auto-restart). 'clean' = the upstream closed cleanly (no
        auto-restart in v1, per BLOCKER review). 'crashed' = unexpected
        exception or non-clean exit (auto-restart with backoff)."""
        return getattr(self, "_exit_intent", "unset")
```

The registry treats `LongRunningJob` instances specially:

- A single `asyncio.Task` runs `run()`. The supervisor decides whether to restart based on `exit_intent`:
  - `operator_stop` → do NOT restart. Status stays `stopped` until the operator clicks `Run now` from the UI.
  - `clean` → do NOT auto-restart in v1 (codex IMPORTANT #3 integration). A clean exit means the upstream closed cleanly — Ariel can decide to reconnect via `Run now`. Earlier draft auto-restarted on clean exits; codex flagged this as masking intent.
  - `crashed` → auto-restart with exponential backoff (1s, 2s, 4s, ..., capped at 300s). Backoff resets after 5 minutes of stable `connected` state.
- `last_run_status` derives from `connection_status()`, not from "did the most-recent run() return ok" — it reflects connection health, per the spec contract.
- `Run now` for a `LongRunningJob` = "force a reconnect": cancel the task with `exit_intent='operator_stop'`, await its exit, restart it. The route returns 202.
- A separate `POST /api/jobs/{name}/stop` endpoint sets `exit_intent='operator_stop'` and lets the task exit. The job appears in the UI as `stopped`. (Discord listener uses this when Ariel wants to take the bot offline without restarting the FastAPI process.)
- `job_runs` rows for `LongRunningJob` record one row per (connect, disconnect) cycle, NOT one per ingested message. The cycle's `output_summary` records the connection duration + message count.

The Discord listener wraps as `DiscordListenerJob(LongRunningJob)` in commit #5. The news daily job stays as a normal `CadenceLoop` (it ticks at 17:00 IDT, runs for seconds-to-minutes, returns).

**Codex NICE #2 confirmation:** the dual class hierarchy (`CadenceLoop` + `LongRunningJob`) is kept. Status fields exposed to `/admin/jobs` are normalized via the `JobView.health` derivation table (§5) so the UI doesn't branch on `long_running`.

## Section 4 — Sprint commit table

Per codex BLOCKER #3 + IMPORTANT #6: the original 7-commit plan grew to 9 (TZ fix isolated, commit #2 split into shell + lifecycle). The single-code-path + dual-write commits are the highest-risk pair.

| # | Commit subject | Files touched | Codex review? |
|---|---|---|---|
| 1 | Migration 0048 — `job_runs` table + model (with `skip_reason`, `idempotency_key`, JSON `CHECK`) | `alembic/versions/0048_job_runs.py`, `argosy/state/models.py` | **Yes** — schema + index design |
| 2 | **TZ fix for `LoopSchedule.next_due_after`** — interpret cron in `self.timezone` before converting back to UTC. Regression tests for every existing loop config. Isolated commit per BLOCKER #3 — landed before any code that depends on the new behavior. | `argosy/orchestrator/loops/base.py`, `tests/test_loop_schedule.py` (extended), regression tests covering all 14 existing loops | **Yes** — pre-existing bug; one-line fix can shift firing times by hours |
| 3a | **`JobRegistry` shell + `RegisteredScheduler` subclass** — composition seam per BLOCKER #5. `_open_job_run` / `_close_job_run` helpers + per-job `asyncio.Lock`. NO lifecycle wiring yet. | `argosy/services/jobs/registry.py` (new), `argosy/services/jobs/retry.py` (new), `tests/test_jobs_registry.py` | **Yes** — single-code-path + dual-write contract (§1.3/§1.7) |
| 3b | **FastAPI lifecycle binding + adapter wiring** — `@app.on_event("startup")` constructs registry + `RegisteredScheduler`; `ARGOSY_RUN_SCHEDULER` env gate; shutdown drains. | `argosy/api/main.py` (startup/shutdown hooks), `tests/test_lifecycle.py` (new) | **Yes** — startup ordering + drain semantics |
| 4 | `GET /api/jobs` + `POST /api/jobs/{name}/run-now` + `POST /api/jobs/{name}/stop` routes **with admin auth gate** (codex BLOCKER #1 — env-token header `X-Argosy-Admin` required for v1; same gate reused by `/admin/*` UI page) | `argosy/api/routes/jobs.py` (new), `argosy/api/main.py` (router include), `argosy/api/auth.py` (new or extended — admin-token dep) | **Yes** — auth posture + 409 contract on already-running |
| 5 | `LongRunningJob` base class + supervisor in `JobRegistry` — operator_stop / clean / crashed exit_intent semantics per IMPORTANT #3 | `argosy/orchestrator/loops/base.py` (extend), `argosy/services/jobs/registry.py` (supervisor section) | **Yes** — exp-backoff + cancel semantics + clean-exit-no-restart |
| 6 | Wrap Discord listener as `DiscordListenerJob(LongRunningJob)` — retire external-cron expectation. Adds `on_connected` callback to `run_discord_listener`. | `argosy/services/jobs/discord_listener_job.py` (new), `argosy/services/discord_listener.py` (docstring + on_connected kwarg), `argosy/cli/discord_ingest.py` (deprecate; keep as 1-shot smoke-test entry only) | **Yes** — connection_status reporting + reconnect race |
| 7 | News pipeline daily job — `NewsDailyJob(CadenceLoop)` at 17:00 IDT. `CadenceLoop.tick` widened to `-> dict \| None` per IMPORTANT #5. | `argosy/services/jobs/news_daily_job.py` (new), `argosy/orchestrator/loops/base.py` (tick return type), all 14 existing loops (return None added explicitly where they currently `return` implicitly), `argosy/agent_settings.py` (add cadence entry) | **Yes** — same-code-path between scheduled + run-now + tick contract widening |
| 8 | `/admin/jobs` Next.js page + RunNowButton + run-history expand + admin-token header on UI side | `ui/src/app/admin/jobs/page.tsx` (new), `ui/src/components/admin/jobs/*`, `ui/src/lib/api.ts` (add `listJobs`, `runJob`, `stopJob`, `getJobRuns`), admin-token cookie/header plumbing | No — UI only |
| 9 | `JobRunsRetentionLoop` + observability polish + test extensions | `argosy/orchestrator/loops/job_runs_retention.py` (new), `argosy/agent_settings.py` (retention cadence config), test extensions | No — maintenance + tests only |

Nine commits. Per [[feedback_work_style_long_sprints]] — long sprint, codex zigzag per risky commit. Per [[feedback_no_dollar_reporting]] no time/cost estimate.

## Section 5 — Per-commit detail

### Commit #1 — Migration 0048 + model

**Scope.** Land the `job_runs` schema (§2.1). Add `JobRun` ORM class to `argosy/state/models.py` after the existing `CadenceState` definition (line 454 area). No code consumers in this commit — the registry that writes to it lands in #3a.

**Files.**
- `alembic/versions/0048_job_runs.py` — new.
- `argosy/state/models.py` — add `JobRun` ORM class.
- `tests/test_migrations.py` — assert 0048 applies cleanly + downgrades cleanly. Assert the partial index on `status IN ('running','error')` is created (introspect `sqlite_master`). Assert `idempotency_key` UNIQUE constraint enforces.

**Migration shape.**

```python
def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_name", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("skip_reason", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("manual_trigger", sa.Integer, nullable=False, server_default="0"),
        sa.Column("triggered_by", sa.Text, nullable=True),
        sa.Column("output_summary", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "status IN ('running','ok','error','skipped','cancelled')",
            name="ck_job_runs_status",
        ),
        sa.CheckConstraint(
            "output_summary IS NULL OR json_valid(output_summary)",
            name="ck_job_runs_output_summary_json",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_job_runs_idempotency"),
    )
    op.create_index("ix_job_runs_job_started", "job_runs", ["job_name", sa.text("started_at DESC")])
    op.execute(
        "CREATE INDEX ix_job_runs_status_started ON job_runs (status, started_at DESC) "
        "WHERE status IN ('running','error')"
    )
```

**Codex review focus:** confirm `manual_trigger` as INTEGER (SQLite-native BOOLEAN) is correct given existing project conventions; confirm partial index syntax works under SQLite (it does — verified via `PRAGMA index_list`); confirm `triggered_by` shape is enough for v1 vs. needing a JSON envelope; confirm `json_valid` is available on the target SQLite version (3.38+; already a project requirement).

### Commit #2 — `LoopSchedule.next_due_after` TZ fix (BLOCKER #3)

**Scope.** Pre-existing bug in `argosy/orchestrator/loops/base.py:60` — `_croniter(self.cron, ref)` is called with a UTC ref and ignores `self.timezone`. Every existing loop with a cron schedule is affected (today their TZ field defaults to `Asia/Jerusalem` but cron evaluation ignores it). Codex BLOCKER #3: this MUST be its own isolated commit with regression tests against every existing scheduled loop, not buried in commit #7's news_daily wiring.

**Files.**
- `argosy/orchestrator/loops/base.py:60-65` — pass a TZ-aware `ref` into `_croniter`; the croniter library accepts tz-aware datetimes from v1.4 onwards.
- `tests/test_loop_schedule.py` — extend with a parametrized fixture covering each existing loop's current `(cron, timezone)` config; for each, assert that `next_due_after(ref)` returns the expected UTC instant in both DST-on (summer) and DST-off (winter) windows.

**Fix shape.**

```python
def next_due_after(self, ref: datetime) -> datetime:
    if self.cron and _croniter is not None:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.timezone)
            ref_local = ref.astimezone(tz)
            ci = _croniter(self.cron, ref_local)
            next_local = ci.get_next(datetime)
            return next_local.astimezone(timezone.utc)
        except Exception:
            return ref + timedelta(hours=1)
    # ... interval branch unchanged
```

**Codex review focus:** confirm croniter v1.4+ is in `pyproject.toml`. Confirm the regression matrix is exhaustive (14 existing loops × 2 DST states × 2 spring/fall transitions = 56 cases). Confirm no loop relies on the current "TZ-naive UTC" behavior (some loops may have hand-tuned their cron string to compensate for the bug).

### Commit #3a — `JobRegistry` shell + `RegisteredScheduler`

**Scope.** Codex IMPORTANT #6 — commit #2 in the original draft was 600 lines (registry + scheduler hook + lifecycle wiring + retry module). Split into 3a (registry shell + scheduler composition seam) and 3b (FastAPI lifecycle wiring) for safer review.

This is the highest-architectural-risk commit; codex zigzag is mandatory.

**Files.**
- `argosy/services/jobs/__init__.py` — new package.
- `argosy/services/jobs/registry.py` — new. `JobRegistry` class + per-job lock dict + `_open_job_run` / `_close_job_run` helpers.
- `argosy/services/jobs/registered_scheduler.py` — new. `RegisteredScheduler(Scheduler)` subclass per §1.6.
- `argosy/services/jobs/retry.py` — new. `RetryConfig` + `retry_transient(coro, *, attempts=2)` per §1.8.
- `argosy/orchestrator/scheduler.py` — UNCHANGED in this commit. The seam is composition, not a public API widening (codex BLOCKER #5).
- `tests/test_jobs_registry.py` — happy-path register + fire_now; lock contention 409; dual-write ordering (BLOCKER #4); single-writer assertion (BLOCKER #2 — grep test).

**Key interfaces.**

```python
# argosy/services/jobs/registry.py

@dataclass(frozen=True)
class JobMetadata:
    name: str
    schedule_cron: str | None
    schedule_human: str  # "Daily 17:00 IDT" computed from cron via cron-descriptor or hand-mapped
    source_kind: Literal["ingest", "monitor", "maintenance", "notification"]
    description: str
    long_running: bool  # True for LongRunningJob, False for CadenceLoop

@dataclass
class JobView:
    metadata: JobMetadata
    last_run_at: datetime | None
    last_run_status: str | None  # 'ok' | 'error' | 'running' | 'skipped' | 'cancelled' | 'connected' | 'reconnecting' | 'stopped'
    last_run_error: str | None
    next_run_at: datetime | None  # None for long-running jobs
    currently_running_run_id: int | None  # non-None while a tick is in-flight
    health: Literal["green", "amber", "red", "unknown"]

class JobRegistry:
    def __init__(self, scheduler: "Scheduler") -> None: ...
    def register(self, *, job: CadenceLoop | LongRunningJob, metadata: JobMetadata) -> None: ...
    def list(self) -> list[JobView]: ...
    def get(self, name: str) -> JobView: ...
    async def fire_now(self, name: str, *, triggered_by: str = "user") -> int: ...
    async def cancel_long_running(self, name: str) -> None: ...  # only LongRunningJob
    async def start_supervisors(self) -> None: ...  # called once at FastAPI startup
    async def stop_supervisors(self) -> None: ...  # called at FastAPI shutdown
```

**Health derivation (codex IMPORTANT — make this explicit, not derived ad-hoc in UI):**

| Last-run status | Last-run at vs. next-run at | Health |
|---|---|---|
| `ok` | last-run < 2× cadence ago | green |
| `ok` | last-run > 2× cadence ago | amber (stale; scheduler may be hung) |
| `error` | any | red |
| `running` | started < 10 min ago | green |
| `running` | started > 10 min ago | amber (taking longer than expected) |
| `skipped` (e.g. market-hours guard) | any | green |
| LongRunningJob `connected` | n/a | green |
| LongRunningJob `reconnecting` | <60s in this state | amber |
| LongRunningJob `reconnecting` | >60s in this state | red |
| LongRunningJob `stopped` | n/a | red |

The `JobView.health` field is computed server-side so the UI never re-implements this.

**Codex review focus:**
- Concurrency: `asyncio.Lock` per job lives on the registry, keyed by name. If the scheduler is restarted without restarting the FastAPI process, do the locks get re-bound to the new scheduler? Spec says yes — the registry sees scheduler restart as a "rebind" operation and recreates locks. Codex must probe whether this is reachable in normal operation (probably not — scheduler restart implies process restart).
- Composition seam (codex BLOCKER #5): `RegisteredScheduler` subclass override of `_fire_once` is the only seam. Confirm there is no other reachable call site of `loop.tick()` in production that bypasses the subclass. `scheduler._fire_once` (private) is the only path the scheduler's per-loop coroutine calls; tests sometimes call `loop.tick()` directly — those tests bypass `job_runs` recording, which is fine because tests aren't supposed to produce audit history.
- Single-writer enforcement (codex BLOCKER #2): the `tests/test_jobs_registry.py::test_job_runs_single_writer` test runs `ripgrep` over `argosy/` for `INSERT INTO job_runs` and `JobRun(` constructor calls, asserting only `argosy/services/jobs/registry.py` is matched.
- Dual-write ordering (codex BLOCKER #4): `_close_job_run` MUST be called BEFORE `super()._record_tick`. Pin via test that injects a failure into `_record_tick` and asserts the `job_runs` row already shows `status='ok'`.
- Adapter robustness: if `_open_job_run` fails (DB unavailable), the tick still runs because the scheduler must not be killed by an audit-write failure. The error is logged and the tick proceeds; cadence_state still updates normally. See §1.7 matrix.

### Commit #3b — FastAPI lifecycle binding

**Scope.** Wire `JobRegistry` + `RegisteredScheduler` into the FastAPI process via startup/shutdown hooks. Read `ARGOSY_RUN_SCHEDULER` env (default `1`).

**Files.**
- `argosy/api/main.py` — `@app.on_event("startup")` constructs `JobRegistry`, `RegisteredScheduler`, calls `register_default_loops()`, registers `DiscordListenerJob` (commit #6) / `NewsDailyJob` (commit #7) once they exist (gated by import availability so this commit lands clean before #6/#7). On shutdown: `scheduler.stop()` + drain awaiting tasks with a 5s join timeout.
- `tests/test_lifecycle.py` — new. Asserts `/api/jobs` returns the registered jobs after startup; asserts `ARGOSY_RUN_SCHEDULER=0` makes startup skip scheduler boot but `/api/jobs` still serves a stale-but-readable registry from `cadence_state`.

**Env precedence (codex NICE #6):**
1. Explicit `ARGOSY_RUN_SCHEDULER=0` → skip scheduler boot, log a startup warning at WARNING level: "scheduler disabled; jobs will not run automatically."
2. Unset OR `=1` → boot scheduler normally.
3. Tests pass `=0` via the pytest fixture `disable_scheduler` (single-decorator opt-in).

**Codex review focus on this commit:**
- Startup ordering: registry must be fully constructed (locks initialized, default loops registered) BEFORE the scheduler's `run_forever` task is created. Otherwise the first tick could race a half-built registry.
- Shutdown drain: `scheduler.stop()` is fire-and-forget; the existing `run_forever` body awaits the cancellation of per-loop tasks. The 5s join timeout is added because a stuck Discord listener could otherwise hold up shutdown.
- The startup hook is `@app.on_event("startup")` (deprecated in newer FastAPI in favor of lifespan handlers). The Argosy project version of FastAPI accepts both; commit can use the simpler decorator. If FastAPI is upgraded mid-sprint, migrate to lifespan handler.

### Commit #4 — `/api/jobs` routes + admin auth gate (BLOCKER #1)

**Scope.** Two endpoints + admin auth gate. Codex BLOCKER #1: `POST /api/jobs/{name}/run-now` triggers LLM-cost + state-changing work — v1 MUST gate it.

**Auth design.**
- A new env `ARGOSY_ADMIN_TOKEN` (loaded by `argosy.config.get_settings`). If unset, server logs a startup WARNING and refuses to mount the `/api/jobs/{name}/run-now` + `/api/jobs/{name}/stop` routes (the `GET /api/jobs` route stays open for monitoring).
- Routes require a `X-Argosy-Admin: <token>` header; FastAPI dependency `require_admin_token` returns 401 if missing or wrong.
- The UI page (`/admin/jobs`, commit #8) reads the token from `localStorage.argosyAdminToken` (Ariel pastes it in once after starting the server) and attaches the header to every mutating request. A small "Admin token" input field at the top of `/admin/jobs` accepts the paste.
- Same gate covers `/admin/*` UI route — Next.js middleware in `ui/src/middleware.ts` checks the localStorage token client-side and renders a "paste admin token" inline form if missing. (Single-user system; no cookie-based session.)

This is a v1-acceptable shape per codex: single-user system, but the trigger is real (LLM costs + DB mutations) so accidental hitting the route from a stray browser tab or a CORS-permitted UI must be blocked.

**Files.**
- `argosy/api/routes/jobs.py` — new. Routes per the API appendix §8.
- `argosy/api/main.py` — `app.include_router(jobs_router)`.
- `argosy/api/auth.py` — new module. `require_admin_token` FastAPI dependency reads `X-Argosy-Admin` header against `settings.admin_token`.
- `argosy/config.py` — add `admin_token: str | None` to `ArgosySettings`.
- `tests/test_api_jobs.py` — auth required → 401; valid token → 200/202; invalid token → 401; mutating routes refuse to mount when `ARGOSY_ADMIN_TOKEN` env unset.

**Endpoints** (full shapes in §8 — API appendix):
- `GET /api/jobs` — open (no auth) for monitoring tools.
- `GET /api/jobs/{name}` — open.
- `GET /api/jobs/{name}/runs` — open.
- `POST /api/jobs/{name}/run-now` — **admin auth required**.
- `POST /api/jobs/{name}/stop` — **admin auth required** (LongRunningJob only).
- `POST /api/jobs/{name}/reconnect` — **admin auth required** (LongRunningJob only; equivalent to stop + start).

**Codex review focus on this commit:** confirm the 401 shape matches existing Argosy admin endpoints (the `/api/wealth-dashboard` and a few audit routes already do header-token checks; reuse that dependency if available rather than introducing a new one).

### Commit #5 — `LongRunningJob` + supervisor

**Scope.** New abstract class + a supervisor pattern in the registry.

**Files.**
- `argosy/orchestrator/loops/base.py` — add `LongRunningJob` (§3 shape).
- `argosy/services/jobs/registry.py` — extend with `_supervise_longrunning` async method.

**Supervisor shape.**

```python
async def _supervise_longrunning(self, job: LongRunningJob) -> None:
    """One coroutine per LongRunningJob. Restarts on any return/exception
    with exponential backoff. Records connect/disconnect cycles in job_runs.
    """
    backoff_s = 1.0
    BACKOFF_CAP_S = 300.0
    while not self._shutdown.is_set():
        # Open a "connect cycle" job_runs row
        run_id = await self._open_job_run(
            job.name, manual_trigger=False, triggered_by="supervisor",
        )
        try:
            await job.run()
            await self._close_job_run(run_id, status="ok",
                                      output_summary={"notes": "clean exit"})
            backoff_s = 1.0  # reset on clean exit
        except asyncio.CancelledError:
            await self._close_job_run(run_id, status="cancelled",
                                      output_summary={"notes": "cancelled by supervisor"})
            raise
        except Exception as exc:
            await self._close_job_run(run_id, status="error",
                                      error_message=str(exc))
            self._log.exception("longrunning.crashed", job=job.name)
            await asyncio.sleep(min(backoff_s, BACKOFF_CAP_S))
            backoff_s = min(backoff_s * 2, BACKOFF_CAP_S)
```

**Codex review focus:**
- Backoff reset: on clean exit (`status='ok'`), backoff resets to 1s. On `CancelledError`, the loop bubbles. On any other exception, exponential growth. Is this the right shape? Specifically — a Discord listener returning cleanly means the gateway closed cleanly (rare, e.g. Discord-side restart). Treating that as "no problem, reconnect immediately" seems right. Codex should challenge.
- Cancellation surface: `cancel_long_running(name)` calls `task.cancel()`. The supervised task's `run()` must respect cancellation (Discord listener's `client.close()` is in a `finally` block — verified at `discord_listener.py:313-315`). Codex should confirm.

### Commit #6 — `DiscordListenerJob`

**Scope.** Wrap `run_discord_listener` as a `LongRunningJob`. Retire the external-cron expectation in the module docstring + `discord_ingest.py` CLI.

**Files.**
- `argosy/services/jobs/discord_listener_job.py` — new. Owns a `DiscordListenerJob(LongRunningJob)` class wrapping `run_discord_listener` + tracking connection_status from inside the existing connect/disconnect log points.
- `argosy/cli/discord_ingest.py` — keep but mark as smoke-test entry point. Add a warning in the docstring: "Production runs through JobRegistry; this CLI is for one-shot dev testing only."
- `argosy/services/discord_listener.py` — docstring update only: replace "the supervisor that schedules `run_discord_listener` is expected to..." paragraph with a pointer to the new job class.
- `argosy/api/main.py` startup hook — register `DiscordListenerJob` IF `load_creds()` returns non-None. If creds missing, register with `enabled=False` so the row appears in the registry as `last_run_status='disabled (no creds)'` — the UI shows "creds missing; drop ~/.argosy/discord_creds.json to activate" rather than hiding the job entirely.

**Connection-status reporting.** The existing listener emits INFO logs at connect / connected / disconnect. The job class subscribes to these via an `asyncio.Event` it passes in as `client_factory` wrapper:

```python
class DiscordListenerJob(LongRunningJob):
    name = "discord_listener"
    
    def __init__(self, creds: DiscordCreds, session_factory: Callable) -> None:
        self._creds = creds
        self._session_factory = session_factory
        self._status: Literal["connected", "reconnecting", "stopped"] = "stopped"
    
    def connection_status(self) -> str:
        return self._status
    
    async def run(self) -> None:
        self._status = "reconnecting"
        try:
            await run_discord_listener(
                self._session_factory,
                creds=self._creds,
                # New: a callback that flips _status to "connected" when
                # the gateway HELLO succeeds. Added to discord_listener.py
                # as an optional kwarg with no-op default.
                on_connected=lambda: setattr(self, "_status", "connected"),
            )
        finally:
            self._status = "stopped"
```

**Codex review focus:**
- The `on_connected` kwarg added to `run_discord_listener` is a small protocol change. Codex should check existing tests for `run_discord_listener` don't break (callback default is no-op).
- Race: between supervisor restart and `_status` update — the supervisor opens a new `job_runs` row before `_status` flips to `reconnecting`, so there's a small window where the UI may show `status='running'` from `job_runs` while `connection_status()` says `stopped`. The `JobView.health` derivation prefers `connection_status()` when both exist; commit lands the precedence rule.

### Commit #7 — `NewsDailyJob` + `CadenceLoop.tick` contract widening

**Scope.** Wire the news pipeline as a 17:00 IDT daily `CadenceLoop`. The same-code-path contract applies: scheduled tick + `Run now` both call `NewsDailyJob.tick`. Also lands the `CadenceLoop.tick -> dict | None` widening (codex IMPORTANT #5).

**Files.**
- `argosy/orchestrator/loops/base.py` — widen the `CadenceLoop.tick` return type from `None` to `dict | None`. The 14 existing loops keep their implicit-`None` returns (`None` is the empty summary).
- `argosy/services/jobs/news_daily_job.py` — new. Subclasses `CadenceLoop`. `tick()` runs Stage 1 ingest (`news_ingest.run_news_ingest`) THEN Stage 2 analyst (`news_analyst_runner.run_unanalyzed_batch`) inside one coroutine. Both stages share one `Session`. Returns the dict.
- `argosy/services/jobs/registered_scheduler.py` — update `_fire_once` override to capture the return value: `result = await super()._fire_once(loop, force=force)` then `_close_job_run(... output_summary=result)`.
- `argosy/agent_settings.py` — add `cadences.news_daily` config with `cron="0 17 * * *"`, `timezone="Asia/Jerusalem"`. TZ correctness is handled by commit #2 (already shipped).
- `argosy/api/main.py` startup — register if `cadences.news_daily.enabled` (default True).

**`tick()` contract change (codex IMPORTANT #5).** The earlier draft proposed a `self.last_output_summary` side-channel attribute to avoid touching the 14 existing loops. Codex flagged this as fragile (mutable side-channel; visibility scattered). Final shape: widen the return type. The 14 existing loops `return` implicitly (returns `None`) — no source change required, they're already compatible. The MyPy / type-checker pass in CI catches any subclass that returns a non-dict.

**`tick()` shape.**

```python
async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
    """Returns output_summary dict — JobRegistry adapter persists it as
    job_runs.output_summary. None == empty summary."""
    with self._session_factory() as session:
        # Per-stage outcome capture (codex NICE #7): even if Stage 2
        # fails, Stage 1's counts are persisted in the summary so the
        # operator can see "ingest worked, analyst broke".
        stage1_result = run_news_ingest(session, ...)
        try:
            stage2_result = await run_unanalyzed_batch(session, ...)
            stage2_status = "ok"
            stage2_error = None
        except Exception as exc:
            stage2_result = None
            stage2_status = "error"
            stage2_error = str(exc)
            # Re-raise so the job_runs row records status='error'; the
            # summary still captures stage1 success.
            raise
        finally:
            # Mutate self.last_output_summary so the adapter sees the
            # per-stage breakdown even when raising.
            self.last_output_summary = {
                "counts": {
                    "ingested_fetched": stage1_result.fetched,
                    "ingested_persisted": stage1_result.persisted,
                    "ingested_duplicates": stage1_result.duplicates,
                    **(
                        {
                            "analyzed": stage2_result.analyzed,
                            "analyzed_batches": stage2_result.batches,
                        }
                        if stage2_result is not None else
                        {"analyzed": 0, "analyzed_batches": 0}
                    ),
                },
                "stages": {
                    "ingest": "ok",
                    "analyze": stage2_status,
                },
                "stage_errors": {"analyze": stage2_error} if stage2_error else {},
                "notes": f"by_source={stage1_result.by_source!r}",
            }
    return self.last_output_summary
```

The `self.last_output_summary` write happens in `finally` so even on raise the adapter can read it from the attribute when `_close_job_run` is called with `status='error'`. This is a small bit of belt-and-suspenders: both the return value and the attribute carry the same dict; adapter prefers return value when the tick completed normally, falls back to attribute on exception.

**Codex review focus:**
- 17:00 IDT cadence vs. Stage 2 LLM latency: the news_analyst runs 1 batch (≤20 signals) per call; the orchestrator iterates batches. If a daily run produces 60 signals, that's 3 LLM calls. The tick may take 60-90s. The lock from §1.4 ensures a `Run now` during that window 409s rather than racing.
- Per-stage failure semantics (codex NICE #7): Stage 2 failure surfaces as `status='error'` for the whole tick BUT Stage 1's success is still recorded in `output_summary.counts` + `output_summary.stages.ingest='ok'`. Operator can split into two jobs in a future iteration if Stage 1 and Stage 2 routinely need different retry semantics. For v1 the single-job shape is acceptable.
- TZ correctness: handled by commit #2; this commit relies on the fix being in place.

### Commit #8 — `/admin/jobs` UI

**Scope.** Next.js page mounted at `/admin/jobs`. Table of jobs, expandable per row to show recent history. "Run now" + "Stop" + "Reconnect" buttons per row (latter two only for `long_running` jobs).

**Files.**
- `ui/src/app/admin/jobs/page.tsx` — new. Server component fetches `/api/jobs` on render; client component handles the per-row interactions + admin-token gate.
- `ui/src/components/admin/jobs/JobsTable.tsx` — new. Renders the table.
- `ui/src/components/admin/jobs/JobRunHistory.tsx` — new. Expand-row content: last 20 `job_runs` for the job; collapsed-by-default group for `status='skipped'` runs.
- `ui/src/components/admin/jobs/RunNowButton.tsx` — new. Calls `POST /api/jobs/{name}/run-now` with the `X-Argosy-Admin` header from `localStorage.argosyAdminToken`, optimistic-updates the row to `running`, polls `/api/jobs/{name}` every 2s until status changes. Surfaces 409 inline as "already running — view history".
- `ui/src/components/admin/jobs/AdminTokenGate.tsx` — new. Renders inline "paste admin token" input when the token is missing from localStorage; saves to localStorage on submit.
- `ui/src/lib/api.ts` — add `listJobs()`, `getJob(name)`, `runJob(name)`, `stopJob(name)`, `reconnectJob(name)`, `getJobRuns(name, opts)`. All mutating helpers read the admin-token from localStorage and attach the header.
- `ui/src/middleware.ts` — extend (or create) to redirect `/admin/*` to a token-gate page when localStorage is empty.

### Commit #9 — `JobRunsRetentionLoop` + observability polish

**Scope.** Optional retention loop + finishing tests + diagnostics. Lands AFTER the UI so any operational issue spotted during commit #8 dogfooding feeds back into this commit's tests.

**Files.**
- `argosy/orchestrator/loops/job_runs_retention.py` — new. `JobRunsRetentionLoop(CadenceLoop)` running `cron="30 03 * * *"` (03:30 Asia/Jerusalem daily). Deletes `status='ok' AND finished_at < now - 30d`; flips `status='running' AND started_at < now - 24h` to `status='cancelled', error_message='reaped: stale running row'`. `status='error'` kept forever.
- `argosy/agent_settings.py` — add `cadences.job_runs_retention` config.
- `argosy/api/main.py` startup — register the retention loop.
- `tests/test_jobs_retention.py` — new. Asserts retention SQL is bounded by the configured window; asserts orphan-row reap works.
- `tests/test_jobs_registry.py` — extend with: lock contention 409 with all three `lock_holder_state` values; supervisor backoff exponential growth; `exit_intent='clean'` does NOT auto-restart.
- `tests/test_api_jobs.py` — extend with: auth missing → 401; auth wrong → 401; auth valid → 202; SKIPPED rows visible in `/api/jobs/{name}/runs`.
- `argosy/api/routes/jobs.py` — small observability addition: every route logs `correlation_id` (already a project convention; see `argosy/logging.py`).

**UI shape (table columns).**

| Col | Source field | Width |
|---|---|---|
| Name | `metadata.name` | 200px |
| Kind | `metadata.source_kind` (badge: ingest/monitor/maintenance/notification color) | 100px |
| Schedule | `metadata.schedule_human` ("Daily 17:00 IDT") + small `<code>` for cron | 200px |
| Last run | `last_run_at` (relative time: "2 min ago") | 140px |
| Status | `last_run_status` as StatusPill (existing `ui/src/components/ui/status-pill.tsx`) | 120px |
| Health | `health` colored dot (green/amber/red/grey) + tooltip explaining derivation | 60px |
| Error | `last_run_error` (clipped at 80 chars + "expand") | flex |
| Next | `next_run_at` (relative; n/a for LongRunningJob) | 120px |
| Source | `metadata.source_kind` description hover | (group) |
| Actions | `Run now` button (per RunNowButton) + "View history" expand toggle | 160px |

**Polling cadence.** The `/admin/jobs` page polls `/api/jobs` every 5s while focused, every 30s when blurred (visibility API). The `RunNowButton`'s post-trigger 2s poll runs until the row's `status` changes from `running` to terminal.

**Codex review focus on this commit (lighter, mostly UI):**
- Polling vs. WebSocket: the spec proposes plain polling. `/ws/events` is reserved (per `argosy/api/main.py:13` Phase 2 comment) but not yet a real surface. For the v1 admin page, polling is fine. Codex may suggest WebSocket as a follow-on.
- Auth on `/admin/*` routes: per commit #3 review, no auth in v1. If codex pushed back on commit #3, the `/admin/jobs` page may need a token gate too.

## Section 6 — Source-kind mapping for existing + new jobs

Codex review needs to vet this concretely. Mapping at sprint-start:

| Job | `source_kind` | Long-running? | New or existing? |
|---|---|---|---|
| `daily_brief` (retired W9) | (omit — retired) | — | existing |
| `weekly_review` | monitor | No | existing |
| `process_cooling` | maintenance | No | existing |
| `monthly_cycle` | monitor | No | existing |
| `quarterly` | monitor | No | existing |
| `annual` | monitor | No | existing |
| `minute` | maintenance | No | existing |
| `hour` | maintenance | No | existing |
| `backup` | maintenance | No | existing |
| `audit` | maintenance | No | existing |
| `watchlist` | monitor | No | existing |
| `plan_watcher` | monitor | No | existing |
| `reconcile` (broker fills) | ingest | No | existing |
| `discord_listener` | ingest | **Yes** | new (this spec) |
| `news_daily` | ingest | No | new (this spec) |
| `job_runs_retention` | maintenance | No | new (this spec, Commit #9) |

The four monitor agents from sibling spec #1 (allocation_drift, mc_regression, macro_shift) register through this registry when they land — all `monitor` kind, all `CadenceLoop`-shaped.

## Section 7 — Schema appendix

### `job_runs` (Migration 0048)

```sql
CREATE TABLE job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT NOT NULL,
  started_at DATETIME NOT NULL,
  finished_at DATETIME NULL,
  status TEXT NOT NULL CHECK (status IN ('running','ok','error','skipped','cancelled')),
  skip_reason TEXT NULL,
  error_message TEXT NULL,
  manual_trigger INTEGER NOT NULL DEFAULT 0,
  triggered_by TEXT NULL,
  output_summary TEXT NULL CHECK (output_summary IS NULL OR json_valid(output_summary)),
  duration_ms INTEGER NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_job_runs_job_started ON job_runs (job_name, started_at DESC);
CREATE INDEX ix_job_runs_status_started ON job_runs (status, started_at DESC)
  WHERE status IN ('running','error');
```

No other schema changes. `cadence_state` stays intact (per §1.7 dual-write contract).

## Section 8 — API appendix

### `GET /api/jobs`

Request: no params.

Response 200:
```json
{
  "scheduler_running": true,
  "jobs": [
    {
      "name": "news_daily",
      "schedule_cron": "0 17 * * *",
      "schedule_human": "Daily 17:00 Asia/Jerusalem",
      "source_kind": "ingest",
      "description": "Stage 1 + Stage 2 news pipeline (RSS + macro + discord).",
      "long_running": false,
      "last_run_at": "2026-05-29T14:00:00Z",
      "last_run_status": "ok",
      "last_run_error": null,
      "next_run_at": "2026-05-30T14:00:00Z",
      "currently_running_run_id": null,
      "health": "green"
    },
    {
      "name": "discord_listener",
      "schedule_cron": null,
      "schedule_human": "Long-running (auto-restart)",
      "source_kind": "ingest",
      "description": "Subscribe to Discord alpha channel; persist to news_signals.",
      "long_running": true,
      "last_run_at": "2026-05-29T08:32:11Z",
      "last_run_status": "connected",
      "last_run_error": null,
      "next_run_at": null,
      "currently_running_run_id": 4421,
      "health": "green"
    }
  ]
}
```

Response 503 (scheduler not running, env override):
```json
{
  "scheduler_running": false,
  "jobs": [/* registry from settings, last-known status from DB */]
}
```

### `GET /api/jobs/{name}`

Response 200:
```json
{
  "metadata": {/* JobMetadata fields */},
  "view": {/* JobView fields */},
  "recent_runs": [/* last 20 JobRunRow */]
}
```

Response 404: `{"error": "job_not_found", "name": "<name>"}`

### `POST /api/jobs/{name}/run-now` (admin auth required)

Headers: `X-Argosy-Admin: <token>` — REQUIRED. Missing/wrong → 401.

Request body: `{}` (empty, reserved for future params like `force=true`).

Response 202:
```json
{"job_run_id": 4422, "started_at": "2026-05-29T14:32:08Z", "name": "news_daily"}
```

Response 409 (already running — full shape per §1.4 + codex IMPORTANT #1):
```json
{
  "error": "already_running",
  "conflict_reason": "lock_held",
  "job_run_id": 4421,
  "lock_holder_state": "running",
  "lock_acquired_at": "2026-05-29T14:00:00Z",
  "retry_after_s": 5
}
```

Response 401: `{"error": "admin_token_required"}` (or `"admin_token_invalid"` for wrong token).

Response 404: as above.

Response 503: `{"error": "scheduler_not_running"}`

### `POST /api/jobs/{name}/stop` (admin auth required, LongRunningJob only)

Sets `exit_intent='operator_stop'` on the supervised task and awaits exit. Does NOT restart.

Response 202: `{"name": "discord_listener", "stopped_at": "2026-05-29T14:32:08Z"}`

Response 400: `{"error": "not_long_running", "name": "<name>"}` (the target is a CadenceLoop, not a LongRunningJob — use `run-now` instead).

### `POST /api/jobs/{name}/reconnect` (admin auth required, LongRunningJob only)

Equivalent to `stop` then immediate `Run now` — convenience operation for the operator.

Response 202: `{"name": "discord_listener", "new_job_run_id": 4423}`

### `GET /api/jobs/{name}/runs?limit=50&before_id=<id>`

Response 200:
```json
{
  "runs": [/* JobRunRow × up to limit */],
  "has_more": true,
  "next_before_id": 4400
}
```

## Section 9 — Test plan

| Test file | Covers commits | Scenarios |
|---|---|---|
| `tests/test_migrations.py` (extend) | #1 | 0048 apply + downgrade; partial index exists; `idempotency_key` UNIQUE rejects duplicate; `json_valid` CHECK rejects malformed output_summary |
| `tests/test_loop_schedule.py` (extend) | #2 | TZ-aware `next_due_after` for 14 existing loops × DST-on / DST-off (regression matrix per BLOCKER #3); spring-forward + fall-back DST transitions |
| `tests/test_jobs_registry.py` (new) | #3a, #5 | `register` + `list` + `fire_now` happy path; lock contention 409 (TOCTOU between scheduled + manual); single-writer assertion (BLOCKER #2 grep test); dual-write ordering (`_close_job_run` before `_record_tick`, BLOCKER #4); supervisor exponential backoff (mock clock); supervisor does NOT restart on `exit_intent='clean'` (IMPORTANT #3); cancel_long_running closes job_runs row with status='cancelled' |
| `tests/test_lifecycle.py` (new) | #3b | FastAPI startup binds JobRegistry; shutdown cancels scheduler task with 5s join; ARGOSY_RUN_SCHEDULER=0 skips boot but keeps /api/jobs serving stale cache |
| `tests/test_api_jobs.py` (new) | #4, #9 | GET /api/jobs shape (no auth required); GET /api/jobs/{name} 404; POST run-now: 401 if no token / wrong token / 202 valid / 409 already-running with full lock_holder_state shape / 404 / 503; POST stop / reconnect (LongRunningJob only); GET /api/jobs/{name}/runs pagination + SKIPPED rows surfaced (IMPORTANT #4) |
| `tests/test_discord_listener_job.py` (new) | #6 | `connection_status()` transitions stopped → reconnecting → connected via on_connected callback; supervisor restarts on `exit_intent='crashed'`; supervisor does NOT restart on `exit_intent='clean'`; cancel mid-run closes ws + records cancelled |
| `tests/test_news_daily_job.py` (new) | #7 | `tick()` runs Stage 1 then Stage 2 inside one session; output_summary shape matches §6 detail (counts + stages + stage_errors); per-stage failure semantics (Stage 2 raise → status='error' BUT stage1 counts persisted, NICE #7); manual + scheduled paths produce identical results given same input (snapshot test) |
| `tests/test_jobs_retention.py` (new) | #9 | retention SQL deletes only ok-status > 30d; error-status retained forever; orphan-running > 24h flipped to cancelled |
| `ui/src/app/admin/jobs/__tests__/JobsTable.test.tsx` (new) | #8 | Renders status pill per health derivation; RunNowButton calls API + polls; 409 surfaces inline error; AdminTokenGate appears when localStorage empty |

**E2e under `pytest -m "not llm_eval"`:** all backend tests. The UI tests run under `npm test` (vitest).

## Section 10 — Risk register

| Risk | Mitigation |
|---|---|
| Scheduler attached to FastAPI process means `npm run dev` (UI) accidentally spawns ingest jobs against prod APIs | `ARGOSY_RUN_SCHEDULER=0` env gate (§1.2). Tests pass it; dev `npm run dev` documentation updated to point at it. |
| Manual `Run now` racing scheduled tick produces duplicate writes despite job-level idempotency | Per-job `asyncio.Lock` (§1.4) + 409 response. Codex IMPORTANT may probe whether the lock spans the right scope. |
| Long-running Discord listener masks "I'm not reconnecting" hangs (stuck in `reconnecting` forever) | `health` derivation (§5) flips to `red` after 60s in `reconnecting`. UI shows red row. |
| `job_runs` table grows unbounded | Retention loop (commit #7, optional) prunes ok-status > 30d. Codex may push back: Ariel may want to keep everything. |
| FastAPI restart kills the Discord listener mid-connection; supervisor restart hammers Discord on every restart loop | Exponential backoff cap (§4 commit detail) at 300s. Discord ratelimit doc says <1k connects/day per token; 300s cap means worst-case 288 reconnects/day. |
| `tick` return-value contract drift (commit #6) | Decision documented: keep `tick() -> None`, use `self.last_output_summary` attribute. Avoids touching the 14 existing loops. |
| 17:00 IDT cron interpreted as 17:00 UTC due to `LoopSchedule` TZ bug | Pre-existing bug; commit #6 fixes `next_due_after` to honor `self.timezone` (croniter accepts tz-aware refs). |
| `/api/jobs/{name}/run-now` is unauthenticated and can fire LLM-cost work | RESOLVED — `X-Argosy-Admin` env-token header gate required in v1 per codex BLOCKER #1 (§Commit #4). Mutating routes refuse to mount when `ARGOSY_ADMIN_TOKEN` env unset. |
| Transient transport failure (RSS / macro / LLM API) takes down the whole tick | Bounded 1-retry-with-jitter for transport-layer errors (§1.8, codex IMPORTANT #2). Business-rule errors still hard-fail. |
| `cadence_state` and `job_runs` diverge after partial DB failure | Per-row failure matrix in §1.7 (codex BLOCKER #4). `idempotency_key` UNIQUE makes retried inserts safe. Worst case: duplicate `ok` rows in `job_runs` — compacted by retention loop. |
| TZ-naive `next_due_after` shifts firing times when fixed | Commit #2 is the dedicated fix with regression matrix covering all 14 loops × DST states. Confirmation step: review the test output before merging downstream commits. |

## Section 11 — Open dependencies for Ariel

None at sprint start. The Discord listener already has a credentials path (`~/.argosy/discord_creds.json`); if creds are absent the job appears in the registry as `last_run_status='disabled (no creds)'` and stays dormant until Ariel drops the file. No new external dependencies introduced.

## Section 12 — Codex review focus appendix

Codex zigzag review (run 2026-05-29) probed these architectural questions. Status of each is noted; see §13 for the final BLOCKER / IMPORTANT integration list.

1. **Single code-path provability.** ✅ RESOLVED — narrowed to "single writer for job_runs" + CI grep test (§1.3, BLOCKER #2).
2. **Race: simultaneous manual + scheduled fire.** ✅ RESOLVED — 409 response shape carries the winner's `lock_holder_state` and (nullable) `job_run_id` (§1.4, IMPORTANT #1).
3. **Long-running vs. cadence job dichotomy.** ✅ RESOLVED — dual hierarchy kept; UI status normalized via `JobView.health` (NICE #2).
4. **Retry policy absence.** ✅ RESOLVED — bounded transient retry added (§1.8, IMPORTANT #2).
5. **`output_summary` JSON shape coercion.** ✅ RESOLVED — `json_valid` CHECK constraint at the DB layer; documented JSON-in-TEXT convention (NICE #5).
6. **Timezone handling in cron evaluation.** ✅ RESOLVED — Commit #2 isolates the TZ fix with regression matrix (BLOCKER #3).
7. **Auth gap on `/api/jobs/{name}/run-now`.** ✅ RESOLVED — `X-Argosy-Admin` token gate required in v1 (Commit #4, BLOCKER #1).
8. **Locking granularity.** ✅ RESOLVED — per-job lock kept; timeout configurable per `JobMetadata.lock_acquire_timeout_s` (NICE #1).
9. **Supervisor restart on `status='ok'` (clean exit).** ✅ RESOLVED — `exit_intent` semantics distinguish operator_stop / clean / crashed; only crashed auto-restarts (§3, IMPORTANT #3).
10. **`cadence_state` vs. `job_runs` dual-write integrity.** ✅ RESOLVED — explicit failure matrix in §1.7 (BLOCKER #4). Ordering invariant: `_close_job_run` before `_record_tick`. Idempotency_key UNIQUE for retry safety.

Additional pre-review concerns from the spec author (some integrated, some flagged as future work):

A. TZ-bug fix scope (loop hand-tuning risk) — flagged in Commit #2 codex review focus: "confirm no loop relies on the current TZ-naive behavior". The regression matrix forces a check.
B. Composition seam vs. callable injection — RESOLVED by codex BLOCKER #5; subclass composition picked.
C. SKIPPED tick audit — RESOLVED by IMPORTANT #4; SKIPPED runs get rows with `skip_reason`.
D. `ARGOSY_RUN_SCHEDULER` default footgun — addressed in §Commit #3b: WARNING log when scheduler skipped (NICE #6).
E. `connection_status()` stale-read window — accepted as eventual-consistency (NICE #4).
F. JSON-in-TEXT schema clarity — RESOLVED via `json_valid` CHECK (NICE #5).
G. Commit #2 size — RESOLVED by split into #3a + #3b (IMPORTANT #6).
H. NewsDaily two-stage failure semantics — RESOLVED via per-stage outcome in `output_summary.stages` (NICE #7); v1 stays as one job, may split in future.

## Section 13 — Codex tandem review summary

**Verdict:** BLOCK (run 2026-05-29). After integration, the BLOCKERs + IMPORTANTs are addressed in this revision; the spec is ready for re-review or proceed-with-caution.
**Session:** `tools/codex-tandem/sessions/2026-05-29-jobs-registry-spec-review/`.

**BLOCKERs (all integrated above):**
1. **Auth missing on manual trigger** — §Commit #4 + §8 routes now gate `POST run-now`, `POST stop`, `POST reconnect` behind `X-Argosy-Admin` header (`ARGOSY_ADMIN_TOKEN` env). Mutating routes refuse to mount if env unset. Same gate covers `/admin/*` UI page.
2. **"Single code path" claim overstated** — §1.3 narrowed contract to "single writer for `job_runs`; one entrypoint per execution kind." `tests/test_jobs_registry.py::test_job_runs_single_writer` enforces via grep test at CI time.
3. **TZ fix isolation** — Commit #2 elevated to its own commit with regression matrix covering all 14 existing scheduled loops × DST-on / DST-off / spring-forward / fall-back.
4. **Dual-write failure matrix** — §1.7 added with explicit 6-row matrix (`job_runs` ok/fail × `cadence_state` ok/fail × `_open_job_run` fail), authoritative-field rules, ordering invariant (`_close_job_run` BEFORE `_record_tick`), and idempotency_key UNIQUE constraint for retry safety.
5. **Scheduler seam API widening** — §1.6 replaced `pre_tick`/`post_tick` callable injection with explicit `RegisteredScheduler(Scheduler)` subclass composition. `Scheduler` itself stays unchanged.

**IMPORTANTs (all integrated):**
1. **409 contention response shape** — §1.4 expanded to include `conflict_reason`, `lock_holder_state` (`running`/`starting`/`unknown`), `lock_acquired_at`, `retry_after_s`. `job_run_id` is now nullable for the deterministic-client behavior codex requested.
2. **Bounded transient retry** — §1.8 added. 1 retry with jitter for transport/timeout errors; hard-fail on business-rule errors; per-job `RETRY_CONFIG` override.
3. **Clean-exit LongRunning restart** — §3 + Commit #5 distinguish `exit_intent='operator_stop' | 'clean' | 'crashed'`. Only `crashed` auto-restarts. Earlier draft auto-restarted on clean exit; codex flagged that as masking intent.
4. **SKIPPED tick audit semantics** — §2.1 now explicit: skipped runs DO get `job_runs` rows with `status='skipped'` + `skip_reason` enum. UI collapses by default but operator can expand.
5. **tick() output via mutable side-channel was fragile** — §Commit #7 widens `CadenceLoop.tick -> dict | None`. 14 existing loops keep their implicit-`None` returns (compatible). `last_output_summary` attribute kept as belt-and-suspenders for exception paths.
6. **Commit slicing** — Old Commit #2 (registry + lifecycle + adapter, ~600 LOC) split into #3a (registry shell + RegisteredScheduler) and #3b (FastAPI lifecycle wiring). Sprint count went from 7 → 9 commits.

**NICEs (acknowledged, mostly integrated):**
1. Per-job `asyncio.Lock` granularity is right; lock timeout configurable per-job via `JobMetadata.lock_acquire_timeout_s`. Contention metric added to observability logging in Commit #9.
2. Dual hierarchy (CadenceLoop + LongRunningJob) kept; status fields normalized via `JobView.health` (§5) so UI doesn't branch on `long_running`.
3. Retention default "errors forever, ok 30d" kept as the default; per-status windows are settings-configurable; retention loop disable knob in `cadences.job_runs_retention.enabled`.
4. Sync `connection_status()` stale-read window accepted as eventual-consistency telemetry.
5. `output_summary` JSON-in-TEXT now explicit: `CHECK (output_summary IS NULL OR json_valid(output_summary))` added to migration.
6. `ARGOSY_RUN_SCHEDULER` env precedence documented in §Commit #3b (boots scheduler if unset/=1; skips with WARNING log if =0).
7. NewsDaily Stage 1 + Stage 2 in one job kept for v1; per-stage outcome fields in `output_summary.stages` so operator can see partial success.

**Open from review (NOT integrated; flagged for sprint kickoff discussion):**
- None. All BLOCKERs and IMPORTANTs are resolved in this revision.
