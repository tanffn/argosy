# Design docs — what was actually built

**Method.** Every entry below was verified against the CODE as of 2026-08-12, not against the doc's own status claims. Those claims were wrong often enough that they were explicitly distrusted during the audit. Eight parallel agents each took a themed group, cited `file:line` evidence, and reported UNVERIFIED rather than guessing. Spot-checks by the reviewing session corrected two agent findings (noted inline) — treat this index the same way: **verify before acting.**

**Status key:** BUILT = verified in code · PARTIAL = core built, named seams missing · NOT_BUILT = absent · SUPERSEDED = replaced by later design · N/A = not a code deliverable.

---

## ⚠️ Read this first — the pattern the audit found

Three independent mechanisms **report success when they did not run**:

| Mechanism | Reports | Reality | Evidence |
|---|---|---|---|
| Codex math gate (plan headline numbers) | pass | returns `(None,None)` fail-soft when codex is dead | `plan_synthesis/codex_second_opinion.py` |
| Whole-artifact coherence reader | `_reader_ok = True` | returns `None` on timeout / missing kit → plan approved | `orchestrator.py:2657`, `whole_artifact_reader.py:643` |
| Weekly email digest | `job_runs.status='ok'` | `send_status: skipped`, `send_error: smtp_not_configured` | `job_runs` output_summary, 2026-08-07 |

**Absence of a result is treated as a passing result.** This is the single most important finding in the audit, and it is a design habit rather than three separate bugs. The SDD (`SDD.md:1354`) explicitly claims the reader is *"fail-closed: an unparseable or timed-out reader yields BLOCK, never a soft pass"* — the code does the opposite.

**Corollary for anyone reading a green check anywhere in this system: confirm the check ran.**

---

## Plan core — distillate, synthesis, derivation graph

| Doc | Status | Note |
|---|---|---|
| `specs/2026-05-05-plan-distillate-design` | BUILT | roles, distiller, plan_watcher, speculation router all live |
| `specs/2026-05-07-plan-amendment-chat-flow-design` | BUILT | tiered small/medium/large amendment path |
| `specs/2026-06-18-derivation-first-plan-design` | PARTIAL | **Slice 6 gap** — from-scratch synthesis writes prose numbers directly to `horizon_*_json`, bypassing `PlanDecisionModel` |
| `specs/2026-06-18-living-plan-derivation-graph-design` | PARTIAL | `blast_radius.py:22` — plan prose/allocations NOT in the graph; "contradiction impossible by construction" doesn't hold for synthesis output |
| `specs/2026-07-03-incremental-plan-refinement` | PARTIAL | money-safety invariant net **INERT** at `/refine` (`plan.py:5688` omits `post_doc`) |
| `plans/2026-05-05-plan-distillate-implementation` | BUILT | |
| `plans/2026-05-07-plan-amendment-chat-flow-implementation` | BUILT | |
| `plans/2026-06-18-derivation-first-plan` | PARTIAL | mirrors spec |
| `plans/2026-06-18-derivation-graph-engine` | BUILT | |
| `plans/2026-06-18-graph-hydration` | BUILT | |
| `plans/2026-06-18-graph-persistence-replay` | BUILT | 5 tables live |
| `plans/2026-06-18-graph-surfaces` | BUILT | |
| `plans/2026-06-18-incremental-plan-pipeline` | BUILT | route absorbed into `plan.py` |

## Expenses / household

| Doc | Status | Note |
|---|---|---|
| `specs/2026-05-09-household-expenses-design` | PARTIAL | **plan never sees real spend** (below) |
| `specs/2026-05-09-ex4-expenses-dashboard-design` | BUILT | |
| `specs/2026-05-09-ex1.1-stabilization-design` | BUILT | 7 bugs + fx module, regression-tested |
| `specs/2026-05-09-ex1.1-verify-findings` | N/A | findings doc |
| `specs/2026-05-10-expenses-overview-monthly-split-design` | BUILT | |
| `specs/2026-05-11-merchant-category-tab-design` | BUILT | |
| all 5 matching `plans/` | BUILT | |

