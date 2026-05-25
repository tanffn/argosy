# Argosy gap analysis — 2026-05-25

**Trigger:** the "Run synthesis" button on `/plan` works mechanically (DecisionRun row created, 13 agents fire, $1.99 of Opus spent) but produces no usable output: fund_manager rejects, no draft persists, no agent_reports written, no "current plan" anchor exists for the rest of the system. This doc inventories the structural gaps between the SDD's product vision and what actually runs today, so we can pick a sequencing.

**Today's evidence:** DecisionRun #5 ran all 5 phases in 23 min, rejected. 8/9 phase-1 analysts crashed before producing any output. Only `plan_critique` succeeded ($0.45, 30k output tokens, confidence LOW). Phase 2: 1 of 3 horizons completed cleanly; medium horizon's bear_researcher hit the `claude.exe` exit-1 flake (handover gap #4); short horizon's bear emitted non-JSON. Phase 3 ran on impoverished input. Phase 4 risk team ran on the resulting weak draft. Phase 5 rejected. Net: no draft, no learning, no record.

## 1. Product vision (per SDD §6.11 + §0)

A monthly (and on-demand via `/check-in`) Opus fleet **re-derives a fresh long/medium/short plan** from {baseline distillate + current portfolio + recent fills + analyst reports + bull/bear/facilitator debates}. Output: a `role='draft'` `PlanVersion` with three `HorizonSection` JSON payloads + pre-rendered markdown + speculative candidates. User accepts → `role='current'`. Every other agent (advisor, expenses, decision-flow) anchors on `current`.

Ariel's UX expectation (this session): for every owned holding, surface per-ticker analyst verdicts → Hold/Sell/Buy + conviction + reasoning + news/macro/FX/tax context. For missing-but-recommended positions, surface them with "WHY." Overall portfolio health card. Retry-resume on partial failure (don't waste $5-8 if one phase flakes). Decision tree per synthesis run.

## 2. DB state inventory (ariel, 2026-05-25)

```
plan_versions             1     (baseline only — never a current, never a draft)
decision_runs             5     (1 failed/exception, 4 stuck running or failed)
agent_reports           117     (ALL from advisor/intake turns; decision_id=NULL for synth)
proposals                 0     (no trade proposals ever)
fills                     0     (no trade execution data)
lots                      0     (no tax lots)
daily_briefs              0     (daily_brief loop has never run productively)
news_cache                0     (no news ingested)
macro_cache               0     (no macro data)
investor_events           0     (no Form 4 / 13F / TipRanks / CapitolTrades)
pension_fund_snapshots    0     (no pension data)
decision_phases           0     (recorder is wired but only runs post-success; never reached)
expense_transactions  2,179     ← only "productive" flow
expense_sources           6
user_files               79
user_context              1     (intake YAML exists)
```

**Implication:** of Argosy's 5 decision teams + 3 cross-cutting flows defined in the SDD, only *expense ingest* and *advisor/intake* have ever produced data. The plan-synthesis, daily-brief, watchlist, per-trade-decision, and risk-preflight flows exist as **code without persisted output**.

## 3. Gap inventory

Grouped by structural layer. "Effort" is rough; assumes Codex-tandem review per task.

### Layer 1 — Data plumbing (the "Argosy lights up" gap)

