# Argosy — living handover

**This is the ONLY handover file.** It is a living document: update it in place, don't add dated siblings. The 33 dated handovers that used to live here (2026-06-01 → 2026-08-12) were consolidated into this file on 2026-08-12 and deleted; they remain in git history at `87ca7f3` — `git show 87ca7f3:docs/handovers/<name>.md` to read one, `git log --diff-filter=D --name-only -- docs/handovers/` to list them.

Last updated: **2026-08-21**.

> **THE BLIND PAIR RAN FOR THE FIRST TIME (2026-08-21, master `f6a7fdc`).** `$1.41`, 9 min,
> `scripts/adjudicate_moonshot_sleeve.py`. It had NEVER executed: MoonshotSleeveAuthorAgent,
> its blind reviewer and `moonshot_divergences` had no callers outside tests. NOT just the
> moonshot pair -- `SleeveInstrumentAuthorAgent`, its reviewer and `sleeve_instrument_divergences`
> STILL have no callers. The adversarial-pair layer is largely written-but-unrun.
>
> **It overturned the morning's sleeve.** RXRX shipped that day as moonshot rank #1 ($3,500,
> "FLOORED: real drug-discovery revenue"). Author now: 9%, UNFLOORED, "NEGATIVE gross profit
> (-$393M)". Reviewer: excludes it. OKLO and RGTI: BOTH agents independently said UNFLOORED,
> both wrote "cushion, not a floor" -- Sol's exact distinction, reached without seeing Sol.
> 7 divergences, deliberately unreconciled. **Consensus pick: LEU (Centrus)**, both blind #1
> (28% vs 34%), and it survives independent checking: gross profit +$112M, operating cash flow
> +$51M, net cash $691M (19% of cap). Verified names the pair disagreed on: TEM has NEGATIVE
> tangible book (-$334M) and net cash (-$628M); CIFR has net cash -$4.755B (-73% of cap);
> UEC's NCAV is 1.8% of cap on NEGATIVE gross profit.
>
> **OPEN DEFECT IN MY OWN MANDATE WORDING.** Both agents use FLOORED to mean "profitable
> operating business", NOT "asset floor" -- correctly, since (c2) says "assets, net cash, OR
> real revenue". But at P/B 4.3-16.6 none of these names has liquidation protection, so
> "floor" does far less work than the SNDK story implies. The wording needs tightening, and
> the agents are not at fault for following it.
>
> **ALSO STILL OPEN:** `downside_math` is free text -- an agent can write "defensible P/S
> floor" and pass (Sol). It needs to be a stored number with burn-to-catalyst + source date.
> SDD 4011 still documents the old numerator-only formula. The deterministic allocator
> (`high_potential_sleeve.py`) still sizes on HIGH/MED/LOW 3/2/1 with no downside input.

