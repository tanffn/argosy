# Handover — "stop overriding the fleet" remediation program

**Date:** 2026-07-01 · **Branch:** `master` · **HEAD at handover:** `284b71d`

## Why this exists

The owner (Ariel) identified a **fundamental error class**: *deterministic code that
makes or overrides an investment/financial JUDGMENT which should come from either
the fleet-authored PLAN (agent-produced target allocation, caps, theses) or the
agent FLEET at decision time.* Argosy's contract is "the fleet decides, the engine
executes + reconciles + renders." Where code encodes the judgment itself, it
usurps the fleet at the most critical point (the money decision / the headline
number).

Two acute instances surfaced live while answering "how do I deploy $100k?":
1. A **concentration policy** invented in `deployment_funnel/gates.py` (veto/cap a
   buy by its NVDA look-through weight). The plan only supplies the cap NUMBER;
   the deployment POLICY was hand-coded.
2. A hardcoded **"required gold sleeve"** in `deployment_funnel/plan_gaps.py` — an
   assertion the fleet's plan never made (the fleet chose a Real-assets/REIT sleeve
   and deliberately omitted gold).

Owner directive: **fix both the deployment layer AND `phase_expenses`** (order
doesn't matter), and parametrize the softer hardcoded defaults so they're
plan/IPS-owned. "The rules come from the plan, created by the fleet."

## Audit result (3 parallel Explore agents, de-noised)

The codebase is **mostly disciplined** (Agent 2 found the plan-synthesis/gates path
clean; most flagged constants are proper plan-first fallbacks or signed-off
numbers). Genuine instances of the error class, ranked:

**TIER 1 — real fleet/data overrides, fix:**
- **`argosy/services/retirement/phase_expenses.py:73–110`** — hardcodes life-stage
  spending phases (kids 43–55 ×1.10, empty-nest 56–64 ×0.85, healthcare 65–80 ×1.10,
  late-life 81–95 ×1.15) that **shape the retirement safe-age** (feeds the MC, not
  display-only), AND **ignores the user's real kids' birth years** available at
  `identity_yaml.pensions_ariel.kid_birth_years`. Overrides real data + moves the
  headline. HIGHEST value.
- **`argosy/services/deployment_advisor.py:215–264`** — DCA pacing market-timing
  policy (S&P >8%/15%, VIX 20/30 → lump vs 2/4/6-week spread). Tactical call that
  should be the risk-officer/fund-manager's, at money-decision time.
- **`argosy/services/retirement/safety_gates.py:384–403`** — conflict-scenario
  verdict boundaries hardcoded (P(ruin)>50%→FAIL, >30%→WARN). Scenario is sourced;
  the risk-tolerance boundaries are not. Parametrize via `resolve()`.
- **`argosy/services/decision_funnel/discovery_candidates.py:70–82`** — silently
  drops the grader's MEDIUM/LOW "BUY" picks (deep-reviews only HIGH). Vetoes the
  fleet's grader; make the conviction floor policy-owned.
- **`argosy/services/action_proposer_runner.py:283`** — `execution_state="proposed"`
  hardcoded, overriding any agent/FM execution intent. Already flagged in-code as
  codex BLOCKER #1.

**TIER 2 — hardcoded defaults that should derive from plan/IPS (parametrize):**
- Behavioral panic/FOMO thresholds (`behavioral.py:29–77`) — a 24h nudge, not a veto.
- Decision-funnel routing thresholds (`decision_funnel/policy.py:36–82`) — a triage
  policy (legit determinism, content-hashed) but thresholds should be IPS-tunable.
- Unallocated-cash overage 1.5× (`unallocated_cash_detector.py`), windfall
  $25k/$75k + 5% auto-classify (`windfall_detector.py`) — materiality defaults.

**NOT violations (agents over-flagged / confirmed clean):** concentration-cap
fallbacks 10%/13% (plan-first, fallback only when IPS pending), cashflow assumption
defaults (plan-first precedence), NVDA 12% target (owner-signed), min-ticket $500
(plumbing), FI sigma anchors (conservative floor), quality gates (validate only),
estate/tax gates (regulatory), anomaly tunables, selectable withdrawal policies.

## What already shipped this session (context)

The `deployment_funnel` package (Increments 1–3 deterministic cores + `/deploy-cash`
shadow→re-rank wiring + DeployCash UI verdict block) is **merged on master**, 40
tests green, codex-reviewed ~6×. Behind `ARGOSY_DEPLOYMENT_FUNNEL_ENABLED`
(default **True**) + `ARGOSY_DEPLOYMENT_FUNNEL_SHADOW` (default **False** = re-ranks).
**Caveat:** its concentration POLICY (`gates.py` step 4) is the invented layer this
program reworks — the interim math is codex-correct (fixed-book, instrument-weight
rule) but the LAYER is wrong. Commit `284b71d` flags this explicitly.

## The remediation program (both, order-independent)

### Fix A — deployment layer: execute plan + route judgment to the fleet
- Reduce `deployment_funnel` gates to the **legitimately deterministic** job:
  fill toward the plan's targets + reconcile against the plan's OWN cap/reserve.
- **Remove the invented concentration POLICY + the DCA market-timing policy** from
  deterministic code. Where a buy raises a genuine judgment call (deploy into US at
  high NVDA%? the FUSA/SCHD overlap? R1GR at 14%? pace or lump?), **route to the
  fleet** (risk officer / fund manager agents) fed the plan + live data — this is
  the deferred Increment 2 (bounded RiskOfficer actions + deterministic sizer).
- Discovery conviction floor → policy-owned, not a hardcoded HIGH-only veto.
- Keep: reconcile-to-plan, reserve-from-plan-cash-class, sizer constraints,
  estate gates, kill switches, trace.

### Fix B — `phase_expenses`: use real data + plan/agent-owned phases
- Read the user's **actual kids' birth years** (`identity_yaml.pensions_ariel.
  kid_birth_years`) to derive the kids_peak / empty_nest ages, instead of
  hardcoded 43–55 / 56–64.
- Move the phase multipliers/premiums out of code constants into **plan/agent
  authorship** (FI methodology component breakdown, or a lifecycle-expense agent /
  intake input) — or at minimum parametrize with sourced rationale, not
  `source_id="argosy_derived"` (which is an admission, not a source).
- This changes the retirement safe-age math — verify against `scenario_mc` /
  `retirement_plan` and the headline; codex-tandem the money math.

### Fix C (follow-on) — parametrize Tier 2 via `resolve()`/plan
Conflict ruin thresholds, decision-funnel routing thresholds, cash/windfall
materiality → plan/IPS-owned with fallbacks flagged as provisional.

## Repo / env state at handover

- **Branch `master`**, HEAD `284b71d`, clean tree. deployment_funnel on master.
- **Tests:** `tests/services/deployment_funnel/` 40 green; run with
  `.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/services/deployment_funnel/ -q`.
- **Servers:** started/stopped repeatedly this session (cleaned at turn end).
  Backend: `ARGOSY_EXPENSE_SAMPLES_ROOT=... uvicorn argosy.api.main:create_app
  --factory --port 8000`; UI: `npm --prefix ui run dev` (port 1337).
- **codex-tandem:** use for the money math (phase-expense recompute, any sizer).
  `codex exec --sandbox read-only < prompt` (NOT `--dangerously-bypass...`, which
  the harness classifier blocks).
- Memory written: `project_deploy_cash_concentration_blind_and_no_gold.md`.

## Open decisions for the owner
- Fix A "route to fleet": which agents adjudicate a deployment judgment call
  (RiskOfficer 3-perspective? FundManager? both)? — the Increment-2 contract.
- Fix B: should phase multipliers be a new intake question, a lifecycle agent, or
  FI-methodology-derived? (Affects how much is "ask the user" vs "fleet derives".)
