# P3 — Unblock the prime-directive experts (per-task plan)

> Authored at phase entry per the realignment roadmap (`2026-06-09-argosy-realignment-roadmap.md`).
> Gate-lift authority: Ariel authorized a **supervised live-LLM run** for this overnight session
> (the gate guardrail on `phase5_agents` required exactly this — a supervised run with hand-checked
> outputs). Outputs reviewed by Ariel in the morning.

## What the exploration found (ground truth, supersedes stale roadmap file:line)

The three "phase 5" experts (`EquityCompAnalystAgent`, `WithdrawalSequencerAgent`, `PlanCoverageAnalyst`)
are **already built, registered, and their numeric-resolver paths already exist**:
- `phase5_agents` is read once at module load in
  `argosy/orchestrator/flows/plan_synthesis/orchestrator.py::_resolve_phase_1_agent_names` →
  extends the 10-member core fleet to 13 when True.
- `argosy/services/plan_numeric_resolver.py` already registers `_resolve_equity_comp_analyst`
  (`savings.annual_net_nis`) and `_resolve_withdrawal_sequencer` (`retirement.fi_*`, `spend.fi_basis_nis`).
- `render.py` already renders the FV trajectory resolved-vs-pending and binds `fi_bridge` /
  `withdrawal_schedule` via `canonical_sections`.

So the "[derivation pending]" on those surfaces was caused by **the agents being gated OFF**, not by a
missing resolver. The remaining work is: flip the gate, **verify end-to-end with tests** that the
resolvers resolve when the agents run, surface `PlanCoverageAnalyst` (currently dropped on the floor),
and **wire the real tax into the MC** (the one genuine build).

## Tasks

### T3.1 — flip `phase5_agents` default True
- `argosy/config.py:125` `Field(default=False)` → `True`. Update the comment (the supervised run happened).
- Red: a test asserting the default is True. Green: existing flag tests still pass (they patch explicitly).

### T3.2 `[money-math]` — EquityComp resolver path verified
- Resolver exists. Add an integration test: given a persisted `equity_comp_analyst` AgentReport with
  3 scenarios, `resolve_plan_numbers` resolves `savings.annual_net_nis` (known_grants_only floor) and
  the FV trajectory renders non-pending. codex-verify the floor/spread money-math.

### T3.3 `[money-math]` — Withdrawal Sequencer resolver path verified
- Resolver exists + `_apply_fi_methodology` override. Add an integration test: given a persisted
  `withdrawal_sequencer` AgentReport, the FI-bridge waterfall + withdrawal schedule sections render and
  `retirement.fi_*` resolve. codex-verify the FI consistency.

### T3.4 `[money-math]` — wire real tax into the MC, retire flat-10%
- `RetirementAssumptions.withdrawal_tax = 0.10` is an explicit "interim shortcut" magic number; the MC
  runs `apply_age_aware_tax=False`. The inline age-bands (25/15/12) apply **statutory CGT to the full
  withdrawal**, which a prior codex review flagged as too harsh (ignores cost basis → `DEFAULT_TAXABLE_
  GAIN_FRACTION = 0.6`; `scenario_mc` already uses `0.25 × 0.6 = 15%`).
- **Decision (codex-verify):** make `tax_curve` the single source of an **effective** withdrawal-tax
  curve — `CGT(0.25) × gain_fraction(0.6) = 15%` pre-67, `12%` post-67 (pension rights-fixation). Set
  `apply_age_aware_tax=True`; replace both inline duplications with the single source. Kills the magic
  10% with a derived, auditable, internally-consistent rate matching `scenario_mc` + the calculator.
- **Headline impact:** 10% → ~15% pre-67 raises the after-tax drawdown → pushes FI/retirement age out
  modestly (far less than basis-blind 25% would). Surface this in the morning report.

### T3.5 — PlanCoverageAnalyst to a surface
- Output is persisted (AgentReport `plan_coverage`) but never read/rendered. Render a coverage/gaps
  appendix (from `unfilled_section_ids` + per-section `missing_data`) into the plan markdown and/or a
  DTO field. Not money-math.

## Acceptance
No "[derivation pending]" left on any surface after a live-LLM synthesis run with phase5 on; every P3
number Argosy-derived + auditable; money-math pieces codex-verified; touched tests + smoke green.
