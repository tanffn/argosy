# /proposals → an action inbox — redesign plan (for review)

**Date:** 2026-06-22. **Status:** PLAN ONLY — not yet implemented (owner: build later).
**Mindset:** Argosy is the back office. `/proposals` exists to answer ONE question
at a glance: **"what, if anything, needs me right now?"** Everything else
(research, tools, audit, system mechanics) is secondary and collapsed.

Reviewed with codex (design review, 2026-06-22). Owner decisions baked in
(see §Decisions).

## The problem (today)

Action items are scattered across ~5 positions on the page — the read-only
"What's on you to do" checklist, the deploy-cash section, the trade-proposal
list, the speculative cards, and the "Things Argosy noticed" notes — and they
are interleaved with Tools (Ask the team, Run a portfolio review), Explore
(discovery, raw sourcing), and Audit (the funnel transparency view, reasoning
trails). To be *sure* nothing needs them, the user must scan the whole page.
There is no single "this is what's on you" queue, and there are 4–5 competing
"lists". A back-office inbox must never make the user hunt.

## The target — four zones, in order

1. **Needs you now** (always open, top) — the only thing the user must look at.
   ONE prioritized queue. Empty most days → a confident quiet state.
2. **What Argosy did for me** (collapsed) — audit / transparency (the daily
   decision-funnel narrative + self-resolved work).
3. **Tools** (collapsed) — client-initiated commands: Ask the team (consult),
   Run a portfolio review (rebalance). Not queue items.
4. **Explore** (collapsed, opt-in) — high-potential discovery + raw sourcing.
   Research the user can browse — but its real job is to FEED the queue (see
   §Discovery drives proposals).

### The unification principle (codex)

Unify the **attention contract, not the workflow mechanics.** Every queue item
shares one envelope:
- plain-language title,
- one-line **why now**,
- a primary action + **Defer** + **Dismiss/Reject** (where applicable),
- an **expander** for the full reasoning/details.

Inside that envelope each item keeps its own body:
- a **trade proposal** → approve / reject / ask-for-deeper-review / execute + reasoning trail (inline);
- a **cash deployment** → ONE item ("Deploy $X idle cash above target") that
  expands into the tiered buy-list + amount input (do NOT put every buy-list
  line in the global queue unless each line is independently decision-worthy);
- a **plan to-do** → mark-done / how-to;
- a **system note** → acknowledge / defer / dismiss.

The user should never wonder "did I miss another action lower on the page?"

### Prioritisation — legible, not a mystery score

Order the queue by an explicit, explainable policy:
1. **Overdue / expiring / blocking** — tax deadlines, plan inputs, trades about
   to expire, execution blockers.
2. **Risk-reduction decisions** — sell / rebalance / concentration / drift where
   inaction has downside.
3. **Material plan commitments** — dated plan to-dos, required info.
4. **Material cash deployment** — idle cash above the plan band, by amount / drag.
5. **Opportunity / speculative** — only if they truly require a decision.
6. **Low-risk observations** — only if material; else → audit/history.

