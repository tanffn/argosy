# Coherence Deliberation — expert panel, arbitrator, and a durable decision ledger

## Problem

Argosy's whole-artifact reader (`whole_artifact_reader.py`) reads the full ~130 KB
rendered plan and flags cross-surface contradictions — the same fact stated
differently in the plan-body markdown, the structured action JSON
(`horizon_*_json`), and the typed Wealth Dashboard. The current fix loop
(reader BLOCK → `surgical_reconcile` closer edits the markdown bodies → reader
re-reads) does not converge, for three structural reasons:

1. **Unreachable surfaces.** The closer only edits the three markdown horizon
   bodies. Roughly half of every contradiction's *other* surface lives in
   structured fields it cannot touch (the action JSON, the dashboard dataclass).
   It can only ever fix one half, so the reader re-flags the other half forever.
2. **A memoryless adversary.** The reader is stochastic and re-reads the whole
   document fresh each round. It attacks a *new angle* on the *same* underlying
   dispute every time — including the qualifier text the previous fix just added.
   Nothing is ever recorded as *settled*, so the dispute set oscillates rather
   than shrinks. (Observed directly: a four-round automated loop went 2→3
   BLOCKERs; a later manual pass that *added* a clarifying qualifier caused the
   reader to attack that qualifier and findings grew 5→7.)