**Gap:** `plan_synthesis/inputs.py:1006 _assemble_household_budget_payload` reads only `identity_yaml`; `monthly_burn_nis` = the number typed at onboarding. **Zero** references to `ExpenseTransaction` in `plan_synthesis/` or `household_budget_analyst.py`. SDD §6 (line 544) claims this analyst is fed "per-category spend" from the expense tables — it is not. `cal.py` / `amex.py` / `diners.py` raise `NotImplementedError`.

## Retirement / insurance / cashflow

| Doc | Status | Note |
|---|---|---|
| `specs/2026-06-05-retirement-optimizer-design` | PARTIAL | `optimize_deconcentration` exists but is **not API-exposed**; `/projection/spend-frontier` + `/cashflow-streams` unbuilt |
| `specs/2026-05-24-insurance-coverage-analysis-design` | **NOT_BUILT** | none of the 5 agents, no `insurance_yaml`, no `/insurance` route |
| `specs/2026-05-29-life-events-cashflow-redesign-design` | BUILT | delta_kind model live; wrong clamp removed |
| `plans/2026-05-28-retirement-companion-overhaul` | PARTIAL | ~17 modules under `services/retirement/` exist |
| `plans/2026-05-28-...-questions` | N/A | records autonomous defaults (12-mo floor, healthcare ramp) never user-confirmed |
| `plans/2026-05-27-cashflow-projection-pivot` | BUILT | |
| `plans/2026-06-20-fi-crossing-year` | BUILT | |
| `plans/2026-05-28-windfall-flow-resume` | BUILT | backend + UI |
| `plans/2026-05-24-insurance-coverage-analysis-implementation` | **NOT_BUILT** | all 24 tasks |

**Substitute in place of insurance:** `services/retirement/insurance_gaps.py` — a 10×-income heuristic reading intake-supplied coverage figures. No policy documents are analyzed.

## Agent fleet / deliberation

| Doc | Status | Note |
|---|---|---|
| `specs/2026-06-19-financial-advisory-team-design` | PARTIAL | `build_figure_registry` **never called** in synthesis; no per-figure cross-model validation; no DataSteward |
| `specs/2026-06-17-coherence-deliberation-arbitrator-roles-design` | PARTIAL | roles exist; **outer reader fails open** |
| `specs/2026-05-22-baseagent-api-features-design` | BUILT | caching, thinking, citations (API-key path only) |
| `specs/2026-06-08-dynamic-allocation-owner-...` | PARTIAL | long_hold default BUILT; **`allocation_path.py` + `allocation_strategist` entirely absent** |
| `plans/2026-06-17-coherence-deliberation` | BUILT | Slices 1–5; registry covers only 2 subjects |
| `plans/2026-06-15-whole-artifact-coherence-reviewer` | PARTIAL | fail-open dispatch |
| `plans/2026-07-11-fleet-calibration-five-agent-pipeline` | BUILT | |
| `plans/2026-06-13-alternatives-fleet-sourcing` | BUILT | ISIN + estate gate live |
| `plans/2026-05-22-baseagent-api-features-implementation` | BUILT | |
| `plans/2026-06-09-p3-unblock-experts` | BUILT | age-aware tax curve wired |

## Monitoring / predictions / delivery

| Doc | Status | Note |
|---|---|---|
| `specs/2026-05-29-predictions-ledger-design` | BUILT | 1,428 predictions / 2,316 outcomes; 5 live sources |
| `specs/2026-05-29-state-observer-agent-design` | BUILT | 41 snapshots; 4 runs lost to `database is locked` |
| `specs/2026-05-29-jobs-registry-design` | BUILT | 22,789 `job_runs`; fail-open status bug fixed |
| `specs/2026-05-29-last-mile-delivery-design` | PARTIAL | **no delivery channel works** (below) |
| `specs/2026-05-29-anomaly-detection-rsu-prevest-design` | BUILT | buckets A–D live |
| `specs/2026-05-29-plan-execute-monitor-reorg-design` | BUILT | `/life-events` page missing |
| `specs/2026-05-29-pre-kickoff-locked-decisions` | N/A | |
| `plans/2026-05-26-tier3-tier4-observability-implementation` | BUILT | daily brief off by default |

