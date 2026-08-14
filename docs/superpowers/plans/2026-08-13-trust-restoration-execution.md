# Trust restoration — execution tracker

**Design:** `docs/superpowers/specs/2026-08-12-trust-restoration-design.md`
**Audit evidence:** `docs/superpowers/INDEX.md`
**Started:** 2026-08-13. **Branch:** `feat/trust-restoration`.

Discipline: money/decision units go **build → Sol (codex gpt-5.5) → fix → commit**. UI / read-projections skip Sol per CLAUDE.md. Mark each box the moment it lands; never mark ahead of the commit.

Legend: `[ ]` open · `[~]` in progress · `[x]` done (commit sha) · `[-]` dropped (reason)

---

## Phase 0 — DID_NOT_RUN ≠ PASS (keystone)

- [x] 0.1 `argosy/quality/verification.py` — `GateStatus` / `GateOutcome` contract + unit tests
- [x] 0.2 Persist gate outcomes per decision run (migration + write path)
- [x] 0.3 `orchestrator.py:2657` — reader `None` ⇒ DID_NOT_RUN ⇒ not approvable **[Sol]**
- [x] 0.4 `codex_second_opinion.py` — return `GateOutcome`, no silent `(None,None)` **[Sol]**
- [x] 0.5 `plan.py:3839-3847` — publish-gate exception ⇒ DID_NOT_RUN, not bare `evaluate_promotion` **[Sol]**
- [x] 0.6 `plan.py:5688` `/refine` — compute `post_doc`; absent ⇒ DID_NOT_RUN **[Sol]**
- [x] 0.7 `email_digest` — unsent ⇒ job status `error`
- [-] 0.8 `period_directive.py:205` — stale FX ⇒ DID_NOT_RUN on FX-dependent legs **[Sol]**
- [x] 0.9 Operator override path — explicit `override_by` + reason, persisted; never implicit
- [x] 0.10 Verification receipt surfaced (plan draft header + `/admin/jobs`)
- [x] 0.11 `SDD.md:1354` corrected (it claims fail-closed today; make doc match code)

## Phase 1 — reconnect the pipes

- [x] 1A Unmanaged NVDA → analyst input — **NO CODE NEEDED: already fixed on master.** Disproved empirically 2026-08-13: `_summarize_positions` yields `NVDA qty=10940 value=$2,379.4k acct=schwab` (57.7%). Cherry-pick of `70008d4` was a no-op. Four handovers propagated a misread docstring.
- [x] 1B Real spend → plan burn (`inputs.py:1006`), `identity_yaml` as explicit fallback **with provenance** **[Sol]**
- [x] 1B.1 Fix `SDD.md` §6:544 — it claims this analyst already reads expense tables
- [x] 1C `/overview` page route + nav — 200, consistency 5/5 (first live exercise), typecheck clean
- [x] 1D Deconcentration optimizer → API; confirm canonical CGT haircut binds to it **[Sol]**
- [ ] 1E Cal/Amex/Diners parsers — **ASK ARIEL FIRST** (only if those cards are held)

## Phase 2 — one number, one source

- [x] 2.1 Bind NVDA cap / net worth / FI year / retention to canonical nodes at render **[Sol]**
- [x] 2.2 Add IPS prose to the canonical-surface set (source of the 12% contradiction)
- [x] 2.3 Cross-surface assertion: same figure key rendering two values ⇒ BLOCK

## Phase 3 — make the caps real

- [x] 3.1 `instrument_classification` table (ticker → sector) + seed
- [x] 3.2 Sector-cap check in `risk_preflight.py:180` **[Sol]**

## Phase 4 — the system speaks again

- [~] 4.1 Configure `ARGOSY_SMTP_*`; verify a real send
- [~] 4.2 Register one real web-push subscription end-to-end
- [~] 4.3 Mark Discord dead in `source_reliability` (stop presenting 434 stale predictions as live)
- [x] 4.4 Triage the 59 open proposals

## Phase 5 — regenerate the plan (Ariel in the loop)

- [x] 5.1 Restart backend on the new SHA
- [x] 5.2 `POST /api/advisor/check-in`
- [x] 5.3 Blind math re-derivation must **PASS** (not DID_NOT_RUN)
- [x] 5.4 Present draft to Ariel — **never auto-accept**
- [ ] 5.5 Re-run `canonical_feasible_dual_track` on the true book

## Acceptance tests (from design §10)

- [x] T1 Kill codex → plan **refuses to promote**, receipt names the gate
- [ ] T2 Break SMTP → digest job goes **red**
- [~] T3 NVDA cap queried from every surface → **one** number
- [x] T4 `/overview` opens (200); consistency guard passes — but source is plan v92 until the regen
- [ ] T5 Regenerated plan's burn traces to `ExpenseTransaction`, not `identity_yaml`
- [x] T6 A buy breaching the 35% tech cap is **refused** at preflight

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

## Regen outcome — run 359 (2026-08-13)

Fired `POST /api/advisor/check-in` on backend SHA `0dce6f4`. Completed 19:15:48 after ~95 min.
Produced **plan 96 `synth-2026-08-13-1915-fm-rejected` (role=draft, decision_run_id=359)**.
**NOT promoted. NOT accepted.** Ariel has not seen it yet.

