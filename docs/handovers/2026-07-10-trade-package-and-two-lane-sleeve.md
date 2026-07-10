# Handover — 2026-07-10 · trade package settled, two-lane sleeve, discovery unblocked

**Branch:** `master` · **HEAD at writing:** `b3bf545`. Read this, then `git log --oneline -30`.
Prior: `docs/handovers/2026-07-09-corrective-marathon-and-tracking.md` (§7 resume session; §6 final close; §0 v73 accept).

**Memory (REQUIRED for non-Claude-Code agents — Claude Code auto-loads it):**
- Index: `C:\Users\ariel\.claude\projects\D--Projects-financial-advisor\memory\MEMORY.md` — one line per memory; read it FIRST, then open the linked files that matter for your task (same directory).
- Binding-behavior files to always read: `feedback_verdicts_defended_not_reopened.md`, `feedback_escalation_bar_fatal_forks_only.md`, `feedback_fleet_authors_determinism_verifies.md`, `feedback_argosy_prime_directive.md`, `feedback_output_trust_doctrine.md`, `feedback_adversarial_review_must_re_derive_blind.md`.
- Project instructions: `D:\Projects\financial-advisor\CLAUDE.md` (routes everything; binding preferences duplicated there for non-Claude agents); canonical design: `docs/design/SDD.md` (start at "Quickstart for new agents").

## 0. PLAN v76 CURRENT — three plan versions promoted this session, all gated accepts

- **v74** (`refinement-2026-07-09-160650`): gold retired → EXUS 18.3 (proposal 67). Verified: sums 100.00, no gold class, proposal executed. Label bug fixed (`389fcd7`): promoted refinement drafts drop the `-draft-` label.
- **v75** (`mandate-split-2026-07-10`): **Ariel chose Option B of proposal 71** — high-growth 5.0% split into **2.0% x10-moonshot lane** (mandate unchanged; RXRX/TEM/OKLO) + **3.0% market-beating-alpha lane** (sub-$100B, outperformance memo vs CSPX, must-justify-vs-IWQU, RKT/NOW-class admissible). Two-lane mandate in the class `rationale` (sleeve_mandate.py reads it — rendering verified) + structured `lanes` list; falsifiers inside. Script: `scripts/apply_growth_sleeve_split.py`.
- **v76** (`dry-powder-earmark-2026-07-10`): **proposal 69 applied** — `discovery_reserve` block on the cash class: 1.5% (~$59.5k), cash/T-bill-class ONLY (held SGOV / IB01 for new parking), replenish-first-from-staged-sells rule, deployment tooling must not treat it as idle cash. Script: `scripts/apply_dry_powder_earmark.py`. NOTE: nothing deterministic ENFORCES the earmark yet — deploy-cash must learn to read `discovery_reserve` (queued below).
- All three accepts carry the warn-only `section_coverage`/`evidence_per_section` gate warnings inherited from v73 prose — next critique round clears them.

## 1. The trade package (Ariel's decision set — where it stands)

**Awaiting Ariel's approve in /inbox:** sells **4 SPCX / 5 BRK.B $31k / 6 GOOG $21k / 7 AMD 100sh / 8 AMZN 67sh** (~$129k gross) + **row 70** (proceeds binding: parked dollars buy **IB01**, excess above the $226k cash-sleeve target buys **EXUS** ~$78k incl. RKT proceeds; big EXUS tranches ride the NVDA glide quota, 3,924 sh remaining 2026).
**Executed this session:** 67 (gold→EXUS), 68 (discovery gates), 69 (dry-powder), 71 (sleeve split). **Cancelled by the fleet's own re-derivations (with audit trails):** 2 (NOW sell — superseded by 10), 3 (CRM sell — run-167 HOLD), 10 (NOW $30k buy — blind re-derivation: 5-for-1 Dec-2025 split REAL, forward P/E ~25x not 21x, no sleeve fits a non-moonshot 1% slot; hold the 75 sh). **RKT (9):** cooling → sells at the 07-16 resurface — x10 eval verdict SELL-AS-PLANNED on its history (fails sub-$30B gate at ~$40B; 10x=$400B not credible; bull case 2-3x cyclical), loss harvest ~$14.4k vs AMD's ~$44k gain (~$3.6k saved), Israel wash-sale-free (ITA Circular 10/2025) so alpha-lane re-entry eval runs AFTER the sell. AMD was RESIZED to 100 shares (stale-price currency figure stranded a stub).
**External-reviewer episode:** their NOW direction was right (numbers wrong both sides), BRK.B claim settled by verification — facts confirmed (Abel CEO 2026-01-01, Apple+Alphabet ~30% of the equity book, ~$397B cash) but our correlation INFERENCE overstated; estate is the recorded primary sell basis; "BRK.B as productive ballast post-no-gold" queued as a plan question.

