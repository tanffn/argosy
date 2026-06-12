# Deployment Advisor — design spec

**Date:** 2026-06-12
**Status:** design, pending user review
**Topic:** an Argosy-native, team-of-experts cash deployment surface that turns "I have $X to deploy" into a clear, market-aware, estate-aware buy/sell list shown in `/proposals`.

## Problem

There is no surface that shows **how much unallocated cash the user holds and how to deploy it.** Today: `/portfolio` has an `UnallocatedCashCard` that only fires when cash exceeds the plan target by ~1.5×, and `/proposals` surfaces no buy/sell actions derived from the plan. The deterministic `allocation_engine` + `GET /allocation-tasks` already produce plan-bound, glide-aware, UCITS-swapped candidates (and an agent pass that orders/paces/explains them), but none of it is surfaced as actionable proposal cards.

The user currently generates the list by hand-prompting an external single LLM (see "Source method" below). Argosy should do this **better than one LLM with a manual prompt** by using its team of specialist agents, its canonical plan as the single source of truth, its wired + cached market reads, a deterministic engine that enforces the math, and `/proposals` persistence with every risk surfaced.

This is the "cash + allocation surface gap" (`project_cash_and_allocation_surface_gap`), tied to the prime directive (deploy idle cash toward the plan + earliest-safe-retirement) and the unallocated-cash-is-the-primary-signal reframe.

## Source method (the functional spec, from the user's external-LLM prompt)

ROLE: portfolio deployment advisor; Israeli resident, NVIDIA employee, reducing NVDA over 5+ years. METHOD, in order:
1. Refresh context: today's S&P, VIX, oil, USD/NIS, BoI rate, inflation, geopolitical (Iran) status; verify NVDA price + share count; never stale.
2. Identify gaps: current allocation vs plan targets. Count tech individuals (AMD/GOOG/AMZN/META/TSLA) as **growth** before calling growth a gap. FWRA/IWDA are ~60–70% US — **don't count them as "international."**
3. Prioritize: (a) biggest underweight gaps, (b) estate-safe UCITS first, (c) diversify away from NVDA/tech, (d) genuine fear-discounts (down 30%+ on *sentiment*, not business deterioration), (e) fill the empty Alternatives bucket with gold (Irish-domiciled ETC, e.g. SGLD/IGLN).
4. Structure: Core/gap-fill (now) + DCA over 4 weeks (volatile/ATH) + small opportunistic sleeve + reserve (existing SGOV; deploy on S&P −7%).
5. Single names: cap the sleeve small; only "cheap + quality + catalyst" (verify P/E from 2 sources); reject hype/expensive (PEG>5) and already-run war-momentum (energy/defense).
6. Verify each ticker available at Leumi; prefer UCITS/Irish-domiciled; flag US-domiciled estate exposure.

OUTPUT: a plain-text table `SYMBOL | TYPE | AMOUNT | TIMING | NEW?`, NEW vs held marked, summing to the full amount, with a 2-line caveat (confirm net-of-Israeli-tax; note single-name estate exposure). Direct, numbers not adjectives, no hedge language.

## Decisions (locked with the user)