**Delivery is dead:** email `smtp_not_configured` (job still reports `ok`), `notification_subscriptions` = **0 rows**, Discord listener dead since 2026-07-08 (auth 4004) while 434 historical Discord predictions still appear in `source_reliability` as if live. 59 open proposals sat in the last digest, undelivered.
*Corrected during review:* an agent reported `pending_reevaluation_daily` failing 57% as a current regression — the 20 errors are historical, last one 2026-07-03; the last 6 runs are all `ok`.

## Deployment / allocation / execution (money path)

| Doc | Status | Note |
|---|---|---|
| `specs/2026-06-12-deployment-advisor-design` | BUILT | P3/P4 tiers diverge from spec |
| `specs/2026-06-12-slice1-allocation-execution-design` | BUILT | conservation verified in `cash_only_deploy` |
| `specs/2026-07-01-research-informed-deployment-design` | PARTIAL | Increment 3 (plan-change coupling) unbuilt |
| `specs/2026-07-02-proactive-period-directive-design` | PARTIAL | spec's **hard** FX-freshness gate is advisory only; severity tiers + glide-corridor sell absent |
| `plans/2026-06-12-deployment-advisor` (+ `-p2`) | BUILT | |
| `plans/2026-06-12-slice1a-allocation-engine` | BUILT | |
| `plans/2026-06-12-allocation-discovery-ux-master-plan` | BUILT | Phase 3 partial |
| `plans/2026-07-01-research-informed-deployment-increment-1` | BUILT | dollar conservation enforced |
| `plans/2026-06-18-ladder-ruling-direction` | BUILT | |

*Severity corrected during review:* an agent called the missing `cash_usd` bound at `portfolio.py:2121` "the most dangerous gap on the money path." It is a **user-supplied** figure producing a proposal, not an execution — the real gap is only the absence of a warning when it exceeds known cash. Recorded as minor.

## Grounding substrate — facts, figures, gates

| Doc | Status | Note |
|---|---|---|
| `specs/2026-06-16-checks-all-the-way-...` | PARTIAL | `allocation_fact_sites` built but never called from the orchestrator |
| `plans/2026-06-16-fact-ledger-substrate` | BUILT | wiring unconfirmed |
| `plans/2026-06-16-fact-surgical-correction` | PARTIAL | `ARGOSY_SURGICAL_CORRECTION` default **OFF**; path inert |
| `plans/2026-06-16-shift-left-instage-gate` | BUILT | logs violations, never blocks |
| `plans/2026-06-18-change-adjudication-substrate` | BUILT | **`can_publish_plan` bypassed** on any exception (`plan.py:3839-3847` → bare `evaluate_promotion`) |
| `plans/2026-06-19-figure-registry-foundation` | BUILT | material judgment figures stay `pending`, uncross-validated |
| `plans/2026-06-19-figure-registry-1b-networth-basis` | BUILT | killed the ₪14.05M vs ₪11.87M split |
| `plans/2026-06-20-phase-1c-surface-cutover` | BUILT | React dashboard not cut over |
| `plans/2026-06-20-phase-2a-registry-review-artifact` | BUILT | default ON |
| `plans/2026-06-20-phase-3a-finding-router` | BUILT | Phase 3b deferred |
| `plans/2026-06-01-fm-obj-7-enforcement-substrate` | **NOT_BUILT** | **no `PlanPolicy`, no `instrument_classification`, no sector-cap check anywhere** |

**The caps are not enforced.** `decisions/risk_preflight.py:180 check_concentration_cap` still uses a `dict[str,float]` keyed by ticker with no sector logic. The 35% info-tech cap and the 15% single-name cap exist **only as prose**. A buy that re-concentrates tech is not blocked.