| ID | Gap | Evidence | Effort | Notes |
|---|---|---|---|---|
| D1 | **No payload assembler exists for the full phase-1 fleet.** `_default_gather_inputs` in `argosy/orchestrator/loops/daily_brief.py:213` covers ~5 of the 9 analysts' kwargs (`positions_summary`, `plan_targets`, `tickers`, `news_payload`, `macro_snapshot`) but is **missing** `fx_payload` (FxAnalystAgent), `fundamentals_payload` (FundamentalsAnalystAgent — partial?), `indicators_payload` (TechnicalAnalystAgent), `social_payload` (SentimentAnalystAgent), and `lots_summary`/`dividends_summary`/`rsu_schedule_summary` (TaxAnalystAgent). | Cross-ref: `fx_analyst.py:54`, `fundamentals_analyst.py:75`, `technical_analyst.py:53`, `sentiment_analyst.py:58`, `tax_analyst.py:82`. `plan_synthesis/orchestrator.py:382` passes the wrong kwargs entirely. | **3-6 hrs** | Build a synthesis-specific input assembler (`argosy/orchestrator/flows/plan_synthesis/inputs.py` or similar) that covers ALL phase-1 signatures. Reuse `_default_gather_inputs` for the ~5 shared payloads. Build the missing 4-5 from `fx` module (already exists for FX), TipRanks/Finnhub for fundamentals/sentiment, etc. Some payloads can degrade to empty dict if API key missing — analysts must tolerate empty input gracefully (verify each agent's build_prompt behavior on empty payload). |
| D2 | **Data adapters degrade gracefully but never run in prod.** `news_cache=0`, `macro_cache=0`, `investor_events=0`. | Adapters raise `AdapterMissingAPIKeyError` and the gather function swallows → empty payloads. | **0-4 hrs** | Depends on API key state. Finnhub, SEC EDGAR (no key needed), TipRanks, CapitolTrades, FRED. May "just work" once D1 is wired; or may need 1 key per source. |
| D3 | **No persisted portfolio snapshot.** `portfolio/snapshot` reads newest TSV from disk each call; `argonaut_snapshots=0`. | Symptom: this morning's $0k bug. `_find_latest_tsv()` picks whichever .tsv is newest by mtime; any stray upload shadows the real file. | **2-3 hrs** | Migrate to a `portfolio_snapshots` table written by a daily/on-demand ingester. Decouples synthesis from filesystem state. |
| D4 | **No tax lots / fills.** `lots=0`, `fills=0`. | Schwab CSV import exists for cross-validation only; no lot writer. | **1-2 days** | Required for TaxAnalyst's `lots_summary` and `dividends_summary` payloads. Real work: Schwab lot reader + a writer that reconciles into `lots` table. |
| D5 | **No RSU schedule.** `identity.rsu_grants` empty per handover gap #1. | TaxAnalyst's `rsu_schedule_summary` will be empty. Plan synthesizer can't recommend "harvest RSU when vest". | **30 min – 2 hrs** | Handover gap #1 fix path: prompt-tweak intake_extractor OR `gap_driven` re-ask. |

### Layer 2 — Synthesis flow correctness

