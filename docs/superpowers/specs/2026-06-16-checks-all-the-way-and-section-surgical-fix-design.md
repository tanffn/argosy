# Checks all the way + fact-level surgical fix — design

**Date:** 2026-06-16
**Status:** design v2.1 (fact-centric, codex-feasibility-hardened); pending final user review → implementation plan
**Owner area:** `argosy/orchestrator/flows/plan_synthesis/`, `argosy/quality/`,
`argosy/services/plan_numeric_resolver.py`

> v2 supersedes the section-centric v1 after an adversarial codex review
> (`tmp_review/codex_design_review_verdict.txt`, verdict NEEDS-REWORK) showed a
> plan *section* is the wrong unit: most real defects span sections, so editing
> one section just moves the contradiction. The surgical unit is a **canonical
> fact + all its render sites**.

## Problem

Plan synthesis is a ~60–90 min sequential pipeline (9 analysts → debates →
synthesizer → risk → codex → fund-manager → whole-artifact reader → gate) with
**all substantive checking bolted on at the very end**, and when a reviewer
blocks, the current reconcile loop **re-runs the entire synthesizer** and
re-reviews the whole document. Two consequences:

1. **Surprises at the end** — a defect introduced in minute 5 is not caught
   until the end-stage reviewers read the finished document ~80 minutes later.
2. **Big-bang, non-convergent correction** — fixing one mislabeled value re-does
   the whole plan; because the synthesizer is stochastic the regen reshuffles
   and re-introduces inconsistencies. Proven empirically: live run 106 (draft
   42) triggered the reconcile loop (`reader_reconcile.triggered=True`) and
   **still** ended `still_blocking=True` with 11 reader findings.

A real advisory team finds the problem where it's made, fixes only the affected
thing, and re-checks it — and a "thing" is a *fact* (the retirement age, the FX
rate, the target weights), not a paragraph.

## Goal

- **Checks all the way through** — deterministic invariants run at honest
  in-pipeline checkpoints so the end-stage review finds nothing new. The
  measurable contract: any defect class an end reviewer keeps catching is a
  missing in-stage invariant, tracked as a defect.
- **Fix only the affected fact** — a finding is corrected at its **canonical
  fact and every render site of that fact at once** (so the fix can't move the
  contradiction), deterministically where the value is known and with a cheap
  prose editor only where genuine wording is involved.

## Organizing principle: canonical facts, not sections

Every load-bearing value in the plan is a **canonical fact** with a stable
`fact_id` and a single derived value, plus the set of **render sites** where it
appears across surfaces. Examples drawn from the run-106 defects:

| fact_id | value | render sites (surfaces) |
| --- | --- | --- |
| `retirement.fi_status` | reached / not-reached (+qualifier) | headline, FI ledger, retirement page, prose |
| `retirement.earliest_safe_age` | e.g. 47 (headline) | headline, trajectory appendix, prose |
| `retirement.fi_age` | e.g. 46 (FIRE-bridge sizing age — deliberately distinct) | FI ledger, prose |
| `retirement.bridge_start_age` | == the sizing age the bridge is actually built from (today `fi_age`) | bridge sleeve sizing, prose |
| `allocation.target_weights` | the TargetAllocationDoc weights | `target_allocation_json`, medium `targets`, IPS section text, medium rationale, dashboard |
| `rsu.net_retention_pct` | e.g. 65% | RSU ledger, equity-comp evidence, A7/A8 prose |
| `event.rsu_tax_2026_06_17` | amount + currency | action line, tax calendar |
| `instrument.SGLN.wrapper_type` | physical-gold ETC (not UCITS) | instrument table, migration action text |

A fact is owned by its **derivation** (the resolver / a specific agent), not by
a section. A finding attaches to one or more `fact_id`s — **multi-owner is
allowed** (a cross-fact contradiction owns both facts).

### Typed locator (replaces the optional `locator` string)

```
FindingLocation = {
  check: GateCheck | invariant_id,
  fact_id: str | None,            # the canonical fact, when known
  surface_id: str,                # body|dashboard|appendix|target_allocation_json|fm_objection|prior_plan
  field_path: str | None,         # json_path / section_id+offset / table cell
  excerpt_hash: str | None,       # for reader surfaces_cited
  scope: "current" | "prior",
}
```
A finding carries `FindingLocation[]`. Attribution that cannot resolve a
`fact_id` is **fail-safe**: it routes to full re-synthesis *and* is logged as an
attribution gap (so missing facts are surfaced, never silently swallowed).

### Render-site source map — the keystone (codex v2 #1,#2,#3)

The fact model is only safe if attribution is **recorded at render time, not
reverse-engineered from finished text**. An `excerpt_hash` proves a string
existed; it does NOT prove which fact the string expresses (duplicate ages,
paraphrases, prior-plan excerpts). So the renderers emit a **`RenderedFactSite`
ledger** as they produce each surface:

```
RenderedFactSite = {
  fact_id, surface_id, field_path, byte_span,
  rendered_text, normalized_value, site_kind, hash,
}
site_kind ∈ { template, structured_field, llm_prose }
```