Signals Argosy already has: due/overdue days + expiry, dollars affected, % of
net worth, risk tier + downside of inaction, drift from target, tax-window
sensitivity, confidence/consensus, reversibility, whether other work is blocked.
**Each item states its rank reason locally** ("Top: overdue 3 days, affects
$84k"). Trust comes from visible reasons.

### Empty / steady state

Most days, nothing needs the user. Open with a confident quiet state, not a
dead screen:

> You're all caught up. Argosy is watching; nothing needs you.

Plus small liveness signals: `0 pending decisions`, `Last checked: today 08:40`,
`Next review: Jun 24`, `Cash within target band`, `No overdue plan tasks`,
`No open approvals`. Then collapsed links to Audit / Tools. **Do not** fill the
empty state with discovery content — that turns "nothing needs you" into "go
browse ideas" and undermines the trust contract.

## Decisions (owner, 2026-06-22)

1. **Scope:** plan only for now — implement later.
2. **Checklist:** REMOVE the read-only "What's on you to do" checklist. Fold its
   dated plan to-dos into the unified queue as first-class action items (a
   read-only list that sends the user hunting elsewhere is bad IA).
3. **Discovery:** keep the discovery/raw-sourcing panel COLLAPSED on /proposals,
   AND make discovery DRIVE proposals (see next).

## Discovery drives proposals (owner insight)

High-potential discovery is not just browsable research — its output should
become **actionable proposals** in the queue. Concretely, connect the two
funnels already built:
- the **discovery funnel** (radar → Sonnet estimator → Opus grader →
  conviction picks, `argosy/services/high_potential_funnel.py`) surfaces
  candidate NEW names;
- the **decision funnel** (`argosy/services/decision_funnel/`) currently routes
  only HELD names. Extend Stage 1 to also take high-conviction discovery picks
  as candidates → Stage 2 triage → Stage 3 deep decision → a **BUY proposal**
  for a new name, sized against deployable cash (deploy-cash advisor).

So the Explore panel stays collapsed as the "where did this idea come from"
browse/audit, while its high-conviction output flows into "Needs you now" as a
proper, sized, propose-and-ask BUY — subject to the same shadow-mode + IPS +
north-star + estate (UCITS-preferred) guards as held-name proposals.

## Funding-aware proposals + sell-to-fund switch (owner, 2026-06-22)

Discovery runs **every weekday** and its conviction picks feed the daily
decision funnel (above). Crucially, every BUY the funnel produces — held-name
add OR discovery-driven new name — must be **FUNDED**, checked against the
user's **settled, available-now cash** ("Cash to allocate"), not the nominal
balance. The funding step:

1. **Cash covers it** → normal Buy, sized to available cash.
2. **Cash short, but the opportunity clears a higher bar** → a **SWITCH**
   proposal: "Sell X (~$Z) → Buy Y", presented as ONE paired decision (not two
   unlinked items). Fires only when Y's conviction MATERIALLY exceeds the
   funding source's (a "fantastic deal" threshold, not a routine swap), and
   only when net-positive after tax + transaction friction.
3. **Cash short, nothing worth selling** → NO action invented; surface the idea
   honestly ("great idea, but no funding without a sale you'd regret"), or hold
   it until cash arrives.

**Funding-source (X) selection — deterministic policy + fleet judgement:**
- Eligible: liquid, **fast-settling** (standard ETF/equity, ~T+1–T+2 → cash in
  days), lowest-conviction / most-overweight-vs-target / weakest-thesis-fit, and
  tax-sensible to trim.
- **NVDA is INELIGIBLE as a funding source** — RSU/NVDA-sale proceeds take
  **~2–3 weeks to settle**, so an NVDA sale cannot fund a buy "now". (NVDA also
  has its own managed deconcentration glide; opportunistic funding must not
  borrow from it.)
- More generally, model cash by **SETTLEMENT DATE, not nominal balance**:
  "Cash to allocate" = settled + available today; pending RSU/sale proceeds are
  tracked separately with their arrival date. A time-sensitive deal can only be
  funded from sources that settle in time; otherwise surface it as a
  "buy once your pending proceeds settle (~<date>)" scheduled item.

This makes the inbox's BUY items honest: each is either funded from cash or
carries its own funding (the paired sell), and the funnel never proposes a buy
it can't pay for.

## Telemetry + debug visibility (owner, 2026-06-22) — REQUIRED for ALL of the above

Everything the funnel does must be recorded in the existing trace
(`funnel_runs` / `funnel_stage_rows` / immutable `decision_snapshots`) and be
visually inspectable for debugging under the **Decisions tab**
(`/decisions/funnel` list → `/decisions/funnel/{id}` full per-stage trace +
snapshots; built 2026-06-22 on the already-shipped `/api/decisions/funnel/runs`
endpoints). As new behaviours land they MUST emit into the same trace — no
silent paths:
- **Discovery-driven candidates** → a Stage-1 row with the source marked
  (e.g. `signal_or_rule="discovery_pick"`, the conviction + grader ref in
  `inputs_json`), so a new-name idea is traceable from radar → proposal.
- **Funding check** → recorded on the deep-decision snapshot / stage row: the
  settled cash considered, the funding outcome (cash / switch / unfundable),
  and — for a switch — the funding-source candidates evaluated + why X was
  chosen (and why NVDA/others were excluded).
- **Sell-to-fund switch** → the paired (sell X, buy Y) recorded as ONE linked
  decision with both legs, tax/friction math, and the conviction delta that
  cleared the bar; both proposal ids on the snapshot.
- **Settlement model** → the as-of settled-cash figure + any pending proceeds
  (amount + arrival date) captured in the snapshot's portfolio state, so "why
  did it (not) fund this?" is answerable after the fact.
Acceptance: for any run, the Decisions-tab debug view shows every name from
"considered" → "acted / dropped / unfunded" with the reason, the model, the
funding decision, and the immutable inputs — no re-run needed.

## Removals / demotions (not just collapse)

- "Escalate tier" → **"Ask for deeper review"** (the current label reads as
  system mechanics).
- Reasoning-trail as a page-level section → **inline** per item (expander).
- Discovery/raw-sourcing → out of the active flow (collapsed Explore, feeding
  the queue per above).
- The funnel transparency view → **below** the queue (it's audit, not action).
- Deploy-cash always-open mid-page section → a single queue item, shown only
  when there is material idle cash.

## Ranked changes

**Clear wins (low risk):**
1. One **Needs you now** section at the top with a count + the quiet empty state.
2. Move all active trade proposals + (folded) plan to-dos + the material
   cash prompt + material notes into that first section; order by the policy above.
3. Move Audit, Tools, Explore below it, collapsed.
4. Remove the read-only checklist (decision #2); its items become queue items.
5. Gate deploy-cash visibility by materiality; collapse to one queue item.
6. Group "Ask the team" + "Run a portfolio review" into a collapsed Tools drawer.
7. Reasoning trails → per-item expander.
8. "Escalate tier" → "Ask for deeper review".
9. Empty/quiet state above all secondary zones; liveness signals.

**Bigger restructures (need a second pass / more wiring):**
1. A real **action-inbox abstraction** spanning trades, plan tasks, cash
   deployment, discovery-driven buys, and notes — one queue, typed bodies.
2. The explicit **priority policy** with thresholds (overdue, $ affected, drift,
   risk, cash drag, confidence) + per-item "why ranked".
3. Wire **discovery → decision-funnel candidate source** (§Discovery drives
   proposals), running every weekday, so new-name BUYs enter the queue.
4. **Funding-aware proposals + sell-to-fund switch** (§Funding-aware): a funding
   step on every BUY against settled cash; a paired "Sell X → Buy Y" when cash
   is short and the deal clears the bar; settlement-date cash model with NVDA
   excluded as a funding source (~2–3wk RSU settlement). Reuses the deploy-cash
   advisor + tax-lot reconcilers already built.
5. Consider renaming `/proposals` → `/actions` or `/inbox` (open question).
6. A proper **audit/history** surface for funnel transparency, reasoning trails,
   resolved/expired proposals, and self-verifications.

## Open question for the owner

- Rename `/proposals` → `/actions` (or `/inbox`)? The IA is an inbox now; the
  name still says "proposals". Low effort, but it's a naming/identity call.
  RESOLVED below.

## Build decision (codex-reviewed, 2026-06-22) — backend-first, route = /inbox

Owner asked to "think with codex what's the best way; if you rewrite, do it
right." Codex design review (full verdict archived in the session). Decisions:

- **Route name: `/inbox`.** `/proposals` is too narrow, `/actions` too generic;
  `/inbox` matches the contract ("what needs me now?"). Keep `/proposals` as a
  client-side legacy shim that replaces to `/inbox` preserving
  `window.location.search + hash` (fragments never reach the server, so no
  hash-specific server redirect). Anchors `#deploy-cash` / `#allocation` move to
  the new page unchanged.

- **The queue lives in the BACKEND. New `GET /api/inbox`.** This is the crux and
  the one place we DIVERGE from this spec's "clear wins = client reorg first"
  ordering. Reordering the 1023-line client page first just makes a prettier
  version of the same problem: the browser deciding what matters. Ranking,
  dedupe, materiality, shadow suppression, funding eligibility, and "needs user
  now" are DOMAIN decisions, not presentation. An `InboxService` adapts the
  canonical sources (trades, action-notes, plan tasks, cash-deploy prompt; later
  discovery-buys + switches) into ONE typed, server-ranked feed. The UI receives
  an ordered list and renders it; it may branch on `item.kind` to pick a body
  component but computes NO membership/rank/materiality/reason.

- **Priority policy = new `argosy/services/inbox/policy.py`** (NOT
  `decision_funnel/policy.py` — the funnel decides investment action; the inbox
  decides human attention order across investments/tasks/cash/notes/blockers).
  Versioned + content-hashed like the funnel policy. Emits per item: bucket,
  stable sort key, server-computed plain-English `rank_reason`, policy version,
  source/trace refs.

- **Typed envelope = discriminated union** `InboxItem` with kinds
  `trade | cash_deploy | plan_task | note | discovery_buy | switch`; shared
  envelope (title · why_now · rank_reason · primary/secondary semantic actions ·
  trace refs · source refs · typed body). `InboxAction` is semantic
  (`intent`/`label`/`style`), never a raw API path. No `T2`/`account_class`/
  proposal-ids/raw-statuses/"escalate tier" in visible copy.

- **Lowest-regret build order** (codex):
  1. Types + rank-reason contract + policy tests.
  2. `InboxService` + `GET /api/inbox` over TODAY's sources (trades, notes, plan
     tasks, cash detector/deploy).
  3. Server ranking + dedupe + materiality gating + quiet-state metadata + debug
     output (dropped/suppressed visible in a debug view).
  4. `/inbox` UI as a pure projection; demote Audit/Tools/Explore below.
  5. Preserve existing flows via semantic handlers (approve/reject/defer/dismiss/
     execute/mark-done/review-cash); current affordances (fills, reasoning trail,
     consult, rebalance, windfall/allocation context, discovery browse) demote
     into item expanders / Audit / Tools / Explore — none lost.
  6. Rename route + legacy `/proposals` shim.
  7. THEN wire discovery → decision funnel (shadow).
  8. THEN funding-aware buys + sell-to-fund switch (shadow + trace-first;
     settlement-date cash, tax lots, friction, NVDA excluded). Money math goes
     through codex-tandem.

  Steps 1–6 = the shippable "inbox shell" over real sources. Steps 7–8 add new
  `kind`s into the already-built union; do NOT block the shell on them.

- **Top risks** (codex): funding/switch correctness (settlement+tax-lot+friction
  — shadow it); duplicate/competing items across sources (need stable source
  refs + dedupe); shadow-mode leakage (north-star surfacing gate stays
  server-side); lost current affordances (demote, don't delete); route/hash
  migration (preserve anchors + client-side hash-safe shim).
