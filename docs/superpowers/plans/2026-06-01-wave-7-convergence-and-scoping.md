# Wave 7 — Convergence, triage, and scoped re-synthesis

**Drafted:** 2026-06-01 (rev 2 — post codex zigzag converge)
**Status:** scoping locked; ready for user approval to start
**Triggered by:** Ariel's three-ask after burning a day on synth loops #58 → #61

## What this wave fixes

Today's failure mode in one sentence: **"I answered the FM's objections, ran another synthesis, got new objections, plan still not done."** Three structural gaps cause it:

| Gap | Today | After wave 7 |
|---|---|---|
| **FM verdict shape** | Binary `approved=bool` + flat list. Plan rejected if ≥1 objection. | Triage agent classifies each objection into one of 5 outcomes; plan ships (APPROVED-WITH-NOTES) when human_in_loop list is empty. |
| **Stance carry-forward** | Stances scoped to `plan_version_id`. New draft = stances forgotten. | Stances carried across drafts via deterministic topic_hash → embedding-fallback matching stack with ambiguity guard. FM cannot re-raise a user-resolved concern without citing what changed. |
| **Re-synthesis scope** | "Start new round" runs full 5-phase fleet, ~30-40 min. | Scoped re-run: only the phases whose inputs are actually affected. Target: 5-10 min for narrow rounds. |

## Three pieces of work

### Piece A — Triage agent (the central wave-7 piece)

A new agent runs **between phase 5 (FM verdict) and the user-facing /plan UI**. Reads each FM objection, evaluates against the prime directive + the plan + the user's prior decisions + carry-forward map, and classifies into **one of five outcome semantics**:

**(1) `auto_dispatch`** — route to an analyst (existing wave-3 path, widened). Carries a `dispatch_strategy` controlling depth:
  - `one_shot` (default): single analyst response + FM verdict, one cycle. Today's wave-3 behavior.
  - `iterative`: bounded multi-turn back-and-forth (e.g., FM ⟷ tax analyst negotiating cost-basis nuance). Bounds: `max_turns=3` AND `max_wall_clock=120s` AND `max_token_budget`. Exhausting ANY bound escalates to `human_in_loop` with the dialogue trail preserved as evidence.

**(2) `defined_fix`** — directive-applied obvious fix the synthesizer auto-applies on the next pass (e.g., "push the tranche window 5 days to clear the estate trip-wire gate"). Triage emits the proposed patch as structured text. **Gated by a four-stage safety pipeline before any commit**:
  - patch produced → schema/type validation against `PlanSynthesisOutput` → targeted risk-officer re-check on touched fields (phase 4 subset) → FM delta-check on touched objections only → commit
  - **Any stage failure downgrades the classification to `human_in_loop`** with the failure reason as evidence
  - **Launch flag:** soft-propose-only for the first 50 drafts; defined_fix is opt-in per classification confidence until calibration data justifies hard-apply

**(3) `human_in_loop`** — genuine value judgment only Ariel can make. Risk-tolerance trade-off, vehicle preference, premise correction, ethical line. Surfaces as a forced-decision card on /plan with the FM concern + the triage agent's proposed-resolution-options + a "your call" CTA.

**(4) `invalid_objection`** — FM emitted a malformed, empty, or self-contradictory objection. Distinct from `human_in_loop options=[]` (which is a real but option-less user call). Mandatory machine reason + evidence; surfaces in audit log only, NOT in user-facing list.

**(5) `dismissed`** — FM raised a concern that downstream evidence shows is non-applicable (e.g., concern about a vehicle the plan has since dropped, or about a constraint the new draft already honors). Mandatory machine reason + evidence pointing at what made it non-applicable; surfaces in audit log only.

**Plan ships (verdict = APPROVED-WITH-NOTES) when `human_in_loop == []`.** The other four outcome classes resolve invisibly via auto-dispatch / defined-fix application / audit-log entry. User sees only the cards that genuinely need them.

#### Triage agent shape

