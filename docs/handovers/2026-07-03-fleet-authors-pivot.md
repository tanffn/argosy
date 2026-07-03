# Handover — 2026-07-03 · the fleet-authors pivot (+ proactive allocation shipped)

**Branch:** `master` · **HEAD:** `8dceae8` · tree clean · 21 commits this session
(`f26b89e..HEAD`). Supersedes `2026-07-02-cash-deployment-and-allocation.md`.
Backend must run in a persistent terminal (see §6).

---

## 1. START HERE — the one thing that matters

**A simple LLM prompt produced a BETTER $180k allocation than Argosy.** That triggered
the diagnosis + the pivot that is now the project's prime directive. Read the memory
`feedback_fleet_authors_determinism_verifies` first.

**Root cause:** Argosy's allocation is authored by DETERMINISTIC code (`cash_only_deploy`
= a water-fill over a coarse 3-field table) with the actual agent fleet bolted on as a
flaky, opt-in phase-2 that TIMES OUT. So the "team of agents" never actually decides.
The engine literally can't know "FWRA is ~62% US" (its table tags FWRA and EXUS both
`region="Global"`), so it credited an all-world US-heavy fund to the "ex-US" sleeve and
poured more US onto a 60%-NVDA book, and it deploys 100% ignoring the coming NVDA-sale
CGT bill. A mind beat a spreadsheet.

**The fix (codex-designed, in progress):** invert it — **the fleet AUTHORS the
allocation; determinism VERIFIES it** (never re-decides: "this violates the facts",
never "therefore buy X"). Fleet judgment at the core, deterministic rigor as guardrails.

**Where the pivot stands (the important part):**
- ✅ **Verifier spine SHIPPED + tested** (`argosy/services/allocation_author/`):
  `AllocationProposal` schema + source-bound `InstrumentFacts` (FWRA≈62% US, EXUS≈0%) +
  `verify_allocation_proposal` → ACCEPT / REVISION_REQUIRED / BLOCK. It already catches
  today's failure (FWRA-as-ex-US, missing CGT reserve, conservation, sell>holdings,
  invented ticker). Pure, no LLM. `tests/test_allocation_verifier.py`.
- ✅ **Orchestration SHIPPED + tested** (`allocation_author/flow.py`):
  `run_allocation_author` = author → verify → bounce-reasons-back (≤2) → accepted /
  rejected / unavailable; injectable, fully tested with no live LLM.
  `tests/test_allocation_author_flow.py`.