1. **Generation model:** deterministic plan-bound skeleton + a **market-aware expert layer**. The engine enforces gaps/UCITS/cap; the analyst fleet (macro/FX/news/fundamentals/sentiment/technical) makes it market-aware. Live experts when the API key is present; **freshest cached reads** (from the last synthesis run, already in `agent_reports`) otherwise — never blank, never a dead spreadsheet.
2. **Expert latitude:** **full tactical** — experts may over/underweight any class and add opportunistic single-names on market views. The user is the final gate. Latitude may NOT hide risk (trust doctrine).
3. **Estate handling (horizon-tagged):** each proposed line carries a hold-horizon. **≤5yr tactical single-names → US-situs allowed freely** (the estate tail bites only at death; a 5-yr hold is unlikely terminal). **10yr+ core → prefer the UCITS/Irish twin, after comparing TER + tracking/performance** (twins aren't always cheaper/better). Estate exposure is always shown per line + in aggregate.
4. **Scope:** the **full method** (all six steps), delivered in phases (features are not cut; see Phasing).
5. **Execution model:** **advisory** — the surface produces the table + Approve / Customize / Defer per line (records the decision; the user places orders at Leumi). No auto-execution (Leumi is not an automated broker).
6. **Gold/Alternatives plan-class weight:** **engine-derived** from the allocation engine's sigma/diversification model, like every other class — no magic number.
7. **Leumi availability:** **heuristic + flag** — assume major UCITS/US tickers are tradeable; flag less-common ones as "verify at Leumi." No maintained list in v1.
8. **Deploy amount is net of tax (user, 2026-06-12):** the entered amount is already post-Israeli-CGT deployable cash — Argosy does NOT model a tax holdback or gross→net reduction. The per-line "confirm net-of-Israeli-tax" stays as a light caveat only, not a sizing input. (FX: ILS↔USD conversion timing for buying USD-denominated UCITS is still an agent-surfaced consideration, but not a tax-reserve concern.)
9. **Risk-tier sizing + tier = the deviation bound (user):** the deploy splits into three tiers — **~70% core** (plan-bound, estate-safe UCITS gap-fill), **~25% medium** (quality large-cap incl. acceptable US mega-caps like GOOG/AMZN), **~5% high-risk** (speculative opportunistic single-names). The tier caps are tunable defaults and apply to the **deploy-now capital after the reserve is carved out** (decision 12) — reserve + core + medium + high sum to the full net amount. The tier label IS the "plan-fill vs tactical bet" marker Codex asked for, and the tier caps ARE the drift bound that keeps full latitude from becoming a second plan. Tactical (medium/high) lines carry an expiry/re-review.
10. **Geographic diversification preference (user):** actively prefer good-quality **Israeli/EU** names as NVDA/US-tech diversifiers where they exist (not a hard exclusion — US dominates growth, so US quality stays allowed). The correlated-exposure cap targets **NVDA/semiconductor/AI-hardware correlation specifically** (the thing being deconcentrated), NOT blanket US-mega-cap — so GOOG/AMZN in the 25% medium tier are fine.
11. **Lump-vs-DCA is size + math driven (user):** small tranches (≈$5K) deploy whole — never split. Large tranches (≈$100K) evaluate DCA-over-N-weeks driven by the math (volatility, valuation vs history, event risk), not a fixed 4-week rule. A size threshold + a math-driven pacing decision per line; the expert layer chooses and explains.
12. **Reserve is agent-sized + parked estate-safe (user):** the dry-powder reserve is sized by the experts based on market climate (not a fixed %), with a deploy condition (e.g. S&P drawdown). Argosy suggests WHERE to park it, preferring an estate-safe instrument (IB01/ERNS UCITS) over US-domiciled SGOV.

## Architecture

### The `deployment_advisor` flow

A new orchestrated flow (mirrors `argosy/orchestrator/flows/plan_synthesis/`), driven by a target deploy amount + the user's current snapshot + canonical plan. Phases:

- **A — Context refresh.** Pull S&P, VIX, oil, USD/NIS, BoI rate, inflation, geopolitical (Iran) via the macro/FX/news analysts; verify NVDA price + share count against the snapshot. A **staleness gate** flags any feed that is stale or internally inconsistent (per the trust-data-feed doctrine: only flag on demonstrable inconsistency, not "feels wrong"). Live on key present; cached `agent_reports` from the latest run otherwise, with the read's age surfaced.
- **B — Honest gap analysis (deterministic).** `allocation_engine` computes current-vs-target with **geographic look-through** (broad funds like FWRA/IWDA decomposed by region — ~65% US — so "international" isn't over-counted) and **tech-individual classification** (AMD/GOOG/AMZN/META/TSLA mapped to the growth class before growth is called a gap).
- **C — Candidate generation, by risk tier (~70/25/5).** **Core (~70%):** UCITS-first gap-fill (cap-aware, glide-aware) + gold/Alternatives fill (engine-derived weight; Irish ETC e.g. SGLD/IGLN). **Medium (~25%):** quality large-cap, acceptable US mega-caps (GOOG/AMZN), **with an active preference for good Israeli/EU diversifiers** where they exist. **High (~5%):** opportunistic single-names via the cheap+quality+catalyst screen; the **discovery funnel** surfaces fear-discounts (down 30%+ on sentiment, fundamentals confirm it's not deterioration). A **correlated-exposure cap** (NVDA/semis/AI-hardware specifically) spans single-names + fund look-through so the deploy doesn't rebuild the concentration being reduced.
- **D — Expert grading + debate.** bull/bear/fundamentals/fund_manager grade each candidate. The **horizon tag** drives the estate rule (decision 3). The single-name screen rejects PEG>5 and already-run war-momentum (energy/defense). P/E is cross-checked from 2 sources (source-scoring ledger). Leumi-availability heuristic flags off-list tickers.
- **E — Structure, pace, surface.** Group by risk tier (core / medium / high) + a **reserve** sleeve. **Pacing is size+math driven** (decision 11): small lines (≈$5K) deploy whole; large lines (≈$100K) get a math-driven DCA schedule (volatility/valuation/event-risk), not a fixed 4 weeks. The **reserve is agent-sized by market climate** (not fixed) with a deploy condition (S&P drawdown) and an **estate-safe park** (IB01/ERNS over SGOV). Deployed tiers + reserve **sum to the full (net-of-tax) amount**. Attach per line: risk-tier label (= plan-fill vs tactical bet), estate tag + horizon, cap/correlated-exposure impact, net-of-Israeli-tax caveat, expert rationale + bull/bear summary, NEW-vs-held, and (tactical lines) an expiry/re-review date.

### Surface (`/proposals` → "Deploy Cash")

Always shows current unallocated cash (NIS + USD, per account, from the snapshot) + a deploy-amount input (pre-filled with detected idle cash; user can type a tranche e.g. `250000`). Renders the table grouped by bucket — `SYMBOL | TYPE | AMOUNT | TIMING | NEW?` + reason — with per-line Approve / Customize / Defer (advisory). A 2-line caveat (confirm net-of-Israeli-tax; single-name estate exposure). Numbers, not adjectives; no hedge language.

### Reuse vs new

**Reuse:** `allocation_engine` (gap-fill/rebalance/UCITS-swap/cap), discovery funnel + grader, analyst fleet + cached `agent_reports`, estate/domicile gate, `/proposals` lifecycle + card components, the existing `GET /allocation-tasks` + `allocation_agent.order_and_explain`.

**New build:** the `deployment_advisor` flow; the "Deploy Cash" surface + entry point; geographic look-through + tech-individual gap classification; the gold/Alternatives plan-class (engine-derived); live on-demand market refresh + NVDA verify + staleness gate wiring; the single-name cheap+quality+catalyst screen with 2-source P/E; the Leumi-availability heuristic; the −7% conditional reserve trigger.

## Delivery phasing (all features ship)

- **P1 — Surface + plan-bound core.** "Deploy Cash" surface + entry; engine gap-fill (UCITS, cap, glide); estate horizon rule; buckets/NEW?/timing/reason; per-line estate + cap + net-of-Israeli-tax. Runs on cached expert reads. *Delivers a "better than the example" list immediately.*
- **P2 — Live market context.** On-demand refresh of S&P/VIX/oil/FX/BoI/inflation/geopolitical + NVDA price/share verify + staleness gate.
- **P3 — Honest gaps + gold.** Geographic look-through + tech-individual classification; gold/Alternatives plan-class (engine-derived).
- **P4 — Tactical sleeve.** Single-name cheap+quality+catalyst screen + 2-source P/E + PEG>5 / war-momentum rejection; Leumi-availability heuristic; −7% conditional reserve trigger.

Each phase: tests + codex-tandem review (money-math/decision-flow) + a usable surface.

## Trust-doctrine compliance

Nothing hidden, nothing lost: every line surfaces its estate-tax implication, cap impact, net-of-Israeli-tax note, and expert rationale; staleness/inconsistency in any market feed is flagged loudly; the deterministic engine guarantees the buys sum to the amount and respect the cap; cached-vs-live expert reads are labeled with their age. No magic numbers — every figure traces to the engine, the plan, or a cited agent read.

## Open implementation questions (resolve in planning, not blockers)

- Which adapters supply VIX / oil / BoI rate / inflation / geopolitical (Iran) — map to existing macro/news adapters or add sources.
- Geographic look-through data source for broad funds (region weights for FWRA/IWDA/etc.).
- The gold/Alternatives sigma input for the engine-derived weight.
- Whether the gold/Alternatives plan-class change re-runs synthesis (plan-level) or is an engine-only target addition.
- The −7% reserve trigger baseline: −7% from the **deploy-date** S&P level (proposed default) vs. from the trailing all-time high. The trigger is a *surfaced condition* the user acts on (advisory), not an auto-buy.
- ILS↔USD conversion timing for funding USD-denominated UCITS buys — agent-surfaced consideration (not a tax-reserve concern; the deploy amount is already net of tax per decision 8).

**Technical definitions to pin in planning** (resolved by the team/codex, NOT user-facing — from the Codex design review):
- Fear-discount baseline ("down 30%+ on sentiment"): which reference (ATH / 52-wk high / pre-event), and the fundamentals tests that prove it's *not* deterioration (revisions, margins, guidance, debt, competitive position) so the screen can't recommend falling knives.
- Authoritative P/E (forward vs trailing vs GAAP/non-GAAP) for the 2-source cross-check; PEG>5 treatment is a guideline not a universal hard reject (fragile for cyclicals/financials/negative-earnings).
- The lump-vs-DCA size threshold (the ≈$5K-whole / ≈$100K-DCA boundary) + the math inputs to the pacing decision.
- The correlated-exposure metric (NVDA/semis/AI correlation) spanning single-names + fund look-through, and its cap.
- The medium/high tier expiry/re-review cadence; the estate horizon is **user-set per line** (confirmed, not system-inferred), with a re-review trigger when a ≤5yr US-situs holding ages past 5 years.
- Manual-order fields surfaced per line (ISIN, exchange, currency, accumulating/distributing class, est. shares, limit price); "verify at Leumi" blocks Approve until confirmed; fees/spread/TER/tracking feed ranking.

**Resolved (no longer open):** deploy amount is net-of-tax (decision 8); tactical-drift bound = the risk-tier caps (decision 9); estate horizon source = user-set per line.

## Testing

Backend: unit tests per new service (gap look-through, tech classification, single-name screen, estate horizon rule, conditional reserve trigger, gold weight) + a flow integration test that asserts the buckets sum to the amount, respect the cap, and surface estate/tax per line. UI: tsc + lint + vitest for the surface; live-LLM e2e for the full flow. No manual click-through (per project convention).
