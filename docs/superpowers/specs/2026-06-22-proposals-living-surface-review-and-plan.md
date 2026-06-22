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

## D. Ariel's decisions (2026-06-22) — build to these

1. **Daily agent = a SMART TIERED FUNNEL, not brute force.** "Agent does a review of the market —
   news, VIX, etc → does it apply to an ETF/index, does it apply to stock X or Y → run
   preliminary analysis, run deep analysis as needed. Be smart, work like the agency we are
   building." → Top-down macro→relevance→triage→deep escalation (mirror the existing
   `discovery_funnel`: radar → quick estimator (Sonnet) → fleet grader (Opus)).
2. **Discovery/raw-sourcing:** KEEP, collapsed/opt-in (don't remove).
3. **Plan freshness:** refresh near-term (short-horizon) actions on MATERIAL change (flag escalation),
   not just monthly.
4. **/proposals shape:** "needs me now" up top + a COLLAPSED "what Argosy did for me" transparency/
   audit section (self-resolved work hidden-by-default but auditable on demand).

## D2. Plan (phased, decisions baked in)

**P1 — Daily decision FUNNEL (keystone).** A daily loop (after the 17:00/18:00 monitors) that
works like the agency, in escalating tiers — cheap-first, deep-only-where-it-matters:
  - **Stage 0 — Market review (macro, 1 cheap pass):** ingest/scan the day's market context —
    news (already via `news_daily`), VIX/volatility, major indices, rates, broad sentiment
    (alpha_report). Produce a compact "what moved + why" macro read.
  - **Stage 1 — Relevance routing (deterministic + cheap LLM):** map the macro read + per-name
    signals (thesis flags, Form 4, single-name news) onto the actual book. Decide *what could be
    affected*: which ETF/index sleeves (broad-market moves) and which single names (X, Y). Most
    of the book is untouched on a normal day → routes to nothing.
  - **Stage 2 — Preliminary triage (Sonnet quick-estimator):** for each candidate Stage 1 surfaced,
    a cheap pass: "does this warrant a real decision today?" Kill the no-ops here.
  - **Stage 3 — Deep decision (Opus full fleet) ONLY for survivors:** analysts → bull/bear →
    trader → 3 risk → FM → a fresh Buy/Sell/Hold proposal (reuse `decisions/flow.py`).
  - Store proposals with a daily re-eval; surface **only NEW/CHANGED** recommendations. Reuse the
    `discovery_funnel` triage architecture (radar→estimator→grader) as the template; reuse the
    estimator/grader model tiers. Use codex-tandem for the routing + any sizing math.

**P2 — Client-surface compaction (frontend).** "Needs me now" up top (fresh decisions + real
actions). Discovery/raw-sourcing → collapsed/opt-in (per decision #2). Add a collapsed
**"What Argosy did for me"** transparency section (per decision #4) that lists self-resolved
work (e.g. the reconciled withholding verdict, monitor checks that passed) — auditable, not
pushed. Apply `feedback_client_in_loop_only_when_needed` everywhere (self-resolved → hidden from
the active list, shown only in the transparency section).

**P3 — Plan freshness (per decision #3).** On material flag escalation (thesis break, big drift,
life event), refresh the plan's short-horizon actions so the to-do reflects today — not the
1st-of-month snapshot. Show plan age regardless.

**P4 — Proposal lifecycle + closed loop.** Daily re-evaluate open proposals (TTL/re-check, resurface
only on change); generalize the payslip closed-loop expectation layer (mark-done → expect
evidence in next report → confirm/resurface) across action types.

**P5 — Verify + north-star check.** Each surfaced item must trace to the prime directive; an
adversarial pass: "does the client actually need this, today, to retire sooner/safer?"

## F. Next-session start point

Start with **P1 Stage 0+1** (market review + relevance routing) — that's the new spine; Stages
2-3 reuse the existing estimator/fleet/decision code. P2 compaction (incl. the transparency
section) can run in parallel on the frontend. Use codex-tandem for P1 routing + sizing.
Backend on :8000; current plan pv62 (2026-06-20); migrations at 0074. All prior /proposals +
payslip work shipped to master (through commit 53763df; this plan at ebc1b3b).
