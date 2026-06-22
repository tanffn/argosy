# /proposals — living-surface review & plan (handover)

**Date:** 2026-06-22. **Mindset:** Argosy is Ariel's back office. The client surface shows
ONLY what needs him (info to provide, or a decision/action to take). Internal agent Q&A —
including self-verifications that pass — stays internal. North star: maximize finances +
earliest safe retirement; **"Proposals = daily actions via a per-stock + market-sentiment
agent."** (see auto-memory: `feedback_client_in_loop_only_when_needed`, `feedback_four_surface_roles`,
`feedback_argosy_prime_directive`).

## A. What runs today (operations ground truth)

DAILY + LIVE (right agents firing): `thesis_monitor` 09:00 (Opus, per-stock news+price+Form4),
`news_daily` 17:00 (Opus signal analyst), `state_observer` 17:00 (Opus portfolio-state flags),
`alpha_report_analyst` 18:00 (Opus Discord sentiment), `inferred_life_event_detector` 03:00,
`payslip_ingest` 06:30, `discovery_funnel` ~24h, `predictions_evaluator` 03:30. Quarterly:
`holistic_rebalance_review`. Monthly: `monthly_cycle` 1st 08:00 → plan synthesis (Opus ×6).
Deploy-your-cash + allocation = recomputed live per request. Current plan = pv62, synthesized
2026-06-20 (fresh now, but only because of a manual re-synth this session).

## B. Per-section freshness + client-need verdict (the review)

| Section | Fresh? | Needs the client? | Verdict |
|---|---|---|---|
| What's on you to do (action items) | plan-derived → **monthly/on-synth**, not daily | yes (real to-dos) | Keep, but **doesn't react to daily data** between syntheses; only withholding is mindset-filtered |
| Deploy your cash (detect + buy list + §102 context) | **LIVE** | yes | Good — unified + canonical |
| Ask the team (consult) | on-demand tool | client-initiated | Fine as a tool |
| High-potential discovery + Raw sourcing | daily signals | **NO** — internal research | Candidate to demote off the client surface |
| Trade-proposal queue (Buy/Sell/Hold) | **on-demand ONLY** | yes when present | **Core gap** — no daily generation; empty unless user consults manually |
| Run portfolio review (rebalance) | on-demand tool | client-initiated | Fine as a tool |
| Action proposals (system observations) | **LIVE daily** | mixed (some notes) | Filter to client-needs only (note_only = internal) |

## C. The north-star gap (Ariel's fear, confirmed)

Argosy's **monitoring half is strong** (daily Opus agents flag risks). The **acting half is
missing**: nothing converts a fresh signal (thesis break, sentiment, drift) into a fresh,
client-ready **Buy/Sell/Hold** proposal. Specifically:

1. **No daily per-stock decision agent.** Trade proposals only exist if Ariel manually runs
   `/consult`. The north-star "daily per-stock + sentiment agent" does not exist. (`argosy/decisions/flow.py` runs only via `POST /api/decisions/run`.)
2. **Thesis breaks → flags, not trades.** `thesis_monitor`/`state_observer` write *action
   proposals* (notes/nudges), never a *trade* proposal. The client sees "NVDA thesis weakened"
   but no "therefore: trim N shares" decision.
3. **Plan reacts only monthly.** Action items derive from the monthly plan; a material mid-month
   change doesn't refresh them (only `monthly_cycle` or manual synth).
4. **Proposals are terminal.** An open proposal isn't re-evaluated daily as facts change.
5. **Surface still leaks internal work** (research panels, note-only observations) onto the client.

## D. Plan (phased)

**P1 — Daily decision agent (the core fix).** New daily loop (after the 17:00/18:00 monitors)
that, for each ticker with a fresh trigger that day (thesis flag ≥ warning, material
alpha_report sentiment, allocation drift), fires the decision flow (analysts → bull/bear →
trader → 3 risk → FM) and writes/refreshes a Buy/Sell/Hold proposal. Store with a daily
re-eval; surface **only NEW/CHANGED** recommendations needing the client. This makes
/proposals the daily-actions surface the north star describes. (Scope/cost fork → see questions.)

**P2 — Client-surface compaction.** Restructure /proposals to "what needs you now": fresh
decisions + real actions on top; demote internal research (discovery/raw-sourcing) and
note-only observations off the client surface (they feed proposals, not the client). Apply the
`feedback_client_in_loop_only_when_needed` filter everywhere (self-resolved → hidden).

**P3 — Plan freshness.** Trigger an inter-cycle near-term refresh on material flag escalation
(so action items react to daily reality), or at minimum show plan age + a "refresh" affordance.

**P4 — Proposal lifecycle + closed loop.** Daily re-evaluate open proposals (TTL/re-check);
generalize the payslip closed-loop expectation layer (mark-done → expect evidence in next
report → confirm/resurface) across action types.

**P5 — Verify + north-star check.** Each surfaced item must trace to the prime directive; an
adversarial pass that asks "does the client actually need this, today, to retire sooner/safer?"

## E. Open questions for Ariel (answered below once reviewed)

1. Daily decision-agent **scope** (cost vs coverage): all holdings+watchlist daily / only
   triggered tickers / rolling cadence + on-trigger.
2. **Discovery/research panels** on the client surface: remove (feed proposals silently) / keep collapsed.
3. **Plan freshness**: refresh near-term on material change / monthly+on-demand is fine (just show age).
4. **/proposals north star** confirmation: "only what needs you now; everything Argosy handles is hidden."

## F. Next-session start point

Begin P1 (daily decision agent) per Ariel's scope answer; it's the keystone. P2 compaction can
proceed in parallel (frontend). Use codex-tandem for the decision-loop trigger logic + any
money-affecting sizing. Backend running on :8000; current plan pv62 (2026-06-20). Migrations at
0074. All prior /proposals + payslip work shipped to master (through commit 53763df).
