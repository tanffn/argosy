# Handover — 2026-07-05 · the LLM team is the architecture + the fleet-reliability blocker

**Branch:** `master` · **HEAD:** `2cba7cc` · tree clean. Everything below is merged + tested.
Read this first in a fresh session, then `git log --oneline -20`.

---

## 0. THE headline (read this first)

**Direction correction (Ariel, binding):** *"I don't want another gate. I want a team of LLMs. I keep asking for the same thing over and over — what am I missing?"* The recurring drift — a hallucinating agent → add a deterministic gate — is now reversed **in the two places a fresh agent reads**: memory `feedback_fleet_authors_determinism_verifies` (2026-07-04 correction) and `CLAUDE.md` binding prefs. **The rule: the LLM team IS the architecture. A JUDGMENT failure is caught by another agent re-deriving blind — never a per-symptom gate. Determinism = inviolable-arithmetic floor only (conservation, estate). Default reaction to a bad decision = "which agent should have caught this, and why didn't it — fix the team," NOT "add a gate."**

**The #1 open blocker (proven live this session):** the agent **fleet is unreliable** — members die on transient `claude.exe exit-1` with **no retry wrapper**, so the fleet can't convene. This is *the* thing standing between here and a working team-of-LLMs. See §3.

## 1. What shipped this session (all on `master`)
- **Deploy path fixes** (`f07bced`): USD/NIS resolves (inject FRED/yfinance into `BoiAdapter`); UCITS ETFs quote via `.L`/`.AS`/… suffixes in `deployment_funnel/from_plan.py`. +regression tests.
- **Permanent high-growth plan sleeve** (`0efed63`): new `high_growth_basket` sigma-class (σ 0.35, ρ 0.60→NVDA — **codex-verified**; the naive concentrated_equity/0.45/ρ1.0 over-reserved FI +6.7pp, the tier gives ~+3.5pp). Threaded through `build_target_allocation` + `build_target_allocation_doc`; pinned through the authored-override renorm. Domicile-agnostic (moonshot sleeve is NOT estate-gated — Ariel's call). **Draft v64 staged** (5% sleeve, 8 global fleet-sourced picks NU/MELI/RKLB/CRWD/RXRX/OKLO/IONQ/INVZ; clears the 13% cap at ~12.4%) — NOT promoted; awaiting Ariel review.
- **Per-stock research→verdict capability** (`90b834e`, `8294c20`, `cbb5065`, `015ea6f`): `StockDecisionAgent` (BUY/HOLD/SELL/TRIM; **HOLD is first-class & silent** — Ariel: "maybe there's no need for a sell, and that's ok"); live fetchers (finnhub news + yfinance price + plan thesis); `run_holdings_review` (triage→fetch→decide); daily `HoldingsReviewJob` (17:30 IDT); blind re-derivation gate on actionable verdicts; inbox sink (HOLD stays silent, audit-only).
- **Deploy-author fetch-before-buy** (`baa14cf`): the buy author now sees fresh news/price on individual-stock candidates.
- **Deploy DECISION TEAM** (`0ab8f11`, `aa3859d`, merged `2cba7cc`): author proposes → **blind reviewers (concentration/diversification/prudence) re-derive from raw facts and object by judgment** → flagged buys surface (not ship). Wired into `get_deploy_cash` as `TeamReviewDTO`. **PROVEN LIVE**: flagged **R1GR BLOCK** ("Russell 1000 Growth ~10%+ NVDA, not credibly ex-NVDA") + **CSPX WARN** — the exact catch an external LLM made, now Argosy's own team, no gate. `argosy/agents/deployment_reviewer.py` + `argosy/services/deploy_decision_team.py`.

## 2. Live findings worth keeping
- **R1GR trace (accountability):** R1GR-the-instrument is a *frozen plan constant* whose code rationale is HONEST ("NOT literally ex-NVDA… open item"). The false "not NVDA-heavy" was **fabricated by the deploy author**; the deterministic verifier ACCEPTed it because it checks US-weight but NOT NVDA look-through, and only checks that a rationale is *non-empty*, not *true*. There is **no FM in the deploy path** (author + verifier by design). Lesson → the team (not a gate) is the fix; now built (§1).
- **The v64 cap reality:** current book is ~58% NVDA look-through (transition); plan *target* clears at ~12.4-12.6% (our table) / ~12.97% (codex issuer pull — our look_through table is slightly stale). Moonshot picks add zero NVDA (cap-neutral).

## 3. NEXT — the top task is fleet reliability (blocking the team)
1. **Generalize the P0 reliability wrapper to the whole fleet.** `allocation_author/reliable.py` (retry-on-transient-`exit-1` on a fresh process + hard timeout + breaker) is **author-only**. Extract a shared runner and wrap: (a) the **consult per-ticker analysts** (`argosy/decisions/per_ticker_analysts.py` — currently NO retry at all), (b) the **deploy decision-team reviewers** (`deploy_decision_team.py` — currently fail-open only). **Proof it's needed:** an ACN `/consult` (`POST /api/decisions/run`, long_hold) was fired TWICE (decision_runs 122, 123) and **BLOCKED both times** — 0/4 then 1/4 analysts met the ≥2 quorum; fundamentals/news/macro died on transient `exit-1`, sentiment had no social data. Different analysts failed each run = transient. Fix this and the consult (and the team) actually convene. This is the highest-leverage item.
2. **Wire flagged team-review buys into the inbox** (right now `team_review` rides the deploy DTO but doesn't create inbox items).
3. **Draft v64** — Ariel to review/promote (or reject) the high-growth sleeve draft.
4. Smaller: durable persistence of the high-growth sleeve through a full re-synthesis; a Run-now UI for holdings-review; the R1GR sleeve — relabel honestly or swap to a lower-NVDA growth UCITS (Ariel: "the suggestion itself is valid").

## 4. Verify / run
- Decision + team tests: `pytest -m "not llm_eval" tests/test_stock_decision.py tests/test_stock_decision_service.py tests/test_holdings_review_job.py tests/test_deploy_decision_team.py tests/test_deploy_candidate_research.py` (all green).
- Allocation/sigma: `tests/test_allocation_plan.py` (+ the touched sigma modules) — green.
- Full suite (~3.5h) NOT run this session — advisable before the next merge.
- Consult (currently blocks on reliability): `POST /api/decisions/run` `{ticker, consult_mode:"long_hold", tier:"auto"}`.

## 5. Discipline that worked (keep it)
Blind adversarial review + firsthand verification (zigzag) — used codex to blind-verify the σ math and to challenge v63; **don't rubber-stamp** (I re-derived R1GR's NVDA weight to adjudicate the external critique). And the meta-lesson: **when a decision is bad, fix the team, not add a gate.**
