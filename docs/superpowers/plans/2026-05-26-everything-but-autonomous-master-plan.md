# Argosy — close everything before autonomous live trading

**Date:** 2026-05-26
**Author:** Claude, with Ariel's sequencing direction.
**Trigger:** Ariel: "leaving autonomous live trading to be last task, we need to resolve all the rest"
**Status:** Active. Tier 1 in flight.

## Direction (locked)

- **Tier 1 in full**, then re-synth verification checkpoint, then Tier 2.
- **Productization (Phase 6) sits between manual live trading (Tier 6 / Phase 4) and autonomous trading (last / Phase 5).**
- Autonomous live trading is the FINAL task.

## What "everything else" means

Six tiers ahead of autonomous trading, total ~35-50 hrs to Tier 5 + ~20-30 hrs Tier 6 + ~30-60 hrs Tier 7 (productization). Autonomous = Phase 5 from the SDD.

```
TIER 1 — Data plumbing (unblocks usable synthesis output)         ~12-18 hrs
TIER 2 — Synthesis hardening (cost-safe iteration)                ~5-7 hrs
TIER 3 — Adapter coverage (W3a.B remainder)                       ~2-4 hrs
TIER 4 — Product surface (per-position UI + replay + brief)       ~10-15 hrs
TIER 5 — Anomaly + hygiene (EX2 + O-gaps)                         ~4-6 hrs
TIER 6 — Trading core MANUAL (Phase 4 — human-approved live)      ~20-30 hrs
TIER 7 — Productization (Phase 6)                                 ~30-60 hrs
LAST  — Autonomous live trading (Phase 5)
```

## Tier 1 — Data plumbing detail

These items unblock proper synthesis output. As of synthesis run #20 (in flight at time of writing), three Phase-1 analysts have already failed for empty-payload citations and Fund Manager will likely reject again for ConcentrationAnalyst null-positions — same as run #19.

