# Handover — plan/execution separation: agreed solution + build list (2026-07-03)

Design principle now in **SDD §1.7**. This doc is the **build roadmap** (kept out of the
SDD body per docs-are-current-state). Agreed between the client, the fleet, and codex
(`tmp_review/solution/verdict.md`), triangulated with two prior codex reviews + a blind
adversarial plan review + a code-level architecture diagnosis (`tmp_review/`).

## The principle (agreed)
- **Plan = strategic IPS (5–30y), regime-ROBUST.** Bands, caps, tax/estate, structural
  diversifiers. Never rewired by a cyclical signal. Admits **secular** shifts only via a
  **governed refresh** (versioned assumptions + threshold tests + sign-off) — codex's key
  refinement: *secular is not a loophole for tactical drift*.
- **Execution = tactical (0–5y).** Maneuvers **within** the plan's bands on current
  conditions + transition state; owns live regime data; raises a **plan-revisit flag**
  but never rewrites the plan.
- **Portfolio physics enforced deterministically** (look-through single-name/factor
  exposure, caps, transition constraints); the fleet **authors**, determinism
  **verifies** (the pivot doctrine).

## Why this matters (the diagnosis it resolves)
The plan fleet "fell to a single codex prompt" not because one prompt was smarter, but
because: no agent owned regime-aware allocation; the fleet's macro input was 4 coarse
numbers; reviewers critiqued one shared manifest (correlated blind spots); the process
anchors to the prior plan; and the one uncorrelated runtime reviewer (codex Phase-4.5)
was scoped to *math* audit, not *allocation*. Most of what was flagged (US-growth tilt,
low FI) was **execution-transition contamination**, not plan defects — resolved by the
separation. Genuine plan-level items: the look-through cap + a structural real-assets
sleeve.

## Build list (codex-agreed, ranked by leverage)
1. **Shared look-through Risk/Constraint Kernel `[BUILD FIRST]`** — deterministic
   `EvaluateProposal(portfolio, trades, plan_policy, transition_policy, snapshot)` →
   approve/reject + violations + `max_allowed_buys_by_sleeve` + post-trade exposures.
   Computes direct+fund look-through single-name / Mag7 / factor / sector exposure. Every
   allocation author call runs through it before emitting trades. *Prevents a coherent
   plan becoming a bad trade list.*
2. **Execution Transition Policy Gate** — while NVDA look-through > ~25%, block additive
   US-growth buys; > final cap, require a defensive buffer + treat US mega-cap/core as
   partially-correlated; migration-aware FUSA (not additive over SCHD); route new cash to
   ex-US / short bonds / real-assets / low-vol.
3. **Plan Policy Schema** — separate steady-state policy (sleeves w/ min/target/max, roles,
   allowed instruments, domicile rules) + `hard_constraints` + `secular_assumptions`
   (versioned). A 13.2% growth sleeve target must NOT imply "buy R1GR today."
4. **Plan-Revisit Flag** — `{severity, category, evidence_refs, triggering_metrics,
   recommendation, blocks_execution}`; emitted by execution/reviewers, consumed by the
   plan fleet. Execution may raise, may not rewrite.
5. **Look-through single-name cap as a plan constraint** — `measurement:
   direct_plus_fund_lookthrough`, applies to current / post-trade / steady-state. (12%
   direct NVDA can still breach a 13% total cap once CSPX/R1GR/FUSA embedded NVDA counts.)
6. **Structural real-assets / inflation-hedge sleeve** — mandatory sleeve (~4–8%),
   TIPS/ILB + infra/REIT + optional gold. Gold NOT mandatory; the sleeve is.
7. **Verified Retrieval Snapshot** — sanctioned `RetrieveFacts` producing source-quoted,
   timestamped facts persisted into the decision packet (yields, spreads, valuations, ETF
   look-through, domicile, breakevens, FX, tax metadata).
8. **Retrieval Zigzag Verifier** — a second agent independently re-fetches/verifies quoted
   facts; no live-data-dependent allocation proceeds without a verified snapshot or an
   explicit stale-data waiver. (Reuses the existing zigzag machinery.)
9. **Decision Packet / Audit Store** — immutable record of inputs, facts, constraints,
   agent opinions, zigzag transcript, final decision, rejected alternatives — one source
   of market facts for replay.
10. **DO NOT BUILD: a regime-chasing plan owner.** No plan-level agent that changes
    strategic targets on current yields/VIX/spreads. Acceptable plan-regime logic =
    versioned secular assumptions + revisit flags + threshold/annual CMA refresh +
    structural sleeve definitions only.

**Highest-leverage first: #1 the shared look-through risk/transition gate.** Run
`EvaluateProposal()` before any author trades: block additive US-growth while NVDA
look-through is above the transition threshold, enforce the single-name cap on a
look-through basis, and require post-trade exposures in the output.

## Already shipped toward this (on master)
- Live **regime feed** (item 3): real yields / breakevens / IG-HY spreads / fed funds /
  10y — wired into the execution author (`market_snapshot.py`, `de2a8e9`). This is the
  input the plan fleet also lacks; reusable when #7/#3 land.
- Execution NVDA-avoidance fix + market-aware deploy (the author already tilts by regime
  and no longer starves US wholesale) — the seed of #2.
