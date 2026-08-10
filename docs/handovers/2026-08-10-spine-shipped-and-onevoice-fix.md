# Handover — spine shipped (3a–3e) + one-voice verdict fix started (2026-08-10)

## Fresh-session reading order
1. This file (state + open items).
2. `git log --oneline -15` — authoritative recent activity.
3. Memory `feedback_one_voice_verdict_vs_plan` (Claude Code) — the binding ruling driving the current work.
4. `docs/design/SDD.md` §"Quickstart for new agents" only if you need the task→file router.

## Git state (authoritative)
- **master = `ccfa2f2`**, **ahead of origin/master by 1 (UNPUSHED).** Push only when Ariel asks.
- **Migrations head = 0100.** No new migration this session after 0100.
- Backend healthy; live book unchanged (no live-DB writes this session — all work on isolated tmp SQLite).

## What SHIPPED this session (all Sol-reviewed, committed)
The **spine** closed-loop system, on top of the 08-08/08-09 repair-fallout work:
- `fd4bf29` Phase 1 integrity floor — `integrity_verdict` + `validated_snapshot` gate (dormant).
- `a60fe67` Phase 2 decision ledger — three-record observed/validated/outcome (dormant).
- `3d85315` per-item value reconciliation made DIAGNOSTIC not a hard gate (prod-validated).
- `0975ffc` Phase 3 recording layer — auto integrity verdicts + fleet decisions to the ledger.
- `768c8b1` Phase 3e deterministic falsifier/revisit-trigger evaluator + auto-fire.
- `ccfa2f2` **Phase 3d all-holdings verdict coverage sweep** — 4 adversarial Sol rounds. Enumerates EVERY held symbol (ETFs/funds/REITs/bonds + durable unmanaged NVDA); daily 08:00 Asia/Jerusalem sweep escalates a capped re-verdict for uncovered/stale names. Completeness is GROUND-TRUTH (a fresh/changed settled `Verdict` row by (id, updated_at) identity), NOT outcome status — closes a false-cover-on-swallowed-write hole. Cooldown markers reuse `action_proposals` (note_only) and are excluded from the user proposal list/inbox/greeting/digest.
  - **CAVEAT:** the round-4 row-identity delta was self-verified by reading (codex went flaky and killed the final two confirm passes). A/C/D + the Defect-B false-cover-closed core were Sol-confirmed across rounds 1–3. Re-run one lean Sol confirm on `verdict_coverage.py::_coverage_attempt_completed` when codex is stable.

## The verdict-RESULTS review + the real finding
Ariel asked "are the verdict results good?" Two independent reviewers on a live sample: **Sol POOR / Claude MIXED**, converging on one root cause — the fleet emits **HOLD-only** verdicts (zero SELL/TRIM in the whole `verdicts` history) including NVDA at ~58% of the book.

**Root cause (traced + DB-confirmed):** a SPLIT-BRAIN between two registries, concentrated on NVDA.
- `position_stances[NVDA]` = **SELL** (source=plan, plan_verdict=SELL, review_verdict=HOLD, divergence=0). The stance projection is CORRECT — a fleet HOLD review can't override a non-HOLD plan.
- `verdicts[NVDA]` (settled, id=34, run 281) = **HOLD** "keep, don't add." The raw fleet output, which the 3d coverage sweep + 3e falsifier layer key off.
- The deep-decision fleet is STRUCTURALLY BLIND to the standing stance/pace: it sees company/market data + position shape + estate + its OWN prior settled Verdict (pushback gate) — but NEVER `position_stances` nor the plan pace/glide (`nvda_policy_sell`/`sigma_glidepath`/`target_progress` have zero refs under `argosy/decisions/**`). The ConcentrationAnalyst isn't even in the per-ticker fleet. So it re-derives "thesis intact → HOLD" and authors a Verdict contradicting the SELL stance.

**NOT "the fleet lacks a trim verb"** — trim ALREADY exists (plan pace chapter + `position_stances`=SELL). Ariel corrected this framing. The bug is the verdict SURFACE contradicting the standing decision.