```python
class TriageClassification(BaseModel):
    objection_index: int                                # back-reference to FM objection list
    classification: Literal[
        "auto_dispatch", "defined_fix", "human_in_loop",
        "invalid_objection", "dismissed",
    ]
    dispatch_strategy: Literal["one_shot", "iterative"] | None  # auto_dispatch only
    proposed_resolution: str                            # the directive-applied answer
    analyst_owner: str | None                           # auto_dispatch only
    defined_fix_patch: dict | None                      # structured patch (typed IR)
    human_decision_options: list[str] | None            # human_in_loop only
    invalidity_reason: str | None                       # invalid_objection only
    dismissal_evidence: str | None                      # dismissed only
    directive_reasoning: str                            # how the prime directive guided this
    confidence: ConfidenceBand
    cited_sources: list[str]
```

Model: Opus by default (heavy reasoning, cheap class of problem). Reads the FM verdict + prior-draft stance map (carry-forward output from Piece B) + current draft plan + an FM-emitted `complexity_signal` (estimated turn-count to resolve, used by triage to default dispatch_strategy).

Plan ships when every objection has a triage classification AND `[t for t in triage_classifications if t.classification == "human_in_loop"] == []`.

### Piece B — Stance carry-forward across drafts

`fm_objection_user_state` already has `topic_hash` (SHA-256 of `topic\ndetail`, first 16 hex chars). Wave 7 uses it for cross-draft matching with a **deterministic matching stack**:

1. **Exact hash match** — current-draft objection's `topic_hash` equals a prior-draft objection's `topic_hash`. Highest confidence; no embedding needed.
2. **Embedding fallback** — when hashes differ but the objections may still semantically match. Use **local sentence-transformers** (`all-MiniLM-L6-v2`, ~80MB, CPU-fast for our scale of <100 objections/draft). Embed `topic + "\n" + detail`. Score = cosine similarity.
3. **Threshold** — match accepted only when `score >= 0.85`.
4. **Ambiguity guard** — when two prior objections both score above threshold against the same current-draft objection, require `top1_score - top2_score >= 0.05` margin. If the margin is too small, abstain (treat as no carry-forward; raise as fresh).
5. **DB-persisted audit fields**: every match writes `embedding_model` + `embedding_model_version` + `score` + `top2_score` on the new draft's `fm_objection_user_state` row.

When a match exists:
- AGREE → triage consumes "user previously resolved this as: <note>"; raising it again requires the FM to cite what *changed*
- DISAGREE + counter-position → counter-position threaded into next-round synthesizer prompt automatically
- DEFER → ignored

**Calibration:** log every match (and every above-threshold abstain) for the first 50 drafts before tuning the 0.85 / 0.05 thresholds from data.

### Piece C — Scoped re-synthesis on "Start new round with my decisions"

Today: every `start-new-round` invocation runs the full fleet, ~30-40 min wall-clock. Wave 7 ships scoped re-runs in **two waves**:

**v1 — Interim heuristic (~2 days, instrumented).** Horizon-touch + topic-keyword overlap heuristic. When user decisions touch only `short.actions`, re-run phase-1 analysts that contribute to short (concentration, fx, tax, plan_critique), skip the rest, re-run phase-3 synthesizer only for the short HorizonSection, skip phase-2 debate when no horizon-level change. **Explicitly framed as interim** in the doc + code + logs; instrumented so we can measure miss-rate vs the full fleet baseline.

**v2 — Full dependency-graph closure (~5-7 days).** Build a graph across plan nodes (`actions`, `rationales`, `targets`, `assumptions`, `risks`). Edges represent reference and dependency. User's stance touches certain nodes; the inspector takes the closure over inbound + outbound edges, returning the minimal re-run set. Catches the case where a short-horizon action's rationale is cited by a long-horizon target — string heuristics miss this; graph closure catches it.

Phase functions in `plan_synthesis/orchestrator.py` become scope-aware: each accepts a `ScopeSpec` arg and short-circuits when out-of-scope. Phase output preserves the carry-forward HorizonSections verbatim.

Target runtime by scope:

| Scope | Phases | Wall-clock target |
|---|---|---|
| Tax-substrate only | Phase 1 (subset: tax + plan_critique) → phase 5 FM | ~5 min |
| Single-horizon adjust | Phase 1 (subset) → phase 3 (single HorizonSection) → phase 4 → phase 5 | ~10 min |
| Multi-horizon refactor | Full fleet | ~30 min (unchanged) |

