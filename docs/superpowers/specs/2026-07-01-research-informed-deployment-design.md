# Research-informed deployment — design

## Problem

The cash-deployment recommendation (`GET /api/portfolio/deploy-cash`,
`deployment_advisor.assemble_deployment_plan`) is a deterministic IPS-percentage
filler that consults **no live price, no news, no analyst, and no look-through**.
On a book that is 56.6% NVDA with a $127k SGOV reserve and 0% gold, a $100k run
recommended: ~69% into US large-cap ETFs (CSPX/R1GR/FUSA — which **re-buy NVDA**
via S&P/Russell index look-through), ~28% into more T-bills (**on top of** the
existing SGOV reserve), and **$0 to gold** (the biggest gap) — because the
canonical plan (v62) has **no gold sleeve at all**. Gold was ~$4,000/oz (ATH) and
nothing checked. The engine mechanically topped up plan percentages while
deepening the concentration being unwound and parking cash already held.

This is the failure class documented in
`memory/project_deploy_cash_concentration_blind_and_no_gold.md`.

## Prior art (this is completion, not greenfield)

- **`docs/superpowers/specs/2026-06-12-deployment-advisor-design.md`** already
  designed the right thing: prioritise biggest underweight gaps, estate-safe
  UCITS first, diversify away from NVDA, **fill the empty Alternatives bucket
  with gold (SGLD/IGLN)**, with **geographic + fund look-through** and a
  **correlated-exposure cap (NVDA/semis/AI) spanning single-names + fund
  look-through**. Its P1/P2 shipped; **P3 (honest gaps + gold + look-through)
  and P4 (tactical) were never finished** — the root of today's behaviour.
- **`argosy/services/contracts.py`** — `AllocationCandidate` / `AllocationLeg`
  (frozen value objects) + `candidate_fingerprint` gate. Reuse; do not redefine.
- **`argosy/services/decision_funnel/`** — the kill-switch/shadow/trace pattern
  to mirror: `ARGOSY_DECISION_FUNNEL_ENABLED` master switch, `*_shadow` default
  True, shadow rows born `shadow=1` atomically, `funnel_trace`/`funnel_view`.
  `stage0_market.build_market_read` produces market regime + per-ticker
  `NewsSignal` sentiment + high-materiality news.
- **`argosy/quality/change_adjudication.py`** (`ChangeRequest`, `ChangeKind`,
  `adjudicate`) + `argosy/orchestrator/flows/incremental_plan.py` — the
  living-plan change-request path, gated by `ARGOSY_INCREMENTAL_PLAN`.
- **`RiskOfficerAgent`** (3 debating perspectives; emits approve/reject/
  conditions) — the judgment layer, used with a **bounded action contract**
  (below), never as a free-form dollar optimiser.

## Owner decisions (fixed)

1. **The fleet re-decides the buy list** (reprioritise / resize / skip on live
   price + news + risk), not just annotate.
2. **A class the plan lacks (gold) triggers a plan change-request** through the
   living-plan graph; owner approves; plan updates; **then** deploy on-plan. One
   canonical plan preserved.

## Codex design-review corrections (binding)

1. **The risk officer must not invent dollar amounts.** It emits a **bounded
   action** per candidate: `APPROVE | VETO | DEFER | CAP_AT_PCT_OF_CANDIDATE |
   MOVE_TO_RESERVE | REQUIRE_PLAN_CHANGE`. A **deterministic sizer** then
   computes final dollars under explicit constraints (cash-only, no sells,
   Σ ≤ deployable cash, plan targets, sleeve/tier caps, concentration cap incl.
   look-through, min ticket, reserve bounds, max deviation from generator).
   Objective: **minimise target-tracking error + penalties** for concentration,
   stale data, adverse news, valuation stretch, and cash drag.
2. **Gold-at-ATH is evidence, not a rule.** Never hard-code "skip ATH." Encode
   distance-from-ATH, z-score vs own history, drawdown, momentum, volatility,
   news sentiment, and gap size as **features**; the risk officer may recommend
   buy / partial / DCA / defer / reserve, but must cite evidence.
3. **The final buy list must be derived from the plan version it claims to
   follow.** If a change-request amends the plan, the deploy list is recomputed
   against the new version before surfacing — override and amendment must never
   diverge or oscillate.
4. **Reserve policy, not just debate.** Existing cash-like holdings
   (SGOV/IB01/cash rows) count against the reserve target **before** any T-bill
   line is proposed.
5. **Typed `PlanGap`**, not prose: `PlanGap(asset_class, current_target_pct,
   proposed_target_pct, reason_refs, blocked_amount_usd)`.
6. **Shadow needs validation criteria** (see Testing).

## Architecture

New package `argosy/services/deployment_funnel/`, a thin orchestrator composing
existing parts. `assemble_deployment_plan` is **demoted to a candidate
generator** — wrapped so its output is typed as `candidates`, never
`recommendation` (codex concern 7).

The build is **three increments**, deterministic-first (codex):

### Increment 1 — deterministic preflight (the core deliverable)

No LLM. Catches the exact failure class deterministically and is fully
unit-testable. Shadow-only.

