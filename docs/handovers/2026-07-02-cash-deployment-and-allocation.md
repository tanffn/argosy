# Handover — 2026-07-02 · cash deployment + the $170k allocation

**Branch:** `master` · **HEAD:** `b0e57c9` · tree clean · 24 commits this session
(`284b71d..HEAD`). Prior handover (`2026-07-01-fleet-override-remediation.md`) is
DONE; this supersedes it. Backend must be run in a persistent terminal (see below).

---

## 1. START HERE — how the cash-deployment system works (simple)

Ariel has ~**$170k free USD** and wants Argosy to tell him **what to do with it**.
The answer has TWO halves: **buy** (deploy cash) and **sell** (deconcentrate NVDA).
The buy half works; the sell half is the main gap (§3).

**The deploy surface** — `GET /api/portfolio/deploy-cash` → `DeployCashCard`
(on `/proposals` and `/inbox`):

1. **Propose** (`deployment_advisor.assemble_deployment_plan` → `cash_only_deploy`):
   fills toward the plan's target sleeves → a buy list (CSPX/FUSA/EXUS/EIMI/…).
2. **Reconcile + flag** (`deployment_funnel/`): the deterministic layer NEVER
   invents an investment judgment — it reconciles against the plan's own numbers
   and attaches FACTS (`CandidateFlag`): NVDA look-through vs the single-name cap,
   reserve-overfund, plan-gap. This is the "stop overriding the fleet" contract.
3. **Deploy the clean fills + REDIRECT overflow** (phase 1, deterministic, no LLM):
   - A fund whose NVDA density is ≤ the cap (CSPX 7%, FUSA 6%) is plan-compliant →
     **deploys** (buying it executes the plan; it's not a new judgment).
   - Cash the funnel won't place — an over-cap instrument (**R1GR** 14% NVDA) or
     **T-bills** when the reserve is already funded — is **redirected into the
     plan's own zero-NVDA diversifier ETFs (EXUS / EIMI / DPYA)**, split by target
     weight (`from_plan.redirect_overflow_to_diversifiers`). **Result: the full
     amount deploys into plan ETFs — no gold, no plan change, no held cash.**
     Verified: $100k → $99.5k deployed / 0 pending; $500k → $500k / 0 pending.
4. **Fleet review** (phase 2, opt-in `?fleet_review=true`): RiskOfficer×3 +
   FundManager adjudicate the flagged items and produce an **affirmative
   disposition** ("what to do with the full amount") — grounded in the plan's
   tickers. ONE agent call (serial — see §3.4). Fail-closed.
5. **FX**: refreshes on-demand when `live=true` (`fx.refresh_if_stale`) so USD/NIS
   is never stale/0.

**The plan (v62) already defines the NVDA sell path** — a `glide`:
`62.5% → 49.9% (Q1, Sep-26) → 37.3% → 24.6% → 12.0% (Q4, mid-2027)`, i.e. sell
~12.6pp/quarter (~$0.3–0.5M/quarter). **That glide is the real deconcentration
lever** — buying $100k of diversifiers only moves NVDA 57%→56.7%.

---

## 2. What shipped this session (the arc)

- **Fleet-override remediation (Fix A/B/C)** — deterministic code no longer invents
  investment judgment; real kids' birth years feed the retirement MC (safe age
  46→48); risk boundaries made IPS-owned.
- **Deploy funnel rebuilt** to the flag-only / fleet-decides / two-phase model,
  then hardened via Ariel's pushback: approve ≤cap ETFs, ground disposition in
  real tickers, **redirect overflow into diversifier ETFs** (the "no gold needed"
  fix), on-demand FX refresh.
- **Fixed the `claude.exe` exit-1 storm** — root cause was CONCURRENCY (we ran ~24
  agent subprocesses at once; the codebase runs them serially). Now serial +
  1-call phase-2.
- **SDD §20.5 reconciled** to the shipped architecture. USD/NIS 0→2.979 fixed.

---

## 3. THE GAPS (read before doing anything)

