# Handover — 2026-07-03 · fleet-authors last mile (SHIPPED to master + live-proven)

**Branch:** merged to `master` · **HEAD:** `e2c2dbd` · tree clean.
Prime directive: the fleet-authors / determinism-verifies pivot
([[feedback_fleet_authors_determinism_verifies]]). Backend runs in a persistent
terminal (see §6).

---

## 1. START HERE — what's true now

The pivot's last mile is **built, merged, and PROVEN LIVE against a real `claude.exe`.**
The fleet AUTHORS the cash-deployment allocation; determinism VERIFIES it. It is now
`deployment_author_enabled` **default ON** (proven + reversible — additive on
`/deploy-cash`, degrades to the deterministic engine, flip off via the env var; an
autouse conftest guard keeps it off in tests so no suite-hang on live calls).

**NVDA-avoidance fix (2026-07-03, live before/after):** the author was declining EVERY
under-target US-equity sleeve (the four largest plan gaps) because the book is 58.5%
NVDA — a bad call: NVDA over-concentration is the SELL SCHEDULE's job, not a reason to
refuse US buys ([[feedback_deployment_must_be_exposure_aware]]). Fixed in the prompt
CONCENTRATION rule + the route `user_constraints`. After the fix the author fills all
eight sleeves weighted by gap (US core largest), first attempt, ~53s. UI now shows
`ticker · New/Add · value · reason` (authored buys carry `is_new`; per-buy justification
required + UI fallback).

**Live proof (real data, zero injected inputs):** given Argosy's own detected idle
cash ($170,980), the author produced an ACCEPTED, verified allocation in ~90–130s,
first attempt — spreading across four under-target sleeves (EXUS/EIMI/IBTA/DPYA)
weighted by gap, correctly declining the biggest raw gap (US core) because the book
is 58.5% NVDA, and explaining itself. That is the prompt-quality reasoning that beat
Argosy — now Argosy does it itself, gated.

**Components (all on master):**
- `argosy/agents/deployment_author.py` — the LLM author. `use_structured_output=False`
  (the `--json-schema` path makes the bundled claude.exe exit-1; prose-JSON works),
  `claude_code_max_retries=1`.
- `argosy/services/allocation_author/packet.py` — `build_decision_packet()`: holdings,
  deployable cash, plan menu **with per-sleeve current_pct + gap_to_target_pct**, NVDA
  look-through concentration, sourced instrument facts, policy signals.
- `argosy/services/allocation_author/reliable.py` — the P0 wrapper: hard ~150s timeout
  + psutil **process-tree kill** (own children only) + retry-on-fresh-process +
  circuit breaker + packet-hash cache + backend selection. **Confirmed live**: on a
  claude.exe exit-1 storm it hit the timeout → killed children → retried → degraded
  honestly, no hang.
- `verifier.py` / `flow.py` — the gate + author→verify→bounce (spine, unchanged core).
- `/deploy-cash` wiring (`api/routes/portfolio.py`) + `AuthoredAllocationDTO`
  (`contracts.py`): accepted → primary; else `degraded=True` with the deterministic
  `tiers` as the labelled fallback.

## 2. What changed this session AFTER the first cut (corrections — read these)

- **NO tax-reserve.** The initial design/test pre-reserved a "pending CGT" from
  deployment cash. Ariel: CGT is paid from the sale that realizes it, not pre-funded
  from unrelated idle cash. The whole tax-reserve machinery was REMOVED
  (`cash_reserved_for_tax`, `cgt_liability_usd`, the `tax_reserve` gate, the
  `pending_cgt_usd` param, the prompt language). Deployable is treated as net-of-tax.
  **Do not reintroduce a pre-reserve.**
- **Don't manipulate the test.** The first "live" run hand-fed a fake $100k CGT to
  force a scenario — that tested the script, not Argosy. Live tests must use ONLY real
  inputs (omit `cash_usd` → Argosy detects it).
- **Cash-detection bug fixed.** `tradeable_holdings` swept a blank-ticker real-estate
  row into cash ($69k), inflating detected cash to $239,980. Fixed → real $170,980.
- **Plan-fit from within, NOT a gate.** Adding a deterministic proportionality gate was
  rejected (over-engineering; re-introduces deterministic authorship). Instead the
  packet feeds the author Argosy's canonical current-vs-target gaps
  (`build_allocation_breakdown`) and the prompt says "fill the most under-target
  sleeves first." Codex-endorsed. **Keep plan-fit in the author, never a gate.**