## Ariel's binding ruling (memory `feedback_one_voice_verdict_vs_plan`)
**ONE VOICE PER POSITION**, resolved as **MIRROR-OR-PROPOSE-REVISION**: the per-holding verdict DEFAULTS to the standing stance; the fleet may only diverge by explicitly PROPOSING a stance revision justified by NEW FACTS (blind-reviewed); it may NEVER silently emit a bare HOLD over a standing SELL/TRIM. Consistent with "fix the team's inputs / blind-review, not a per-symptom deterministic gate."

## The one-voice fix — phased
- **Phase 1 — DONE, UNCOMMITTED, pending Sol review.** Stops the fleet AUTHORING the contradiction.
  - Seam #1 `argosy/services/decision_funnel/position_context.py` — new `_stance_lines()` renders a "STANDING PLAN STANCE (one-voice, authoritative)" block for the subject (reads `get_stances`), best-effort guarded, flows via existing channel to trader+risk+FM. Only SELL/TRIM stances impose a reconcile mandate.
  - Seam #2 `argosy/agents/trader.py` — `_STANCE_RECONCILE_RULE` in both prompt branches: no bare HOLD over a standing SELL/TRIM; MIRROR or write `PROPOSED STANCE REVISION:` with new facts; HOLD stays valid when there's no SELL/TRIM stance.
  - Tests: `tests/test_decision_funnel_position_context.py` (+6), `tests/test_trader_stance_reconcile.py` (new, 8). 25 pass + position_stance 3 pass.
  - Pace descriptor is **stance-only** (no cheap robust per-symbol quota accessor; deferred). TRIM-vs-SELL sizing left to the trader's normal reasoning — **Ariel to review this semantic** before it goes live.
  - Uncommitted files: `M argosy/agents/trader.py`, `M argosy/services/decision_funnel/position_context.py`, `M tests/test_decision_funnel_position_context.py`, `?? tests/test_trader_stance_reconcile.py`.
- **Phase 2 — pending.** The propose-revision ROUTING: consume the trader's `PROPOSED STANCE REVISION:`, run it through blind re-review, and (on survival) actually move the stance. Today nothing consumes the label.
- **Phase 3 — pending.** Pushback gate (`deep_decision.py:90` / `verdict_registry.check_pushback_gate`) also reads `position_stances` so a standing SELL contradicting a prior settled HOLD forces re-derivation; and flag `divergence` when a review contradicts the stance (today it's silently 0).

## Open queue (priority order)
1. **Sol-review Phase 1 → commit.** Then Ariel eyeballs the TRIM-vs-SELL mirroring wording (the one flagged semantic).
2. **Phase 2** propose-revision routing.
3. **Phase 3** pushback gate + divergence flag.
4. **Phase 3c enforcement** (spine) — route money surfaces through `read_validated_snapshot` to refuse a non-validated book. DEFERRED (behavior-changing).
5. Re-run the lean 3d Sol confirm when codex is stable (round-4 delta).
6. Push master when Ariel says.

## Traps / discipline
- **Codex (Sol) is flaky this session** — model `gpt-5.5`, pass `--model gpt-5.5` or omit; heavy prompts get killed, LEAN prompts finish. Kit at `tmp_review/_run_codex_review_long.py`.
- **NEVER write live `db/argosy.db`** except explicitly-approved reviewed migrations; read-only via `mode=ro`. Never run any coverage/backfill sweep against live.
- **alembic env.py trap:** programmatic `alembic.command.upgrade` ignores the cfg and can hit live — env.py now honors `-x db_url`/`ARGOSY_ALEMBIC_URL`; still, never run upgrades against live.
- **Working-tree noise (pre-existing, leave alone):** `.tmp_*` files, several `docs/handovers/2026-08-08-*` docs, `domain_knowledge/*.md` mods — all pre-existing/unrelated; stage explicitly by path, never `git add -A`.
- **Stash:** `stash@{0}` = unrelated `feat/stream-d-managed-holdings-abstention` WIP — preserved intact; do not pop it.
- **PYTHONIOENCODING=utf-8** for any script printing ₪/→/Hebrew; durable side-effects before printing.
