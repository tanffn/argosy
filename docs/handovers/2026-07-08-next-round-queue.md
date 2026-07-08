# Handover — 2026-07-08 · next-round queue (Ariel-reviewed) after the FM-home/critique-loop marathon

**Branch:** `master` · **HEAD:** `065c2c7` · tree has one uncommitted cosmetic edit (`synthesis_liveness.py` UTC-alias rename — safe, commit or drop).
Read this first, then `docs/handovers/2026-07-07-scheduler-audit-sdd-refresh.md` (§UPDATE, §3b–3d — yesterday's full arc), then `git log --oneline -40`.

## 0. Where things stand

Live and verified: FM greeting (needs_you = 2 real decisions), charts with 12 months of history (Jul-25 $2.96M/80.5% NVDA → today $3.98M/57.3%), tax-year NVDA pace on Schwab CSV truth (3,380 sold in 2026), critique on **Fable 5** + the bounded reconcile loop (FIND→CORRECT→RE-VERIFY, escalations aggregate to ONE inbox decision), closed-loop fill verifier armed, proactive directive loop, smart news intake, loop-tier adoption + catch-up. The first full autonomous daily cycle runs today — **verify-run after it**.

## 1. ARIEL'S QUEUE FOR THIS ROUND (his words paraphrased, in his order)

1. **Critique-FED re-synthesis (design first — do NOT run the re-synthesis until this exists).** "If I run re-synthesis, will it feed in the Critique? It should not start from zero — it will probably make the same mistakes. Feed in the Critique and make corrections — harder design, but cheaper run and a more correct one." Design: the re-synthesis consumes the latest critique's findings + the reconcile outcomes as STRUCTURED corrective guidance (per-finding: what's wrong, the canonical value/derivation, which surface) — an edit-with-corrections run, not from-scratch. Touchpoints: `plan_synthesis` guidance param, the coherence-deliberation mechanism, the incremental plan graph (edit-don't-rebuild is the standing doctrine — this extends it to synthesis). The pending run must clear: 9 aggregated critique findings (proposal `critique_resynth:ariel`) + apply the glide-adjudication verdict (proposal 49).
2. **Wealth trajectory is SLOW → derived cache.** Recompute on change + cache (version-keyed derived-cache pattern exists — see memory `project_overview_explainer_and_derived_cache`; warming is SEQUENTIAL, parallel backfired). The legacy-TSV backfill parse likely runs per request.
3. **Plan tab "NVDA share trajectory" panel vs the home Deconcentration panel — do they match?** Consistency check (one canonical derivation — the deconcentration/pace unification precedent from yesterday applies).
4. **Retirement tab ERROR: "Couldn't load your plan story"** — /api/overview (fact-registry-bound plan story). Investigate — possibly broken by yesterday's plan_export/critique changes, or the derived cache.
5. **Proposal 49 vs the current glide — which is more correct?** Answer properly: 49 IS the adjudicated schedule (fast-on-eligible-core, 2026/27/28 = 4,136/5,094/592, §102-feasible) vs the current 12-mo glide (infeasible last ~592 shares at capital rates). Present the comparison crisply; his confirm applies it via the (now critique-fed) re-synthesis.
6. **PlanAdherenceCard crash — FIXED tonight** (`065c2c7`, tolerate flat /api/jobs shape) — but the ROOT is the api.ts `JobView` DTO claiming nested `metadata` that the API never serves; **audit JobsTable + fix the DTO to the real shape** (JobsTable uses `.metadata.` throughout — either it transforms somewhere or /admin/jobs is silently broken).

## 2. New defects noticed in passing (not yet fixed)

- **state_observer_daily errored** (2026-07-07 14:00): `UNIQUE constraint failed: state_snapshots.user_id, snapshot_date` — the one-row-per-day table collides when the observer runs twice in a day (catch-up + scheduled). Upsert or skip-if-exists.
- Critique cites domain_knowledge files it isn't handed (input plumbing; harmless, flagged by the now-trustworthy detector).
- Genuinely-OVERDUE action items (the June-17 vest) do NOT surface in greeting needs_you — only "looks executed" confirms do. The overdue class needs a needs-you entry ("you need to sell the June vest — overdue since Jun 17").

## 3. Standing open items (carried)

- **June-17 vest sale** — genuinely unsold per the Schwab ledger (Ariel's real-world action).
- Funnel un-shadow as labelled beta (graduation counter) — still queued.
- `test_api_phase4.py` first-test hang → fix, then overnight full suite with pytest-timeout (845 passed / 0 failed before the hang; yesterday's 3 failures never reproduced).
- Catchup `KeyError` startup race (loops catch-up racing registry adoption — 19 log hits).
- Refinement path needs a whole-artifact reader (inherited surfaces re-checked at edit time).
- Backend persistent hosting (auto-start) — Ariel hasn't decided.
- Bank DPYA reply → IWDP→DPYA swap; next cash tranche EXUS-first; **next real broker ingest auto-verifies the armed closed-loop expectations** (SGOV fill price still an estimate).
- NVDA→employer-equity-class refactor (~148 files) — noted, unscheduled.
- Discord listener disabled — value review before any fix.
- Macro calendar hardcoded through 2026-12-16 (warns from Nov 16); phase C2+ signal sources (Form 4, congressional, 13F) wait for the predictions-parser fix.

## 4. Discipline notes for the next session

The team is the architecture; determinism = arithmetic floor only. Blind re-derivation caught real money bugs FIVE times yesterday (phantom deploy, buy-NVDA legs, stale allocations, wrong pace basis, false vest-confirm) — keep routing every "is this right?" through raw-data re-derivation, never trust derived tables or rendered charts. Charts/lineage rule: every rendered number traceable to raw rows; no cosmetic absorption. One decision = one inbox row. Subagent-driven; SendMessage to resume a finished agent keeps its context.
