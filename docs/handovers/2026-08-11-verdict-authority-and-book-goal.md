# Handover — one-voice shipped+pushed; verdict-authority follow-ups next (2026-08-11)

## Fresh-session reading order
1. This file (state + active goal + next task).
2. `git log --oneline -12`.
3. `docs/handovers/2026-08-10-spine-shipped-and-onevoice-fix.md` — full spine (3a–3e+3d) + one-voice Phase 1–3 detail.
4. Memory `feedback_one_voice_verdict_vs_plan` (Claude Code).

## Git state (authoritative)
- **master = origin/master = `2f32bd8`** (PUSHED, in sync). Migrations head **0100**. Backend healthy; no live-DB writes (all work on tmp SQLite / a hash-guarded DB copy).
- Push note: `git push` is blocked by the harness auto-approve classifier — Ariel runs `! git push origin master` himself.

## ACTIVE GOAL (Ariel's /goal, 2026-08-11)
Every position in the book carries a verdict you can trust: (1) one-voice (never contradicts the plan/stance), (2) fresh & substantive (no stale placeholder wearing "settled"), (3) accurately grounded (any plan number it cites is actually in the plan — no hallucinated "40%"), (4) calibrated & falsifiable. **"Closed" = the known defect classes are fixed (one-voice ✅ + #32 plan-number grounding + #33 freshness/substance floor) AND a fresh independent review of a NEW 5-position batch comes back GOOD — not MIXED.** That review coming back GOOD is the finish line.

## Done this session (all committed + PUSHED, Sol-passed)
- **Spine** 3a–3e + **3d** all-holdings coverage (see 08-10 handover).
- **One-voice fix Phases 1–3**: `185ceee` inject stance + trader reconcile rule; `ec0d43d` surface stance-revisions for approval (never auto-flip — 4 Sol rounds, gameable auto-flip caught & redesigned); `6eba133` gate forces ONE re-derivation of a stale stance-contradicting verdict (loop-bounded) + divergence flag.
- **Empirical validation** (fixed fleet on 5 positions vs a hash-guarded DB copy; real DB untouched): **NVDA HOLD→SELL reconciliation PROVEN** (gate forced re-run → fleet mirrored the SELL, frame = concentration+estate not thesis, + falsifiers + Aug-26 trigger). AMD/NOW correctly DEFENDED; IWDP honest quorum-abstain; BMY stale seed persisted.
- **Dual review (Sol + Claude) = MIXED, materially improved from POOR/MIXED.** Both flagged: NVDA's flagship falsifier hallucinated a "stop trimming at ~40%" target (real = 8% steering / 13% cap; 40% is the estate-tax rate) → **#32**; DEFENDED path has no freshness/substance floor (BMY 43-char month-old seed gets full authority) → **#33**. Both named the same fix: a verdict-authority check.

## NEXT TASK — build the VERDICT-AUTHORITY check (#32 + #33 as one unit), then re-batch
Design per philosophy (`agents/_plan_authority.py:19` — plan is NOT obedience; catch *contradiction/fabrication*): **#32 grounding = agent/blind check; #33 freshness = deterministic floor.** Scoped seams (read-only trace, 2026-08-11):
1. **#33 deterministic freshness/substance** — `verdict_registry.check_pushback_gate` DEFEND returns (`:406-414` no-facts, `:448-455` facts-miss): add a branch that returns `allowed=True` (force ONE re-derivation, loop-bounded exactly like `_stale_verdict_contradicts_stance` `:324` — a forced run bumps `updated_at`, self-limits) when the settled verdict is stale (`now-updated_at > N days`) OR thin (`len(reasoning_md) < floor` / falsifier count / `flow.py:_is_degenerate_falsifier` `:177`). MUST copy the tz-safe fail-SAFE discipline (`_predates` `:308`) and the PLTR-scar "don't churn winners on noise" caution (`flow.py:157-160`).
2. **#32 grounding (cheap half)** — `flow.py::_coerce_verdict_falsifiers` (`:196`, already runs before `write_verdict` and already floors falsifiers): thread in the plan targets and FLAG (do NOT silently drop) a falsifier whose plan-attributed number contradicts them.
3. **#32 grounding (judgment half)** — extend `agents/fund_manager.py` (the in-fleet agent that already reads the target allocation) or a small blind numeric-grounder modeled on `base.py:_detect_hallucinated_sources` (`:2914`): hand it the authoritative targets and have it flag the trader's mis-grounded number (40% conflates the estate-tax rate with a concentration target — a SEMANTIC check, not substring presence).
- **Authoritative plan targets (load these, NOT the LLM prose distillate):** `allocation_plan.NVDA_TARGET_PCT = 8.0` (`:79`) + `scenario_mc.DEFAULT_NVDA_CAP_PCT = 0.13` (`:553`), overlaid by `load_plan_target_allocation(pv).nvda_cap_pct` (`target_allocation_doc.py:841/:100`) when a per-version doc exists. NO migration (all fields/constants exist).
- Any DB read added to the gate MUST stay inside its best-effort try/except (gate "must not crash stage 3" — `deep_decision.py:112`).
Then: **run a NEW batch of 5 positions** (different names, e.g. GOOG/AMZN/TSLA/META/SCHD or the next uncovered set) via the harness at `scratchpad/run_fixed_fleet.py` (copy DB → `init_engine(copy)` → `run_deep_decision(long_hold)` → hash-guard real DB) → **dual independent review** → GOOD closes the goal.

## Open (non-goal) queue
- #24 Phase 3c enforcement (money surfaces refuse a non-validated book) — deferred, behavior-changing.
- Re-run the lean 3d Sol confirm (round-4 delta) when codex is stable.

## Traps / discipline
- **Codex/Sol flaky** — model gpt-5.5, LEAN prompts finish / heavy get killed. Kit: `tmp_review/_run_codex_review_long.py`. Each phase: build → Sol → fix → commit on PASS.
- **Run harness:** `scratchpad/run_fixed_fleet.py` — always run the fleet against a COPY (`init_engine(copy)`) with a real-DB md5 guard; NEVER write live `db/argosy.db`. Fleet gets its Anthropic key from `Settings` (not shell env). `ARGOSY_DB_FILE` env does NOT override (db_file is default_factory'd) — use `init_engine(<copy-url>)`.
- Stage commits explicitly by path, never `git add -A` (`.tmp_*`, old handovers, `domain_knowledge/*.md` are pre-existing noise). Don't pop `stash@{0}` (unrelated WIP).
- PYTHONIOENCODING=utf-8; durable side-effects before printing.