> **⚠️ THE SCOUT / DISCOVERY WORK (2026-08-21) — master `30037d1`. Read before touching discovery.**
>
> **Question asked:** would Argosy have caught SanDisk (SNDK: $45.50 -> $1,600.62, +3,417%)?
> **Answer: no, and it failed at FOUR independent points.** All four fixed 2026-08-21.
>
> **READ THIS BEFORE CITING THE SNDK CASE.** The first version of this block, and the
> mandate text it shipped, claimed SNDK traded at "0.72x BOOK -- less than the accounting
> value of its net assets." **That is false** and an adversarial (codex/Sol) review caught it;
> the numbers were then verified against the filings. $4.999B of the $9.216B FY25 equity was
> GOODWILL, so tangible book was $4.217B and the stock traded at **1.58x TANGIBLE book, not
> below it**; NCAV was $1.317B against a $6.66B cap. It was also NOT "losing money at a
> cyclical trough": FY25 gross profit **+$2.212B**, operating income **+$0.507B**, operating
> cash flow **+$0.084B** -- the headline GAAP loss was a NONCASH goodwill impairment.
> What SNDK actually was: a depressed cyclical/spinoff **valuation** on real cash-generating
> operations plus a demand catalyst, at **P/S 0.91x**. That -- not liquidation protection --
> is the pattern. And it is ONE winner: cheap cyclicals that never inflect, dilute or delist
> are the base rate and are invisible in this case. `test_x10_sleeve_mandate` now asserts the
> false version CANNOT return.
>
> | link | was | now |
> |---|---|---|
> | radar could SEE it | day-gainer screens only | `MOMENTUM_SUSTAINED` 12-1 family |
> | radar could ADMIT it | $30B cap excluded the theme | thematic lane, backtest 0/4 -> 4/4 |
> | funnel could SURFACE it | shadow mode + 3 broken jobs | live; surfaced proposal #33 GRAL |
> | allocator could BUY it | estate rule blocked all US-situs | bounded, disclosed carve-out |
>
> **Backtest exists and is repeatable: `scripts/backtest_radar.py`.** Look-ahead-free by
> construction — as-of features only, cap reconstructed as shares x price_T, tickers ANONYMIZED
> before any judgement, winners scored against a 14-name control. Imports the real band constants
> from `trend_radar` so it cannot drift from the code it tests.
> Honest reading: the thematic lane is COVERAGE (it admits 14/14 controls too); the
> DISCRIMINATION comes from `MOMENTUM_SUSTAINED` (fires 4/4 winners, 4/5 of top-5).
>
> **THREE JOBS WERE SILENTLY BROKEN, none diagnosable from its own error message:**
> - `decision_funnel` "database is locked" — the funnel COMPUTED fine every run and threw the
>   result away; a bare `sa.create_engine` (busy_timeout=0) in `state_observer.py`. The same
>   builder was also breaking `state_observer_daily`. ~15 other loop files STILL use this
>   pattern — highest-value cleanup left in the repo.
> - `signal_streams_daily` "getaddrinfo failed" — DNS was a red herring. The gov_contracts cursor
>   had SELF-DEADLOCKED since 2026-07-11: a stalled cursor widens the catch-up window, which
>   re-trips the page cap, which stalls it further. Six weeks, no escape.
> - The insider stream (1,978 lines of SEC Form 4) had produced ONE nomination ever because it
>   was `enabled: False` AND `ARGOSY_SEC_CONTACT_EMAIL` was unset. Verified working: a live 2-day
>   EDGAR run parsed 4,153 filings -> 9 cluster nominations.
>
> **CONVICTION MODEL REWRITTEN.** Argosy graded 31 of 38 positions HOLD/LOW — no view on 82% of
> the book, incl. NVDA at 57.9% vs its own 13% cap. Cause: `_aggregate_conviction` AVERAGED
> analyst confidence, so one uncertain analyst dragged deterministic decisions down.
> Conviction now = confidence in the ACTION, computed as a MINIMUM over NECESSARY inputs.
> `decision_basis` in {CONSTRAINT, FORECAST, MIXED}; a missing fair value caps a VALUATION call
> at LOW but cannot touch a domicile or policy-cap decision. `SELL` now means target weight zero
> (the old "large reduction => SELL" heuristic is why NVDA read SELL for a trim-to-cap).
> Plan layer {HIGH:1, LOW:31, MED:6} -> {HIGH:25, LOW:13}. Stored stances -> {HIGH:23, LOW:13, MED:2}.
> **Stale reviews no longer bury constraints:** a `holding_reviews` row from ANY date could
> previously overturn a fresh plan verdict (no staleness check existed). GOOG was the proof — a
> stale HOLD was burying a live US_SITUS+DUPLICATE+TARGET_ZERO violation.
>
> **RISK THIS INTRODUCES:** the system now ASSERTS confidence on 23 positions where it used to
> abstain. Confidently wrong is worse than silent. The falsifier / revisit-trigger requirement is
> the intended backstop and has NOT been exercised yet. The conviction design came from
> codex-tandem, so codex cannot serve as its independent review — it needs a genuinely
> independent adversarial pass before its verdicts are trusted at scale.
>
> **LIVE $100k DEPLOYMENT (Argosy's own, gate ACCEPT, 2 attempts):** CSPX 20k / EXUS 14k /
> IWQU 12k / IB01 14k / IBTA 10k / FUSA 8k / EIMI 6k / SPMV 5k / DPYA 3k / RXRX 3k / ACHR 2.5k /
> RGTI 2.5k. $8,000 US-situs, disclosed, inside the derived ~$132,800 sleeve cap.
> It does NOT touch the 57.9% NVDA concentration — only selling does.


> **⚠️ CLOSURE STATE 2026-08-18 — plan 116 is the live draft, 12 RED, 5 are ARIEL BLOCKERS.**
> RED trend across reviews: 18 (p109) → 19 (p111) → 14 (p112) → 13 (p115) → **12 (p116)**.
> Plans 113-116 were HAND-EDITED (no LLM): 21 verified substitutions, scripts in scratchpad
> `manual_edit_11*.py`. Each asserts its target string exists before substituting, so a silent
> no-op is impossible. DB backed up to scratchpad before the first edit.
>
> **THE FIVE BLOCKERS — none fixable in code:**
> 1. **How many NVDA sold YTD 2026?** Ledger says both `1,560` and `3,380`; a third surface says
>    `1,440 remaining` and a fourth `0 remaining`. Needs the Schwab transactions CSV.
> 2. **How many to sell in 2026?** Verdict `3,924`, resolver quota `7,304`, schedule total
>    `9,417`, canonical sell count `9,480`. Needs a ruling on which is authoritative.
> 3. **Sell the 2026 tranche or defer?** 2026's surtax thresholds are spent, so that leg prices
>    ~28-30% vs possibly 25% in 2027. Real trade: ~2-5pp of tax against a year less concentration.
> 4. **Is NVDA managed?** `DEFAULT_UNMANAGED_SYMBOLS = {"NVDA"}` and the snapshot says
>    `managed: False`, so Argosy will NEVER emit an NVDA order — while the plan's largest action
>    is an NVDA sale. Today that means manual execution at Schwab.
> 5. **Estate dates + a missing liability.** Proposed dates are in the plan marked PROPOSED
>    (counsel 2026-09-15; will+guardianship 2026-10-31; POA/directive/trust 2026-12-31).
>    Separately **€147,452** of documented foreign property obligations is absent from the
>    finite-liability reserve — larger than the entire published sufficiency margin.
>
> **STOP HAND-EDITING THE TAX SECTION.** Three passes, three partially-wrong results. The surtax
> interaction is genuinely subtle: TWO layers with different tests (3% on TOTAL income — already
> breached at ₪868,285 vs ₪721,560; 2% on CAPITAL income alone — ₪549,467, headroom ₪172,093),
> and TWO distinct comparisons that must not be conflated (splitting beats a single 2026 sale;
> deferring the 2026 leg further is cheaper still). The 2026 leg realizes ≈₪172,830 of gain
> against ₪172,093 of headroom — it sits ON the boundary. This needs a derivation against real
> per-lot gains, not another prose patch.
>
> **NEW SUBSTANTIVE FINDINGS worth acting on (not blockers, not noise):**
> - The glide never nets against the VEST STREAM: holding every fresh vest adds **4,107 shares
>   through March 2030**, so concentration does not fall as modelled.
> - The trajectory holds RSU savings flat to 2031 while assumption A8 scopes it to 2026-2029.
> - At full plan completion ~$1.21M of US-situs NVDA remains — ~20x the $60,000 NRA exemption.


> **⚠️ PLAN 111 IS THE LIVE DRAFT (2026-08-18). 18/18 RED findings addressed. UNREVIEWED.**
> Runs 404 + 405 (section-frozen amendments off 109 → 110 → 111). v92 still `current`.
>
> | metric | 92 | 107 (worst) | **111** |
> |---|---|---|---|
> | chars | 56,123 | 44,629 | **51,220** |
> | section slugs | 18 | 15 | **18** |
> | numeric facts | 33 | 14 | **22** |
> | qualitative placeholders | 1 | 2 | **0** |
> | `[derivation pending]` | — | 19 | **0** |
>
> Erosion reversed. `tax-year allowance` 0, `SGOV` 0, raw `46` 0. Retirement pair published
> as tokens per Ariel's ruling. Surtax threshold cited 4×.
>
> **TRAP THAT COST A WHOLE AMENDMENT RUN:** the `freeze_except` allow-list is now LOAD-BEARING.
> Run 404's list was built from where the FIGURES lived, so the freeze correctly protected
> `short/actions` + `long/action_items` — the exact sections holding the SGOV instruction and the
> fictitious tax-year-allowance rationale. Result: plan 110 CONTRADICTED ITSELF (tax_plan said the
> allowance does not exist; actions acted on it). No error, no warning — a silent no-op on the
> sections you cared about. **Always derive the allow-list by grepping the defect strings out of
> the baseline plan, never from memory.** Run 405 fixed it with a measured list.
>
> **RESIDUAL — 8 drift violations on plan 111, one of them a REAL CONFLICT:**
> The dated glide figures (3,924 / 5,493 / 1,523 / 9,417) are written as DIGITS because no
> resolver key owns a per-year tranche — so they will drift on the next regeneration, the same
> disease this session cured everywhere else. Fix = resolver keys for the 2026/2027 tranches,
> then bind them.
> **AND:** `concentration.nvda_quota_tax_year_sh` resolves to **7,304** while the adjudicated
> glide verdict (`action_proposals #66`) says the 2026 tranche is **3,924**. Two sources disagree
> on how many NVDA shares to sell in 2026. Nothing could detect this before today (the quota key
> had no AnchorSpec). NOT resolved — needs derivation before any 2026 sale order.
>
> **NEXT:** review plan 111. `plan_critique` is runnable again (`claude-opus-5`), and
> `/api/plan/draft` now reports `critique_is_for_this_version` honestly — 111 will correctly
> report FALSE until something actually reviews it.


> **DERIVED FIGURES FOR THE CLOSING AMENDMENT (verified 2026-08-18 on the live DB — cite these,
> do NOT let a synthesizer re-derive them).**
>
> **RED-4 SWR band** — `compute_fi_target`, baseline ₪277,008 / permanent spend ₪311,584 /
> reserve ₪1,450,000. Liquid NW ₪12,218,310 (BOI FX 2.954):
> | SWR | total target | margin |
> |---|---|---|
> | 2.4% | ₪14,432,667 | **−₪2,214,357** |
> | 3.0% (selected) | ₪11,836,133 | **+₪382,177** |
> | 3.5% | ₪10,352,400 | **+₪1,865,910** |
> Wording: "Gross sufficiency is reached at the selected 3.0% assumption, but NOT at the
> conservative 2.4% sensitivity." Note the live 3.0% margin (₪382,177) equals the plan's
> DESCRIPTION figure, not its stale ₪401,585 headline — independent corroboration of RED-3.
>
> **RED-10 tax rationale is FICTITIOUS** (settled against domain_knowledge, full derivation in
> scratchpad `red10_derivation.md`): Israel has **no annual CGT allowance**
> (`capital_gains.md`) and **no §102 tax-year deadline** — eligibility is 24 months from GRANT
> date, per-lot (`section_102.md`, Amendment 147). The only real year-end effect is **surtax
> bunching**: 3% + 2% above **₪721,560/yr**, ~30% effective vs 25% (`surtax.md`). So the plan's
> "sell the remaining 2026 quota" deadline rationale would RAISE the effective rate on a ~₪1.95M
> realization. The NUMBER is right — the adjudicated verdict already splits 3,924 (2026) +
> 5,493 (2027), which is the surtax-optimal shape. Strike the rationale, keep the schedule.
>
> **RED-14 "100% USD" is false, but barely** — 99.57% as tagged, **98.60%** corrected; per-0.10
> FX sensitivity ₪407,821 (not ₪411,827); break-even FX to reach the 3.0% target = **2.860**.
> Root cause is a CODE bug, not prose: `ת"א-200` (TASE-listed, NIS-priced) is tagged USD because
> `leumi_xls.py` derives currency from a whole-file $ vs ₪ header while
> `instrument_reference.py` independently calls it `REGION_ISRAEL`. Fix in flight.
>
> **RED-17 Keren Hishtalmut ₪384,000 IS liquid** — `keren_hishtalmut.md`: tax-free withdrawal of
> principal and gains after a 6-year per-plan vesting clock; the household ledger already records
> `keren_hishtalmut_accessible: likely_now` on 6+ years NVIDIA tenure. Confidence MEDIUM (no
> explicit plan-open date recorded). Reconciles exactly: Ariel ₪2,015,054 + Noga ₪75,000 =
> **₪2,090,054**; less the liquid ₪384,000 leaves **₪1,631,054** genuinely age-locked
> (executive insurance + kupat_gemel age 60, kupat_pensia age 67). Add ₪384,000 to the liquid FI
> basis, carry ₪1,631,054 as a separate age-60/67 tranche, open an intake gap for Noga's
> hishtalmut (her value is `?`).


> **⚠️ STATE AS OF 2026-08-18 — master `42bd75a`, pushed through `61b3c1b`; later commits local.**
> Plan **109** is the live draft (v92 still `current`, unpromoted, 18 RED open at review time).
>
> **8 of the 18 RED findings are CLOSED in code.** Sol triaged all 18: 9 CODE / 5 DERIVATION /
> 2 MANDATE / 2 DATA and **zero Ariel forks** — the two that looked like escalations (the
> no-principal-drawdown mandate, the SGOV domicile conflict) are constraints Ariel ALREADY set,
> so applying them is compliance, not a choice. Full triage: scratchpad `sol_red18.md`.
>
> | RED | fix | commit |
> |---|---|---|
> | 1 spend table didn't sum | computed residual row; rows reconcile to the printed total | `19f957c` |
> | 2 net worth mislabelled | refuses to publish under a label it can't satisfy (subtract-without-add) | `0efeb63` |
> | 3 bullet stated two margins | anchor widened to "margin of safety" | `9b80d93` |
> | 6 dashboard NVDA target 11% | reads canonical doc, not prose labels | `0efeb63` |
> | 7 horizon targets lagged canonical | projected from TargetAllocationDoc (4 drifts, not 2) | `3f289f5` |
> | 9 glide exceeded eligible pool | dated §102 seasoning: 9,588 by-horizon ≥ 9,480 sold | `42bd75a` |
> | 5 after-tax margin placeholder | keys already resolved; verified only | — |
> | 15 amendment facts pending | follows donor run 379; pending 6 → 1 | `61b3c1b` |
>
> **FIVE of these were ONE disease: a surface trusting authored prose over the canonical
> structured document.** RED-6 and RED-7 are literally the same bug on two surfaces, and RED-6
> had already been patched once with another text heuristic that then broke on a new label
> ("Global quality growth (screened to avoid NVDA-heavy names)" contains NVDA as a whole token).
> Fix pattern: read the structured doc, match on a stable identifier, NEVER fuzzy prose — and log
> loudly when you cannot match, so non-coverage is visible.
>
> **SUBSTANTIVE FINDING, unresolved:** canonical `retirement.fi_age` = **49**, while the plan's
> headline says **46**. Corroborated from three directions (RED-12's bridge implies 47, RED-16's
> mandate conflict, and the now-resolving resolver key). This matters more than every formatting
> fix combined and was invisible while those keys sat pending.
>
> **STILL OPEN (10):** #10 tax-year-allowance rationale (Sol says Israeli CGT has no such
> allowance — verify against `domain_knowledge/tax/israel/**` before acting), #12/#13/#16
> retirement math + drawdown mandate, #14 FX "100% USD" false, #17 FI basis omits ₪384k Keren
> Hishtalmut, #11 sale-ledger contradiction, #8 SGOV violates the domicile mandate, #18 estate
> documents undated, #4 SWR band (publish 2.4/3.0/3.5 and label non-robust).
> **#8, #11, #14, #17, #18 are PLAN CONTENT, not code** — they close with a section-frozen
> amendment, not a patch.
>
> **ENDGAME:** finish the code fixes → ONE section-frozen amendment repairing prose against the
> now-correct resolver (RED-9's flat "sell N from eligible lots" sentence must become the dated
> 2026/2027 schedule; the adjudicated verdict `action_proposals #66` already says 3,924 + 5,493
> retaining 1,523) → re-review.
>
> **TRAPS FOUND THE HARD WAY THIS SESSION:**
> - `derived_from_id` is `5` (the May intake upload) on EVERY amendment — plan ancestry is a flat
>   fan, so no lineage walk works until that is fixed at the source in `_medium_worker`.
> - `plan_critique` is pinned to `claude-fable-5`, which this account CANNOT access; the
>   `models:` block in `agent_settings.yaml` is DEAD CONFIG (`model_for_role()` has zero callers).
>   The `/plan` "Re-critique now" button is therefore broken. The API route is fine otherwise —
>   only the CLI reads the empty `raw_markdown`.
> - Don't sort `decision_id` by casting to int: `'plan-synth-99'` casts to 0. That error made me
>   report a donor as two months stale when it was three days old.


