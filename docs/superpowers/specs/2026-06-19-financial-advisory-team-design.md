# Design — Argosy as a real financial-advisory firm

## Status
Design (approved in brainstorming 2026-06-19). Supersedes the from-scratch
whole-plan synthesizer as the *generation model*. Decomposed into four phased
sub-projects; Phase 1 is detailed here and specced first.

## Context and problem

Argosy produces a comprehensive financial plan via a fleet of LLM agents. Today
the fleet runs as a **waterfall with a critic**: analysts → debate → a
synthesizer that regenerates the entire ~100K-char plan from scratch → a reviewer
that can only veto the whole monolith. When the reviewer finds a contradiction,
the only recovery is to regenerate everything, which fixes that contradiction and
introduces different ones. The plan therefore cannot converge: deterministic-gate
false positives and reviewer blockers both shift every run, and contradictions
live in surfaces the regenerator does not control (a structured FI-crossing table,
a wealth-dashboard net-worth figure with its own data source).

A real wealth-management firm does not work this way, and neither should Argosy.
This design models the system on how an actual financial-planning firm operates —
the same responsibilities, ownership, collaboration, and escalation — so the plan
is coherent **by construction** and a review finding routes to the responsible
owner for a targeted fix instead of a full regeneration.

The design is corroborated by three independent sources that strongly agree:
real-firm practice (CFP 7-step process, SEC/FINRA substantiation + single-source-of-truth),
an independent codex design, and the existing SDD — which already specifies this
vision (§6.11 "the synthesizer is FORBIDDEN from inventing numbers"; `plan_numeric_resolver`
as the single source; §2 "the UI is a pure projection of the current plan"). The
implementation drifted from the SDD; this design realigns to it and generalizes it.

## Principles (the doctrine the three sources converge on)

1. **One owner per figure.** Every quantitative output has exactly one owning
   specialist who publishes it as authoritative. Every surface (plan body,
   dashboard, retirement, portfolio, proposals, appendices) renders that figure
   **by ID**. There is no second copy of any number, so two surfaces cannot
   disagree by construction.
2. **The Lead integrates, invents no numbers.** A Lead Planner / CIO owns the
   integrated plan — narrative, sequencing, trade-offs, sign-off — but references
   owned figures by ID and never re-types or re-derives a specialist's number.
3. **Specialists consume by reference.** A specialist may read another owner's
   figure but may not re-type, override, or locally recompute it; if it needs to
   change, that goes to the owner.
4. **Compliance is an orthogonal gate that routes, never rewrites.** The reviewer
   reviews the integrated plan and opens a targeted finding against the owner of
   the defective figure/artifact. It cannot edit the plan and is not a higher rung
   on the ladder — it is a parallel veto-gate that must clear regardless of
   consensus.
5. **Collaboration is bidirectional (ZigZag), not a waterfall — but bounded by a
   dependency contract.** Every producer→reviewer/consumer edge allows the
   reviewer to push back AND the producer to tweak (targeted edit, never
   regenerate from zero) or counter (defend with evidence); unresolved → escalate
   to an arbiter. To actually converge (codex review), edges form a **directed
   acyclic dependency graph with deterministic invalidation** (a changed figure
   invalidates only its downstream blast radius, by version), and disputes are
   **materiality-gated** — only a material disagreement opens a ZigZag; a
   deterministic recompute mismatch is corrected, not negotiated. Cycles are a
   build error, not a runtime negotiation.
6. **Everything is backed and checked; rigor scales with the figure's tier AND
   materiality.** Figures are classified by *kind* — `source_fact`, `assumption`,
   `formula_result`, `model_projection`, `interpretation` (legal/tax), or
   `recommendation` — not a crude deterministic/judgment binary. `formula_result`
   over source data → validated by recomputation against the raw source.
   `assumption` / `model_projection` / `interpretation` / `recommendation` →
   require evidence (citations to raw inputs) AND, **when material/high-risk**, an
   independent cross-model blind re-derivation. Low-impact display figures do not
   carry the heavy machinery. A material figure with no traceable basis cannot be
   published.