1. **Candidates** — run `assemble_deployment_plan` as the generator; map its
   lines to `AllocationCandidate`s.
2. **Enrichment** (deterministic, fail-closed on staleness):
   - live quote per candidate symbol via the yfinance adapter;
   - price-history features (distance-from-ATH, z-score vs own trailing window,
     drawdown) — recorded as **features**, never as a gate;
   - `NewsSignal` sentiment already ingested (trace says *"no recent ingested
     signal"* when absent, not "no news" — codex concern 8).
3. **Look-through map** — an explicit, versioned map of the household's held
   broad funds + candidate ETFs to their material NVDA / US / region weights
   (`CSPX`≈7% NVDA, `R1GR`≈14% NVDA, SGOV/IB01→cash-like, gold ETCs→alternatives,
   FWRA/ACWD/IWDA→region split). Yields **effective** NVDA / US / class exposure,
   not nominal.
4. **Reserve policy** — sum existing cash-like holdings against the reserve
   target; a T-bill/cash candidate is only kept for the shortfall (if any).
5. **Deterministic gates → candidate status**: `approve_candidate | veto |
   defer | requires_plan_change | cap_at_pct`, from:
   - reserve duplication (reserve already funded → veto/cap cash lines);
   - effective-NVDA / correlated-exposure cap breach via look-through
     (→ cap or veto the offending US-index line);
   - stale-quote fail-closed (→ defer);
   - plan-gap detection (class with a real need but no plan sleeve, e.g. gold
     → `requires_plan_change` + a typed `PlanGap`).
6. **Output**: a `deployment_funnel` trace (mirrors `funnel_trace`) with every
   candidate's features + status + reason code, and a revised candidate list.
   Client-facing reason codes (codex concern 11): *"reduced CSPX — NVDA
   look-through breaches cap," "no added T-bills — reserve already funded,"
   "gold skipped — pending plan approval."*

**Increment 1 alone** would have caught all three of the original failures
without any LLM.

### Increment 2 — bounded LLM judgment

- `RiskOfficerAgent` (3 perspectives) receives the enriched candidate +
  features and returns a **bounded action** (contract above) with cited
  evidence. Gold-at-ATH is judged on features, not a rule.
- A **deterministic sizer** turns approved/capped actions into final dollar
  amounts under the constraint set + objective (codex correction 1). The LLM
  never emits dollars.

### Increment 3 — plan-change-request coupling

- `requires_plan_change` findings become a `ChangeRequest` (add gold sleeve at
  an **engine-derived** weight, trim US) through `incremental_plan` /
  `change_adjudication`.
- On approval, the deploy list is **recomputed against the amended plan
  version** (codex correction 3), then surfaced in `/inbox` as needs-confirm.

## Data & config

- Reuse `contracts.AllocationCandidate` / `AllocationLeg` + `candidate_fingerprint`.
- New typed `PlanGap` (frozen dataclass) + `CandidateStatus` enum in the package.
- New `DeploymentFunnelRun` + per-candidate rows for the trace (migration),
  mirroring `funnel_runs` / `funnel_stage_rows`; shadow rows born `shadow=1`.
- Kill switches: `ARGOSY_DEPLOYMENT_FUNNEL_ENABLED` (master, default off),
  `ARGOSY_DEPLOYMENT_FUNNEL_SHADOW` (default on). Fail-closed.

## Testing

- **Deterministic money-math + gates**: unit tests with stubbed quote/history/
  news. Fixtures replay the exact failure book (56.6% NVDA, $127k SGOV, 0% gold)
  and assert: CSPX/R1GR capped/vetoed for effective-NVDA breach; no net-new
  T-bills (reserve funded); gold → `requires_plan_change` + `PlanGap`. Dollar
  conservation (Σ candidates ≤ deployable) asserted every run.
- **Shadow validation criteria** (go-live gate, codex correction 6): persisted
  frozen inputs + structured outputs measuring dollar conservation, cap
  breaches, stale-data fail-closed, delta vs the old advisor, veto/defer counts,
  plan-gap detections, **repeat-run stability on identical frozen inputs**, and
  human accept/reject labels. Materially different buy lists on identical inputs
  = not ready.
- **LLM eval** (`@pytest.mark.llm_eval`) only for judgment quality (Increment 2).
- **codex-tandem** on the sizer math, the look-through/exposure computation, and
  the bounded-action contract.

## Scope guard (YAGNI)

Deployment of idle cash only — **no auto-execution, no sells/rebalance, no new
news sources.** Selling NVDA stays a plan decision. Increments 2–3 do not block
Increment 1 shipping in shadow.

## Open questions from the prior spec — resolved here

- **Look-through data source**: an explicit, versioned map for the *handful* of
  held broad funds + candidate ETFs (not a live holdings feed) — enough for the
  correlated-exposure cap; expandable.
- **Gold/Alternatives weight**: engine-derived via the change-request (Increment
  3), not a magic number; deployment never invents the target.
- **Gold class = plan-level change** (change-request), not an engine-only target
  addition — preserves the one-canonical-plan contract.
- **Reserve trigger baseline** and the tactical single-name screen (prior P4)
  are **out of scope** for this design; deferred.
