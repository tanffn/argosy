# Wave 7 — Convergence, triage, and scoped re-synthesis

**Drafted:** 2026-06-01 (late afternoon, wave 6 scoping closed)
**Status:** scoping only — not approved, not started
**Triggered by:** Ariel's three-ask after burning a day on synth loops #58 → #61

## What this wave fixes

Today's failure mode in one sentence: **"I answered the FM's objections, ran another synthesis, got new objections, plan still not done."** Three structural gaps cause it:

| Gap | Today | After wave 7 |
|---|---|---|
| **FM verdict shape** | Binary `approved=bool` + flat list. Plan rejected if ≥1 objection. | Triage agent classifies each objection: system-resolves OR human-in-the-loop. Plan ships when human-in-the-loop list is empty. |
| **Stance carry-forward** | Stances scoped to `plan_version_id`. New draft = stances forgotten. | Stances carried across drafts via `topic_hash` + semantic similarity. FM cannot re-raise a user-resolved concern without citing what changed in the new plan. |
| **Re-synthesis scope** | "Start new round" runs full 5-phase fleet, ~30-40 min. | Scoped re-run: only the horizons / topics the user's decisions touch. Target: 5-10 min for narrow rounds. |

## Three pieces of work

### Piece A — Triage agent (the central wave-7 piece)

A new agent runs **between phase 5 (FM verdict) and the user-facing /plan UI**. Reads each FM objection, evaluates it against the prime directive + the plan + the user's prior decisions, and classifies into exactly two buckets:

**(1) `system_resolves`** — the agent proposes a concrete resolution that doesn't need user input. Two sub-kinds:
  - `auto_dispatch`: route to an analyst (existing wave-3 path, widened to handle objections that don't carry an explicit `agent_report:X` citation). Triage picks the most-relevant analyst from `{tax, fx, concentration, plan_critique, household_budget, …}` based on the objection topic.
  - `defined_fix`: directive-applied obvious fix the synthesizer should auto-apply on the next pass (e.g., "push the tranche window 5 days to clear the estate trip-wire gate"). Triage emits the proposed patch as structured text the next-round synthesizer prompt consumes.

**(2) `human_in_loop`** — genuine value judgment only Ariel can make. Risk-tolerance trade-off, vehicle preference, premise correction, ethical line. Surfaces as a forced-decision card on /plan with the FM concern + the triage agent's proposed-resolution-options + a "your call" CTA.

**Plan ships (verdict = APPROVED-WITH-NOTES) when `human_in_loop == []`.** The system_resolves bucket flows through auto-dispatch / defined-fix application invisibly. User sees only the cards that genuinely need them.

#### Triage agent shape

```python
class TriageClassification(BaseModel):
    objection_index: int                       # back-reference to FM objection list
    classification: Literal["auto_dispatch", "defined_fix", "human_in_loop"]
    proposed_resolution: str                   # the directive-applied answer
    analyst_owner: str | None                  # when auto_dispatch — which analyst
    defined_fix_patch: str | None              # when defined_fix — structured text for synth prompt
    human_decision_options: list[str] | None   # when human_in_loop — concrete option list
    directive_reasoning: str                   # how the prime directive guided this call
    confidence: ConfidenceBand
    cited_sources: list[str]
```

Model: Opus (heavy reasoning, but cheap class of problem). Carries the prime directive in its system prompt + reads the FM verdict + prior-draft stance map + current draft plan as context.

Plan ships when every objection in the FM verdict has a triage classification AND `[t for t in triage_classifications if t.classification == "human_in_loop"] == []`.

### Piece B — Stance carry-forward across drafts

`fm_objection_user_state` already has `topic_hash` (SHA-256 of `topic\ndetail`, first 16 hex chars). Today it's used for staleness detection within one draft; wave 7 uses it for cross-draft matching.

On new-draft commit:

1. For each new-draft FM objection, compute its `topic_hash`.
2. Look up prior-draft objections with the same hash OR ≥0.85 cosine-similarity on the topic+detail embedding.
3. If a match exists with stance `AGREE` (with optional resolution note): pass that resolution to the triage agent as **prior context** — *"the user previously resolved this concern this way; only re-raise if conditions changed."*
4. If a match exists with stance `DISAGREE` + counter-position: thread the counter-position into the next-round synthesizer prompt automatically. User doesn't re-type it.
5. If a match exists with stance `DEFER`: ignored — user explicitly said "skip this round."

The triage agent in Piece A consults this carry-forward map when classifying. A previously-resolved concern that the synthesizer's new draft doesn't materially change should never re-surface as a human-in-loop card.

### Piece C — Scoped re-synthesis on "Start new round with my decisions"

Today: every `start-new-round` invocation runs `assemble_phase1_inputs` + all 9 phase-1 analysts + the phase-2 debate triplet + phase-3 synthesizer + phase-4 risk officers + phase-4.5 codex + phase-5 FM. ~30-40 min wall-clock for what's often a 2-paragraph adjustment.

Wave-7 shape: a **scope inspector** runs first and returns the minimal phase set that must execute.

- If user decisions touch only `short.actions` → re-run phase-1 analysts that contribute to short (concentration, fx, tax, plan_critique), skip the rest, re-run phase-3 synthesizer ONLY for the short HorizonSection (long + medium carry forward verbatim), skip phase-2 debate (no horizon-level change), re-run phase-4 risk + phase-5 FM against the patched plan.
- If user decisions touch a long-horizon target → wider scope; might need full phase-2 debate + full synth.
- If user decisions touch only tax-substrate → only tax + plan_critique analysts, no synthesizer re-run (just append to short.actions), skip phase-2 entirely.

Target runtime by scope:

| Scope | Phases | Wall-clock target |
|---|---|---|
| Tax-substrate only | Phase 1 (subset: tax + plan_critique) → phase 5 FM | ~5 min |
| Single-horizon adjust | Phase 1 (subset) → phase 3 (single HorizonSection) → phase 4 → phase 5 | ~10 min |
| Multi-horizon refactor | Full fleet | ~30 min (unchanged) |

The orchestrator gains a `--scope` arg or a structured `ScopeSpec` object; phase functions become aware they may run against a partial scope.

## Why these three together, not separate waves

Piece A without B → triage agent re-classifies the same concerns every round because it doesn't know the user resolved them. Piece B without A → carried-forward stances reach the synthesizer prompt but the FM still gates on binary verdict. Piece C without A+B → scoped re-runs still produce binary FM verdicts that gate on now-stale concerns.

The three asks compose into one user experience: **"I answered the questions only I could answer; the system applied directive-driven fixes to the rest; the plan is done."**

## Scope checklist (wave 7)

- [ ] **Migration 0061_objection_triage**: `objection_triage_classifications` table (objection_index, classification, proposed_resolution, analyst_owner, defined_fix_patch JSON, human_decision_options JSON, directive_reasoning, confidence, cited_sources JSON, decision_run_id FK, created_at)
- [ ] **Pydantic types**: `argosy/agents/triage_types.py` with `TriageClassification` + `TriageOutput`
- [ ] **New agent**: `argosy/agents/objection_triage.py` — `ObjectionTriageAgent`. Opus by default. System prompt embeds the prime directive verbatim + the classification contract + how to use the carry-forward map
- [ ] **Phase 5.5 wiring** in `plan_synthesis/orchestrator.py`: after FM verdict commits, dispatch the triage agent against the FM's objection list + carry-forward map. Phase output is the `TriageOutput`
- [ ] **Auto-dispatch widening**: extend `schedule_auto_dialogues_for_draft` to consume `triage.classification == "auto_dispatch"` entries with `analyst_owner` set, not only objections with literal `agent_report:X` citations
- [ ] **Defined-fix application**: new step between FM verdict and `/plan` UI surface that takes `triage.classification == "defined_fix"` entries and patches the synthesizer's draft accordingly. Either re-runs phase 3 against the patched prompt (if material) or hot-patches the draft in place (if cosmetic)
- [ ] **Carry-forward matcher**: `argosy/services/objection_carry_forward.py` — given a new draft's FM objections + the prior-draft state map, return the matched-stance map keyed by current-draft objection index. Topic-hash exact match first, embedding-similarity fallback at threshold 0.85
- [ ] **Pass carry-forward to FM + triage prompts**: the prior-draft AGREED/DISAGREED notes are threaded into the FM's plan_revision prompt AND the triage agent's prompt as "prior-resolved" context
- [ ] **Scope inspector**: `argosy/services/plan_synthesis/scope.py` — given a stance map, return `ScopeSpec(horizons, topics, agents)` listing what needs re-running
- [ ] **Phase functions accept ScopeSpec**: `_run_phase_1_analysts`, `_run_phase_3_synthesizer`, `_run_phase_4_risk`, `_run_phase_5_fm` consume a scope arg and short-circuit when out-of-scope
- [ ] **Verdict-surfacing rewrite**: `/api/plan/draft/objections` returns the triage-classified list, with `human_in_loop` rows at the top and `system_resolves` rows in a collapsed audit footer
- [ ] **/plan UI update**: forced-decision card layout for human_in_loop items; auto-resolved items in the existing "resolved among the fleet" footer (already wave-3)
- [ ] **APPROVED-WITH-NOTES verdict state**: new DecisionRun terminal state that means "plan ships even though FM raised concerns, because triage marked all of them system-resolves"
- [ ] **Tests**: triage classification roundtrip; carry-forward matcher (exact hash + similarity); scope inspector boundary cases; phase functions respect scope; UI render of mixed human-in-loop + system-resolves; APPROVED-WITH-NOTES end-to-end

## Open questions (kept minimal per the directive)

1. **Triage agent override authority.** When triage classifies a concern as `defined_fix` and proposes a patch, can Ariel review/reject the auto-applied fix before it commits? Two shapes: (a) hard-apply, surface in the "resolved among the fleet" footer for audit (no friction; trusts triage); (b) soft-propose for one round, then auto-apply on the next if Ariel doesn't intervene (one-round grace period). **My read:** (a) for AMBER/YELLOW, (b) for RED. RED gets one-round grace because the cost of a wrong auto-apply on a RED is high.
2. **Carry-forward threshold tuning.** Topic-hash exact match is unambiguous. Embedding-similarity at ≥0.85 is empirically arbitrary — could be 0.80 or 0.90. **Proposed:** start at 0.85, log every match for a calibration window, tune from data. Not a wave-blocking decision.

(Notably *not* an open question: "what's the right schema for triage classification" — Piece A above is the answer, codex can poke at the shape if it wants.)

## What this wave does NOT do

- New Anthropic-API-side feature work (no caching tuning, no batch-API integration). Existing agent infra extends naturally.
- UI redesign of /plan beyond the forced-decision card layout for human_in_loop items.
- Backfill triage classifications onto historical drafts — only new drafts get triaged.
- Replace the existing wave-3 auto-dispatch hook — extend it.

## Dependencies on other in-flight work

- **Wave 5 fixes must be stable in production** — substrate analysts producing real outputs (confirmed by run #61). If macro/news/tax/fundamentals continue to drop, the triage agent's classification quality degrades because the FM's input substrate is degraded.
- **Wave 6 is parallel-compatible** — wave 6 ships enforcement substrate (typed PlanPolicy, instrument_classification, sector-cap preflight). Wave 7 ships triage / carry-forward / scoped re-synth. They share no code paths; could ship in either order.

## Timeline estimate

| Piece | Days |
|---|---|
| A — Triage agent + phase 5.5 wiring + verdict-surfacing rewrite + UI card | 2-3 days |
| B — Carry-forward matcher + prompt threading | 1 day |
| C — Scope inspector + phase functions accept scope | 2-3 days |
| Total wave 7 | ~1 week of focused sessions |

Recommended ship order: **B first** (lowest risk, biggest user-pain reducer for the "I already answered this" complaint), then **A** (the central piece, unlocks the convergence policy), then **C** (the polish — makes iterations fast).

Per the prime directive: every piece must pass *"does this advance FI faster?"* B saves the user from re-answering ≈3-5 questions per round = ~30 min saved per round = real time. A makes the plan ACTUALLY ship → unblocks action. C makes re-runs feel fast → less friction → more rounds attempted = better-tuned plans. All three pass.