## Why these three together, not separate waves

Piece A without B → triage re-classifies the same concerns every round. Piece B without A → carried-forward stances reach the synthesizer but the FM still gates on binary verdict. Piece C without A+B → scoped re-runs still produce binary FM verdicts that gate on now-stale concerns.

Composed: **"I answered the questions only I could answer; the system applied directive-driven fixes to the rest; the plan is done."**

## Scope checklist (wave 7)

- [ ] **Migration 0061_objection_triage**: `objection_triage_classifications` table (objection_index, classification, dispatch_strategy, proposed_resolution, analyst_owner, defined_fix_patch JSON, human_decision_options JSON, invalidity_reason, dismissal_evidence, directive_reasoning, confidence, cited_sources JSON, decision_run_id FK, created_at)
- [ ] **Migration 0062_objection_carry_forward**: extend `fm_objection_user_state` with `embedding_model`, `embedding_model_version`, `match_score`, `match_top2_score`, `matched_from_plan_version_id` (nullable)
- [ ] **Migration 0063_decision_runs_outcome**: add nullable `decision_runs.outcome` enum (`approved`, `approved_with_notes`, `needs_user_input`, `rejected`). **Do NOT** add a new `status` value — `outcome` is the FM-verdict surface, `status` stays {running, completed, failed, blocked}
- [ ] **Pydantic types**: `argosy/agents/triage_types.py` with `TriageClassification` (5-class union) + `TriageOutput`
- [ ] **New agent**: `argosy/agents/objection_triage.py` — `ObjectionTriageAgent`. Opus by default. System prompt embeds the prime directive verbatim + the 5-class classification contract + how to consume the carry-forward map + the FM's `complexity_signal`
- [ ] **FM agent extension**: `FundManagerPlanRevisionDecision` gains `complexity_signal: list[int] | None` (one estimated turn-count per objection); FM prompt updated to emit it
- [ ] **Phase 5.5 wiring** in `plan_synthesis/orchestrator.py`: after FM verdict commits, dispatch the triage agent against the FM's objection list + carry-forward map. Phase output is the `TriageOutput`
- [ ] **Auto-dispatch widening**: extend `schedule_auto_dialogues_for_draft` to consume `triage.classification == "auto_dispatch"` entries with `analyst_owner` set, not only objections with literal `agent_report:X` citations. Honor `dispatch_strategy` (one_shot vs iterative + bounds)
- [ ] **Defined-fix safety pipeline**: new helper `argosy/services/triage/defined_fix.py` — runs the 4-stage gate (patch → schema validate → risk re-check → FM delta check → commit). Any failure → downgrade to `human_in_loop`. Soft-propose launch flag controls whether `commit` is automatic or pending user approval
- [ ] **Carry-forward matcher**: `argosy/services/objection_carry_forward.py` — exact-hash → embedding fallback → ambiguity guard → audit-field persistence. Uses local `sentence-transformers` (`all-MiniLM-L6-v2`). Lazy-load the model; cache in memory after first call
- [ ] **Pass carry-forward to FM + triage prompts**: the prior-draft AGREED/DISAGREED notes thread into the FM's plan_revision prompt AND the triage agent's prompt as "prior-resolved" context
- [ ] **Scope inspector v1** (instrumented interim): `argosy/services/plan_synthesis/scope.py` — horizon-touch + topic-keyword heuristic. Logs every scope decision + the would-be-touched nodes the heuristic skipped, so we can measure v1 miss-rate against full-fleet baselines
- [ ] **Scope inspector v2** (full graph closure): same module, but builds the dependency graph from `PlanSynthesisOutput` + `Phase1Inputs` references and runs closure. Replaces v1 once miss-rate data + tests support the swap
- [ ] **Phase functions accept ScopeSpec**: `_run_phase_1_analysts`, `_run_phase_3_synthesizer`, `_run_phase_4_risk`, `_run_phase_5_fm` consume a scope arg and short-circuit when out-of-scope; carry forward prior-draft HorizonSections verbatim for out-of-scope horizons
- [ ] **Verdict-surfacing rewrite**: `/api/plan/draft/objections` returns the triage-classified list; `human_in_loop` rows in main list, `auto_dispatch` rows in "resolved among the fleet" footer (existing wave-3 UI), `defined_fix` rows in a new "system-applied" footer, `invalid_objection` + `dismissed` rows in audit log only
- [ ] **`/plan` UI update**: forced-decision card layout for human_in_loop items; "system-applied" footer for defined_fix items showing the patch diff
- [ ] **APPROVED-WITH-NOTES verdict state** via `decision_runs.outcome = "approved_with_notes"`. UI shows "Plan ready" with a "View N system-handled concerns" expander
- [ ] **Tests**: 5-class triage classification roundtrip; dispatch_strategy bound exhaustion; defined-fix safety pipeline gate failures; carry-forward matcher (exact hash + similarity + ambiguity guard); scope inspector v1 miss-rate instrumentation; phase functions respect scope; UI render of mixed human_in_loop + system-applied; APPROVED-WITH-NOTES end-to-end