**The system refused its own plan, on substance:**
- **FM BLOCKER — FI sign flip on a user-locked fact.** The user directive locks honest-liquid margin at **+579,730 NIS => FI MET**, but the medium-horizon posture and `capital_sufficiency` assert **-186,670 NIS => FI NOT reached**. Codex independently returned `user_directive_respected=false` on the same point. Three risk officers warned the negative framing "manufactures urgency that could rationalize a rushed sale".
- **In-stage gate: GATE FAIL** — `headline_numeric_source=1`, `fact_placeholder_protocol=82`, **`cross_surface_coherence=1`**.
- Earlier in the run the **codex math gate BLOCKED** (`codex_review_unavailable`) and forced a reconcile round — the independent re-derivation could not verify the headline numbers, so it fail-closed rather than soft-passing.

**Prediction FALSIFIED (recorded so nobody repeats it):** I predicted a completed decision run would wake the deconcentration optimizer. It did NOT — the route still returns `nvda_current_pct: null, sell_nis: 0.0`, because it resolves from the **current** plan (v92, `decision_run_id=None`), not the newest draft. Fixing it needs either a promoted plan or a changed lookup. Do not assume the regen fixed it.

**NVDA now:** target **8%** (5 occurrences, the stale bare "12% direct target" is GONE). One 12% remains with new meaning — *"The 8% steering target sits inside the 12% hard cap"* — while code says `DEFAULT_NVDA_CAP_PCT = 0.13`. **This is the 12-vs-13 cap class CLAUDE.md names as a DERIVATION question: zigzag it, do not escalate.**

**Gap found in 0.2/0.10:** `gate_outcomes` is EMPTY for run 359. The persist call sits inside the FM-converge branch, so a run where FM rejects outright never writes a receipt. The receipt only covers the converge path. Needs moving to cover all terminal paths.

**Orphan warning:** plan 95 (run 358) was produced by pre-fix code and orphaned when the backend was restarted mid-synthesis. Superseded now, but **restarting the backend silently kills an in-flight synthesis** — add a pre-restart check for running decision_runs to the recipe.

---

## Sol review of the previously-unreviewed money paths (2026-08-14)

Ariel authorised merge **conditional on Sol review**. Running it on the three pieces Sol had
NOT seen (burn, NVDA single-sourcing, migrations) returned **6 blockers**, so the condition
was not met and the merge is held.

- [ ] S1 `inputs.py:1065` — **refunds never netted.** Query filters `direction=="debit"`, so a
      charge later refunded still counts as spend. Derived burn (₪24,032) is OVERSTATED.
- [ ] S2 `inputs.py:1057` — partial-month test counts post-filter spend debits, not all
      transactions; a complete but low-spend month can be wrongly excluded.
- [ ] S3 `household_budget_analyst.py:42` — fallback provenance is prompt text only, not on the
      report object; downstream math reading `monthly_burn_nis` cannot tell derived from typed.
- [ ] S4 `inputs.py:1222` — YAML fallback bypasses the round-up rule (typed 24,001 → 24,001).
- [ ] S5 `inputs.py:1199` — any burn exception is swallowed and mislabelled "insufficient
      transaction data" when the truth is "computation failed". The exact pattern this branch
      exists to remove, reintroduced by this branch.
- [ ] S6 `assembled_artifact.py:282` — **NVDA coherence never compares the rendered PROSE.** It
      compares canonical values to other canonical values, so a stale prose "12%" passes clean.
      Live proof: plan 96 says *"the 8% steering target sits inside the 12% hard cap"* while code
      says `DEFAULT_NVDA_CAP_PCT = 0.13`, and the gate did not fire.

## WORKSTREAM 2 (next) — close the FM objection loop

Ariel's framing, which is correct and matches the CLAUDE.md doctrine: *"if I go to an agency and
ask for a plan, all agents work together to reach a plan they agree with; they don't come back
with 'failed'. They CAN and SHOULD come back with questions — if input was lacking, or if there
are several VALID options and it's a matter of user preference, ASK the client."*

**Diagnosed root cause of run 359's dead end** — `dispatched=0, total_objections=7`:
1. **Owner routing is a regex on prose.** `fm_objection_dialogue._parse_analyst_refs_any_form`
   scans objection text for a literal `agent_report:Name` token. Two LLM agents hand off through
   a string format neither is obliged to honour.
2. **Two named agents are absent from the owner map**: `WithdrawalSequencerAgent → None`,
   `FXAnalystAgent → None`. A correctly-formatted citation to either routes nowhere.
3. **The convergence loop is default-OFF** — `ARGOSY_FM_DIALOGUE_CONVERGE` defaults `"0"`
   (`orchestrator.py:2660`).
4. **Unroutable objections die at log level INFO.** Seven BLOCKERs from the fund manager
   evaporated without a warning.

**Planned fix (team-shaped, not gate-shaped):**
- [ ] W2.1 Owner resolution by JUDGMENT — when no owner parses, ask an agent "who owns this?"
      rather than dropping it. Doctrine-correct: a routing failure caught by another agent.
- [ ] W2.2 Add the missing roles to `ANALYST_AGENT_NAME_TO_ROLE`.
- [ ] W2.3 An unroutable BLOCKER is a loud failure, never an INFO line.
- [ ] W2.4 Real `needs_user_input` path: converge (peer rounds → arbiter), and if the dispute is
      a genuine fork or the input was missing, **ask Ariel a concrete question**. For run 359 the
      right question was: *"Your directive locks honest-liquid margin at +579,730 (FI MET); the
      retirement model derives -186,670. Which basis governs — and if yours, what does the model
      have wrong?"*
- [ ] W2.5 Decide whether `ARGOSY_FM_DIALOGUE_CONVERGE` should default ON.