> **⚠️ PLAN 109 WAS REVIEWED (2026-08-18) — 18 RED. Do NOT promote it.**
> First review of any plan since run 397. Plan 109 is the best-BUILT draft
> (no sections lost, 25 numeric facts, 27 `{{fact:}}` tokens, erosion reversed)
> but its SUBSTANCE has serious problems that stayed invisible while nothing
> reviewed a draft for six runs. 32 findings: 18 RED / 8 YELLOW / 6 GREEN,
> confidence MEDIUM, $2.17, against the real 91,588-char assembled artifact.
>
> Reviewer's summary: *"The plan's arithmetic backbone does not hold."*
>
> 1. **Spend Basis Arithmetic** — The category table anchoring the entire FI spend basis sums to ₪261,588, not the ₪277,008 total it asserts — a ₪15,420/yr unexplained residual.
> 2. **Net Worth Reconciliation** — Three mutually inconsistent net-worth figures, and the figure that INCLUDES real estate is smaller than the one that EXCLUDES it.
> 3. **Internal Contradiction** — A single target bullet states two different margin figures — ₪401,585 in the headline and ₪382,177 in its own description.
> 4. **Capital Sufficiency** — The 'capital sufficiency reached' verdict holds only at the midpoint of the plan's own declared SWR band; at the band's low end the plan is ₪2.19M short.
> 5. **Tax Treatment** — The single claim that flips the plan's headline verdict from 'sufficient' to 'not sufficient' carries no figure and is admitted by the plan to be a placeholder.
> 6. **Allocation Target Conflict** — The dashboard steers NVDA to 11.0% while the plan's self-declared governing table steers it to 8.0% — and 11.0% is the weight of a different sleeve entirely.
> 7. **Allocation Drift** — Two authored sleeve targets in the medium-horizon Targets list contradict the governing allocation table by 1.6pp and 0.8pp.
> 8. **Estate Exposure** — The mandated SGOV parking floor adds US-situs estate exposure, directly contradicting the plan's own 'hard domicile mandate' to contain the US-situs tail.
> 9. **Tax Treatment** — The wind-down requires selling 9,479 shares 'only from Section-102 capital-track-eligible lots', but only 9,230 shares are eligible — a 249-share shortfall the plan never acknowledges.
> 10. **Tax Treatment** — There is no Israeli CGT 'tax-year allowance' and no Section-102 tax-year deadline; the only real year-end effect is surtax bunching, which argues for the opposite of what the plan directs.
> 11. **Data Integrity** — The plan orders 3,924 more shares sold in 2026 while the supplied user_context sale ledger contradicts itself and both of its readings contradict the plan.
> 12. **Retirement Math** — The bridge is exactly 13 years of spend, implying retirement at 47 — one year later than the plan's own headline age of 46, leaving the bridge ₪311,584 short.
> 13. **Retirement Math** — 'Unreachable' is arithmetically false on the plan's own inputs — at 0% real return the coverage test is met at roughly age 48.
> 14. **FX** — The claim that the book is 100% USD is false on the plan's own evidence and overstates FX fragility.
> 15. **Model Completeness** — Every forward-looking figure the appendix promises to own is '[derivation pending]' — including two inputs the plan states confidently elsewhere.
> 16. **Mandate Conflict** — The headline retirement age is produced by a principal-drawdown model the user has explicitly ruled out.
> 17. **Asset Coverage** — The FI sufficiency basis omits ₪384,000 of fully liquid, zero-tax Keren Hishtalmut and ₪2.09M of Israeli retirement capital.
> 18. **Estate Exposure** — A ~₪3.79M quantified estate tail plus two minor children and tri-jurisdictional assets sits behind an action with no target date and no evidence of progress.
>
> **THREE INDEPENDENT DEFECTS BROKE THE REVIEW PATH — each hid the next:**
> 1. `plan_critique` is pinned to `claude-fable-5` (`agents/base.py:251`), which
>    this account CANNOT ACCESS. Tested against the bundled CLI: `claude-opus-4-8`,
>    `claude-opus-5`, `claude-sonnet-5` all work; only `claude-fable-5` fails. It
>    fails LOUDLY (by design), so the agent simply never ran.
> 2. The documented fix knob is DEAD CONFIG. `configs/ariel/agent_settings.yaml`
>    already sets `plan_critique: sonnet` and it has no effect —
>    `agent_settings.model_for_role()` has **zero callers**. The `models:` block
>    looks like a control surface and is wired to nothing; `base.py`'s override
>    loader reads only `thinking_effort` / `thinking_budget` / `citations_enabled`.
> 3. `argosy critique` reads `PlanVersion.raw_markdown`, which is **0 bytes** for
>    every amendment-produced plan (104-109) — the body lives in `horizon_*_md`.
>    Even with a working model it would have reviewed an EMPTY document.
>
> This review was obtained by routing around (1) and (3): explicit
> `model="claude-opus-5"` plus `assemble_plan_artifact()` (which resolves to the
> pending draft = 109 and builds the real markdown). The owner-approved model
> default was NOT changed.
>
> **RENDER DEFECT (user-visible):** `render_plan_facts` passes
> `decision_run_id=None`, but `_apply_canonical_allocation` — the only thing that
> sets the canonical 13% cap — is guarded by `if decision_run_id is not None`. So
> `concentration.nvda_cap_pct` is ALWAYS pending at render time and any plan
> binding it prints `[derivation pending]`. Plan 109 binds it in 8 places. The
> plan version carries its own `target_allocation_json` (24,727 bytes,
> `nvda_cap_pct: 13.0`), so the fix is to resolve with the plan's OWN
> decision_run_id.


