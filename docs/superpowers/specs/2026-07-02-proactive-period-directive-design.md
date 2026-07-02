# Design — the proactive period directive (investment-committee-in-software)

**Status:** approved (Ariel, 2026-07-02). Supersedes the handover §4 "build glide logic into
`holistic_rebalance_review`" plan — that would duplicate the existing `breach_router`.

## 1. The mindset (governing policy — see SDD §1.6)

Argosy is a team of experts on retainer whose only job is to **protect and elevate** the
family's wealth. It is **proactive**: it watches continuously and comes to the owner with
"here's your move," unprompted. The owner having to *ask* what to do with idle cash is a
**failure**, not the primary path. The client-initiated `/deploy-cash` request stays as an
"act now" fallback.

The target is **not a smart alerting system** — it is an **investment committee (IC) process in
software.** Every proactive message reads like a mini IC memo: **objective · facts · alternatives
considered · recommendation · trade-off (cost of acting vs not) · dissent · tax note · next
review trigger.** Proactive without being impulsive.

## 2. Protect-before-elevate hierarchy (explicit priority order)

1. Preserve family liquidity + tax/legal compliance.
2. Protect the safe-retirement plan.
3. Reduce uncompensated concentration risk.
4. Execute scheduled policy actions.
5. **Elevate** returns — *with surplus risk budget only*.

**Consequence:** the ~5% high-growth / discovery sleeve is funded **only from surplus risk
capacity**, never from capital needed to make retirement safe.

## 3. The SELL is categorised by *why it exists* (not "time vs risk")

The glide is the **base policy**; everything else is an **exception protocol** justified by the
portfolio no longer fitting the owner's risk budget / retirement objective / liquidity / thesis.

| Sell type | Fires when |
|---|---|
| **Policy sell** | Scheduled glide-path deconcentration tranche is due. Boring, tax-aware. |
| **Catch-up sell** | A policy sell was missed / delayed / invalidated by updated values. |
| **Risk-budget sell** | Portfolio risk moved *outside the approved envelope*. |
| **Thesis-break sell** | The reason for holding the concentrated position materially changed. |

The glide is a **risk-reduction liability**, not a calendar reminder: "quiet between waypoints"
holds **only while the portfolio stays inside a defined concentration corridor.** If NVDA rises,
the required reduction can grow before the next waypoint.

## 4. Urgency & size — threshold the OBJECTIVE, not each symptom

No hardcoded per-symptom thresholds (`if VIX>30`). Measure against the client objective / risk
budget — retirement fundedness impact, probability of missing the safe-retirement date,
drawdown-under-stress, NVDA marginal contribution to loss, concentration vs cap + corridor,
thesis-confidence decay, liquidity runway, **tax cost per unit of risk reduced** — then map to
action bands:

- **Routine** — inside risk budget → execute scheduled glide / buys only.
- **Elevated** — budget mildly breached / trend deteriorating → pull forward one glide tranche, or prep an approval memo.
- **Urgent** — concentration now threatens retirement safety / liquidity / acceptable drawdown → recommend a sale sized to restore the *nearest acceptable risk corridor* (not necessarily to target).
- **Critical** — thesis break / liquidity event / legal-tax deadline / severe plan impairment → immediate de-risking with explicit tax cost + alternatives.

**Size = the smallest after-tax trade that brings the portfolio back inside the acceptable risk
corridor** (or materially reduces failure risk) — computed backward from the objective.

## 5. Alternatives + the no-action memo

- A risk response must show it **considered alternatives**, not only "sell stock": staged sale,
  collar / protective put, offsetting diversifier, TLH offset, charitable transfer, or
  no-action-with-review-date — under §102 / FX / liquidity / broker constraints. Unavailable
  ones are named as considered-and-rejected.
- **No-action memo:** doing nothing at 57% is itself an active recommendation. When the team
  stays quiet on NVDA it states *why*: "no sale today because X; cost of not acting is Y; next
  review trigger is Z."

## 6. Failure-mode guards (proactive ≠ impulsive)

| Failure mode | Guard |
|---|---|
| Over-trading | **Action hurdle** — must beat no-action after tax/fees/spread/risk/retirement-impact; + cooldowns unless severity escalates |
| Whipsaw on false signals | Require **confluence** — a sensor triggers *assessment*, not a sale; distinguish noise / risk-budget breach / thesis impairment |
| Tax-dumb urgent sells | Quantify tax (gross risk reduction, tax cost, after-tax proceeds, cheaper alternatives); sell the least-tax-expensive lot achieving the objective |
| Alarm fatigue | **Separate sensor events from owner messages** — owner sees only decision-grade items, blocked-info requests, or the compact heartbeat |
| Stale-data recs | **Hard freshness gates** — no directive touching FX / tax lots / discovery / holdings if stale → "need updated X" |
| Narrative overconfidence | Every major action carries bull + bear + base; the **risk officer signs the "what would make this wrong?"** section |
| Falling-knife buys | Opportunity buys need regime + liquidity checks + staging + invalidation criteria; label entry (valuation / momentum / rebalance / liquidity-led) |
| Cash drag | **Idle-cash SLA** — deployable cash over target for N days → recommend deployment or explain the option value of holding |
| Discovery-sleeve overfitting | Cap position size; require thesis durability; **avoid AI-beta that re-adds the NVDA risk the trim is reducing** |
| Owner trust | Every directive fully auditable: inputs, stale checks, alternatives, rejected alternatives, tax est., size, dissent, next review trigger |

