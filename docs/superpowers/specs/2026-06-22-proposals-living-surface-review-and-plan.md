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

## D3. Observability & audit (REQUIRED — Ariel, 2026-06-22)

The daily funnel is autonomous, so it CANNOT be a black box. Every run must be fully logged and
replayable — for Ariel to SEE how it worked and for us to DEBUG it.

**Per-run trace (persisted, one record per daily run + per-stage rows):**
- `funnel_run`: run_id, started_at/finished_at, status, totals (candidates in/out per stage,
  tokens, cost, wall-clock).
- Stage 0: the macro read + the exact inputs it used (with source refs: which news ids, VIX
  value, indices, sentiment rows).
- Stage 1: every name considered → routed or DROPPED, with the SIGNAL/RULE that fired (or "no
  match"). Nothing drops silently.
- Stage 2: each candidate's triage verdict + the cheap-model rationale + tokens.
- Stage 3: each deep decision — the fleet transcript ref, the proposal, tokens/cost.
- Surface routing: what went to needs-me-now vs the transparency view vs nothing, and why.
Each row records inputs (source-cited), decision, REASON, model used, tokens, duration — so a
human can answer "why did it (not) act on X today?" without re-running.

**Two views off the same trace:**
1. Client "how it worked" (plain-language, in the transparency section): "Scanned the market
   (semis −3%, risk-off) → flagged NVDA + the index sleeve → reviewed NVDA deeply → proposed a
   trim. 48 names: no action." 
2. Debug/trace (full per-stage detail incl. dropped names + reasons + model/tokens) behind an
   endpoint (e.g. `/api/decisions/funnel/runs/{id}`), reusing the `job_runs` accounting +
   structured-log pattern + predictions ledger.

Build on existing infra: structured JSON logging, `job_runs` (cadence accounting), the
predictions ledger (per-call outcomes), monitor_flags. Do NOT invent a parallel logging stack.
Acceptance: for any day, Ariel can open the run and trace every name from "considered" to
"acted / dropped" with the reason and the model that decided it.

## D4. Codex design review (2026-06-22) — INCORPORATED

Verdict: **"Build it, but build it as a conservative escalation system, not a daily recommender."**
Sound to build; the hardening below is mandatory. Top-3-first: (1) deterministic Stage-1 policy,
(2) immutable decision snapshots, (3) shadow mode + kill switch + proposal expiry from day one.

Design changes (override the lighter descriptions above):
- **Stage 1 is a DETERMINISTIC, thresholded policy** — not just a cheap LLM. Explicit
  exposure/sector map (signal → affected holdings), materiality thresholds, per-name COOLDOWNS,
  HARD-TRIGGER bypasses (earnings, big price move, thesis break, drift band breach), default
  NO-OP, and a periodic **random audit of Stage-1 DROPS** to catch false-drops. A name must EARN
  a deep review.
- **Stage 3 is PROPOSE-AND-ASK, never auto-act** for discretionary Buy/Sell/Trim (real money,
  long-hold). Auto-action allowed ONLY for pre-authorized mechanical rules (idle-cash → T-bill
  sweep, rebalance within explicit bands, TLH under constraints, user standing orders).
- **Cadence is escalation, not uniform daily:** daily cheap scan (Stage 0–2); Stage 3 SPARSE
  (hard triggers / high materiality only); weekly portfolio-level review; monthly/quarterly plan
  alignment; event-driven forced reviews (earnings). Mitigate cheap-stage false-drops with
  hard-trigger bypasses, primary-source priority, price-move backstops, periodic full-portfolio
  sweeps, and miss-tracking.
- **Hold has its own discipline** — "do nothing" is a real decision, not a weak Buy/Sell.
- **Portfolio-level guard:** single-name decisions must not fight the total target allocation —
  reconcile against the canonical plan/IPS before surfacing.
- **Tax-lot-aware sells:** account type, cost basis, lots, long/short, wash-sale — required on
  any SELL/TRIM (ties to the §102 + cash-source reconcilers already built).

Observability (extends D3) — capture IMMUTABLE per-decision snapshots, else "why did it (not)
act on X?" is unanswerable: model name/version + prompt-template hash + temperature/seed; full
model inputs or immutable refs; source + fetch timestamps; the EXACT portfolio snapshot
(holdings, cost basis, tax lots, cash, prices, FX, account); the EXACT market snapshot
(price/quote time, benchmarks); the decision-POLICY version (thresholds/cooldowns/routing);
dedup key + "unchanged" explanation; why-not-act for drops; post-decision execution DRIFT;
human action state (proposed/accepted/rejected/expired/superseded).

**P0 prerequisites (build FIRST, before any live proposal):**
- **Shadow mode:** run the funnel, record proposals, but surface NOTHING — compare against
  actual outcomes + Ariel's manual decisions for a calibration period.
- **Backtest/replay harness** over historical monitor flags + portfolio snapshots.
- **Kill switch:** disable proposals / Stage 3 / any auto-action instantly.
- **Proposal expiry:** recommendations go stale on price/news/portfolio drift.
- **IPS (Investment Policy Statement):** target risk, max concentration, sell discipline,
  retirement horizon, tax priorities — the policy Stage 1/3 reason against (derive from the
  canonical plan).
- **User-feedback loop:** accepted/rejected/ignored tunes thresholds + cooldowns over time.

## F. Next-session start point

Per the codex review, build as a conservative escalation system in this order:
1. **P0 FIRST:** decision-trace/snapshot logging (D3+D4 observability), shadow mode, kill switch,
   proposal expiry, and the IPS derived from the canonical plan. No live proposal until shadow
   mode has calibrated against Ariel's real decisions.
2. **P1 Stage 0 + deterministic Stage 1** (market review + thresholded exposure-map routing with
   hard triggers / cooldowns / default-NO-OP) — the spine. Stages 2–3 reuse the existing
   estimator / fleet / `decisions/flow.py`; Stage 3 is propose-and-ask only.
3. **P2 compaction + transparency view** (frontend), in parallel.
Use codex-tandem for the Stage-1 policy + sizing/tax-lot math. Backend on :8000; plan pv62
(2026-06-20); migrations 0074. Prior /proposals + payslip work shipped to master (through
53763df; this plan ebc1b3b onward).