- **`template` / `structured_field`** sites are produced *from* the canonical
  value, so they can be **deterministically re-rendered** when the fact changes.
- **`llm_prose`** sites are authored free text (`HorizonSection.rationale`,
  `Action.detail`/`rationale`, `Section.body_md`). They CANNOT be deterministically
  re-rendered; they must either (a) carry an explicit `fact_id` claim emitted by
  the synthesizer alongside the prose, or (b) route through the prose editor.

**Fact inventory (net-new work, do not hand-wave).** The resolver
(`ResolvedValue`: key/value/unit/status/source_locator) and `TargetAllocationDoc`
cover the *numeric/allocation* facts, but not RSU retention, the tax-event
currency, the SGLN wrapper type, the SOFI evidence state, FM-objection staleness,
action-level estate routing, or coverage status. Phase 1a (below) must produce an
explicit table: **each run-106 `fact_id` → its derivation function → current
source object → which render sites + their `site_kind`** before any invariant
runs. Anything not derivable today is a named new derivation, not an assumption.

## Invariants (the checks), three kinds

1. **Single-fact** — the fact's value is well-formed: FX in NIS-per-USD band,
   a date not past-due-but-rendered-pending, a number == the resolver value.
   (The three gates already shipped 2026-06-16 are single-fact invariants.)
2. **Cross-fact / relational** — relations between facts hold. These are the
   NEW invariants the run-106 evidence requires. NOTE on the retirement ages
   (codex v2 #4): the resolver *deliberately* distinguishes
   `retirement.earliest_safe_age` (headline) from `retirement.fi_age` (the
   FIRE-bridge sizing age) — they are NOT meant to be equal. So the invariant is
   **not** "the two ages are equal"; it is (a) each age is **labeled by its
   definition** everywhere it appears (the S22/S23 distinct-labeling rule), and
   (b) `bridge_start_age` **consistently uses the resolver's chosen sizing age**
   (add an explicit `retirement.bridge_start_age` fact rather than inferring it
   from `fire_bridge_nis`). Other relational invariants: `fi_status` coherent
   with the FI age set; `rsu.net_retention_pct` equal across ledger + equity-comp;
   FI "reached" is FX-shock-robust (not just NVDA-shock-robust).
3. **Render-site consistency** — every render site of a fact shows the SAME
   value/label (cross-surface coherence, generalized to fact level). Includes
   the IPS equality across `target_allocation_json` + medium targets + IPS prose
   + rationale.

## Phase 1 — Shift-left invariants (ships first)

Phase 1 splits in two (codex v2 #5 — the fact registry + render ledger is real
work and must not be smuggled in as "just run the checks earlier"):

- **Phase 1a — fact + render-site inventory.** Build the fact inventory table
  above for the run-106 facts: each `fact_id`, its derivation function, current
  source object, render sites + `site_kind`. Instrument the renderers to emit the
  `RenderedFactSite` ledger. Build `TargetAllocationDoc` BEFORE rendering so the
  IPS/allocation sites render from it (codex v2 #6 — today `_assemble_draft_bodies`
  renders markdown then resolves `target_allocation_json` after; that order must
  flip). No invariants yet — just the addressable substrate.
- **Phase 1b — invariant execution** at honest checkpoints, **layered**:

- **Layer A — after the analysts:** per-analyst *typed* input checks + input
  freshness/completeness (an analyst's FX value, dates, internally-consistent
  numbers). These are new analyst-output-shaped checks; the existing doc-level
  gates cannot run here because they need the assembled artifact.
- **Layer B — after the assembled draft is fully built** (after the language
  rewriter, speculation-cap enforcement, appendix render, TargetAllocationDoc
  resolution, and body assembly — NOT on raw phase-3 JSON, which is mutated
  afterward): the full single-fact + cross-fact + render-site invariant suite.
  This is the real shift-left point.
- **Layer C — end:** codex re-derivation + whole-artifact reader stay as the
  holistic net. They should be quiet.

Phase 1 surfaces every deterministic defect before the end reviewers; it does
not yet auto-correct. It requires the fact registry + typed locators (below) but
not the editor/patcher machinery.

## Phase 2 — Fact-level surgical correction (ships second)

For each finding attributed to `fact_id`(s):

1. **Deterministic re-render** of the fact's `template` + `structured_field`
   sites from the canonical source, in one step (IPS prose renders FROM
   `TargetAllocationDoc`; the sizing age propagates to the bridge sleeve, ledger,
   appendix). Fixing the fact fixes all its deterministic sites at once — this is
   what stops the contradiction moving. (codex v2 #3: this only works for sites
   the `RenderedFactSite` ledger marks deterministic.)
2. **Prose editor (cheap LLM)** for `llm_prose` sites that can't be re-rendered —
   the realistic majority of body prose. Handed only the fact, its canonical
   value, and the offending text; returns a minimal corrected snippet.
3. **Re-verify** the touched fact bundle **plus the full deterministic suite**
   (the suite is global by design — FI-shock and coherence read artifact-wide),
   then **keep the whole-artifact reader as the holistic net**. Section-scoped
   re-review is explicitly rejected (it would miss cross-fact contradictions).
4. **Full re-synthesis only for *structural* findings** — when a fact's
   *derivation* is wrong (the strategy/number itself), not its rendering. The
   existing full-resynth reconcile loop survives here, and is **not demoted**
   until the fact-invariant graph demonstrably covers the run-106 classes.

## Run-106 ground-truth coverage (the acceptance backbone)

Each finding must be caught by a NAMED invariant before reader review — "the LLM
reader might catch it" does NOT count as coverage. (Derived from codex's walk;
`tmp_review/codex_design_review_verdict.txt`.)