7. **Ranges, not constants.** Policy parameters (safe-withdrawal rate,
   concentration cap, return/inflation assumptions) are owned parameters with
   ranges, never hardcoded constants.
8. **Ownership beyond numbers.** The registry also owns binding NON-numeric
   claims — "safe", "suitable", "high priority", "client prefers liquidity",
   "tax-sensitive", "defer exercise" — and assumptions, disclosures, and client
   constraints. These can contradict figures and each other, so each has an owner
   and is checked like any figure.
9. **Generalized domain experts.** Each role is an expert in its own domain and
   serves any client (employee, business owner, retiree, multi-property,
   equity-comp recipient). No role is overfit to one client's situation; a
   specific instrument (e.g. employer RSUs in one stock) is one *instance* a
   general specialist handles, not the model.

## The team (generalized roster and owned figures)

Each role is the single accountable owner of the figures/artifacts listed. Roles
engage only when the client has the relevant situation (a client with no business
interest does not engage the Business-Interest specialist).

| Role | Owns (authoritative) | Existing agent(s) |
|---|---|---|
| **Lead Planner / CIO** | Integrated plan, recommendation hierarchy, sequencing, decision log, sign-off. **No raw figures.** | `fund_manager` (decision) + `plan_synthesizer` (integration/narrative) |
| **Client Discovery** | Goals, constraints, risk/liquidity preferences, time horizons, intended retirement age, household/member map | `intake` / `advisor` |
| **Data Steward** | Source-document library, fact provenance, freshness, missing-data list, account/property/policy inventory | *(new — partially in ingest services)* |
| **Balance-Sheet Specialist** | Net worth (all bases — total/investable/liquid), assets, liabilities, liquidity, concentrated positions | resolver + `wealth_dashboard` (to be bound) |
| **Cash-Flow Planner** | Income, spending, savings rate, surplus/deficit, emergency-reserve target | `household_budget_analyst` |
| **Tax Strategist** | Marginal/effective rates, tax cost of gains, withholding, sequencing rules, eligibility windows | `tax_analyst` |
| **Investment Strategist / PM** | IPS, target allocation + ranges, return/volatility assumptions, rebalancing bands, asset location, sell quantities, concentration cap & target | `concentration_analyst` + `allocation_agent` |
| **Retirement / FI Planner** | FI capital target, FI margin, feasibility, withdrawal rate, recommended/earliest-safe age, preservation age, MC solvency, **FI-crossing year** | `withdrawal_sequencer` |
| **Insurance / Risk Specialist** | Coverage gaps, needs analysis, sufficiency-under-shock | `risk_officer` / `risk_facilitator` |
| **Estate Planner** | Estate exposure, domicile/situs constraints, titling, beneficiary map | *(split out of `concentration_analyst`)* |
| **Equity-Comp / Business-Interest Specialist** | Vesting schedules, concentrated employer/business equity, liquidity-event scenarios (RSUs are one instance) | `equity_comp_analyst` |
| **Debt / Real-Estate / Benefits / Charitable Specialists** | Domain figures when the client has them | *(future)* |
| **Compliance / Reviewer** | Review findings, approval status, disclosures. **Routes to owners; never rewrites.** | `whole_artifact_reader` + `audit_agent` + codex reviewer |
| **Senior Partner / Committee** | Methodology, tie-breaks, exceptions, firm assumptions library | `coherence_arbitrator` / FM-as-arbiter |
| **Operations / Implementation** | Implementation checklist, task owners, deadlines, action tracker | `action_proposer` |

## The Canonical Figure Registry (the spine)

Every quantitative output is one registry record:

```
FigureRecord {
  id            # stable key, e.g. "retirement.fi_target_nis"
  value         # the authoritative value (or pending)
  unit          # nis | pct | age | shares | nis_per_usd | claim | ...
  kind          # source_fact | assumption | formula_result |
                #   model_projection | interpretation | recommendation
  owner         # the role accountable for this figure/claim
  # --- semantic identity (codex review: single-ID is not enough; two surfaces
  #     can "agree" on a number under different assumptions) ---
  basis         # which basis this is (e.g. liquid | investable | total)
  scenario      # baseline | stress | which MC regime, when applicable
  as_of         # the date/state the value is true for
  jurisdiction  # tax/legal jurisdiction when relevant
  policy_version# version of the owning policy/assumption set
  precision     # display precision + match tolerance
  # --- provenance / evidence / lifecycle ---
  inputs        # the figure-IDs / raw sources this derives from
  method        # formula or judgment rationale
  evidence      # citations to raw inputs (required for non-source-fact kinds)
  source_freshness # freshness of the underlying source (from Data Steward)
  confidence    # HIGH | MEDIUM | LOW
  materiality   # high | medium | low — gates how much validation it must clear
  validated_by  # recompute | cross_model_rederivation | none
  status        # resolved | pending | blocked
  version       # monotonic; bumps invalidate the downstream blast radius
  timestamp     # when published
}
```

- The registry extends the existing `plan_numeric_resolver` (which already owns
  the deterministic scalars) and the derivation graph (canonical nodes), adding
  `owner`, `determinism`, `evidence`, and `validated_by`.
- **Every client-facing surface renders by figure-ID** via the `graph→plan`
  render bridge and a registry-bound dashboard. No surface holds a private copy
  of a number. Cross-surface drift becomes structurally impossible — the failure
  class behind every chart-consistency session.
- Net worth is registered as **three labeled bases** — total (incl. residence),
  investable, liquid — each its own figure-ID; surfaces render the labeled basis
  they mean (resolves the ₪14.05M-vs-₪11.87M dashboard contradiction).

## Collaboration, validation, and escalation (baked into the flow)

### ZigZag on every edge
Every producer→reviewer/consumer hand-off is bidirectional:
- Reviewer ACCEPTS or PUSHES BACK with a specific, evidenced objection.
- Producer responds: **TWEAK** (targeted edit to the disputed node only — never
  regenerate from zero) or **COUNTER** (defend with evidence; the reviewer may be
  wrong).
- Bounded rounds (default 2–3). Unresolved → escalate to arbiter. The ruling is
  recorded and binds.
- Realized by the existing `negotiation_ladder` + `RealLadderParticipants` +
  `plan_node_owner`, generalized so it runs on every dependency edge, not only at
  the FM.

### Cross-model adversarial validation (separate, fail-closed, materiality-gated)
- A **material** non-`source_fact` figure (assumption / model_projection /
  interpretation / recommendation, `materiality=high`) must be independently
  re-derived by a validator that is **blind to the producer's reasoning** —
  re-deriving from raw evidence, not ratifying the producer's logic.
- **Cross-model on purpose:** producer = Opus, validator = Codex (gpt-5), and/or a
  second Opus with a different frame. Anti-correlated failure modes catch
  hallucinations a same-model check shares.
- Divergence triggers a ZigZag; unresolved divergence is **fail-closed — the
  figure is BLOCKED**, never soft-passed. Realized by the existing codex
  second-opinion reviewer + the "re-derive blind" rule, applied per material
  judgment figure rather than once at the end.
- **NOT blanket** (codex review): low-materiality display figures and
  `formula_result`s are validated by deterministic recompute, not negotiation or
  cross-model calls — the heavy machinery is reserved for material/high-risk
  judgments to keep cost and latency bounded.

### Ownership contracts (RACI), not just a label
Some concepts are interdependent across owners (concentration touches Balance-Sheet,
Investment, Equity-Comp, and Tax). "Exactly one owner" is the **Responsible/source**
of the value; dependent owners are **Consulted** and may hold a **required sign-off**
on changes that affect them. Each figure carries its consult/sign-off set so a
change to a shared concept routes to every affected owner, not silently.

### Compliance finding schema
A reviewer finding is structured so it can be routed and tracked:
```
Finding { kind            # contradiction | unsupported | stale | unsuitable |
                          #   missing_disclosure | bad_method | narrative_conflict |
                          #   extraction_error
          root_cause_owner# the role accountable for the fix (may differ from where seen)
          severity        # blocker | amber | yellow
          materiality     # high | medium | low
          evidence        # the surfaces/figures involved
          disposition     # open | routed | remediated | escalated | accepted_risk
          audit_ref }     # provenance link
```
Compliance opens findings; it never edits figures. A finding whose `root_cause_owner`
spans multiple owners routes to all, with the Lead arbitrating sequencing.