> **All 2026-08-14/15 work is MERGED, PUSHED and DEPLOYED** — master `86a50df`, backend live on it, migrations 0102+0103 applied. No branch in flight.

> **⚠️ WHY THE PLAN WOULD NOT CLOSE (2026-08-16) — read this before running anything.**
> Seven drafts were rejected in a row while quality measurably improved. Two mechanisms, both now fixed:
>
> 1. **Figures written as digits are RE-SAMPLED on every regeneration.** The NVDA glide sell count went 9,479 → 9,417 during an amendment told to change one unrelated sentence; an invented ₪209,389 margin appeared; the cap read 13/12/7/13. Meanwhile five figures bound to `{{fact:}}` tokens were byte-identical across four consecutive amendments. **Rendering works; writing does not.**
> 2. **Verification kept checking a proxy of the real path.** The ETF verdict path shipped with 31 green tests that all patched the LLM seam and failed on its first live call. A gate receipt was reported working twice with no live run having written a row. A tokenizer's headline numbers came from a hand-patched path.
>
> **AMEND, NEVER FULL-REGENERATE.** Use `plan_amendment.workers._medium_worker` (Phase 3 only, ~8 min) targeting named sections; the full 15-phase run (~80 min, ~$20) is for genuine strategy changes only. Measured: amendments took Sol blockers 4 → 3 → 1; full regens re-rolled everything and stayed flat. See memory `feedback_amend_never_regenerate`.
>
> **⚠️ "by freezing untouched sections" was WRONG — corrected 2026-08-17.** No freezing mechanism existed. `prior_items_index` preserves *item_ids*, not prose (its own docstring says so); `_medium_worker` re-authored every horizon body on every run. Measured 106 → 107 with guidance naming exactly two sections: `medium` and `short` had **0 sections identical**, long shrank 38,852 → 35,043 chars, and slugs `cover_assumptions` / `fi_bridge` / `monte_carlo` were lost outright. Worse, the erosion is **cumulative**: plan 92 = 56,123 chars / 18 sections, plan 106 = 50,107 / 16, plan 107 = 44,629 / 15 — roughly 20% of the plan destroyed across the amendment chain, which is itself a reason drafts kept failing review (reviewers read a progressively thinner document). Amendments were CHEAPER (Phase 3 only), never NARROWER.
>
> **Now fixed:** `plan_amendment/section_freeze.py::merge_frozen_sections` merges by SLUG (headings get renamed; slugs don't), restoring any section the model dropped and discarding unrequested new ones. Wired into `_medium_worker` as `freeze_except={slugs}` + `freeze_baseline_plan_id`, applied BEFORE the fact-tokenizer pass so a frozen section citing a changed figure still surfaces drift. Default `None` preserves the old behaviour — you must opt in. Demonstrated on real data (106 vs 107, `allow={'concentration'}`): all three lost sections restored, 30/31 sections byte-identical, nothing dropped.
>
> **Trap:** `prior_current` is the `role='current'` row = **plan 92 (2026-07-13)**, NOT the live draft. Freezing against it would revert everything drafts 93→106 added (92 has no `fi_bridge`). Always pass an explicit `freeze_baseline_plan_id`.
>
> **The 12-vs-13 cap was never drift.** `resolve_plan_numbers()` returns 13% with `include_canonical_ages=True` (doc-anchored settled cap, what every `{{fact:}}` renderer uses) and 12% without (the analyst's MIN-over-constraints floor). Two callers, two canonical answers, decided by a keyword argument.
>
> **Landed `8166286`:** fact tokenizer (drift surfaced, never silently corrected) · generation-time numeric guard (the synthesizer cannot type a keyed digit) · `@pytest.mark.real_seam` + `scripts/check_real_seam.py` + `scripts/smoke_real_paths.py` · six finite reviewer criteria with a deterministic backstop (`_enforce_finite_criteria`) so blocking-only-on-the-list is code, not prompt.
>
> **Open:** 198 of 661 risk modules have zero real-seam coverage (`--all`). Plan 106 is the live draft; v92 still `current`.

> **⚠️ THE ABOVE "Open" ITEM WAS WRONG — corrected 2026-08-17 (uncommitted in the working tree).**
> It read: *"`fact_tokenizer` `exclude_any` fires on ANY 'eligible'/'quota' in the clause window … gate the exclusion on proximity to a DIFFERENT digit group."* Both halves were wrong, and each was only found by executing the real path.
>
> 1. **That gate was not the blocker.** On ANY amendment run (Phase 3 only ⇒ no concentration `agent_reports` row), `concentration.nvda_cap_pct` is still `pending` when `_apply_nvda_deconcentration` consumes it, so `nvda_sell_sh` / `nvda_target_sh` / `nvda_eligible_now_sh` all resolve `pending` — the tokenizer's anchors for those concepts **were never activated at all**. The exclusion never ran. That is why 9,417 drifts.
> 2. **The cap is a FALSE dependency.** Executed: `derive_nvda_deconcentration` returns target 1,461 / sell 9,479 identically at cap 0.07 / 0.12 / 0.13 / 0.99 — cap affects only `nvda_cap_breach_x`, which `_apply_nvda_deconcentration` discards. Fix = drop the cap from the gate, not hydrate the cap earlier.
> 3. **Do NOT "fix" this by calling `_apply_canonical_allocation` sooner.** It is NOT idempotent — executed twice from an analyst cap of 12% it yields `cap=13%, floor=12%` then `cap=13%, floor=13%`, mistaking its own canonical assignment for the analyst floor. And in `plan_amendment/workers.py` tokenization runs at ~L245, BEFORE `resolve_target_allocation_json` (~L284) and the `PlanVersion` insert (~L289), so any fix that reads the persisted allocation doc finds nothing at tokenize time. (Both points came from the codex-tandem reviewer and were re-executed independently.)
> 4. **The prescribed exclusion rule was itself wrong.** Real prose reads *"3,924 sh … quota remaining"* — the exclude phrase modifies **its own** number, so requiring a *different* nearby number stopped the exclusion firing and added two FALSE drift flags. Correct discriminator: the exclude term must be attached to **any** number, within `_EXCLUDE_PROXIMITY = 15` chars. Measured stable across 5..30; **40 was the one value that breaks the case it was added for.**
> 5. **`graph_hydration` enforced the false cap edge**, not merely documented it: `MANIFEST_EDGES` + `KNOWN_RECIPE_ARGMAP` declared `nvda_cap_pct` upstream of target/sell, which would falsely invalidate them on a cap-only change. Removed.
>
> **Real-path proof** (`decision_run 400`, no monkeypatching): sell `pending→9,479`, target `pending→1,461`, eligible `pending→9,230`, `tax.nvda_embedded_cgt_glide_nis` `pending→₪1,949,354`, `fi_margin_net_of_realization_glide_nis` `pending→−₪1,547,769`. Plan 106 end-to-end: 16 substitutions, 9 drift violations surfaced (incl. `9,417` vs 9,479 and `1,508` vs 1,461), idempotent. Note `₪209,389` vs `₪401,585` was ALREADY being flagged before this fix — it comes from `fi_margin_signed_nis`, which was already resolved.
>
> **Residual, NOT regressions** (present in every rule variant tested): `1,523 shares` is flagged as `sell_sh` drift though the prose says *"(retains 1,523 shares)"* — right finding, wrong concept label (canonical target is 1,461); and `9230 sh` in the SHORT horizon is a true false positive, where *"the most that can be sold…"* lets the sell anchor claim the eligible count.
>
> **Amendment loop deliberately NOT re-run this session** (Ariel's call): with the anchors dead it would have re-baked the same drifted digits. Re-run it once these land.

> **⚠️ THE HEADLINE FINDING (2026-08-15).** The plan reported FI "REACHED" with a +616,678 NIS cushion. That was GROSS of realization tax. Ariel's own per-lot RSU simulation (`tax_simulation_lots`, 57 rows, all 10,940 NVDA shares) was being read for ONE key (`concentration.nvda_eligible_now_sh` = 9,230) and never for the tax. Derived and now published:
>
> | | NIS |
> |---|---|
> | FI margin GROSS | **+616,678** |
> | NVDA embedded realization tax (revalued @ $225.16) | **2,510,030** |
> | **FI margin NET OF REALIZATION** | **−1,893,351** |
>
> The tax is **4.1× the entire gross cushion**. Six render sites now route through one shared helper so no surface can print "FI reached" without stating the basis. The FM had reported "no Schwab tax lots loaded" because it checks the `lots` table (0 rows) while the data is in `tax_simulation_lots`. Execution tracker with per-task checkboxes: `docs/superpowers/plans/2026-08-13-trust-restoration-execution.md`. Design: `docs/superpowers/specs/2026-08-12-trust-restoration-design.md`. Audit evidence: `docs/superpowers/INDEX.md`. Read the tracker before picking anything up — it records what landed, what was disproved, and what awaits Ariel.

---

## North star (why any of this matters)

Argosy is a trustworthy, always-on financial brain for one family. It holds the whole picture (holdings, RSUs, pensions, cashflow, Israeli tax, FX, life events), forms its own opinions, and is right, current, and self-consistent across `/plan`, `/portfolio`, `/retirement`. **The user should not have to be the investing expert — Argosy is, out loud.** Operationally: maximize finances + earliest safe retirement, with Argosy as the back office — Ariel is in the loop only when something genuinely needs him.

Canonical copy: `docs/design/SDD.md` → `## North star`. Auto-memory: `project_argosy_north_star`.

---

## State (authoritative — verified 2026-08-12)

| Thing | Value |
|---|---|
| master | **`94f02d5`** = origin/master — trust-restoration merged, pushed, deployed |
| Working branch | `feat/objection-loop` — closing the FM objection dead-end |
| Migrations | `0103_instrument_classification` (0102 gate_outcomes, 0103 sector caps, both applied) |
| Push | blocked by the harness classifier → Ariel runs `! git push origin master` himself |
| Working tree | clean; **no worktrees** |
| Backend | up on :8000, healthy (`/api/health`, `/api/portfolio/snapshot`, `/api/positions/thesis` all 200) |
| UI | up on :1337 (Next.js 16.2.4 / Turbopack) |
| Current plan | `refinement-2026-07-13-165608` — **pre-restore, from 2026-07-13** |
| Newest draft | `synth-2026-08-08-2200` — **do NOT accept** (see NVDA note) |
| Backup | `scripts/backup_to_sibling.ps1` → `D:\Projects\financial-advisor-backup` |

**Book (post-restore truth):** a 2026-07-13 Leumi-only-import bug erased $2.4M / 59% of the book (incl. NVDA); it was restored on production 2026-08-08/09. US-situs estate ~$1.28M.

**⚠️ NVDA — three live numbers, verified 2026-08-12.** `/api/portfolio/snapshot` returns `Strategic single-stock (NVDA): pct 58.02, target_pct 8.0`, while the **IPS prose says 12%** (a RED cross-surface finding logged 2026-07-07 and still open). Book **58.02%** / plan target **8.0%** / IPS **12%**.
Earlier handovers said the draft was "computed with NVDA @ 0%" — **that phrasing was wrong and is retired.** Nobody set a target of 0%. What exists: ConcentrationAnalyst report 2915 *recommends* `"force cap to 0% and liquidate to target"` under a zero-FI-delay-tolerance branch, and the analyst's input excludes the unmanaged Schwab NVDA (precondition A below). The claimed log line "current NVDA weight 0.0 UNKNOWN" could **not** be found in current logs — treat it as unverified.

---

## Trust-restoration pass — state as of 2026-08-13

**Landed on `feat/trust-restoration`** (tests green at each step; Sol reviewed the money paths):
- `9dba3cf` `GateOutcome` contract — tri-state PASS / BLOCK / **DID_NOT_RUN**, where DID_NOT_RUN blocks promotion. A non-PASS must carry a reason; an override must name who and why. Silent overrides are unrepresentable.
- `bcd9179` `/overview` shipped — was fully built and had no page route. 200, consistency guard 5/5 (first live exercise).
- `a10edc1` **fail-closed everywhere**: reader `None` blocks; codex math gate reports an outcome on every path; `/accept` graph-build exception no longer falls through to an open-flag-free promotion; `/refine` refuses to stage a draft whose invariants never ran (422); digest fails when it sends nothing. Plus **1B**: plan burn now derived from real transactions.
- `1fc2b75` SDD:1354 corrected — it had claimed the reader was fail-closed while the code did the opposite.
- `80d226d` deconcentration-optimizer route + VAPID generator.

**Ariel's rulings (2026-08-13):** thin-month threshold **50**; planning burn **rounds UP to the nearest ₪1,000** → **₪25,000** from a derived ₪24,032 (11 months, 1,082 txns). Implemented as a *labelled buffer on a derived number* (`monthly_burn_raw_nis` + `monthly_burn_buffer` travel alongside), never a typed figure.

**⚠️ Two claims this pass DISPROVED — do not re-fix:**
1. **Precondition A was never broken.** Executing `_summarize_positions` on the live snapshot yields `NVDA qty=10940 value=$2,379.4k acct=schwab` — the true 57.7%, already in the analyst's input. Four handovers propagated a misread docstring; nobody ran it. Cherry-picking `70008d4` was a no-op.
2. **Discord was not a silent failure.** `config.py:209` records a deliberate shutdown on 2026-07-07 (reconnect bug ~150 supervisor restarts/day, Discord blocked the API, **0 signals since 2026-05-29**). Auth-4004 errors date from 2026-06-15. Re-enabling was gated on a *value review that never happened*. **Ariel said "we need to fix the feed" — that decision is open**, and should start from whether a source with 0 signals in its final 5 weeks and a 21% hit-rate earns the work.

**Open money-math decisions (deliberately NOT applied):**
- **CGT taper binding.** `scenario_mc.py:552` pins `DECONCENTRATION_TAPER_YEARS = 3`; the plan headline never consults the optimizer's chosen horizon. SDD §19.4 is partially false (shared formula, unshared horizon). Worth ~₪100–170k and up to a year of retirement age.
- **The optimizer currently sees no NVDA at all** — `nvda_current_pct=None`, `sell_nis=0.0` for every horizon, because plan v92 has `decision_run_id=None` (refinement-path draft) so a `if drun is not None` guard skips resolution and a bare `except: return 0.0, None, None` hides it. **Expected to resolve itself once the regen creates a decision run — verify, don't assume.**
- **A FOURTH NVDA number:** `DEFAULT_NVDA_CAP_PCT = 0.13` (`scenario_mc.py:553`), alongside book 58% / plan target 8% / IPS prose 12%.

**Needs Ariel (blocked on a human):**
- SMTP creds. Set then restart: `ARGOSY_SMTP_HOST` (e.g. `smtp.gmail.com`), `ARGOSY_SMTP_PORT=587`, `ARGOSY_SMTP_FROM`, `ARGOSY_SMTP_USERNAME`, `ARGOSY_SMTP_PASSWORD` (Gmail app password). Send path already verified end-to-end against a local `aiosmtpd` sink.
- **Web push final step:** open `http://localhost:1337/settings/notifications`, click enable, grant the Chrome prompt. `notification_subscriptions` is 0 until then; VAPID keys now exist and the endpoint returns 200.
- **73 open action proposals** (not 59 — the queue grew), **40 stale >30d**: 45 `note_only`, 14 `set_watchlist`, 6 `update_plan_assumption`, 4 `rebalance` (all stale, likely superseded by the regen), 1 `replan_full` (2026-07-26, actionable — it is a regen trigger), 1 each `stock_decision` / `deploy_team_flag` / `allocate`. Nothing deleted.

**Trust-restoration is DONE and deployed.** Sol reviewed every money path across three passes: 11 blockers found and fixed, final verdict COMMIT AS-IS. Two of those blockers were introduced BY the fix work and were the exact pattern the branch removes — a swallowed burn exception relabelled "insufficient data", and a non-finite guard that raised into a handler which wrote the bad value through anyway.

**Burn corrected again:** refunds were never netted (the query filtered `direction=="debit"`). Real burn is **₪22,519 raw → ₪23,000 planning**, not the ₪25,000 fed to regen run 359. `/dashboard-overview` does NOT share the bug.

**⚠️ The 12-vs-13 NVDA cap is now DETECTED, not silent.** The coherence gate fires on the live artifact: `nvda_cap_pct disagrees across surfaces (body=12.0; alloc_doc=13.0; prose=13.0)`. The RESOLVER carries 12; the allocation doc and prose carry 13. Per CLAUDE.md this is the derivation-question class — **zigzag it before the next regen, or synthesis will block on it.**

**OPEN — the FM objection loop (workstream 2, in progress on `feat/objection-loop`).** Run 359: 7 FM objections, **0 dispatched**, all skipped at log level INFO, user told nothing. Causes: owner routing is a regex over prose (`fm_objection_dialogue._parse_analyst_refs_any_form`); `WithdrawalSequencerAgent` and `FXAnalystAgent` are absent from `ANALYST_AGENT_NAME_TO_ROLE`; `ARGOSY_FM_DIALOGUE_CONVERGE` defaults `"0"`. The machinery to fix it already exists and is simply unwired — `negotiation_ladder.run_ladder` (peer rounds → arbiter → `ESCALATED_TO_USER` with a question) and `critique_reconcile`'s `needs_user_input` → open ActionProposal.

---

## OPEN QUEUE (priority)

### 1. ⚠️ Regenerate the plan — the highest-value open item
The current plan predates the restore, and the draft was produced by a fleet whose concentration input excludes the unmanaged NVDA. Two hard preconditions, then a codex-free regen:

**Precondition A — ✅ ALREADY FIXED. Empirically disproved 2026-08-13.**
Running `_summarize_positions` against the live snapshot yields `NVDA qty=10940 value=$2,379.4k USD @ $217.50 acct=schwab` — i.e. **57.7% of the $4,121.4k book, present in the ConcentrationAnalyst's input.** The resolver half (`plan_numeric_resolver._apply_nvda_current_weight`) also already counts present-but-unmanaged NVDA on master; cherry-picking branch `70008d4` produced a **no-op** (3 comment lines).
The claim below was inherited through four handovers and repeated without ever being run. The docstring phrase "stays focused on tradeable holdings" refers to skipping **symbol-less** rows (cash sentinels, real-estate, pension) — *not* to excluding unmanaged positions. **Do not re-fix this.** Retained for the record:

<details><summary>superseded claim</summary>
`1f6ca68` fixed the *resolver* (the final rendered number) but not the analyst's input. `argosy/orchestrator/flows/plan_synthesis/inputs.py::_summarize_positions` (defined :1287, comment at :1310) still keeps the summary "focused on tradeable holdings", so the **unmanaged Schwab NVDA is excluded from the ConcentrationAnalyst's input**. (The often-quoted "run 284 logged *no NVDA position … weight 0.0 UNKNOWN*" could not be reproduced from current logs — the code-level exclusion is the verified part; treat the log quote as hearsay.) Until this surfaces unmanaged-but-present NVDA, a fresh synthesis reasons on a book missing its largest position. **Fix before firing any regen.**
→ Branch `worktree-agent-afb7cdd941018a1fc` is **already effectively on master** — safe to delete in branch triage.
</details>

**So the only remaining blocker to the regen is Precondition B** (below): the math gate must fail CLOSED.

**Precondition B — replace the codex math-gate with an in-harness blind re-derivation.**
`plan_synthesis/codex_second_opinion.py` is the blind headline-number audit that BLOCKs a plan whose NVDA weight / estate / net worth don't independently re-derive. When codex was 401-dead it returned `(None,None)` fail-soft — which is exactly how the bad v94 draft got green-lit. Codex is alive again (model `gpt-5.5`), but plan for it dying: substitute a read-only in-harness agent that re-derives NVDA weight, US-situs estate and net worth from raw snapshot rows and BLOCKs on divergence from the known-true numbers above.

**Regen sequence:** fix A → restart backend on the new SHA → fire `POST /api/advisor/check-in` → run the math audit (B) → present the draft to Ariel, **do NOT auto-accept**. Also re-run the sequence-aware retirement MC (`canonical_feasible_dual_track`) on the true book.

### 2. ⚠️ Make the guardrails fail CLOSED — the audit's central finding
Three mechanisms **report success when they did not run** (full evidence in `docs/superpowers/INDEX.md`):
- **Codex math gate** — `codex_second_opinion.py` returns `(None,None)` fail-soft; this is how the bad draft was green-lit.
- **Whole-artifact coherence reader** — `orchestrator.py:2657` computes `_reader_ok = (_reader_verdict is None or ... != "BLOCK")`, and `whole_artifact_reader.py:643` returns `None` on timeout / missing kit / pytest. **`None` ⇒ approved.** `SDD.md:1354` claims the exact opposite ("fail-closed … never a soft pass") — the SDD is wrong and must be corrected either way.
- **Weekly digest** — `job_runs.status='ok'` while `send_status: skipped`, `send_error: smtp_not_configured`.

This is one design habit, not three bugs: **absence of a result is treated as a passing result.** It is fully consistent with the "determinism is the inviolable-arithmetic floor" rule — a gate that blocks when it *cannot verify* is not judging whether a decision is good.

### 3. Consistency + last-mile gaps found by the 2026-08-12 audit
Ranked; all evidence in `docs/superpowers/INDEX.md`.
- **NVDA reads 58% / 8% / 12%** across book, plan target, and IPS prose. Open since 2026-07-07.
- **Plan never sees real spend** — `inputs.py:1006` feeds the analyst `identity_yaml.monthly_expenses_total_nis` (typed at onboarding); zero `ExpenseTransaction` references in `plan_synthesis/`. SDD §6:544 claims otherwise. Cal/Amex/Diners parsers are `NotImplementedError`.
- **Nothing can reach Ariel** — email skipped (`smtp_not_configured`), `notification_subscriptions` = 0, Discord dead since 2026-07-08 while its 434 stale predictions still appear live in `source_reliability`. 59 open proposals undelivered.
- **Caps are prose-only** — `risk_preflight.py:180` has no sector-cap logic; `PlanPolicy` / `instrument_classification` NOT_BUILT. The 35% tech and 15% single-name caps are unenforced.
- **`/overview` is built and unreachable** — router registered (`main.py:129`), assembler + 10 components exist, but no `ui/src/app/overview/page.tsx` and not in nav.
- **`can_publish_plan` bypassed** on any exception (`plan.py:3839-3847` falls back to bare `evaluate_promotion`).
- **`/refine` money-safety net inert** — `plan.py:5688` omits `post_doc`, so `evaluate_plan_invariants` never runs.
- **Insurance entirely NOT_BUILT** — substitute is a 10×-income heuristic with no policy data.

### 4. #24 Phase 3c enforcement — deferred, needs a decision
The spine gate shipped **warn-first and DORMANT** (`f45acf1`). Flipping money surfaces to route through `read_validated_snapshot` and refuse a non-validated book is behavior-changing; it needs a deliberate call, not a drive-by.

### 5. Re-run the lean Sol confirms
Self-verified during the codex outage: 3d round-4 delta, verdict-authority round-2. Codex is stable again (`gpt-5.5`).

### 6. Triage 10 unmerged branches (no worktree)
| Branch | Unmerged | Tip subject |
|---|---|---|
| `feat/stream-a-data-integrity` | 3 | restore provenance liveness path; keep vintage enforce off |
| `feat/stream-b-bear-independence` | 3 | make independence semantic; choke trader prompt assembly |
| `feat/stream-c-prediction-ledger` | 1 | wip: make the prediction ledger actually score |
| `feat/stream-e-async-cache-loop` | 1 | wip: loop-safe bridge instead of `asyncio.run` |
| `worktree-agent-a10db172e619def77` | 1 | graduate mark-staleness so normal gaps degrade gracefully |
| `worktree-agent-a65837e8271f39123` | 1 | route six raw-TSV readers through one guarded book accessor |
| `worktree-agent-a7ad1ac05ea0314da` | 1 | make silent-success / fail-open job failures visible |
| `worktree-agent-a8c47c9e4b388501d` | 1 | label opaque symbol-less rows; stop live-DB test |
| `worktree-agent-aab67d6154b20533a` | 1 | stall alert must alert-not-throw + `busy_timeout` on sync engines |
| `worktree-agent-afb7cdd941018a1fc` | 1 | count present-but-unmanaged NVDA toward concentration % ← **see item 1** |

### 7. Smaller open items
- **6 decision-funnel tests red on master** (`test_decision_funnel_position_context`, `_sleeve_mandate`) — they hit the live DB and short-circuit because SOFI has a settled verdict. Live-DB test-isolation defect (same class as `9b98085` fixed), **not a source bug**. Isolate with a fixture DB.
- **~40 sync `create_engine(` sites** lack `busy_timeout` — migrate to `create_sync_engine()`. Mechanical; `8963406` did the two hot ones.
- **Owner action:** a fresh Schwab statement would close the restore's carry-forward assertion (it assumes nothing traded in Schwab / Schwab 876 / Aborad since 2026-07-13 — Ariel confirmed, but it's unverifiable from inside the system).

---

## Recently shipped (don't re-do)

Prior handovers listed these as open **after** they had already shipped — check `git log` before believing any open-item list, including this one.

- **`b2e331c`** UI render of the "how our calls did" greeting scorecard.
- **`f45acf1`** spine Phase 3c, warn-first + DORMANT.
- **`0c4d692`** seam 4 — executed fills link back to the verdict that recommended them (+ migration 0101).
- **`fbf2030`** #35 falsifier evidence-contract + concise advisor-voice verdicts.
- **`a9d844f` / `f0a02d3` / `e97e56b`** the decision-learning loop: `/positions` surfaces settled-verdict reasoning; every verdict gets a recheck date and becomes a graded prediction keyed by `verdict_id` so the live evaluator scores it; greeting surfaces wins/misses.
- **One-voice fix (Phases 1–3, Sol-passed)** — resolved a verdict-vs-stance split-brain (NVDA had a settled HOLD contradicting the plan's standing SELL). Binding ruling in memory `feedback_one_voice_verdict_vs_plan`: a per-holding verdict contradicting the plan's standing stance is a **bug — fix the inputs, not with a gate**.
- **Book-trust goal CLOSED** — both reviewers GOOD.
- **P1 restore complete** — the $2.4M erasure was restored on production and the fallout fixed across 7 commits.

---

## Working discipline

- **The LLM TEAM is the architecture — not deterministic gates.** A *judgment* failure (bad rationale, wrong instrument, imprudent call) is caught by ANOTHER agent re-deriving blind — never by adding a per-symptom deterministic gate (whack-a-mole antipattern). Determinism is the **inviolable-arithmetic floor only** (conservation, estate / US-situs). Default response to a bad decision: *"which agent should have caught this, and why didn't it — fix the team."*
- **Accuracy over LLM cost.** Synthesizer + bull/bear/trader/fund_manager/audit/plan_synthesizer default to Opus. No Haiku defaults.
- **Ask, don't assume.** Surface judgment calls to Ariel.
- **Escalation bar: fatal FORKS only.** Two judges disagreeing on a value/wording is a DERIVATION question — zigzag it (each side argues from raw sources, a blind third re-derives, converge, record the rationale). Ariel gets only structurally different PATHS (sell-vs-hold the core, adopt-vs-exit a class, goal changes).
- **Money/decision units:** build → Sol → fix → commit. Skip Sol for UI / read-projections.
- **Manual UI smokes are skipped** — backend tests + live-LLM e2e are the verification surface.
- **A claim of success must cite a command that exercised the real path and its output — not a summary, and never a mocked-seam test alone.** (2026-08-16, `fix/real-path-proof`.) Three incidents in one day were all the same shape: work verified against a PROXY of the real path (every test patched the LLM seam; a persist call sat in a code region the real path never reached; a derivation helper was called directly, routing around a resolver that turned out not to be buggy) — reported as success, wrong on first live run. Concretely:
  - **Real-seam test convention.** `@pytest.mark.real_seam` (registered in `pyproject.toml`) marks a test that exercises a real DB engine/session and/or a real agent object (only the innermost LLM call stubbed, never the whole dispatch function replaced by a `MagicMock`). `scripts/check_real_seam.py` flags agent-dispatch / DB-write modules with zero `real_seam` coverage — reporting tool, not a blocking gate (198/661 modules are gaps today; it's new). Default scope is changed/untracked files vs `HEAD` (exits 1 on a gap in that scope); `--all` gives the full-repo inventory (report-only). Genuine exceptions go in `scripts/real_seam_allowlist.txt` with a reason — reviewed, not silently skipped.
  - **Smoke entry point.** `scripts/smoke_real_paths.py` hits the four paths that burned us — gate-outcome persistence, fact-tokenizer (via the real `resolve_plan_numbers`, not a hand-built stand-in), plan-amendment (`dispatcher.run_small`), fund-vehicle verdict (`run_fund_vehicle_decision`, a REAL live `agent.run()` call) — against a throwaway copy of `db/argosy.db` (never the real one) and prints what actually happened. Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/smoke_real_paths.py [gate-outcome fact-tokenize plan-amendment fund-vehicle]`.

## Traps

- **Codex/Sol:** model `gpt-5.5` (a ChatGPT-account codex rejects literal `--model sol`). LEAN prompts finish; heavy ones get killed.
- **Never junction-link `<worktree>/ui/node_modules`** to main's — it has twice wiped main's `@babel/` scope on `git worktree remove`. Use `cd <worktree>/ui ; npm ci` (~45 s).
- **`git worktree remove` does not delete commits** — branch refs survive. Safe as long as you don't also `git branch -D`.
- **Console is cp1252** — set `PYTHONIOENCODING=utf-8` for anything printing ₪ / → / Hebrew, and do durable side-effects BEFORE printing (a print crash silently killed three domain-refresh runs).
- **Windows 32,767-char argv cap** — this was the real cause of "claude.exe not found". Pass system prompts >8k via `--system-prompt-file`, never inline (`3b9ae78`).
- **`proposals.account_class`** — the DB string is `"limited"`, never `"argonaut"`; the broker router checks that exact value.
- **Don't bypass** `argosy/services/file_catalog.py::catalog_upload` for new user-byte-blob ingest paths (SDD §17.1).
- **Only `docs/design/SDD.md`** is canonical — never edit `.docx` siblings.

## Restart recipes

**Backend** — kill the 4-proc chain first; the start script's idempotency guard refuses if a supervisor + busy port already exist:
```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000        # find the chain
Stop-Process -Id <top supervisor> -Force                  # children die with it; verify all 4 gone
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
& "D:\Projects\financial-advisor\scripts\start_backend_detached.ps1" -Port 8000
Invoke-WebRequest http://127.0.0.1:8000/api/health        # expect 200
```

**UI** — must be launched detached; a double-background from bash silently loses the process:
```powershell
Start-Process cmd.exe -ArgumentList "/c","npm run dev" -WorkingDirectory "D:\Projects\financial-advisor\ui" `
  -RedirectStandardOutput D:\Projects\financial-advisor\tmp\ui_dev.log `
  -RedirectStandardError  D:\Projects\financial-advisor\tmp\ui_dev.err.log -WindowStyle Hidden
```

**Tests:** `.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