| # | finding | catching invariant | fix unit |
| --- | --- | --- | --- |
| 0 | FI sufficiency fragile under −10% FX | NEW `fi_status` FX-shock invariant | `retirement.fi_status` render sites |
| 1 | FI crossed-today vs age 47 vs age 45 | NEW FI timeline/status cross-fact invariant | FI fact bundle |
| 2 | retirement age 46 vs bridge 47→60 | NEW: distinct age labels + `bridge_start_age` consistently uses the resolver sizing age (NOT forced equality — see invariant note) | retirement-age fact bundle |
| 3 | RSU retention 47% vs 65% | NEW RSU-retention consistency | `rsu.net_retention_pct` sites |
| 4 | June-17 tax NIS vs USD | NEW event-amount currency invariant | `event.*` amount+unit sites |
| 5 | IPS 100% vs ~106 | `check_ips_allocation_sum` rebuilt to render IPS from `TargetAllocationDoc` + equality across sites | `allocation.target_weights` sites |
| 6 | stale FM objection 3,000 vs 5,600 | NEW stale-reviewer-text invariant | FM-objection surface |
| 7 | SGLN not UCITS but in UCITS migration | NEW instrument-taxonomy invariant | `instrument.SGLN.wrapper_type` sites |
| 8 | SOFI promoted while news adapter missing | NEW candidate evidence-readiness invariant | candidate evidence-state |
| 9 | estate precondition vs SGOV at Schwab | NEW action-level estate-routing invariant | action + rationale |
| 10 | coverage appendix confidence contradiction | NEW coverage-status invariant | renderer/status metadata |

A run-106 regression fixture (full reader JSON persisted) asserts each row is
caught by its named invariant.

## New components

| Component | Responsibility |
| --- | --- |
| Fact inventory (Phase 1a) | the explicit table: each run-106 `fact_id` → derivation fn → current source object → render sites + `site_kind`; flags net-new derivations (RSU retention, tax-event currency, SGLN wrapper, SOFI evidence, FM-objection staleness, estate routing, coverage status) |
| `RenderedFactSite` ledger | renderers emit fact→site mapping at render time (the keystone); classifies each site `template`/`structured_field`/`llm_prose` |
| Pre-render allocation doc | build `TargetAllocationDoc` BEFORE body/appendix render so allocation sites render from it (flip current `_assemble_draft_bodies` order) |
| Typed locator + multi-owner findings | `FindingLocation[]`; replaces the optional `locator` string |
| Attribution | finding (locator / `surfaces_cited` excerpt) → `fact_id`(s); fail-safe + logged on miss |
| Single-fact / cross-fact / render-site invariants | the three invariant kinds; the run-106 table is the initial set |
| Layered in-stage hooks | Layer A (post-analyst typed checks) + Layer B (post-assembly full suite) |
| Deterministic re-renderer | set a fact value → re-render all its sites from canonical source |
| Prose editor agent | cheap single-fact prose correction where re-render can't apply |
| Scoped + global re-verify | touched fact bundle + full suite + whole-artifact reader retained |
| Run-106 fixture | full reader JSON + per-finding coverage assertions |

## Testing

- **Attribution** — map representative locators and reader `surfaces_cited`
  excerpts (incl. duplicate, prior-plan, appendix, FM-objection, non-section
  surfaces) to the correct `fact_id`(s); unattributable → structural path + log.
- **Invariants** — per invariant, red→green on a planted defect; the run-106
  fixture asserts coverage of all 11 findings by named invariants.
- **Deterministic re-render** — set a fact (e.g. retirement age 46→47) → every
  render site updates → all relevant invariants pass; no other fact changes.
- **Prose editor** — stubbed-LLM wire test (mirror the reconcile test's
  isolation of `run_alternatives_phase`).
- **Re-verify** — after a surgical patch the FULL suite + whole-artifact reader
  still run; a planted cross-fact contradiction is NOT missed.
- **Gate placement** — a defect in a rewriter/cap/appendix-rendered surface is
  caught at Layer B (post-assembly), proving raw-phase-3 gating would miss it.

## Out of scope (this design)

- Re-running the owning analyst as the fix mechanism (root-cause) — deferred;
  fact-level deterministic re-render + prose editor covers the observed classes.
- Demoting the full-resynth reconcile loop or the whole-artifact reader — they
  stay until the fact-invariant graph provably covers the run-106 classes.
- Changing the analyst fleet, allocation methodology, or speculation routing.
- The genuine ~1% FI-margin fragility is real, not a wording defect; the
  `fi_status` invariant forces an honest qualifier — it must not hide it.