3. **Goal-level tensions, not prose bugs.** Some flagged "contradictions" are
   genuine tensions between user-stated goals (e.g. the stated *capital-preservation*
   drawdown style vs. the prime directive's *earliest-safe retirement*). No prose
   edit resolves these; they need a binding **ruling**, recorded once, not an edit.

## Goal

Replace the blind closer loop with a role-based deliberation that (a) reaches
**every** surface of a contradiction, (b) records a **durable, machine-checkable
ruling** so a settled question is never re-litigated, and (c) routes genuine
goal-level tensions to an arbitrator bound by the prime directive — while keeping
the adversarial reader honest so a *wrong* ruling cannot be laundered through.

Success: draft 45 (the allocation rebuild) reaches no-BLOCKER and is promoted via
this mechanism, and the mechanism is reusable for every future synthesis.

## Design decisions (settled with the user + adversarial codex review)

- **Resolver / panel split.** Pure canonical-fact mismatches (a surface states a
  number/label that disagrees with the canonical resolver) are conformed
  **deterministically** by a resolver — no LLM panel. The panel + arbitrator are
  reserved for genuine **goal/directive tensions**. Cheaper and more correct.
- **Distinct `coherence_arbitrator` role.** Not the raw `fund_manager`. It embodies
  the fund-manager's prime-directive authority but is its own prompt/schema/
  telemetry label, because its job is *"which claim is binding under the authority
  order, and what invariant must every surface satisfy?"* — not *"the best plan."*
- **Reader keeps an appeal path (no silent laundering).** Settled rulings are fed
  back to the reader, but it is NOT simply told "don't re-flag." It may emit
  `ruling_divergence` (a surface still disagrees with the ruling) and `ruling_defect`
  (the ruling itself is stale / overbroad / unsupported / violates the authority
  order), plus `new_dispute`. It may not re-litigate the *preferred answer* of a
  settled ruling. This was codex's #1 ranked risk.
- **Machine-checkable invariants + a deterministic verifier.** Every ruling carries
  a `coherence_invariant` (a typed, code-evaluable predicate over named surfaces).
  A deterministic verifier checks all invariants **before** the stochastic re-read.
  Telemetry is *visibility*, not proof of correctness — the verifier is the proof.
- **Fail-closed = BLOCK.** Any panel / arbitrator / conformer / verifier failure
  blocks promotion. Never fall back to the old markdown closer (the known-unsound
  path).
- **First-class telemetry.** Every panelist / facilitator / arbitrator call flows
  through `BaseAgent`, so it already lands in the `agent.run.finished` event stream
  and the "Analysis team receipts" appendix. A dedicated, persisted deliberation
  record additionally renders one row per dispute (question → positions →
  consensus/arbitration → ruling → invariant → surfaces conformed → verifier
  result). Shipped in the user export; stripped from the reader artifact.

## Architecture & flow

When the reader returns BLOCK with contradiction-class findings:

```
reader findings (typed)
   │
   ▼
[1] Clusterer  ── splits multi-issue findings; assigns each a STRUCTURED dispute identity
   │                 → {disputes}, each with a stable dispute_key
   ▼
[2] Router  ── per dispute, classify:
   │     • canonical-fact mismatch  ──▶ [3a] Resolver (deterministic conform)
   │     • goal/directive tension   ──▶ [3b] Panel → Facilitator → Arbitrator
   │     • un-typeable / ambiguous  ──▶ BLOCK (cannot represent as an invariant)
   ▼
[3b] Panel (owning agents state position+basis, see each other) → Facilitator
        (consensus? ) → Arbitrator (coherence_arbitrator) issues ruling+invariant
   │
   ▼
[4] Conformer  ── typed all-surface patch plan (field paths + postconditions),
   │                applied atomically to markdown bodies AND structured JSON;
   │                number-boundary guard; idempotent; updates derived surfaces
   ▼
[5] Ledger  ── persist ruling, invariant, basis, conformed_surfaces (coherence_decisions)
   │
   ▼
[6] Verifier  ── evaluate every ledger invariant deterministically over the artifact
   │                fail ⇒ BLOCK (do not re-read, do not promote)
   ▼
[7] Reader re-read  ── ledger injected; reader may emit new_dispute /
                        ruling_divergence / ruling_defect only
   │
   ▼ (no new disputes, all invariants hold) ⇒ promote per decision-grade rule
   (bounded round cap; ledger is the audit trail)
```

**Convergence (honest claim, per codex).** This is *not* globally monotonic. It is
bounded-convergent under: stable structured dispute identity, atomic all-surface
conformance, machine-verified invariants, and a reader that treats settled rulings
as binding-but-challengeable. The set can still grow via a conform edit touching a
derived surface or a genuinely new dispute — which is why the verifier (deterministic)
gates promotion, the conformer tracks derived-surface dependencies, and the round
cap fails closed.

## Components & data flow

### Reader findings → typed (`whole_artifact_reader.py`)
Findings already carry `severity`, `kind`, `detail`, `surfaces_cited`. Extend the
finding schema with: `subject_type`, `field_path` (canonical surface path where
known), `normalized_claim` (the value/label/option each cited surface asserts), and
the new finding kinds `new_dispute | ruling_divergence | ruling_defect` for re-reads.

### Structured dispute identity (the Clusterer)
`dispute_key` is a hash over a **structured** record, never the natural-language
question:
- `subject_type` — e.g. `retirement_age_headline`, `rsu_vest_policy`,
  `sgln_ucits_membership`, `fi_crossing_basis`, `tranche_execution_gate`.
- `subject_field_path` / `subject_id` — canonical field path when one exists.
- `scope` — person / household / account / tax-year / horizon / scenario.
- `conflict_type` — `value_mismatch | policy_tension | calc_inconsistency |
  representation_mismatch`.
- `normalized_options` — the distinct positions in play.
- `implicated_canonical_fact_ids`, `implicated_user_directive_ids`.
Surface IDs are **evidence**, not identity (except for pure
`representation_mismatch`). An alias/equivalence table maps re-phrasings to the same
key across stochastic re-reads. The clusterer must **split** a finding that bundles
several issues, and must **reject** (→ BLOCK) any dispute it cannot express as a
typed invariant.

### Router
Deterministic classification of each dispute by `conflict_type`:
`value_mismatch | calc_inconsistency` with a canonical source → **Resolver**;
`policy_tension` (goal/directive) → **Panel/Arbitrator**;
`representation_mismatch` with a canonical render → **Resolver** (or Conformer
directly); anything un-typeable → BLOCK.

### Resolver (deterministic, no LLM)
For canonical mismatches: read the authoritative value from
`plan_numeric_resolver` / the canonical fact, build the typed conform patch
(see Conformer), and a `coherence_invariant` of the form *"every surface stating
subject X equals the canonical value/label."* Reuses the Slice-2 fact substrate
(`fact_ledger`, `fact_inventory`, `allocation_fact_sites`) for surface→field-path
mapping.

### Panel — `coherence_panelist` (new agent, thin wrapper over an owning role)
For goal/directive tensions only. For each dispute, the agents that **own** the
conflicting surfaces (resolved via the surface registry below) each return
`{position, basis ∈ (prime_directive|user_directive|canonical_fact), cites}`,
shown each other's positions so they can concede or hold. Reuses existing role
context; one panelist invocation per implicated role.

### Facilitator — `coherence_facilitator` (new, mirrors `risk_facilitator`)
Reads panel positions → `{consensus: bool, ruling?, crux_of_disagreement?}`.
Consensus → ruling recorded as `resolved_by=consensus`. No consensus → escalate.

### Arbitrator — `coherence_arbitrator` (new; embodies fund-manager authority)
On no-consensus, issues a **binding ruling** under the authority order
**prime directive > user directives > canonical facts > panelist preference**.
Output schema: `{ruling_statement, rationale, basis, per_surface_instructions[],
coherence_invariant}`. It decides *which claim binds and what invariant every
surface must satisfy* — not the best plan.

### Surface registry
A declarative map: `subject_type → [ {surface_name, field_path, conformance_method,
derived_from?} ]`. Names every surface a subject appears on (long/medium/short
markdown sections, action JSON paths, dashboard fields, export appendices),
**who owns it**, **how to conform it** (markdown find/replace vs. JSON field set vs.
derived-recompute), and **derived-field dependencies** so a conform that changes an
input also refreshes dependents. Seeds from the existing fact-site mapping; extended
to cover the non-allocation subjects (vest policy, retirement age, dates).

### Conformer (the critical correctness component)
Consumes a typed **patch plan**: per surface, `{field_path, method, new_value/text,
expected_postcondition}`. Applies atomically across markdown bodies AND structured
JSON; **idempotent**; number-boundary safety guard (no figure silently changed);
refreshes derived surfaces named by the registry. Partial/failed application ⇒ no
commit ⇒ BLOCK.

### Ledger — `coherence_decisions` (Alembic migration 0026)
`id, user_id, decision_run_id, dispute_key, subject_type, question, ruling,
coherence_invariant (json), rationale, basis, resolved_by (resolver|consensus|
arbitrator), conformed_surfaces (json), superseded_by (nullable FK), created_at`.
Rulings are **versioned and supersedable** (a later, higher-authority ruling
supersedes an earlier one rather than silently overwriting).

### Verifier (deterministic, pre-re-read gate)
Evaluates every non-superseded ledger invariant against the current artifact +
canonical facts. Any failure ⇒ BLOCK (no re-read, no promote). This is the
correctness proof; the LLM re-read is an additional adversarial layer on top.

### Re-read with appeal path
Reader prompt gets the settled rulings. Allowed emissions: `new_dispute`,
`ruling_divergence` (with the offending surface), `ruling_defect` (with which
authority test the ruling fails). It may NOT re-open the preferred answer of a
settled ruling on preference grounds. A `ruling_defect` re-opens that dispute
(supersession path); a `ruling_divergence` re-runs the conformer for that ruling.

## Error handling / fail-closed
- Clusterer can't type a dispute → BLOCK.
- Panel/facilitator/arbitrator error or schema-invalid → BLOCK.
- Conformer partial/failed/guard-violation → no commit → BLOCK.
- Verifier invariant fails → BLOCK.
- Round cap reached with open disputes → BLOCK; ledger persisted as audit trail.
- Never fall back to the markdown-only closer for anything involving numbers,
  structured fields, canonical facts, user directives, or policy trade-offs.

## Telemetry / observability (explicit requirement)
- All LLM-role calls land in `agent.run.finished` + the "Analysis team receipts"
  appendix (role, model, tokens, cost, key finding) — same surface as today's fleet
  receipts, now including `coherence_panelist`, `coherence_facilitator`,
  `coherence_arbitrator`.
- A persisted, rendered **"## Appendix — Coherence deliberations"** block: one row
  per dispute — `subject_type | question | positions | resolved_by | ruling |
  invariant | surfaces conformed | verifier result`. Ships in the user export;
  stripped from the reader artifact (it describes *how* the plan was reconciled, not
  the plan), via the existing `_strip_internal_metadata_sections` path.
- Telemetry is for human visibility/audit; the **verifier** — not the telemetry — is
  the correctness gate.

## Testing
- **Resolver**: a canonical mismatch is auto-conformed across markdown + JSON; the
  invariant holds; idempotent on re-run.
- **Panel/arbitrator (mocked LLM)**: a goal tension yields a ruling + invariant; the
  conformer rewrites all registered surfaces; the verifier passes.
- **Ledger/verifier**: a seeded invariant fails when a surface is perturbed → BLOCK;
  supersession replaces a ruling without losing the audit row.
- **Reader appeal**: a deliberately wrong ruling produces a `ruling_defect` on
  re-read (laundering guard); a still-divergent surface produces `ruling_divergence`.
- **Fail-closed**: malformed arbitration / partial conform / round-cap each BLOCK and
  never promote.
- **Draft-45 e2e** (the acceptance case): the retirement-age dispute routes to
  arbitration → 46 leads (prime directive), capital-preservation honored as
  target-sizing basis, 54 as the strict track; the vest / SGLN / date disputes route
  to the resolver/conformer; verifier passes; reader returns no BLOCKER; draft 45
  promoted. Focused regression (gate/surgical/run106 suites) green.

## What this is NOT (YAGNI)
- No full panel for every contradiction — only goal/directive tensions.
- No verbose deliberation in the user export by default beyond the one-row-per-dispute
  appendix.
- No reliance on telemetry as proof of correctness.
- The "bake ruling into canonical render" approach (regenerate every surface from one
  source) is a **future** hardening, not this build; the resolver/conformer + verifier
  achieve the same guarantee for the contested subjects without a full render rewrite.

## Integration points (grounded in current code)
- Reader + finding schema: `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py`
- Loop entry (replaces blind closer): `argosy/orchestrator/flows/plan_synthesis/surgical_reconcile.py`,
  wired from `orchestrator.py`.
- Facilitator pattern to mirror: `argosy/agents/risk_facilitator.py`,
  `argosy/agents/researcher_facilitator.py`.
- Arbitrator authority source: `argosy/agents/fund_manager.py` (capability reused,
  role distinct).
- Canonical facts / resolver: `argosy/services/plan_numeric_resolver.py`.
- Surface registry seed: `argosy/quality/fact_{ledger,inventory,attribution}.py`,
  `argosy/services/allocation_fact_sites.py`.
- Artifact assembly + reader-strip: `argosy/services/assembled_artifact.py`,
  `argosy/services/plan_export.py`.
- Ledger model + migration: `argosy/state/models.py` + `alembic/versions/0026_*.py`.