## 7. The standing loop — Watch → Assess → Assemble → Speak

- **Watch** (cheap, continuous): existing sensors — minute loop (volatility-band breach,
  flash-crash), hourly (material news on a holding, macro surprise, FX move), drift detection,
  cash-overage. Trip-wires, not decisions.
- **Assess** (expert judgment; on trip OR on cadence): the already-approved **daily decision
  funnel** (market review → relevance routing → cheap triage → deep fleet only where warranted),
  now producing **risk-budget / thesis-break** verdicts too — not just buy/monitoring. Emergent
  judgment on live indicators; refreshes stale FX / discovery **first**.
- **Assemble**: ONE coherent **period directive** (the IC memo) — ranked actions with the action
  bands of §4, each naming both sides of the trade-off.
- **Speak** via three channels: **Immediate** (decision needed) · **Blocked** (needs data) ·
  **Heartbeat** (periodic "on track / nothing due / watched X").

## 8. What already exists (do NOT rebuild)

- **BUY good engine:** `deployment_advisor.assemble_deployment_plan` + `deployment_funnel/`
  (diversifier redirect, flags, fleet review) and `_high_potential_lines` (discovery sleeve via
  `build_high_potential_sleeve` over cached discovery BUY picks). Reachable via
  `GET /api/portfolio/deploy-cash`.
- **SELL policy engine:** `breach_router.compute_breach_tranche` / `route_breach_tranche` — the
  glide-paced NVDA tranche (over-cap ÷ H×4 glide quarters), codex-verified, idempotent, never
  executes, routes to `awaiting_human`. Wired into `monthly_cycle.py:64`.
- **Inbox surfacing:** `_adapt_trades` already surfaces `awaiting_human` trade proposals with an
  Approve action; `_adapt_cash` surfaces idle cash with a buy list.
- **Sensors:** minute/hour/daily cadence loops (SDD §5).

## 9. The gaps (what this project builds)

1. **Divergent buy engine.** The inbox `_adapt_cash` uses the *old* `unallocated_cash_detector`
   → `_allocate_long_term_from_plan` (`cash_only_deploy`), **not** the good funnel — and it
   **omits the discovery sleeve**. The docstring's "SAME engine /deploy-cash uses" is now stale.
2. **Sell is monthly-only, in a separate table, not on-demand, not glide-corridor-gated, no
   no-action memo.**
3. **Two disconnected inbox items** (cash + trade) instead of ONE IC-memo directive.
4. **No freshness gate / idle-cash SLA** on the proactive path.
5. **No "Assess" brain** producing risk-budget / thesis-break sells (the daily decision funnel
   is approved but unbuilt).

## 10. Build sequencing

**Step 2 — Allocation (this sprint). Adopt framing §1–§6; build the buildable core:**
- 2a. Unify the buy onto the **good engine** (core + discovery sleeve, surplus-only) across
  `/deploy-cash` and the inbox; retire the divergent `cash_only_deploy` inbox buy-list.
- 2b. Surface the glide **policy sell** into the inbox **on-demand** (compute the breach tranche
  when an allocation is requested, not only monthly) + **glide-corridor gate** it.
- 2c. Emit the **no-action memo** when nothing on NVDA is due.
- 2d. **Freshness gate** (refresh stale FX/discovery before advising) + **idle-cash SLA**.
- 2e. **Group** buy + sell + no-action + tax note into ONE "Your move this period" IC-memo card.
- Codex-zigzag the money-math (glide-corridor gate, tranche sizing, surplus-risk sleeve carve).

**Step 3 — Flow (next). The Watch→Assess→Assemble→Speak loop:**
- The daily decision funnel as "Assess," producing risk-budget / thesis-break sells as IC memos
  with the four sell categories, action bands, alternatives, and dissent.
- The full quant lens set (marginal risk contribution, factor/correlation stress,
  expected-shortfall) phases in here — not a blocker for Step 2.
- Register FX as a scheduled job; three communication channels.

## 11. Out of scope (YAGNI for now)

- Full options/collar execution plumbing (the *evaluation* of hedging alternatives is in scope;
  actually placing a collar is not).
- Multi-tenant generalisation beyond what the single-user path needs.