### 3.1 — MAIN GAP: the inbox never tells Ariel to SELL NVDA
We HAVE a holdings-checking fleet (`holistic_rebalance_review.py`, → inbox
`action_proposals`, quarterly + on-demand). It **ran, status ok, produced ZERO
legs.** Why: it only trims within an over-target **COARSE class** (equity / bonds
/ cash); equity-as-a-class is ~at target, so it **never inspects NVDA the single
name** (57% vs its 12% sleeve target). `holistic_rebalance_review.py:294`
("only trim within an over-target class"). **So the single biggest risk in the
book produces no inbox action.** This is the next session's main build (§4).

### 3.2 — Not glide-aware
Even when the rebalance fleet trims, it sizes to the **final target (12%)**, not
the **quarterly glide waypoint (49.9%)**. No glide/quarter references in it. A
correct NVDA sell should be **paced to the glide** (sell ~$X *this quarter*), which
also spreads the tax.

### 3.3 — Staleness / scheduler
- **FX refresh is not a scheduled job** (fixed on-demand for deploy; still no
  recurring job → will go stale for other consumers). Should register one.
- **Discovery / moonshot picks are ~19 days stale** (`trend_scan_state` last
  2026-06-12). A daily `DiscoveryFunnelLoop` IS registered — it just hasn't run
  because the backend/scheduler wasn't continuously up. Owner directive: **when
  the user requests an allocation, refresh stale inputs on demand ("wait while I
  work") — never compute on stale data.** Done for FX; discovery still needs it.

### 3.4 — Fleet phase-2 LLM reliability
`claude.exe` is intermittently flaky. The single-call disposition mostly completes
now (serial fixed the storm) but can still stall and need a retry. Fail-closed
holds the candidate rather than auto-approving.

### 3.5 — Backend won't stay up from an agent session
Background processes launched inside the agent session are killed on cleanup (that
caused the "stale UI"/"STALE DATA" confusion). **Run the backend in a real
terminal** so it + the scheduler persist:
```
ARGOSY_DEPLOYMENT_FLEET_REVIEW_ENABLED=true \
ARGOSY_EXPENSE_SAMPLES_ROOT="D:/Google Drive/Family/Finances/Portfolio/Resources" \
  .venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --port 8000
```

### 3.6 — Full test suite not run (~3.5h). Touched areas green (~104 deployment +
retirement/gate/fleet tests). Run before any PR.

---

## 4. NEXT SESSION — MAIN FOCUS: allocate the free $170k

$170k total ≈ **$73k reserve (funded)** + **~$98k deployable**. The BUY side works
(§1). Build the **SELL side** so the inbox gives the complete allocation:

**Build: single-name / glide-aware NVDA-sell action in the inbox.**
1. In the holdings-checking fleet (`holistic_rebalance_review` or a sibling), flag
   **single-name over-concentration** vs the plan (NVDA 57% ≫ 12% sleeve target /
   13% cap) **independent of coarse-class drift** — the current coarse-class gate
   (§3.1) is why it's silent.
2. **Pace the trim to the glide** (§1): "sell ~$X NVDA this quarter" toward the
   Q1 49.9% waypoint, not all-the-way-to-12% at once. Carry the existing
   taxable-event note.
3. Emit it as a first-class SELL `ActionProposal` → the inbox. Then the inbox =
   **"deploy your cash into these ETFs" + "sell ~$X NVDA this quarter per your
   plan"** — the buy and the sell, both plan-driven, both actionable.
4. While at it: register FX refresh as a scheduled job + make discovery refresh
   on-demand when an allocation is requested (§3.3).

Owner's framing (verbatim intent): *"the inbox is for this — it should tell me sell
X. Don't we have a fleet that checks holdings?"* We do; it has the coarse-class
blind spot above. Close that and the allocation loop is complete.

---

## 5. Key files
- Deploy: `argosy/services/deployment_advisor.py`, `argosy/services/deployment_funnel/`
  (`gates.py` flags, `from_plan.py` redirect, `fleet_review.py` + agent
  `argosy/agents/deployment_disposition.py`), route `argosy/api/routes/portfolio.py`
  (`get_deploy_cash`), UI `ui/src/components/proposals/DeployCashCard.tsx`.
- Sell/holdings fleet: `argosy/services/holistic_rebalance_review.py` (the §3.1
  blind spot).
- Plan glide: `doc.glide` on the TargetAllocationDoc (`target_allocation_doc.py`).
- FX: `argosy/services/fx/__init__.py` (`refresh_if_stale`), `market_snapshot.py`.
- SDD §20.5 describes the current deploy architecture.
