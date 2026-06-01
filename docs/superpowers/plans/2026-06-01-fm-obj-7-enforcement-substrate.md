# Wave 6 — Plan-policy enforcement substrate (FM-OBJ #7)

**Drafted:** 2026-06-01 (wave 5 closing, wave 6 scoping)
**Status:** scoping only — not approved, not started
**Triggered by:** FM-OBJ #7 on plan-revision draft #16 (run #56), plus three-issue compound investigation in wave 5

## What FM-OBJ #7 actually says

> *"short.actions schedules plan_targets wiring at 2026-09-01, concurrent with the plan-rebuild gate and AFTER the Q2/Q3 NVDA tranche, SGOV migration, ETF liquidation window, and first redeployment trades. The estate trip-wire, 55%/15% NVDA caps, and 35% info-tech sector cap are all self-certified documentation, not system-enforced controls, during the cycle in which they are most actionable. Advance wiring to a hard 2026-07-15 milestone — specifically ahead of the first NVDA-deconcentration redeployment trade — so the sector-cap check is live before any UCITS purchase that could re-concentrate the tech tilt."*

## The investigation behind this wave

Wave 5 dry-run of `assemble_phase1_inputs` against ariel's active baseline showed `plan_targets_count: 0`. The three-issue compound:

| # | Issue | Where |
|---|---|---|
| A | **Distillation has never run for anyone.** Zero rows in `agent_reports` with `agent_role='plan_distiller'`. Zero `plan_versions` rows with non-null `distillate_json`. The baseline-upload flow at `intake.py:516` calls `distill_baseline_plan_async()` as fire-and-forget but the task never completes. Upload succeeds anyway per the wrong-but-shipped tolerance at `intake.py:511`. | data layer |
| B | **Producer-consumer contract mismatch.** `_extract_plan_targets(baseline)` at `inputs.py:710` produces `{label: value}` (e.g., `{"NVDA target": 15.0}`). `risk_preflight.check_concentration_cap` at `risk_preflight.py:163` calls `plan_targets.get(ticker)` keyed by ticker. Same dict, two callers, incompatible key conventions. ConcentrationAnalystAgent happens to be a third caller that uses labels and works fine. | contract layer |
| C | **No sector-cap or position-class enforcement code.** `check_concentration_cap` does per-ticker lookup only. FM-OBJ #7 wants 35% info-tech sector cap + 15% NVDA single-position cap. The dict shape can't carry both kinds of cap. | scope layer |

Codex tandem architectural audit (`tools/codex-tandem/scripts/_plan_targets_architecture_audit.py`, run 2026-06-01) returned an opinionated recommendation summarized in §"Architectural direction" below.

## Architectural direction (codex recommendation, accepted-pending-approval)

### Policy model — typed pydantic with discriminated union

Replace `dict[str, float]` with a structured model:

```python
class TickerCapRule(BaseModel):
    kind: Literal["ticker"]
    ticker: str
    max_pct: float

class SectorCapRule(BaseModel):
    kind: Literal["sector"]
    sector_code: str
    max_pct: float

class ExposureCapRule(BaseModel):
    kind: Literal["exposure"]      # estate trip-wire, US-situs sum, etc.
    exposure_type: str
    max_value: float
    currency: str

Rule = Annotated[
    TickerCapRule | SectorCapRule | ExposureCapRule,
    Field(discriminator="kind"),
]

class PlanPolicy(BaseModel):
    policy_version: int
    effective_at: datetime
    source_plan_version_id: int
    rules: list[Rule]
```

Reason: a single `dict[str, float]` already has two-caller key-collision; explicit discriminated union prevents drift and makes preflight testable.

### Ticker → sector mapping — internal classification table

New table `instrument_classification`:

```
ticker          TEXT PRIMARY KEY
sector_code     TEXT NOT NULL
source          TEXT NOT NULL   -- "finnhub", "manual", "fmp"
as_of           DATETIME
confidence      TEXT            -- "high" | "low"
updated_at      DATETIME
```

Populated by:
1. A scheduled job (`instrument_classification_sync`) that pulls Finnhub `/stock/profile2` for known tickers, deduplicating against existing rows
2. A manual-override path for cases where vendor classification is wrong (e.g. UCITS tracker ETFs that vendors mis-classify)

**Critical**: `risk_preflight` reads from this table only — NEVER live vendor calls at preflight time. Determinism + auditability.

### Wave strategy — one substrate, two waves; sector caps in wave 1

| Wave | Scope | Must-haves |
|---|---|---|
| **Wave 6** (this plan) | Enforcement backend | Distillation reliability, typed `PlanPolicy`, `instrument_classification` table + sync, preflight checks for ticker + sector caps, explicit override logging |
| **Wave 7** (future) | Richer exposure rules + UX | Estate trip-wire via `ExposureCapRule` (needs FX-aware sum-of-US-situs-positions logic), `/plan` policy editor surface, override-history audit page |