### Escalation ladder
1. Owner↔owner ZigZag (direct).
2. Lead / CIO arbitrates (facts agreed, priority disputed); records rationale.
3. Senior Partner / Committee (technical dispute, methodology, material risk,
   exception).
4. Compliance = orthogonal gate (holds delivery; routes findings to owners).
5. Tie-break norms: legality/compliance → client best-interest (fiduciary) →
   client's lawful instruction.

### Delivery / review cycle
1. Discovery + Data Steward publish goals + normalized source facts (provenance +
   freshness).
2. Specialists publish owned figures to the registry; deterministic → recomputed
   vs raw source; judgment → blind cross-model validated; ZigZag on dependency
   edges.
3. Lead integrates **by reference to figure-IDs** (narrative/sequencing only).
4. Every surface renders by figure-ID.
5. Compliance reviews the integrated artifact (cross-model) → targeted findings
   routed to owners.
6. Owner remediation via ZigZag; only the affected node + blast radius recompute.
7. Sign-off (owners → Lead → Compliance → Committee for exceptions) → fail-closed
   publish gate (`can_publish_plan`).

## Gap vs. what is already built

**Built:** derivation graph (canonical nodes), negotiation ladder (A↔B↔arbiter),
owner agents (`plan_node_owner`, `RealLadderParticipants`), fail-closed publish
gate, codex blind re-derivation, whole-artifact reader, deterministic gates,
resolver (single-source scalars), `graph→plan` render bridge.

**Gaps:**
1. Registry coverage is partial — only core scalars are owned nodes; the
   contradiction-prone surfaces (FI-crossing table, retention rates, dashboard
   net-worth bases, tranche quantities) are not owned figures.
2. ZigZag/validation runs only at the FM/reader, not on every edge.
3. The Lead still regenerates the monolith instead of integrating-by-reference +
   rendering-from-registry.
4. Missing roles: Data Steward, Operations, Client Discovery as owners.
5. Evidence/determinism tiering is not uniformly enforced at publish.

## Phased decomposition (each phase = its own spec → plan → build, flag-gated, reversible)

Phase order revised per codex review: provenance/Data-Steward and the
contradiction-prone-surface render cutover move EARLY, because Phase 1's evidence
+ single-source claims depend on them.

- **Phase 1 — Registry + ownership + provenance foundation + hot-surface render
  cutover (detailed below).** Includes the Data-Steward provenance/freshness
  fields and cutting the *contradiction-prone* surfaces (FI-crossing, the three
  net-worth bases, retention rates, tranche pool/slice) to registry rendering —
  so the convergence claim is real for those surfaces, not deferred.
- **Phase 2 — Full render-from-registry cutover:** every remaining surface reads
  by figure-ID; the Lead integrates by reference; retire monolith re-typing; the
  reviewer reviews the registry-rendered artifact.
- **Phase 3 — Materiality-gated ZigZag + cross-model validation across edges:**
  generalize the ladder over the acyclic dependency graph with deterministic
  invalidation; material judgments get blind cross-model validation; fail-closed
  at publish.
- **Phase 4 — Remaining roles + governance:** Operations (implementation), full
  Client Discovery, and the governance responsibilities below.

**Governance responsibilities a real firm has (codex review — assign explicitly,
not necessarily as new agents):** client consent / lawful-instruction capture,
advisor-of-record, suitability/best-interest supervision, legal/tax-advice
boundary (Argosy informs, does not give regulated legal/tax advice), privacy/
security of client data, model-risk + methodology/version governance (who owns the
assumptions library and its versions), trading/implementation supervision, and the
final client-facing approval. Several map to existing gates (publish gate,
authorities); the rest are owned by the Lead/Committee.

## Phase 1 — Registry + ownership + provenance + hot-surface cutover (specced first)