## UI surfaces + roadmaps

| Doc | Status | Note |
|---|---|---|
| `specs/2026-05-23-wave-b-ui-agent-visibility-design` | BUILT | |
| `specs/2026-05-24-plan-tab-synthesis-button-design` | BUILT | |
| `specs/2026-05-26-plan-ui-redesign-design` | PARTIAL | Tier C (sources heatmap) unbuilt |
| `specs/2026-05-31-tab-cleanup-and-advisor-welcome-design` | PARTIAL | `PlanBaselineView` fallback missing |
| `specs/2026-06-21-overview-plan-explainer-design` | PARTIAL | **built but unreachable** (below) |
| `specs/2026-06-22-proposals-action-inbox-redesign` | PARTIAL | discovery→funnel wiring absent |
| `specs/2026-06-22-proposals-living-surface-review-and-plan` | SUPERSEDED | |
| `specs/2026-07-13-ibta-cash-reclassification-design` | BUILT | |
| `specs/2026-06-19-generator-swap-spike` | PARTIAL | M1 only; M2–M4 open |
| `plans/2026-05-23-…`, `2026-05-24-…`, `2026-06-19-m1-render-bridge`, `2026-06-20-retention-split`, `2026-07-13-ibta-…` | BUILT | |
| `plans/2026-05-26-plan-ui-redesign-implementation` | PARTIAL | |
| `plans/2026-05-29-logo-and-tagline` | N/A | |
| `plans/2026-06-01-wave-7-convergence-and-scoping` | PARTIAL | scoped re-synthesis (Piece C) unbuilt — every objection round costs a full 5-phase run |
| `plans/2026-06-01-wave-8-plan-recap-view` | PARTIAL | code comment calls it "placeholder so the page renders SOMETHING" |
| **Roadmaps:** `2026-05-25-argosy-gaps-master-roadmap`, `2026-05-26-everything-but-autonomous-master-plan`, `2026-06-09-argosy-realignment-roadmap`, `2026-06-08-foundation-remediation` | PARTIAL | never-built items listed below |

**`/overview` is built and unreachable.** `api/routes/overview.py` is registered (`main.py:129`), `services/overview_assembler.py` exists, and 10 chapter components live under `ui/src/components/overview/` — but `ui/src/app/overview/page.tsx` **does not exist** and `/overview` is not in `nav.tsx`. The 7-chapter plain-language plan story — the most direct expression of "the user should not have to be the investing expert" — cannot be opened.

**Never built, from the roadmaps:** autonomous daily trade-proposal generation without a manual `/consult` trigger (`discovery_pick` never wired as a Stage-1 source); the predictions/source-reliability UI; IBKR or any broker write path; bidirectional Discord / WhatsApp / Telegram.

---

## Where the north star actually stands

> *Trustworthy, always-on financial brain for one family — right, current, self-consistent across /plan, /portfolio, /retirement; maximize finances and earliest safe retirement; the user should not have to be the investing expert.*

| Claim | Verdict |
|---|---|
| **right** | ⚠️ The plan predates the restore; the analyst's concentration input excludes unmanaged NVDA; burn comes from a typed onboarding number, not real spend. |
| **current** | ⚠️ Current plan is `refinement-2026-07-13-165608`. |
| **self-consistent** | ❌ NVDA: book **58.02%**, plan target **8.0%**, IPS prose **12%**. Flagged 2026-07-07, still open. |
| **always-on** | ❌ Watches well; cannot speak. Email skipped, 0 push subscribers, 59 proposals undelivered. |
| **user isn't the expert** | ⚠️ The feature built for exactly this (`/overview`) has no route. |
| **safe** | ⚠️ Caps are prose-only; no insurance review; sequence-risk glide engine absent. |

The machinery is real and largely well-built. What's missing is mostly **the last mile**: pipes between subsystems that exist, and gates that fail closed instead of open.