| # | Item | Effort | Why load-bearing | Acceptance |
|---|---|---|---|---|
| **T1.1** | `_assemble_portfolio_summary` plumbing fix | 2 hrs | Currently a stub returning a placeholder string. ConcentrationAnalyst gets null positions, FM rejects every draft on this ground. Real fix: thread parsed-TSV positions through to synth Phase 1 inputs. | Synthesis run produces non-null `portfolio_summary` reaching ConcentrationAnalyst's `build_prompt`; agent emits citations including `portfolio/holdings:<ticker>`. |
| **T1.2** | Adapter probe follow-up | 1-2 hrs | Run #20 saw `news_count=0`, `macro_count=0`, `fundamentals_count=0` despite `FINNHUB_API_KEY` + `FRED_API_KEY` configured. Either keys aren't read, ticker lists are wrong, or adapters silently fail. | `argosy diagnose adapters` reports OK for Finnhub + FRED; a fresh synth run shows `news_count > 0` and `fundamentals_count > 0`. |
| **T1.3** | W3b.C RSU grants → structured `identity.rsu_grants` | 1-2 hrs | TaxAnalyst's Section 102 reasoning (FM's #2 RED concern from run #19) needs structured grant info. Today it's free-text in YAML. | `identity.rsu_grants[]` populated with `{award_id, award_date, shares_outstanding, vest_schedule}`. |
| **T1.4** | O6 intake_extractor prompt tweak | 30 min | Same root cause as T1.3 — handover gap #1. Tweak prompt so intake writes into `rsu_grants:` directly when processing RSU portal screenshots. | Next intake turn that processes RSU data writes the structured field. |
| **T1.5** | W3b.A portfolio_snapshots persistence table | 2-3 hrs | Removes filesystem-walk fragility. T1.1 reads from this once it exists. Migration 0029 (or next). | `portfolio_snapshots` table created via migration; writer fires on TSV ingest; `/api/portfolio/snapshot` reads from DB. |
| **T1.6** | W3b.B tax lots + fills from Schwab CSV | 4-8 hrs | TaxAnalyst's `lots_summary` and `dividends_summary` payloads are empty without this. FM's "tax-net proceeds unquantified" objection (#4 from run #19) sits here. | Schwab Equity Awards CSV → `lots` table; sells reconciled to `fills`; TaxAnalyst gets non-empty `lots_summary` payload. |
| **T1.7** | EX3 HouseholdBudgetAnalystAgent | 3-5 hrs | Synth Phase 1 #10 analyst per SDD §6.11. Feeds cash-flow context (monthly burn, RSU/salary income, safe-withdrawal headroom) to the synthesizer. | New agent class; wired into `_run_phase_1_analysts`; emits `monthly_burn_nis`, `income_streams_summary` per SDD §18. |

**Order (within Tier 1):** T1.2 first (cheap, may unblock the others) → T1.1 → T1.4 → T1.3 → T1.5 → T1.7 → T1.6.

**Tier 1 exit gate:** synthesis re-run with FM **approval**, OR FM rejection for substantively different reasons than runs #19/#20 (e.g. drawdown-stop missing, FX confidence — reasoning-level concerns rather than data-integrity ones).

## Tier 2 — Synthesis hardening

| # | Item | Effort |
|---|---|---|
| T2.1 | W2.B cost cap ($10/run, $30/day) | 1-2 hrs |
| T2.2 | W2.D startup orphan-row sweep | 30 min |
| T2.3 | W4.A per-phase persistence + resume endpoint | 6-10 hrs |
| T2.4 | W4.B retry-from-phase-N button on cascade panel | 1-2 hrs |
| T2.5 | W4.C WS `ws.send_failed` storm fix | 30 min - 2 hrs |

## Tier 3 — Adapter coverage

| # | Item | Effort |
|---|---|---|
| T3.1 | SEC 13F endpoint 404 — switch to working EDGAR FTS path | 1-2 hrs |
| T3.2 | TipRanks 403 — accept-as-failed OR Finnhub social proxy | 1-2 hrs |

## Tier 4 — Product surface

| # | Item | Effort |
|---|---|---|
| T4.1 | W5.B per-position thesis cards (Hold/Buy/Sell + conviction + reasoning per holding; "should add" cards for missing-but-recommended) | 4-6 hrs |
| T4.2 | P2 speculative-candidates polish on /proposals | 2-3 hrs |
| T4.3 | Deeper push back — per-delta re-evaluation flow (slim debate with user pushback as guidance) | 3-4 hrs |
| T4.4 | Decision-replay rows for push back / Decide / reject (new `decision_kind` values; shallow /decisions/{id} view) | 2-3 hrs |
| T4.5 | Daily-brief loop in production (Phase 2 goal) | 2-3 hrs |

## Tier 5 — Anomaly + hygiene

| # | Item | Effort |
|---|---|---|
| T5.1 | EX2 anomaly-detection agent + advisor surface + Card 2923 fee-waiver monitor | 3-4 hrs |
| T5.2 | O4 DecisionAccordion empty-array fallback (15 min handover fix) | 15 min |
| T5.3 | O5 TLH constraint surfacing in advisor (user-driven, no code) | — |
| T5.4 | Frontend test scaffold (vitest + first useDecisionStream test) | 1-2 hrs |

## Tier 6 — Trading core MANUAL (Phase 4)

Human-approved live trading. NOT autonomous. Sequence:

| # | Item | Effort |
|---|---|---|
| T6.1 | IBKR adapter — read path (positions + fills sync from broker) | 4-6 hrs |
| T6.2 | IBKR adapter — write path (place + cancel orders) | 4-6 hrs |
| T6.3 | Risk preflight wired to broker submission | 3-5 hrs |
| T6.4 | Email approval channel for T1 | 3-4 hrs |
| T6.5 | 1-click approve on /proposals pivots to broker when account != "paper" | 2-3 hrs |
| Gate | 5+ small live trades via 1-click without surprises | — |

## Tier 7 — Productization (Phase 6)

Multi-tenant, billing, hosted deploy. Sits BETWEEN manual live and autonomous — second tenant can use paper / manual-live before Phase 5 unlocks on Ariel's own account.

| # | Item |
|---|---|
| T7.1 | Multi-tenant infrastructure |
| T7.2 | License / billing |
| T7.3 | Hosted deploy |
| Gate | Second tenant onboarded end-to-end |

## LAST — Autonomous live trading (Phase 5)

Limited account autonomy with T0/T1 auto-execution. Hard SDD gate: 4-week autonomous soak in limited account with no kill-switch trips.

## Verification checkpoints

- After Tier 1: synthesis re-run; check FM verdict.
- After Tier 2: simulate phase-3 crash → resume → verify only remaining phases cost.
- After Tier 3: re-run synthesis; verify news + fundamentals + sentiment payloads populated.
- After Tier 4: paper-mode usability check on /plan + /positions + /proposals.
- After Tier 5: daily-brief lands fresh in the morning; anomaly detection catches a synthetic case.
- After Tier 6: 5+ live trades via 1-click without surprises (SDD Phase-4 gate).
- After Tier 7: second tenant runs end-to-end in paper mode (SDD Phase-6 gate).
- After LAST: 4-week autonomous soak passes (SDD Phase-5 gate).

## Out-of-band: live synthesis #20

In flight at start of this plan. Likely to reject for the same reasons as #19 (data gaps in ConcentrationAnalyst, News, Macro, Fundamentals). Used as the empirical motivator for Tier 1 ordering.