| ID | Gap | Evidence | Effort | Notes |
|---|---|---|---|---|
| S1 | **Phase-1 analysts crash on signature mismatch.** 8/9 fail with `build_prompt() missing required keyword-only arguments`. | Today's log: `ConcentrationAnalystAgent.build_prompt() missing 2 required keyword-only arguments: 'positions_summary' and 'plan_targets'` (and 7 more). | **1-2 hrs after D1** | Direct consequence of D1. Cannot fix in isolation — needs the input assembler to exist first. `_safe_run_agent`'s narrow-retry can't *add* missing args — it only removes unknown ones. |
| S2 | **No `agent_reports` persistence for synthesis-flow agents.** Spent $1.99 on run #5; zero rows in DB; no way to read what each agent said. | `agent_reports` query for `decision_id LIKE 'plan-synth-%'` returns 0. `BaseAgent.run` builds an `AgentReport` dataclass (`base.py:665`) and emits WS events, but does **not** persist. Advisor/decisions flows have their own explicit persistence (`advisor.py:_persist_turn:157`, `decisions/flow.py:659`); synthesis has none. SDD line 364 acknowledges this as a known deferred gap. | **1-2 hrs** | Either: (a) write a row in `BaseAgent.run` after each agent finishes when a `decision_id` was passed, or (b) explicit `_persist_agent_report(...)` call in each phase helper. (a) is more general but touches all flows. |
| S3 | **No per-phase output persistence → no retry-resume.** A flake in phase 4 forfeits the $4 spent on phases 1-3. `decision_phases` IS invoked via `record_negotiation_phase(...)` at `orchestrator.py:303` — but only on the success path (after draft persistence). Failed runs never write phase rows. | `decision_phases=0` — user has never had a successful synthesis run, so the recorder has never been reached. Phase outputs only exist as in-memory strings during `run_synthesis()`. | **6-10 hrs** | Two things: (i) move phase recording earlier so failed runs leave a forensic trail (one row per phase as it completes, not one row at the end); (ii) add resume logic — detect last completed phase per `decision_run_id`, load its persisted output, skip the phase on retry. Touches the 5-phase orchestrator + a new `POST /api/advisor/check-in/{decision_run_id}/resume` route + UI affordance. Schema: probably extend `decision_phases` with a `phase_output_json` blob column rather than a new table. |
| S4 | **No partial-failure retry within a phase.** If 2 of 9 phase-1 analysts crash, the other 7's outputs are still consumed but the 2 crashed ones are silently dropped (logged but not retried). | Phase 1 loop catches per-agent exceptions and appends `"=== AgentName (FAILED) ===\n{exc}"` to the concatenated text. Synthesizer reads garbage-in-stub-out. | **2-3 hrs** | After S3 lands, add a `retry_failed_agents` flag that re-runs only the `FAILED`-marked agents on a retry. |
| S5 | **claude.exe transient exit-1 flake (handover gap #4).** Phase 2's bear_researcher hit this today. | Log: `bear_researcher: claude-agent-sdk error: Command failed with exit code 1 ... [claude.exe stderr was empty]`. | **30 min** | Already-designed: auto-retry-once in `_call_via_claude_code_inner`'s except block (handover gap #4). |
| S6 | **No cost cap per synthesis run.** Today's run was $1.99 because most agents crashed. A "good" run is $5-8 per SDD §3.10. No cutoff if a run goes wild. | No `cost_cap` field on `DecisionRun` or the run_synthesis signature. | **1 hr** | Add a soft cap; abort + mark `failed_cost_capped` when the running sum exceeds N. |
| S7 | **Weak forensic trail when synthesis fails.** Compound observability gap (S2 + S3 + phase recording only on success). User can spend $5+ on a failed run and have nothing but raw `application.log` lines to debug what went wrong. | Today's run #5 — $1.99 spent, fund_manager rejected, but zero structured artifacts in DB about what plan_synthesizer produced or what the risk team's verdict text said. | **0 hrs (subsumed by S2+S3)** | Once S2 + S3 land, the forensic trail is automatic — each agent_reports row carries `response_text` (full output) and each phase row carries the consolidated phase output. No new work required; surfaced separately here because the *consequence* (debugging failed runs) is its own UX concern. |

### Layer 3 — Product surface (the "Argosy talks back" gap)

| ID | Gap | Evidence | Effort | Notes |
|---|---|---|---|---|
| P1 | **No per-position thesis UI.** Owned tickers → no "Hold/Sell/Buy + conviction + reasoning" card today. | No `/positions/{ticker}` page; `/plan` shows critique findings but not per-stock verdicts. | **3-5 hrs** | Depends on S1 + S2 (analyst outputs need to actually exist and be persisted). UI itself: new `PositionsPage` with per-ticker accordion. Aggregates `agent_reports` filtered to `decision_id`. |
| P2 | **No "should add" candidate surface.** Spec'd via `PlanSynthesisOutput.short.speculative_candidates`; never made it to UI because never produced. | `proposals=0`; `/proposals` page exists but empty. | **2-3 hrs** | Once a draft exists (S1+S2+D1), `/proposals` should already work. May need polish on the speculative-candidate row rendering. |
| P3 | **No portfolio health card on `/plan`.** `plan_critique` ran today and produced 30k tokens of critique. Visible only in raw log. | `/plan` already shows `latest_critique_json` findings if a `plan_critiques` row exists. But `plan_critiques=0` — `plan_critique` output is never persisted as a `PlanCritique` row (vs an `agent_reports` row). | **1 hr** | Either: (a) have synthesis-flow `plan_critique` write to `plan_critiques` table too, or (b) update `/plan` to also surface critique-style outputs from `agent_reports`. |
| P4 | **No decision-tree visualization.** `<DecisionAccordion>` (home) + `<AgentDetailDrawer>` exist; they're empty for synthesis because no `agent_reports` (gap S2). | Existing UI primitives. | **0 hrs (once S2 lands)** | This is "free" after S2 — the cascade panel built today + the decision accordion both already surface the data. |
| P5 | **No retry button on a failed synthesis.** Today's failed run (`status='failed'`) has no UI affordance to resume. | No retry endpoint, no button. | **1-2 hrs** | Depends on S3. Add `[Resume from phase N]` button on the cascade panel when `synthesisError !== null` and the run reached at least phase 2. |
| P6 | **`agent.run.finished` cards show $0 cost** because synthesis WS events aren't backed by persisted rows. | User's observation today: "13 agents should all 0". | **0 hrs (once S2 lands)** | WS events DO carry cost. The cascade panel might be reading cost from REST rows (which don't exist) and defaulting to 0. Verify after S2; possibly a small panel-logic fix. |

### Layer 4 — Operational / hygiene (small but real)

| ID | Gap | Evidence | Effort | Notes |
|---|---|---|---|---|
| O1 | **WS `ws.send_failed` storm during phase-1 burst.** Today's log: 6+ `RuntimeError: Unexpected ASGI message 'websocket.send' after sending 'websocket.close' or response already completed` errors at synthesis start. | `argosy/api/main.py:144`. | **30 min – 2 hrs** | Likely a connection-state race. Either dedupe via send queue or detect `state=disconnected` before send. |
| O2 | **`_find_latest_tsv()` global mtime walk vulnerable to stray uploads.** This morning's $0k incident. | `argosy/api/routes/portfolio.py:53`. | **15 min** | Restrict to canonical portfolio dirs OR require the TSV to contain the "Bank account / funds allocation" header marker. |
| O3 | **Stale "running" `DecisionRun` rows.** Rows #2-3 from May 23 still `status='running'` — uvicorn restart orphaned the background tasks. | DB query above. | **30 min** | A startup-time sweep that marks any `status='running'` rows older than N hours as `failed` with `notes='orphaned by restart'`. |
| O4 | **Home page DecisionAccordion empty when only intake turns exist (handover gap #2).** | `useDecisionStream` falls back to `/api/agent-activity` only on HTTP error, not on empty array. | **15 min** | Already-designed handover gap #2 fix. |
| O5 | **TLH constraint unset (handover gap #3).** TaxAnalyst is ready but household hasn't toggled. | `constraints.tax_loss_harvesting_enabled` null. | **0 hrs** | User-driven via advisor batched `gap_driven` turn. Not a code change. |
| O6 | **RSU extraction shape mismatch (handover gap #1).** 23 RSU mentions in free-text notes, but `identity.rsu_grants` empty. | Blocks stage_3 → stage_4 advance. | **30 min – 2 hrs** | Prompt-tweak intake_extractor. |

### Layer 5 — Per-trade decision flow (separate but related)

The per-trade `decisions/flow.py` evaluates *individual* Hold/Buy/Sell decisions for a single ticker with the same analyst+researcher+trader+risk+FM team. **It has never run in production.** This is the OTHER major flow in Argosy. Once the data plumbing (Layer 1) is fixed, this flow can also start producing `proposals` rows. Out of scope for this gap doc but worth flagging — most of D1-D4 unlock this flow too.

## 4. Dependency graph

```
                  ┌─────────────────────────────────────┐
                  │ D1: shared input assembler         │
                  └──────────────┬─────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        ┌──────────┐      ┌──────────┐      ┌──────────┐
        │ S1: phase│      │ D2: API  │      │ D3: snap │
        │  1 fix   │      │   keys   │      │  persist │
        └────┬─────┘      └────┬─────┘      └────┬─────┘
             │                 │                 │
             ▼                 ▼                 │
        ┌──────────┐                             │
        │ S2: AR   │                             │
        │ persist  │◄────────────────────────────┘
        └────┬─────┘
             │
   ┌─────────┼─────────┬─────────┬─────────┐
   ▼         ▼         ▼         ▼         ▼
 ┌────┐   ┌────┐   ┌────┐   ┌────┐   ┌────┐
 │ P1 │   │ P2 │   │ P3 │   │ P4 │   │ P6 │
 │posn│   │spec│   │crit│   │tree│   │cost│
 └────┘   └────┘   └────┘   └────┘   └────┘
             │
             ▼
        ┌──────────┐
        │ S3 + S4  │
        │  resume  │◄── enables P5 (retry button)
        └──────────┘
```

**Critical path** (Layer 1 → Layer 2 → Layer 3):

1. **D1** (3-6 hrs) — synthesis-specific input assembler covering all 9 phase-1 signatures → unblocks **S1**, **D2**.
2. **S1** (1-2 hrs) — phase 1 wires through D1.
3. **S2** (1-2 hrs) — agent_reports persistence → unblocks **P1, P2, P3, P4, P6 simultaneously**.
4. After 1-3: synthesis will produce a real draft if fund_manager approves, with full visibility for every agent.
5. **D2-D5** (variable) — fill in real data so analysts have real material to reason on.
6. **S3-S6** (8-12 hrs) — cost/retry hardening + phase recording earlier in the flow.
7. **P1-P6** (5-10 hrs) — UI surface for what the system now produces.

## 5. Two paths forward

### Path A — Incremental (ship-then-iterate)

Order: D1 → S1 → S2 → run synthesis live → see what happens → iterate.

**Pros:** every step ships value within hours. After D1+S1+S2 you can click `[Run synthesis]` and see real analyst output for every owned ticker in the cascade drawer. That's already a major UX win over today's "$0 cards / no output."

**Cons:** UX surfaces (P1, P2, P3) come piecewise. Architectural decisions (cost cap, retry semantics, decision tree representation) get deferred and accreted.

**Total to "first useful draft":** ~7-10 hrs (D1+S1+S2 + 1 synthesis run to verify).
**Total to "full vision":** ~30-45 hrs spread across 6-10 sessions.

### Path B — Top-down design (design-all-first)

Order: write a master design doc covering all of L1-L4 + L5 implications + the UX surface (P1-P6) + retry/resume state machine + cost controls. Get Codex tandem review on the whole shape. Then implement bottom-up.

**Pros:** consistent architecture; no later "oh we should have done this differently" rework; the decision tree + per-position UX can be designed once, not bolted on.

**Cons:** 1-2 days of design before any code; the design will probably be wrong in places that only show up in implementation; defers the "first useful draft" by a week.

**Total time-to-first-useful-draft:** ~12-20 hrs (design pass + first implementation chunk).
**Total to full vision:** ~35-50 hrs spread but more cohesive.

## 6. My recommendation

**Path A, with a constrained scope.** Ship D1+S1+S2 in one session (~6-10 hrs); manually verify a synthesis run produces a draft and the cascade drawer surfaces each agent's reasoning. Then look at the actual output and decide whether the UX vision (P1-P3) needs a redesign or just polish.

The rationale: D1+S1+S2 are forced moves regardless of design. They have no architectural ambiguity — the SDD already specifies them. P1+ depends on what the analysts actually produce, which we won't know until D1+S1+S2 reveals it. Designing P1 before seeing the analyst outputs is premature.

If after D1+S1+S2 the output looks like it needs a different shape (e.g. analysts produce mostly empty payloads because D2 API keys aren't set), we re-evaluate; Path B becomes more attractive.

## 7. Open questions for Ariel

These I cannot answer alone; they shape the design:

1. **Cost ceiling per synthesis run.** SDD says $5-8 per run. With on-demand `[Run synthesis]` button you could rack up $50/day by clicking. Hard cap? Daily cap? No cap? (Default proposal: $10 soft cap per run, $30 daily soft cap for ariel's tenant.)
2. **Data source readiness.** Are Finnhub / SEC / TipRanks / CapitolTrades / FRED API keys set in your env? If not, D2 needs prep before S1's analysts will have anything to reason on.
3. **"Decision tree" — visual semantic.** Do you want a literal tree node graph (D3 / Mermaid / similar) or the existing flat AgentDetailDrawer-per-row drill-in? The latter is free after S2; the former is a 1-2 day new component.
4. **Per-position card scope.** Just owned holdings, or also "watchlist" / "considered" / "rejected" categories? The first is easy; the others need a watchlist table that doesn't exist.
5. **Retry granularity.** Resume from phase N (S3) or also from individual agents within a phase (S4)? Within-phase retry is materially more code.

## 8. Related but not in this doc

- **Layer 5 / per-trade decision flow** — same fixes (D1-D5) unlock it. Worth a sibling gap doc.
- **Argonaut snapshots / Wave-X argument tracking** — `argonaut_snapshots=0` suggests another never-run loop.
- **Insurance & pension coverage analysis** (proposed handover wave) — depends on the file_catalog work being solid; not in synthesis critical path.

---

**Last edit:** 2026-05-25 by Claude. Trigger: Ariel's observation that the live synthesis button produces nothing usable, leading to a "we're not there yet" reality check. Pre-existing handover gaps #1-4 cross-referenced as O5/O6/S5/O4 above.