## Open questions (kept minimal per the directive)

1. **Max autonomy budget.** How deep can iterative auto-resolution chain before forced `human_in_loop` escalation? Two layers:
   - **Per-objection**: `dispatch_strategy.max_turns=3` (analyst back-and-forth ceiling). **Proposed default: 3.**
   - **Per-draft**: across objections, the triage agent can chain at most N `defined_fix` applications in a single draft cycle before pausing for user review. **Proposed default: N=5.**
2. **Auto-accept severity threshold.** APPROVED-WITH-NOTES ships the plan when `human_in_loop == []`. Should it also ship when `human_in_loop` contains only YELLOW-severity items (advisories), pausing only on AMBER+? **Proposed default: ship on `[YELLOW-only]`; pause on any `AMBER` or `RED`.** Configurable per-user later.

(Notably *not* an open question — codex reviewed and confirmed: triage classification contract / dispatch_strategy bounds / safety-gate stages / embedding provider / scope-inspector v1-v2 split / decision_runs outcome vs status / wave 6/7 sequencing. All locked in this rev.)

## What this wave does NOT do

- New Anthropic-API-side feature work (no caching tuning, no batch-API integration).
- UI redesign of /plan beyond the human_in_loop card + system-applied footer.
- Backfill triage classifications onto historical drafts — only new drafts get triaged.
- Replace the existing wave-3 auto-dispatch hook — extend it.
- **Typed-IR `defined_fix` patches**: rev-2 ships freeform-text patches with strict safety gating. Migration to typed IR (PlanPolicy from wave 6) is a **required follow-up** once wave 6 lands. Tracked as `# TODO: migrate to typed IR` markers in the defined_fix patcher.
- Scope inspector v2 graph closure: shipped after v1's instrumented heuristic has miss-rate data.

## Dependencies on other in-flight work

- **Wave 5 fixes must be stable in production** — substrate analysts producing real outputs (confirmed by run #61). If macro/news/tax/fundamentals continue to drop, triage classification quality degrades.
- **Wave 6 parallel-ship is approved** with strict guardrails per codex zigzag: `defined_fix` ships against freeform synthesizer text initially, with mandatory soft-propose flag + 4-stage validation gate + dated migration checkpoint to typed IR. When wave 6's `PlanPolicy` lands, defined_fix patcher gets a refactor to consume typed patches.

## Timeline estimate (revised post codex)

| Piece | Days |
|---|---|
| B — Carry-forward matcher + prompt threading + audit fields | 1-2 days |
| A — Triage agent + phase 5.5 wiring + safety gate + verdict-surfacing rewrite + UI card | 3-4 days |
| C v1 — Scope inspector heuristic + phase functions accept scope + instrumentation | 2 days |
| C v2 — Dependency-graph closure | 5-7 days (deferred until v1 has miss-rate data) |
| **Total wave 7 (excluding C v2)** | **~1.5 weeks of focused sessions** |
| **Including C v2 ship** | **~2 weeks** |

Ship order: **B → A → C v1 → (data) → C v2**. B first because it's the lowest-risk biggest-pain-reducer for "I already answered this." A next, the central piece. C v1 last for this push; C v2 a few weeks later once instrumented data justifies the design.
