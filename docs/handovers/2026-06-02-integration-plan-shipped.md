# Argosy comprehensive-plan integration — session handover

**Date:** 2026-06-02
**Outcome:** Integration plan delivered + one clean end-to-end supervised synth (plan_version 21, role=current).

---

## TL;DR for next session

The 7-phase integration plan in `docs/plans/argosy-comprehensive-plan-integration.md` is **architecturally complete**. Code is shipped (15 commits today). One supervised live-LLM synth completed end-to-end and produced a readable v21 retirement plan that the user accepted as `role='current'`. Two known residuals + one architectural bug remain — see "Known residuals" below.

## What v21 looks like

- `plan_version.id = 21`, `version_label = synth-2026-06-02-1358`
- `role = current` (accepted 14:17:15 UTC)
- Replaces v20 (now superseded)
- Long / medium / short horizon markdown all clean and household-readable; medium horizon dumped to `tmp_review/v22_supervised/medium.md` for review reference
- `decision_run_id = 71` — only successful run in today's chain (66, 67, 68, 69, 70 all failed and are now stamped `status='failed'`)

## Commits delivered today (15)

```
1af994d fix(phase5): use_structured_output=False — SDK fails on complex schemas
ae102f0 fix(synth): tighten EVIDENCE DISCIPLINE rule on numeric formatting
3a861c5 fix(rewriter): force-preserve structured subtrees + evidence-preserve guidance
471dbe2 docs(sdd): describe the integration-plan deliverables as current state
a256f74 fix(rewriter): label-translation guidance + iteration harness
0cb5b58 fix(plan): supervised-run hardening — Phase 5 kwarg contract + rewriter soft-fail
43ebbef feat(plan): Phase 5 — PlanCoverageAnalyst + WithdrawalSequencerAgent + smoke test (FULL SHIP)
709ba86 feat(plan): Phase 4 + Phase 6 — distillate schema + feature flag
c61cb9c feat(plan): Phase 3 — SectionEvidence contract + canonical sections (MVP ship)
dde95c6 feat(plan): Phase 2 — PlanLanguageRewriter + invariant validator
ed28a40 feat(plan): Phase 1 — clean synth context + renderer split + audit migration
75be640 feat(quality): plan output gate (Phase 0) — failing CI fixture for v20
```

Plus an SDD update with the force-preserve + use_structured_output flag notes (today, after the above).

## Architecture in one paragraph

`argosy/quality/` holds the 5-check plan-output gate (`history_leak`, `jargon_leak`, `section_coverage`, `evidence_per_section`, `distillate_section_binding`) + a `validate_rewriter_invariants` validator. The synthesis pipeline now runs `PlanLanguageRewriter` between Phase 3 and the speculation-cap enforcer; the orchestrator's `_force_preserve_structured_fields` restores SectionEvidence + deltas + speculative_candidates + inputs + Target.source_section from the synth's pre-rewrite output before validation runs. `PlanDistillate` gained 12 typed fields for P0/P1 buckets (`plan_assumptions`, `cashflow_phases`, `equity_comp_grants`, `unmapped_sections`, `fi_bridge`, `withdrawal_schedule`, `monte_carlo_grid`, `tax_schedule`, `cross_border`, `real_estate_plan`, `fx_strategy`, `etf_reference`). `PlanSynthesisOutput` gained `sections: list[Section]` (Phase 3 canonical shape); `Section` carries `SectionEvidence` with `FactClaim` + `Citation` + `Assumption` and 5 Pydantic validators. `POST /api/plan/draft/{id}/accept` runs the gate per `ARGOSY_PLAN_GATE_ENFORCE` (false default → warning mode; true → 422 on failure; `?override_gate=true` bypasses with audit log). Two new Phase 1 analysts (`PlanCoverageAnalyst`, `WithdrawalSequencerAgent`) gated behind `ARGOSY_PHASE5_AGENTS` (false default).

## Feature flags

| Env var | Default | Effect |
|---|---|---|
| `ARGOSY_PLAN_GATE_ENFORCE` | `false` | true → `/accept` returns 422 on gate failure; false → surfaces `gate_warning` in response |
| `ARGOSY_PHASE5_AGENTS` | `false` | true → 12-agent Phase 1 fleet (10 core + Phase 5); false → 10 core only |

Recommended rollout: keep both at default for now; observe v21's gate output, then promote `ARGOSY_PLAN_GATE_ENFORCE=true` after a few cycles of observation.

## Replay-harness tooling

Two harnesses make iteration ~30× faster than full supervised synth cycles:

- `tools/iterate_rewriter.py [--fixture <path>]` — load a `PlanSynthesisOutput` JSON, run rewriter+force-preserve+validator+gate against it. One Opus call (~6 min, ~$0.50). Default fixture is plan_version=20 from dev DB.
- `tools/iterate_synth_evidence.py [--decision-run-id N]` — replay just the synthesizer against captured Phase 1 analyst reports from a real decision_run. ~6 min, ~$0.75.
- `tools/smoke_test_gate_against_v20.py` — runs the gate against the persisted v20 fixture, prints structured verdict. No LLM cost.
- `tools/trigger_supervised_synth.py` — full Phase 0-5 pipeline (~25-30 min, ~$15). Use sparingly.

