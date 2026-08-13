# Trust restoration — execution tracker

**Design:** `docs/superpowers/specs/2026-08-12-trust-restoration-design.md`
**Audit evidence:** `docs/superpowers/INDEX.md`
**Started:** 2026-08-13. **Branch:** `feat/trust-restoration`.

Discipline: money/decision units go **build → Sol (codex gpt-5.5) → fix → commit**. UI / read-projections skip Sol per CLAUDE.md. Mark each box the moment it lands; never mark ahead of the commit.

Legend: `[ ]` open · `[~]` in progress · `[x]` done (commit sha) · `[-]` dropped (reason)

---

## Phase 0 — DID_NOT_RUN ≠ PASS (keystone)

- [x] 0.1 `argosy/quality/verification.py` — `GateStatus` / `GateOutcome` contract + unit tests
- [ ] 0.2 Persist gate outcomes per decision run (migration + write path)
- [x] 0.3 `orchestrator.py:2657` — reader `None` ⇒ DID_NOT_RUN ⇒ not approvable **[Sol]**
- [x] 0.4 `codex_second_opinion.py` — return `GateOutcome`, no silent `(None,None)` **[Sol]**
- [x] 0.5 `plan.py:3839-3847` — publish-gate exception ⇒ DID_NOT_RUN, not bare `evaluate_promotion` **[Sol]**
- [x] 0.6 `plan.py:5688` `/refine` — compute `post_doc`; absent ⇒ DID_NOT_RUN **[Sol]**
- [x] 0.7 `email_digest` — unsent ⇒ job status `error`
- [ ] 0.8 `period_directive.py:205` — stale FX ⇒ DID_NOT_RUN on FX-dependent legs **[Sol]**
- [ ] 0.9 Operator override path — explicit `override_by` + reason, persisted; never implicit
- [ ] 0.10 Verification receipt surfaced (plan draft header + `/admin/jobs`)
- [ ] 0.11 `SDD.md:1354` corrected (it claims fail-closed today; make doc match code)

## Phase 1 — reconnect the pipes

- [x] 1A Unmanaged NVDA → analyst input — **NO CODE NEEDED: already fixed on master.** Disproved empirically 2026-08-13: `_summarize_positions` yields `NVDA qty=10940 value=$2,379.4k acct=schwab` (57.7%). Cherry-pick of `70008d4` was a no-op. Four handovers propagated a misread docstring.
- [x] 1B Real spend → plan burn (`inputs.py:1006`), `identity_yaml` as explicit fallback **with provenance** **[Sol]**
- [x] 1B.1 Fix `SDD.md` §6:544 — it claims this analyst already reads expense tables
- [x] 1C `/overview` page route + nav — 200, consistency 5/5 (first live exercise), typecheck clean
- [ ] 1D Deconcentration optimizer → API; confirm canonical CGT haircut binds to it **[Sol]**
- [ ] 1E Cal/Amex/Diners parsers — **ASK ARIEL FIRST** (only if those cards are held)

## Phase 2 — one number, one source

- [ ] 2.1 Bind NVDA cap / net worth / FI year / retention to canonical nodes at render **[Sol]**
- [ ] 2.2 Add IPS prose to the canonical-surface set (source of the 12% contradiction)
- [ ] 2.3 Cross-surface assertion: same figure key rendering two values ⇒ BLOCK

## Phase 3 — make the caps real

- [ ] 3.1 `instrument_classification` table (ticker → sector) + seed
- [ ] 3.2 Sector-cap check in `risk_preflight.py:180` **[Sol]**

## Phase 4 — the system speaks again

- [ ] 4.1 Configure `ARGOSY_SMTP_*`; verify a real send
- [ ] 4.2 Register one real web-push subscription end-to-end
- [~] 4.3 Mark Discord dead in `source_reliability` (stop presenting 434 stale predictions as live)
- [ ] 4.4 Triage the 59 open proposals

## Phase 5 — regenerate the plan (Ariel in the loop)

- [ ] 5.1 Restart backend on the new SHA
- [ ] 5.2 `POST /api/advisor/check-in`
- [ ] 5.3 Blind math re-derivation must **PASS** (not DID_NOT_RUN)
- [ ] 5.4 Present draft to Ariel — **never auto-accept**
- [ ] 5.5 Re-run `canonical_feasible_dual_track` on the true book

## Acceptance tests (from design §10)

- [ ] T1 Kill codex → plan **refuses to promote**, receipt names the gate
- [ ] T2 Break SMTP → digest job goes **red**
- [ ] T3 NVDA cap queried from every surface → **one** number
- [x] T4 `/overview` opens (200); consistency guard passes — but source is plan v92 until the regen
- [ ] T5 Regenerated plan's burn traces to `ExpenseTransaction`, not `identity_yaml`
- [ ] T6 A buy breaching the 35% tech cap is **refused** at preflight

---

## Log

- 2026-08-13 — tracker created; branch `feat/trust-restoration` cut; codex 0.147.0 confirmed (reviewer role → codex).
- 2026-08-13 — 0.1 GateOutcome contract landed, 11 tests green.
- 2026-08-13 — **1A closed as already-done.** The blocker to the plan regen is now Precondition B (0.4) alone.
- 2026-08-13 — `bcd9179` /overview shipped (200, consistency 5/5 first live exercise).
- 2026-08-13 — `a10edc1` Phase 0 fail-closed (0.3–0.7) + 1B real burn. 116 tests green.
  Sol found 1 real blocker (GateOutcome NameError in post_plan_refine -> 500 not 422); fixed pre-commit.
  Ariel rulings: thin-month threshold 50; planning burn rounds UP to nearest 1,000 (derived 24,032 -> 25,000).
- 2026-08-13 — **4.3 REOPENED.** Ariel: "we need to fix the feed". Diagnosis corrected: the Discord
  listener was DELIBERATELY disabled 2026-07-07 (`config.py:209`, reconnect bug ~150 supervisor
  restarts/day + Discord blocked the API + 0 signals since 2026-05-29). Auth-4004 errors date from
  2026-06-15, not 2026-07-08. This was a recorded decision, not a silent failure. The prior note says
  "re-enable after value review + fix" — the value review has never happened. AWAITING ARIEL.