## 3. NEXT — what's genuinely left

- ✅ **UI (shipped):** `DeployCashCard` renders `dto.authored`. Accepted → the
  authored allocation is the PRIMARY block (buys/sells/holds + cash split + rationale
  + "fleet-authored · verifier-approved" badge) and the deterministic tiers demote to
  a collapsed reference. Degraded (rejected/unavailable) → a loud "Degraded —
  deterministic fallback" banner (with gate failures) while the labelled `tiers` stay
  primary. `authored=null` (pivot off) → unchanged. Read-only for now (see below).
  Frontend types + 3 tests added.
- ✅ **Rationale robustness (shipped):** the verifier bounces a blank/whitespace
  `rationale` on any active disposition as REVISION_REQUIRED, so the author→verify→
  bounce loop re-authors until the move is explained — an ACCEPTED proposal can no
  longer ship without reasoning. Prompt got an explicit "ALWAYS fill rationale" rule.
- ⬜ **Inbox / period-directive integration (Ariel asked):** the authored allocation
  lives on `/deploy-cash` today; it should also surface as proactive **inbox actions**
  (the push-not-pull contract, §1.6). `argosy/services/period_directive.py` is the
  shared composition point the inbox + directive card + `/deploy-cash` all read — route
  the authored buys through it so they appear as inbox actions, not only on the deploy
  surface. Not started.
- ⬜ **Off-plan redeployment (needs Ariel's decision):** the author only deploys
  *fresh cash*. The book carries an "Individual Stocks (non-NVDA, to redeploy)" bucket
  at ~7.3% (target 0% — BRK/B, GOOG, AMZN, RKT…). Letting the author *propose trims*
  (emit `sells`; the verifier already credits sell proceeds to conservation) expands
  its authority to selling existing positions — **ask before enabling by default.**
- ⬜ **Action the authored buys:** the UI renders `dto.authored` read-only; the
  per-line Accept/Defer decision ledger still lives on the demoted deterministic tiers.
  Wiring Accept/Defer onto the authored buys (persisted allocation_actions) is a
  money-semantics step — do it after the off-plan decision, together.
- ⬜ **api_key backend:** no key in this env, so the money path runs on the flaky
  `claude_code` CLI (contained by the wrapper). Set `deployment_author_backend="api_key"`
  once a key exists → subprocess-free, no exit-1 storms.
- ⬜ **Full suite (~3.5h) not run** this session; touched clusters green (author +
  packet + reliable + verifier + flow + deploy-cash-author + UI). NOTE:
  `test_deploy_cash_route.py::test_deploy_cash_returns_tiered_plan` fails in this env
  **pre-existing** (unrelated to this work — confirmed by stashing): the engine
  returns only a `core` tier on a `deploy_funnel.quote_miss` (no live quotes / DB
  data), so the 4-tier assertion fails. Data-dependent, not a code regression.

## 4. Reviews

Two adversarial passes this session. (1) In-harness reviewer on the money path → found
+ fixed 2 blockers (over-deploy via negative reserve; evadable look-through). (2) Codex
(via `sandbox="workspace-write"` — `danger-full-access` is blocked by the auto-mode
classifier, see [[reference_codex_tandem]]) diagnosed the exit-1 (structured output)
and endorsed the plan-fit-from-within approach.

## 5. Verifier invariants (what determinism checks — keep it to FACTS, not authorship)
Non-negativity (BLOCK), invented ticker / fail-closed on empty known-symbols (BLOCK),
sell>holdings (BLOCK), unsanctioned US-situs (BLOCK, NVDA exempt), conservation
(deploy+reserve == deployable + sell proceeds), required `claimed_us_weight` +
look-through vs sourced facts (revision). NO tax reserve, NO plan-proportionality gate.

## 6. Run the backend (author path on)
```
ARGOSY_DEPLOYMENT_AUTHOR_ENABLED=true \
ARGOSY_EXPENSE_SAMPLES_ROOT="D:/Google Drive/Family/Finances/Portfolio/Resources" \
  .venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --port 8000
```
Honest live test (nothing injected): `GET /api/portfolio/deploy-cash?user_id=ariel`
(omit `cash_usd` so Argosy detects it). Takes ~90–150s (real claude.exe).