## Known residuals (acceptable, document-not-fix for next iteration)

1. **2 jargon residuals in v21 short horizon** (`TechnicalAnalyst`, `substrate` inside `SpeculativeCandidate.thesis_summary` / `sourced_from` fields). The rewriter's `validate_rewriter_invariants` iterates themes/actions/targets prose but NOT speculative_candidate sub-fields. Fix: extend `_check_rewritten_prose` in `argosy/quality/rewriter_invariants.py` to scan SpeculativeCandidate.thesis_summary + sourced_from.
2. **Phase 3 `sections[]` empty in v21**. Earlier captured Phase 3 fixture from #69 had 18 sections; v21 has 0. Likely model variance, possibly synth-prompt drift after the ae102f0 monetary-format expansion. Investigation: diff the synth prompt that produced #69's 18 sections vs current. Could be (a) random variance, (b) prompt-size threshold, (c) `use_structured_output=False` interaction.
3. **1 residual evidence violation (semantic)** observed in iter #1 against synth #69 fixture: `value=15000` paired with a technical-signal extract (`RSI 17.78`). Down from 14→1 violations via the monetary-format rubric. Iter #2 broader citation-pairing fix regressed badly (1→16) and was reverted. Next iteration: narrower position-sizing example only.

## Known bugs

1. **`run_synthesis` doesn't stamp `DecisionRun` on exception.** Today's session left 5 ghost rows (id 66-70) at `status='running'` with `finished_at=NULL`. They were manually stamped `failed` at session end. Fix: wrap `run_synthesis` body in `try/except` that stamps `status='failed'` + `finished_at` before re-raising. Otherwise the UI's "in-flight" indicator surfaces ghosts.
2. **Phase 5 SDK schema bug.** `claude.exe` fails with `exit code 1 [empty stderr]` when given the Phase 5 agents' Pydantic JSON schemas. Worked around with `use_structured_output=False`. Should be reported upstream or investigated for which specific schema pattern triggers it.

## Followups for next session

Priority order:

1. **Investigate v21 `sections[]=0`.** Compare the synth prompt now vs at the time of #69's 18-section output. Worth iter #2 of synth-evidence-prompt against captured Phase 1 inputs to see if the model emits sections under the current rubric.
2. **Fix the rewriter SpeculativeCandidate gap.** ~20-line patch to `argosy/quality/rewriter_invariants.py::_check_rewritten_prose` + extend the rewriter prompt to translate `SpeculativeCandidate.thesis_summary` + `sourced_from` jargon.
3. **Fix `run_synthesis` exception-stamping bug.** ~5-line patch to `argosy/orchestrator/flows/plan_synthesis/orchestrator.py::run_synthesis`. Prevents future ghost-DecisionRun rows blocking the UI.
4. **Hand-review v21's medium horizon** against the canonical 18-section spec. Is the prose decision-grade or just verbose? Are the targets / themes / actions actually actionable, or do they need user-judgment calls?
5. **Optional iter #3 on synth evidence rubric.** Narrower citation-pairing fix targeting just the position-size case ($15K cited against RSI signal). Iter #2 attempt regressed; iter #3 must be surgical.

## Verification commands

```bash
# Run the dedicated test suite for everything shipped today
.venv/Scripts/python.exe -m pytest \
  tests/test_plan_output_gate.py \
  tests/test_plan_synthesizer_history_leak.py \
  tests/test_plan_language_rewriter.py \
  tests/test_plan_synthesizer_evidence.py \
  tests/test_plan_distillate_phase4.py \
  tests/test_plan_gate_feature_flag.py \
  tests/test_plan_coverage_analyst.py \
  tests/test_withdrawal_sequencer_agent.py

# Smoke-test the gate against v20 fixture (no LLM)
.venv/Scripts/python.exe tools/smoke_test_gate_against_v20.py

# Re-run the rewriter against the persisted #69 phase-3 fixture
.venv/Scripts/python.exe tools/iterate_rewriter.py \
  --fixture tmp_review/synth_69_phase3_output.json
```

## Read these to onboard

In order:

1. `docs/plans/argosy-comprehensive-plan-integration.md` §0 progression checklist (all 7 phases done)
2. `docs/design/SDD.md` §3.1 (analyst fleet) + §3.6 (cross-cutting agents) + §6.11 (plan synthesis flow + gate)
3. This file
4. `tmp_review/v22_supervised/medium.md` — example of the v21 output
5. The 15 commits above via `git log --oneline -15`

## Stop-conditions for next session

Mark "validation complete" only when:
- ✓ The Phase 5 agent CONTENT has been human-reviewed (PlanCoverageAnalyst's baseline sections + WithdrawalSequencerAgent's FI bridge for sensibility)
- ✓ v22 (or later) is produced with `sections[]` populated and all 5 gate checks passing
- ✓ The exception-stamping bug is fixed so DecisionRun rows don't ghost
- ✓ The rewriter's SpeculativeCandidate coverage gap is closed