## 2. Machinery shipped this session (all committed, tests green)

- **Trade-plan overview** (`40e46e9`,`9aed49a`,`b0114e0`): /api/inbox `trade_plan` block + UI — sleeve-GROUPED table (+sleeve header w/ plan target, nested movement lines), cooling sells render dated, every buy (IB01/EXUS) is a real row, cash line = sleeve summary at target. Backend `argosy/services/inbox/trade_plan.py`.
- **Discovery gates** (`601f7c2`): conviction floor HIGH→MEDIUM (`decision_funnel/policy.py`), radar cap $8B→$30B (`trend_radar.py`); tests pin the MEDIUM default.
- **Discovery cron** (`d6ce4b5`): DiscoveryFunnelLoop interval→cron 16:00 Asia/Jerusalem (interval schedules re-anchor on restart — starved the loop to ONE lifetime run). Live-verified in the jobs registry.
- **Inbox note rendering** (`680feea`): decision-note rationale_md renders as markdown; why-now strips md tokens.
- **Retract-on-reversal discipline applied by hand 3×** (proposals 2/3/10) — the systemic fix is QUEUED (below).
- **Backend hosting**: runs DETACHED via Start-Process (survives session cleanup; logs tmp/uvicorn_detached.*.log). Session-tied background tasks kept getting killed. Proper service wrapper still queued.
- **Domain refresh LIVE on fresh backend** (`b3bf545`): first successful hourly tick post-restart; the stale-process citation failures (pre-aad2df4 process) are gone.
- **156-chain verify-run: PASS ×5** (roster/degradation/groundedness/verdicts/delivery); degraded_to_monolith was loudly logged, NOT silent; observations queued (expose degradation in a DTO; jobs.open_job_run_failed SQLite-lock during runs; next weekly critique must pick up graph plans rawlen=0).

## 3. BINDING DIRECTIVE (Ariel, 2026-07-10): verdicts are DEFENDED, not reopened

Memory `feedback_verdicts_defended_not_reopened`. Every verdict ships **verdict + conviction (mid explained) + falsifiers**; pushback runs a **new-facts test** first — no new fact hitting a falsifier → DEFEND, cite the derivation, don't re-run; new fact → blind re-derivation of affected inputs only, NEVER seeded with the challenger's framing; defers announced as "verdict stands, testing X". Root cause of the NOW churn: run 166 was seeded with Ariel's "maybe x2-3" framing + un-re-derived 21x forward P/E + no sleeve-fit question.

## 4. Next-session queue (priority order)

1. **Watch Ariel's approves land**: sells 4-8 + row 70; then the deploy flow sizes IB01/EXUS from SETTLED proceeds. RKT resurfaces 07-16 → sells → THEN alpha-lane re-entry eval (new mandate) + NOW 75-sh alpha-lane eval.
2. **Verdict registry + pushback gate (the §3 directive as machinery):** falsifiers in the deep-decision output schema; settled-verdict registry (zigzag_settled class); new-facts gate at every re-adjudication entry point; blind valuation re-derivation (live data) mandatory in buy runs; deterministic sleeve-fit check (buy names its v74+ sleeve or fails structural validation).
3. **Retract-on-reversal as machinery**: a decision run whose verdict contradicts an open proposal on the same ticker cancels it in the same transaction (3 hand-cleanups this session).
4. **deploy-cash reads `discovery_reserve`** (v76 cash class) — earmark enforcement, currently prose-only.
5. **Alpha-lane sourcing**: the 16:00 discovery cron + MEDIUM floor + $30B radar now feed it; pre-momentum sources still queued (Finviz sleeve-tuned screen, earnings-acceleration, 13F/Form4 post-parser-fix); multi-mandate adjudication (hold/sleeve/ballast per name).
6. **BRK.B productive-ballast plan question** (post-no-gold; verification on proposal 5 history).
7. Carried from 07-09 §6: alternatives_phase systemic fix (hardcoded 3.0% + blind to settled records); patch/sliced flags default-ON; in-product zigzag; run-149 crash; cost-cap resume; synthesizer fact tokens; NVDA avg_price per-lot; Schwab equity ingest; slash-ticker BRK/B data bug; ips.no_current_plan keying; test_api_phase4 hang; catchup KeyError; bank DPYA; SGLN LOOKTHROUGH_MAP; backend service wrapper; degraded_to_monolith DTO; section_coverage warn-cleanup critique round.

## 5. Discipline notes

Blind re-derivation with LIVE data kept winning (NOW split/multiples, BRK.B 13F, RKT cap-math, Israeli wash-sale). Fleet closes its own reversals — never leave a contradiction in the client's inbox. Plan mutations go draft→gated-accept (v74/v75/v76 pattern, scripts in scripts/). cp1252: PYTHONIOENCODING=utf-8, durable side-effects before prints. Backend must run detached, not as a session background task.
