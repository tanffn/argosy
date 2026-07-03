# Handover — 2026-07-03 · fleet-authors last mile (SHIPPED to master + live-proven)

**Branch:** merged to `master` · **HEAD:** `6c066d5` · tree clean.
Prime directive: the fleet-authors / determinism-verifies pivot
([[feedback_fleet_authors_determinism_verifies]]). Backend runs in a persistent
terminal (see §6).

---

## 1. START HERE — what's true now

The pivot's last mile is **built, merged, and PROVEN LIVE against a real `claude.exe`.**
The fleet AUTHORS the cash-deployment allocation; determinism VERIFIES it. It is
behind `deployment_author_enabled` (default **off**), fully additive on `/deploy-cash`.

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

- ⬜ **UI:** render `dto.authored` on the deploy surface (primary when accepted; a
  clear "degraded — deterministic fallback" banner + the labelled `tiers` otherwise).
  Still the biggest gap — the feature is API-only today.
- ⬜ **Off-plan redeployment:** the author only deploys *fresh cash*. The book carries
  an "Individual Stocks (non-NVDA, to redeploy)" bucket at ~7.3% (target 0% — BRK/B,
  GOOG, AMZN, RKT…). Consider letting the author *propose trims* of these toward the
  plan (it can emit `sells`; the verifier already credits sell proceeds to
  conservation). A real next step, ask Ariel before enabling sell-authoring by default.
- ⬜ **Rationale robustness:** `rationale` came back empty on one run (LLM variance).
  Consider making it non-optional / retry-if-blank so a money recommendation always
  carries its reasoning.
- ⬜ **api_key backend:** no key in this env, so the money path runs on the flaky
  `claude_code` CLI (contained by the wrapper). Set `deployment_author_backend="api_key"`
  once a key exists → subprocess-free, no exit-1 storms.
- ⬜ **Full suite (~3.5h) not run** this session; touched clusters green (author +
  packet + reliable + verifier + flow + engine + deploy-cash all pass).

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