- ⬜ **THE LAST MILE (next session's main job) — the live author + reliability:**
  1. **`DeploymentAuthorAgent`** (LLM authors the `AllocationProposal`) — evolve the
     existing `argosy/agents/deployment_disposition.py` / `deployment_funnel/fleet_review.py`
     into a strict author with a **compact JSON schema**.
  2. **Decision-packet builder** — assemble the author's input: holdings, deployable
     cash, `TargetAllocationDoc`, reserve shortfall, **CGT/tax liability** (from the NVDA
     sell tranche), NVDA concentration, **instrument look-through facts**, domicile, and
     the policy signals (`nvda_policy_sell` etc.).
  3. **Reliability wrapper (P0 — this is why the fleet was unusable):** dedicated
     `deployment_author` role, **120–180s hard timeout** (not FM's 900s in
     `agents/base.py`), one call by default (NO RiskOfficer×3), retry on a FRESH
     `claude.exe` process, **kill the process tree on timeout** (not just await),
     cache by decision-packet hash, circuit-breaker, and honest
     `status=degraded_deterministic_fallback`. **Strongly consider the `api_key`
     backend** as a production fallback so the money path doesn't depend on the flaky
     bundled `claude.exe` CLI (see memory `reference_agents_run_via_claude_code_cli`).
  4. **Wire `/deploy-cash` behind a flag:** build packet → `run_allocation_author` →
     if accepted, render the proposal; if rejected/unavailable, fall back to
     `cash_only_deploy` **labelled degraded**. Acceptance test = the failure case: $180k,
     ~60% NVDA, FWRA known US-heavy, pending NVDA CGT → no FWRA-as-ex-US, no added US
     without justification, explicit CGT holdback, plan-safe diversifiers only.

**Keep/demote (codex):** KEEP `validate_instrument_domicile`, conservation math,
`deployment_funnel/gates`, `reserve`, and the sell policies **as verifiers/inputs**;
UPGRADE `look_through.py` into the sourced `InstrumentFacts` registry; DEMOTE
`cash_only_deploy`→degraded fallback, `exposure_attribution`→input, RETIRE the
diversifier redirect as an author (the agent redirects; determinism verifies).

---

## 2. What else shipped this session (before the pivot)

The proactive-allocation work — all live, tested, codex-reviewed. NOTE: some of this
(the deterministic exposure-aware engine) is exactly what the pivot DEMOTES to a
fallback; it's correct + useful as a guardrail/baseline, not as the author.

- **Proactive period directive** (mindset SDD §1.6): `GET /api/period-directive` +
  `period_directive.py` compose buy+sell+freshness into one object; "Your move this
  period" card on `/inbox`. Freshness gate fails closed. FX daily-refresh loop.
- **One canonical buy engine** (`deployment_funnel/canonical.py`) shared by inbox /
  directive / `/deploy-cash`.
- **Exposure-aware deployment** (SDD §20.5): credits held substitutes, tops them up,
  migrates US-situs, opens only uncovered exposure. *(This is the layer the pivot
  supersedes — it's still coarse-table-bound, which is why it mislabels FWRA.)*
- **NVDA sell exception protocol** (`nvda_policy_sell.py`) — all four categories live:
  policy (glide) · thesis-break · risk-budget (FI-floor, cap-independent) · catch-up
  (missed scheduled tranches). Max-sale selection, precedence thesis-break > risk-budget
  > catch-up > policy.
- **Decision funnel exposed as BETA** ("nothing hidden" doctrine, SDD §1.6/§20.8): it's
  now enabled by default (shadow=calibrating), surfaces beta view-first in the inbox,
  and `GET /api/decisions/funnel/calibration` + `FunnelBetaCard` show data-collected.

---

## 3. Critical context / gotchas

- **The running backend is STALE.** As of this writing `/api/health` reports
  `git_sha b0e57c9` (pre-session). RESTART it to serve any of this session's work (§6).
  The `:1337` UI dev server is current but talks to the stale backend.
- **The agent fleet (`claude.exe`) is FLAKY** — a live disposition call hung ~7 min and
  was killed. This is THE reliability problem the pivot's §1.4 must solve; it's why the
  fleet is currently an unusable opt-in. Do not treat it as a nuisance — it's P0.
- **The funnel now runs by default** (`decision_funnel_enabled`/`_stage3` default True in
  `config.py`) — beta/shadow calibrating, autoact off. It only actually runs when the
  backend+scheduler are up; watch its count on the `/inbox` beta card.
- **codex-tandem kit** (`tools/codex-tandem/`, gitignored) is the review partner used all
  session — `sandbox="danger-full-access"`, tight `node_dir`, prompt-to-file (no
  backticks). See memory `reference_codex_tandem`. Every money-path change was codex-
  reviewed to COMMIT AS-IS.
- **Full test suite (~3.5h) NOT run** this session; touched areas extensively green. Run
  before any PR.
- New memories this session: `feedback_fleet_authors_determinism_verifies` (THE one),
  `feedback_nothing_hidden_expose_as_beta`, `feedback_deployment_must_be_exposure_aware`,
  `feedback_proactive_agency_push_not_pull`.

---

## 4. NEXT SESSION — main focus

Build the pivot's last mile (§1, item ⬜) — the live `DeploymentAuthorAgent` + decision
packet + **reliability wrapper** + `/deploy-cash` wiring behind a flag. Acceptance test:
the $180k / 60%-NVDA / FWRA-known-US-heavy / pending-CGT case must produce the
prompt-quality allocation (true ex-US, no added US on a concentrated book, explicit CGT
reserve) — authored by the fleet, gated by the (already-built) verifier. Get the
`claude.exe` reliability solved first; the API-key backend is likely the right call.

---

## 5. Key files
- Pivot: `argosy/services/allocation_author/{proposal,instrument_facts,verifier,flow}.py`;
  tests `tests/test_allocation_verifier.py`, `tests/test_allocation_author_flow.py`.
- To evolve into the author: `argosy/agents/deployment_disposition.py`,
  `argosy/services/deployment_funnel/fleet_review.py`; agent base + timeouts
  `argosy/agents/base.py`.
- Data to upgrade: `argosy/services/deployment_funnel/look_through.py` → `InstrumentFacts`.
- Demote/verify: `argosy/services/allocation_engine.py` (`cash_only_deploy`),
  `deployment_funnel/canonical.py`, `exposure_attribution.py`, `deployment_advisor.py`.
- Sell protocol: `nvda_policy_sell.py`, `nvda_risk_budget.py`, `nvda_catchup.py`,
  `breach_router.py`. Directive: `period_directive.py`. Funnel: `services/decision_funnel/`,
  `funnel_view.py`, route `api/routes/decisions.py` (`/funnel/calibration`).

## 6. Run the backend (to make this session's work live)
```
ARGOSY_DEPLOYMENT_FLEET_REVIEW_ENABLED=true \
ARGOSY_EXPENSE_SAMPLES_ROOT="D:/Google Drive/Family/Finances/Portfolio/Resources" \
  .venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --port 8000
```