Codex's note: "Deferring sector caps makes FM-OBJ #7 materially unmet" — Wave 1 must include sector. The threat model is the NVDA-deconcentration redeployment trade buying tech-heavy UCITS (CSPX/CNDX) and re-concentrating the tilt that's been actively unwound.

### Distillation reliability — backfill job + invariant

Replace the silent fire-and-forget upload-time call with:

1. **CLI**: `argosy distill --missing-baselines` (idempotent, resumable, prints metrics)
2. **Queue-backed**: distillation goes through the in-process scheduler with retry/DLQ, not raw `asyncio.create_task`
3. **Upload-path observability**: upload returns `distillation_pending: true` until the distill row commits; UI shows a "distillation pending" state instead of acting as if the baseline is fully ready
4. **Startup health check**: at server startup, count `plan_versions` with `role='baseline' AND distillate_json IS NULL` for active users; if non-zero, write a `monitor_flags` row of kind `distillation_backlog` so it shows up on /home

Reason: the latent "fire-and-forget never completed" failure cannot recur silently. State exists or doesn't; staleness is observable; backfill is idempotent.

## Scope checklist (wave 6)

- [ ] **Migration 0060_plan_policies**: `plan_policies` table (policy_version, effective_at, source_plan_version_id, rules JSON), `instrument_classification` table
- [ ] **Pydantic types**: `argosy/agents/plan_policy_types.py` with discriminated union (`TickerCapRule` + `SectorCapRule` only; `ExposureCapRule` stub for Wave 7 but no production wiring)
- [ ] **Synthesizer schema extension**: `PlanSynthesisOutput` emits `policy: PlanPolicy` (or equivalent) so the distillate carries typed rules instead of label-keyed targets
- [ ] **Distiller update**: `PlanDistillerAgent` produces `PlanPolicy` from synthesizer output
- [ ] **Backwards-compat adapter**: convert typed `PlanPolicy` back to `{label: value}` legacy dict for `ConcentrationAnalystAgent` until consumers migrate (or document explicit consumer-migration plan inline)
- [ ] **Distillation backfill**: `argosy/cli/distill.py` adds `--missing-baselines`, scheduled job, retry logic, upload-time `distillation_pending` state
- [ ] **Backfill validation gates**: post-backfill assertion (parse-failure count + row count + spot-check on at least one distillate's `policy.rules`) before flipping the active baseline pointer; rollback path if gate fails
- [ ] **Instrument classification sync**: `argosy/services/instrument_classification.py` + scheduled job pulling from Finnhub `/stock/profile2` with manual-override write path
- [ ] **Preflight rewrite**: `risk_preflight.check_concentration_cap` becomes `evaluate_policy(proposal, snapshot, policy, classification_map)` returning per-rule results
- [ ] **Rule-evaluation semantics**: define precedence (ticker rules > sector rules?), aggregation (does a per-ticker PASS override a per-sector FAIL?), and HARD_FAIL short-circuit policy upfront — encoded in `evaluate_policy` + tested
- [ ] **Unknown-classification behavior**: explicit policy when proposal ticker is absent from `instrument_classification` — currently considering SOFT_FAIL with banner OR HARD_FAIL with override; decision in open questions
- [ ] **Policy selection at decision time**: given multiple `plan_policies` rows (different `effective_at`, different `source_plan_version_id`), define which one applies to "right now" + tie-break semantics + rollback shape
- [ ] **Override logging**: when a user explicitly confirms past a HARD_FAIL, record structured `decision_runs.notes_json.override_reason` (enum + actor identity + free-text justification)
- [ ] **Startup health check**: `distillation_backlog` monitor flag if any active-user baseline has NULL distillate
- [ ] **Tests**:
  - per-rule preflight: ticker pass/fail, sector pass/fail (no exposure tests in Wave 6 — those land with `ExposureCapRule` in Wave 7)
  - unknown-ticker / unknown-sector classification behavior
  - policy selection + effective-date tie-break behavior
  - backfill idempotence + validation gate failure modes
  - classification sync end-to-end (mocked Finnhub)
  - override logging audit trail
  - backwards-compat adapter shape

## Open questions for approval

### Original four (wave-5 scoping pass)

1. **Wave 6 timeline target.** Codex says 2026-07-15 is feasible **if** scope hard-caps to enforcement backend (no UX redesign for /plan policy editor — that's Wave 7). Ariel: is the first NVDA-deconcentration redeployment trade confirmed before or after 2026-07-15? If after, we have slack; if before, this wave is on the critical path.
2. **Sector code source-of-truth.** Finnhub uses GICS (Information Technology / Semiconductors / etc.). The user's plan-prose uses "info tech" / "tech tilt." Question: do we adopt GICS verbatim, or normalize to a smaller user-facing taxonomy? Affects how synthesizer expresses caps + how UI renders them.
3. **Backwards compatibility.** Existing `_extract_plan_targets` returns `{label: value}` and ConcentrationAnalystAgent consumes it that way. Do we (a) ship the new typed model alongside the legacy dict and migrate consumers over a few cycles, (b) hard-flip everyone to the typed model in one commit, or (c) treat ConcentrationAnalystAgent's iteration as deprecated and remove the label-keyed extractor entirely?
4. **Override authority.** FM-OBJ #5's text says *"override authority reserved to user-confirmation only."* Today every trade goes through the user anyway (single-user system). Multi-tenant ready by design — is the override permission a per-policy-rule property (e.g., "this ticker cap is overridable; this sector cap is not") or one global "user can override anything" flag?

### Added by codex review (must answer before code starts)

5. **Unknown-ticker / unknown-sector enforcement policy.** When a proposed trade's ticker is absent from `instrument_classification` (new symbol, vendor gap, sync lag), what does preflight return? Options: (a) **HARD_FAIL with override** — block by default, force user confirm with explicit reason; (b) **SOFT_FAIL with banner** — allow trade, flag the gap visibly; (c) **PASS with audit trail** — assume benign, record for review later. Codex flagged this as "the biggest regression-risk gap" in the current plan.
6. **Policy lifecycle semantics.** Each successful synthesis writes a new `plan_policies` row. At decision time, multiple policy rows may exist (different `effective_at`, different `source_plan_version_id`). Question: (a) how is "the active policy" selected — most recent `effective_at` past `now()`? Most recent `source_plan_version_id` matching the active baseline? (b) What happens when a synthesis is rolled back — does its policy get superseded automatically, marked inactive, or deleted? (c) Replay behavior: when re-running a preflight check against a historical proposal, which policy applies?
7. **Override audit schema contract.** What fields are mandatory on a user-confirmed override past HARD_FAIL? Codex calls out: `actor identity` (user_id), `enum reason` (e.g., `risk_accepted` / `data_correction` / `emergency` / `other`), `free-text justification`. Anything else? Affects schema for `decision_runs.notes_json.override` + downstream replay/audit pages.
8. **Sync SLO + rate-limit / backoff policy.** Finnhub free tier rate-limits at 60/min. The `instrument_classification` table has ~26 user tickers + watchlist. Refresh cadence proposal: nightly full-sync? Hourly delta against recent fills? On-demand refresh when a new ticker enters positions? Plus: how stale is too stale before preflight downgrades to "unknown-classification" semantics? Codex flags this as risk #3 (rate limits / staleness causing sync lag).

## What this wave does NOT do

- `/plan` UI policy editor (Wave 7)
- Estate trip-wire enforcement (Wave 7 — needs FX-aware sum-of-US-situs logic that doesn't yet exist)
- Multi-currency cap evaluation (rules in single currency for now)
- Backfilling sector classifications for tickers Argosy hasn't seen yet (only sync what's in current positions + watchlist)
- Replacing the existing single-ticker preflight checks in `risk_preflight.py` (e.g., wash-sale stub, daily-loss-limit) — those stay as-is

## Dependencies on other in-flight work

None blocking. Wave 5 fixes (Finnhub/FRED keys, tax plumbing) need to land cleanly first so wave 6's distill rerun has a healthy substrate to read from.

## Known risks (codex review)

To flag explicitly before the wave starts:

1. **Unknown/stale classification.** A position whose actual sector has shifted (M&A, business pivot) but whose `instrument_classification` row hasn't been refreshed could cause a **false block** (legitimate trade rejected) or a **missed block** (real cap breach allowed through). Severity depends on the unknown-classification policy chosen in open question #5. Mitigation: SLO-driven sync cadence + staleness threshold + audit log of preflight decisions made against potentially-stale rows.
2. **Typed-schema rollout breaking legacy distillates.** During the rollout window, some `plan_versions` rows will have legacy `distillate_json` (label-keyed) and new rows will have typed-policy `distillate_json`. Consumers must handle both shapes until migration completes. Without an explicit compatibility adapter (see scope checklist), readers will break on whichever shape they didn't expect.
3. **Finnhub rate limits + staleness.** Free tier is 60 calls/min. Sync job that hits Finnhub for every position + watchlist daily would burn ~30 calls — fine in isolation, but if cache invalidation cascades or the sync re-fires on every position change, the budget gets thin. Backoff policy + staleness threshold needed (open question #8).
4. **Backfill partial success.** `argosy distill --missing-baselines` runs against an unknown number of baselines. If 10 succeed and 3 fail mid-run, the system ends up with mixed policy generations — some users on the new schema, some on the legacy. The backfill validation gate (scope checklist) is the mitigation: don't flip the active baseline pointer until validation passes; on failure, rollback path defined.