**Goal:** every figure Argosy publishes is a registry record with an owner, a
`kind`, semantic identity (basis/scenario/as-of), evidence, provenance/freshness,
materiality, and a validation status — AND the contradiction-prone surfaces are
cut over to registry rendering, so for *those* surfaces single-sourcing is real
(not deferred). Phase 1 does NOT claim the whole plan converges — the rest of the
prose still flows through the existing path until Phase 2.

**Scope:**
1. Extend `FigureRecord` (over `ResolvedValue` + graph nodes) with the full shape
   above: `kind`, `owner`, `basis`, `scenario`, `as_of`, `policy_version`,
   `precision`, `evidence`, `source_freshness`, `materiality`, `validated_by`,
   `version`. Provenance/freshness fields are populated by a minimal **Data
   Steward** seam (pulled forward from Phase 4) — initially over existing ingest
   metadata, not a full new agent.
2. Define the **owner map** with consult/sign-off sets: every existing resolver
   key + every contradiction-prone figure assigned to exactly one Responsible
   owner, plus Consulted owners for shared concepts (concentration, net worth).
3. Add the missing canonical figures as registry nodes derived from their owner's
   authoritative source:
   - `net_worth.total_incl_residence_nis`, `net_worth.investable_nis`,
     `net_worth.liquid_nis` (Balance-Sheet owner) — three `basis`-labeled bases.
   - `retirement.fi_crossing_year` (Retirement owner) — derived from the
     **trajectory projection** (scenario/time-series aware), and **reconciled with**
     the current FI-margin verdict (a `formula_result` whose validation asserts the
     crossing year is consistent with the not-yet-reached margin — NOT "derived
     from the margin," which would be a false invariant per codex review).
   - `tax.retention_at_vest_pct`, `tax.retention_capital_track_pct` (Tax owner) —
     two labeled rates, never one conflated "retention."
   - `concentration.nvda_eligible_pool_sh`, `concentration.first_slice_sh`
     (Investment owner) — pool (cap) vs slice (cadence), labeled distinctly.
4. **Cut the contradiction-prone surfaces to registry rendering:** the FI-crossing
   table, the wealth-dashboard net-worth tile (to the labeled bases), the
   retention statements, and the tranche pool/slice statements render by figure-ID
   via the `graph→plan` bridge + a registry-bound dashboard. This is the part that
   makes the convergence claim REAL for these surfaces.
5. **Publish gate per figure (materiality-gated):** a material figure cannot reach
   `status=resolved` unless it carries the required evidence for its `kind`
   (`formula_result` → recompute check; material judgment → cross-model blind
   re-derivation cleared). Low-materiality figures need evidence but not the heavy
   check. Enforced in the registry build, surfaced by the existing gate suite.

**Out of scope for Phase 1:** retiring the from-scratch synthesizer + full
render-from-registry (Phase 2); generalizing the ZigZag across all edges with the
acyclic dependency graph (Phase 3); Operations/full-Discovery agents + governance
(Phase 4).

**Testing:**
- Unit: `FigureRecord` shape; owner-map completeness (every published figure has
  exactly one Responsible owner + a consult set for shared concepts); each new
  figure resolves from its owner source.
- Materiality-tier enforcement: a material judgment figure with no evidence / no
  cleared cross-model check cannot reach `resolved` (fail-closed); a low-impact
  figure is not forced through cross-model validation.
- Cross-surface: the three net-worth bases render with their `basis` labels; the
  cut-over surfaces (FI-crossing, dashboard net worth, retention, tranche) render
  by figure-ID and a reader pass finds no contradiction among THEM.
- FI-crossing reconciliation: the crossing-year validation fails if the year is
  inconsistent with the FI-margin verdict (the live pv53–pv57 blocker).
- Live: the incremental cutover cycle closes with the new figures present, the
  cut-over surfaces consistent, and the publish gate reflecting per-figure
  validation.

## Non-goals / YAGNI

- No new derivation math — figures are sourced from existing owners; the registry
  adds ownership + evidence + validation metadata, not new computations.
- No big-bang rewrite — the from-scratch synthesizer stays as cold-start /
  fallback until Phase 2 proves the render-from-registry path promotes cleanly.
- No client-specific roles or instruments baked in — roster is generalized.
